"""Round-robin редиректор с трекингом доставки. FastAPI на Vercel + Upstash Redis."""
import base64
import os
import random
import re
import secrets
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


def parse_targets(raw: str) -> list[str]:
    parts = raw.splitlines() if "\n" in raw.strip() else raw.split(",")
    return [p.strip().rstrip("/") for p in parts if p.strip()]


def env_by_suffix(*suffixes: str) -> str | None:
    # Vercel может добавить к переменным Upstash любой префикс (STORAGE_KV_REST_API_URL)
    for k, v in os.environ.items():
        if v and k.endswith(suffixes):
            return v
    return None


DEFAULT_TARGETS = """
https://site1.com/?q=информация из поиска
https://site2.com/?q=информация из поиска
https://site3.com/?q=информация из поиска
"""

# Ротатор не редиректит платный трафик в ИИ-ассистенты: попандер открывает их
# у человека, который ничего не просил, и запрос уходит от его имени.
BLOCKED_HOSTS = ()


def is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


_ALL_TARGETS = parse_targets(os.getenv("TARGETS") or DEFAULT_TARGETS)
BLOCKED_TARGETS = [t for t in _ALL_TARGETS if is_blocked(t)]
TARGETS = [t for t in _ALL_TARGETS if not is_blocked(t)]
CLICK_TTL = int(os.getenv("CLICK_TTL_DAYS", "30")) * 86400
KV_URL = env_by_suffix("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL")
KV_TOKEN = env_by_suffix("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN")

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
NO_CACHE = {"Cache-Control": "no-store"}
CID_RE = re.compile(r"[0-9a-f]{32}")
STAT_KEYS = ["rr:seq", "rr:sent", "rr:arrived", "rr:delay_ms", "rr:mismatch", "rr:bots"]

# Боты и превью мессенджеров не участвуют в ротации, иначе съедают очередь
BOT_MARKERS = ("bot", "crawler", "spider", "facebookexternalhit", "whatsapp",
               "telegram", "slack", "preview", "curl", "python-requests", "headless")

# ARGV: cid, ts_ms, ttl, target1..targetN
LUA_CLICK = """
local n = redis.call('INCR', 'rr:seq')
local target = ARGV[4 + ((n - 1) % (#ARGV - 3))]
local key = 'rr:click:' .. ARGV[1]
redis.call('HSET', key, 't', target, 'ts', ARGV[2])
redis.call('EXPIRE', key, ARGV[3])
redis.call('HINCRBY', 'rr:sent', target, 1)
return target
"""

# ARGV: cid, ts_ms, host. Засчитывается только первый пиксель по cid
LUA_ARRIVE = """
local key = 'rr:click:' .. ARGV[1]
local target = redis.call('HGET', key, 't')
if not target or redis.call('HSETNX', key, 'arr', 1) == 0 then return 0 end
redis.call('HINCRBY', 'rr:arrived', target, 1)
redis.call('HINCRBY', 'rr:delay_ms', target, ARGV[2] - redis.call('HGET', key, 'ts'))
local expected = string.match(target, '^%a+://([^/:]+)')
if ARGV[3] ~= '' and ARGV[3] ~= expected and ARGV[3] ~= 'www.' .. expected then
  redis.call('HINCRBY', 'rr:mismatch', target, 1)
end
return 1
"""

LUA_STATS = """
return {redis.call('HGETALL', 'rr:sent'), redis.call('HGETALL', 'rr:arrived'),
        redis.call('HGETALL', 'rr:delay_ms'), redis.call('HGETALL', 'rr:mismatch'),
        redis.call('GET', 'rr:bots') or 0}
"""

TRACKER_JS = """(function(){try{
var o=new URL(document.currentScript.src).origin,k='rr_cid',
c=new URLSearchParams(location.search).get('cid')||sessionStorage.getItem(k);
if(!c||sessionStorage.getItem(k+'_sent')===c)return;
sessionStorage.setItem(k,c);sessionStorage.setItem(k+'_sent',c);
new Image().src=o+'/t/arrive?cid='+encodeURIComponent(c)+'&host='+encodeURIComponent(location.hostname)+'&r='+Date.now();
}catch(e){}})();"""


class KVError(Exception):
    pass


async def kv(*cmd):
    if not (KV_URL and KV_TOKEN):
        raise KVError("redis not configured")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"},
                             json=[str(x) for x in cmd])
            data = r.json()
    except Exception as e:
        raise KVError(str(e)) from e
    if "error" in data:
        raise KVError(data["error"])
    return data.get("result")


def now_ms() -> int:
    return int(time.time() * 1000)


