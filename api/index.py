"""
cookies_rotator: основной домен -> X1/X2/X3 строго по кругу (round-robin)
+ трекинг доставки трафика до целевого сайта. FastAPI на Vercel + Upstash Redis.

Поток:
  1. Пользователь заходит на https://<rotator>/?utm_source=...
  2. Один атомарный Lua-скрипт в Redis: INCR счётчика -> выбор домена (n % 3)
     -> запись клика с уникальным cid -> +1 в "отправлено" по домену.
  3. 302 на https://x1.com/?utm_source=...&cid=<cid>
  4. На целевом сайте стоит <script src="https://<rotator>/t.js" async></script>,
     скрипт берёт cid из URL и дёргает пиксель /t/arrive?cid=...&host=x1.com
     -> отмечаем, что трафик реально дошёл (засчитывается только первый раз).
  5. /stats: сколько отправлено, сколько дошло, по каждому домену.

Почему Redis: на Vercel каждый запрос может попасть в новый инстанс, память и
файлы (SQLite) между ними не шарятся. Счётчик в Redis общий для всех инстансов,
INCR атомарный, поэтому 900 кликов = ровно 300/300/300.

ENV:
  TARGETS             через запятую (default: https://x1.com,https://x2.com,https://x3.com)
  STATS_TOKEN         секрет для /stats и /reset (обязателен)
  KV_REST_API_URL     Upstash Redis REST url   (Vercel Marketplace -> Upstash, любой префикс)
  KV_REST_API_TOKEN   Upstash Redis REST token
  CLICK_TTL_DAYS      сколько хранить клики (default 30)
"""
import base64
import os
import random
import time
import uuid
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

TARGETS = [
    t.strip().rstrip("/")
    for t in os.getenv("TARGETS", "https://x1.com,https://x2.com,https://x3.com").split(",")
    if t.strip()
]
CLICK_TTL = int(os.getenv("CLICK_TTL_DAYS", "30")) * 86400

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

# Превью ссылок в мессенджерах и краулеры: редиректим, но в ротацию не пускаем,
# иначе они съедают очередь и портят ровное распределение.
BOT_MARKERS = ("bot", "crawler", "spider", "facebookexternalhit", "whatsapp",
               "telegram", "slack", "preview", "curl", "python-requests", "headless")

KEYS = ["rr:seq", "rr:sent", "rr:arrived", "rr:delay_ms", "rr:mismatch", "rr:bots"]

app = FastAPI(title="cookies_rotator")


# ---------- Upstash REST ----------
def _env_suffix(*suffixes):
    # KV_REST_API_URL, UPSTASH_REDIS_REST_URL или любой префикс вроде STORAGE_KV_REST_API_URL
    for s in suffixes:
        if os.getenv(s):
            return os.getenv(s)
    for k, v in os.environ.items():
        if v and any(k.endswith(s) for s in suffixes):
            return v
    return None


KV_URL = _env_suffix("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL")
KV_TOKEN = _env_suffix("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN")


class KVError(Exception):
    pass


async def kv(*cmd):
    """Одна команда Redis через Upstash REST. Бросает KVError, если Redis недоступен."""
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


# Клик: счётчик + выбор домена + запись клика + "отправлено" одной атомарной операцией.
# ARGV: cid, ts_ms, ttl, target1, target2, ...
LUA_CLICK = """
local n = redis.call('INCR', 'rr:seq')
local ntargets = #ARGV - 3
local target = ARGV[4 + ((n - 1) % ntargets)]
local key = 'rr:click:' .. ARGV[1]
redis.call('HSET', key, 't', target, 'ts', ARGV[2], 'seq', n)
redis.call('EXPIRE', key, ARGV[3])
redis.call('HINCRBY', 'rr:sent', target, 1)
return {n, target}
"""