def is_bot(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    return not ua or any(m in ua for m in BOT_MARKERS)


def check_token(token: str | None):
    expected = os.getenv("STATS_TOKEN", "")
    if not expected or not secrets.compare_digest(token or "", expected):
        raise HTTPException(status_code=403)


def target_url(target: str, request: Request, cid: str | None) -> str:
    # Параметры целевой ссылки главнее входящих
    p = urlparse(target)
    params = dict(parse_qsl(p.query, keep_blank_values=True))
    for k, v in request.query_params.items():
        params.setdefault(k, v)
    if cid:
        params["cid"] = cid
    return urlunparse(p._replace(path=p.path or "/", query=urlencode(params)))


def hash_to_dict(flat: list) -> dict[str, int]:
    return {flat[i]: int(flat[i + 1]) for i in range(0, len(flat), 2)}


app = FastAPI(title="cookies_rotator", docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/go", methods=["GET", "HEAD"])
async def redirect(request: Request):
    if request.method == "HEAD":
        return Response(headers=NO_CACHE)
    if not TARGETS:
        return JSONResponse({"error": "no allowed targets", "blocked": len(BLOCKED_TARGETS)},
                            status_code=503, headers=NO_CACHE)
    if is_bot(request):
        try:
            await kv("INCR", "rr:bots")
        except KVError:
            pass
        return RedirectResponse(target_url(TARGETS[0], request, None), 302, headers=NO_CACHE)

    cid = uuid.uuid4().hex
    headers = NO_CACHE
    try:
        target = await kv("EVAL", LUA_CLICK, 0, cid, now_ms(), CLICK_TTL, *TARGETS)
    except KVError:
        # Redis недоступен: трафик не теряем
        target, cid = random.choice(TARGETS), None
        headers = {**NO_CACHE, "X-Rotator-Fallback": "random"}
    return RedirectResponse(target_url(target, request, cid), 302, headers=headers)


@app.get("/t/arrive")
async def arrive(cid: str = "", host: str = ""):
    if CID_RE.fullmatch(cid):
        try:
            await kv("EVAL", LUA_ARRIVE, 0, cid, now_ms(), host[:255].lower())
        except KVError:
            pass
    return Response(PIXEL, media_type="image/gif", headers=NO_CACHE)


@app.get("/t.js")
async def tracker_js():
    return Response(TRACKER_JS, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300"})


@app.get("/stats")
async def stats(token: str | None = None):
    check_token(token)
    try:
        sent, arrived, delay, mismatch, bots = await kv("EVAL", LUA_STATS, 0)
    except KVError as e:
        return JSONResponse({"redis": False, "error": str(e)}, status_code=503)
    sent, arrived = hash_to_dict(sent), hash_to_dict(arrived)
    delay, mismatch = hash_to_dict(delay), hash_to_dict(mismatch)

    targets = {}
    for t in TARGETS:
        s, a = sent.get(t, 0), arrived.get(t, 0)
        targets[t] = {
            "sent": s,
            "arrived": a,
            "arrival_rate_pct": round(a / s * 100, 2) if s else 0,
            "avg_delay_sec": round(delay.get(t, 0) / a / 1000, 3) if a else None,
            "host_mismatch": mismatch.get(t, 0),
        }
    counts = [v["sent"] for v in targets.values()]
    total_sent = sum(counts)
    total_arrived = sum(v["arrived"] for v in targets.values())
    return {
        "targets": targets,
        "total_sent": total_sent,
        "total_arrived": total_arrived,
        "total_arrival_rate_pct": round(total_arrived / total_sent * 100, 2) if total_sent else 0,
        "distribution_spread": max(counts) - min(counts),
        "bots_skipped": int(bots),
    }


@app.post("/reset")
async def reset(token: str | None = None):
    check_token(token)
    try:
        await kv("DEL", *STAT_KEYS)
    except KVError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return {"ok": True}


@app.get("/health")
async def health():
    try:
        await kv("PING")
        redis_ok = True
    except KVError:
        redis_ok = False
    return {"ok": True, "redis": redis_ok, "targets": TARGETS,
            "blocked_targets": len(BLOCKED_TARGETS)}


# Vercel может передать функции путь с префиксом /api/index, срезаем его
_app = app


async def app(scope, receive, send):  # noqa: F811
    if scope["type"] == "http":
        path = scope["path"]
        for prefix in ("/api/index.py", "/api/index", "/api"):
            if path == prefix or path.startswith(prefix + "/"):
                path = path[len(prefix):] or "/"
                scope = {**scope, "path": path, "raw_path": path.encode()}
                break
    await _app(scope, receive, send)