# Приход: засчитываем только первый пиксель по cid.
# ARGV: cid, ts_ms, host. Возврат: 0 = неизвестный cid, 1 = повтор, 2 = засчитан.
LUA_ARRIVE = """
local key = 'rr:click:' .. ARGV[1]
local target = redis.call('HGET', key, 't')
if not target then return 0 end
if redis.call('HSETNX', key, 'arr', ARGV[2]) == 0 then return 1 end
redis.call('HSET', key, 'host', ARGV[3])
redis.call('HINCRBY', 'rr:arrived', target, 1)
local sent_ts = tonumber(redis.call('HGET', key, 'ts'))
redis.call('HINCRBY', 'rr:delay_ms', target, tonumber(ARGV[2]) - sent_ts)
local expected = string.match(target, '^%a+://([^/:]+)')
if ARGV[3] ~= '' and expected ~= ARGV[3] and ('www.' .. expected) ~= ARGV[3] then
  redis.call('HINCRBY', 'rr:mismatch', target, 1)
end
return 2
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def is_bot(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    return not ua or any(m in ua for m in BOT_MARKERS)


def check_token(token: str | None):
    expected = os.getenv("STATS_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="bad token")


def target_url(target: str, request: Request, cid: str | None) -> str:
    params = dict(request.query_params)
    if cid:
        params["cid"] = cid
    qs = urlencode(params)
    return f"{target}/?{qs}" if qs else f"{target}/"


# ---------- routes ----------
@app.api_route("/", methods=["GET", "HEAD"])
async def redirect(request: Request):
    # HEAD (проверки ссылок) и боты не участвуют в ротации
    if request.method == "HEAD":
        return Response(status_code=200, headers=NO_CACHE)
    if is_bot(request):
        try:
            await kv("INCR", "rr:bots")
        except KVError:
            pass
        return RedirectResponse(target_url(TARGETS[0], request, None), status_code=302, headers=NO_CACHE)

    cid = uuid.uuid4().hex
    try:
        _, target = await kv("EVAL", LUA_CLICK, 0, cid, now_ms(), CLICK_TTL, *TARGETS)
        headers = NO_CACHE
    except KVError:
        # Redis лёг: трафик не теряем, отдаём рандомом и помечаем заголовком
        target = random.choice(TARGETS)
        cid = None
        headers = {**NO_CACHE, "X-Rotator-Fallback": "random"}
    return RedirectResponse(target_url(target, request, cid), status_code=302, headers=headers)


@app.get("/t/arrive")
async def arrive(cid: str = Query("", max_length=64), host: str = Query("", max_length=255)):
    """Пиксель подтверждения. Всегда отдаёт GIF, даже если cid кривой."""
    if len(cid) == 32 and all(c in "0123456789abcdef" for c in cid):
        try:
            await kv("EVAL", LUA_ARRIVE, 0, cid, now_ms(), host.lower())
        except KVError:
            pass
    return Response(content=PIXEL, media_type="image/gif", headers=NO_CACHE)


@app.get("/t.js")
async def tracker_js(request: Request):
    """Сниппет для целевых сайтов: <script src="https://<rotator>/t.js" async></script>"""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    js = """(function(){try{
var p=new URLSearchParams(location.search),c=p.get('cid'),k='rr_cid';
if(c){sessionStorage.setItem(k,c);}else{c=sessionStorage.getItem(k);}
if(!c||sessionStorage.getItem(k+'_sent')===c)return;
new Image().src='%s://%s/t/arrive?cid='+encodeURIComponent(c)+'&host='+encodeURIComponent(location.hostname)+'&r='+Date.now();
sessionStorage.setItem(k+'_sent',c);
}catch(e){}})();""" % (proto, host)
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@app.get("/stats")
async def stats(token: str | None = None):
    check_token(token)

    def as_dict(flat):
        if isinstance(flat, dict):  # некоторые клиенты отдают HGETALL словарём
            return {k: int(float(v)) for k, v in flat.items()}
        flat = flat or []  # Upstash REST: плоский список [k1, v1, k2, v2]
        return {flat[i]: int(float(flat[i + 1])) for i in range(0, len(flat), 2)}

    try:
        seq = int(await kv("GET", "rr:seq") or 0)
        bots = int(await kv("GET", "rr:bots") or 0)
        sent = as_dict(await kv("HGETALL", "rr:sent"))
        arrived = as_dict(await kv("HGETALL", "rr:arrived"))
        delay = as_dict(await kv("HGETALL", "rr:delay_ms"))
        mismatch = as_dict(await kv("HGETALL", "rr:mismatch"))
    except KVError as e:
        return JSONResponse({"redis": False, "error": str(e)}, status_code=503)

    by_target = {}
    for t in TARGETS:
        s, a = sent.get(t, 0), arrived.get(t, 0)
        by_target[t] = {
            "sent": s,
            "arrived": a,
            "arrival_rate_pct": round(a / s * 100, 2) if s else 0,
            "avg_delay_sec": round(delay.get(t, 0) / a / 1000, 3) if a else None,
            "host_mismatch": mismatch.get(t, 0),
        }

    sent_counts = [v["sent"] for v in by_target.values()]
    total_sent, total_arrived = sum(sent_counts), sum(v["arrived"] for v in by_target.values())
    return {
        "redis": True,
        "targets": by_target,
        "total_sent": total_sent,
        "total_arrived": total_arrived,
        "total_arrival_rate_pct": round(total_arrived / total_sent * 100, 2) if total_sent else 0,
        "distribution_spread": max(sent_counts) - min(sent_counts) if sent_counts else 0,  # 0 или 1 = идеально ровно
        "counter": seq,
        "bots_skipped": bots,
    }


@app.post("/reset")
async def reset(token: str | None = None):
    """Обнулить счётчики перед новым тестом. Сами клики доживут свой TTL."""
    check_token(token)
    try:
        await kv("DEL", *KEYS)
    except KVError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return {"ok": True}


@app.get("/health")
async def health():
    redis_ok = True
    try:
        await kv("PING")
    except KVError:
        redis_ok = False
    return {"ok": True, "redis": redis_ok, "targets": TARGETS}


@app.api_route("/{rest:path}", methods=["GET", "HEAD"])
async def not_found(rest: str, request: Request):
    return JSONResponse({"detail": "Not Found", "path": request.url.path}, status_code=404)


# Vercel может отдать функции путь вида /api/index или /api/index/stats.
# Нормализуем, чтобы роуты матчились при любом роутинге платформы.
_inner_app = app


async def app(scope, receive, send):  # noqa: F811
    if scope.get("type") in ("http", "websocket"):
        path = scope.get("path", "")
        for prefix in ("/api/index.py", "/api/index", "/api"):
            if path == prefix or path.startswith(prefix + "/"):
                new = path[len(prefix):] or "/"
                scope = dict(scope, path=new, raw_path=new.encode())
                break
    await _inner_app(scope, receive, send)
