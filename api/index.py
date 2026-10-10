"""
MnogoReels landing rotator (FastAPI on Vercel).

Round-robin / weighted split of paid traffic across your own landing variants,
pass-through of tracking params (subid, zone, utm_*), click + conversion stats.

ENV:
  VARIANTS            JSON list: [{"id":"a","url":"https://mnogo-reels.ru/?lp=a","weight":1}, ...]
  ALLOWED_HOSTS       comma list of allowed destination hosts (default: mnogo-reels.ru)
  STATS_TOKEN         secret for /stats and /postback
  KV_REST_API_URL     Upstash Redis REST url   (optional, Vercel Marketplace -> Upstash)
  KV_REST_API_TOKEN   Upstash Redis REST token (optional)
Without Redis the split is weighted-random (serverless instances share no memory).
"""
import json
import os
import random
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

app = FastAPI(title="mnogo-rotator")

FALLBACK_URL = "https://mnogo-reels.ru/"
PASS_PARAMS_PREFIX = ("utm_",)
PASS_PARAMS = {"subid", "zone", "zoneid", "campaign", "cid", "clickid", "geo", "os", "browser"}


def load_variants() -> list[dict]:
    raw = os.getenv("VARIANTS", "")
    try:
        variants = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        variants = []
    allowed = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "mnogo-reels.ru").split(",") if h.strip()}
    ok = []
    for v in variants:
        host = (urlparse(v.get("url", "")).hostname or "").lower()
        if v.get("id") and any(host == a or host.endswith("." + a) for a in allowed):
            ok.append({"id": str(v["id"]), "url": v["url"], "weight": max(int(v.get("weight", 1)), 0)})
    return [v for v in ok if v["weight"] > 0]


# ---------- tiny Upstash REST client ----------
KV_URL = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")


async def kv(*cmd):
    if not (KV_URL and KV_TOKEN):
        return None
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=list(cmd))
            return r.json().get("result")
    except Exception:
        return None


async def pick(variants: list[dict]) -> dict:
    # expand by weight: a(2), b(1) -> [a, a, b]
    wheel = [v for v in variants for _ in range(v["weight"])]
    n = await kv("INCR", "rot:counter")
    if isinstance(n, int):
        return wheel[(n - 1) % len(wheel)]
    return random.choice(wheel)


def build_target(url: str, incoming: dict, variant_id: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    for k, val in incoming.items():
        if k in PASS_PARAMS or k.startswith(PASS_PARAMS_PREFIX):
            q.setdefault(k, val)
    q["lp"] = variant_id
    return urlunparse(p._replace(query=urlencode(q)))


def check_token(token: str | None):
    expected = os.getenv("STATS_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="bad token")


# ---------- routes ----------
@app.get("/go")
async def go(request: Request):
    variants = load_variants()
    if not variants:
        return RedirectResponse(FALLBACK_URL, status_code=302)
    v = await pick(variants)
    params = dict(request.query_params)
    await kv("HINCRBY", "rot:clicks", v["id"], 1)
    if params.get("subid"):
        await kv("SET", f"rot:sub:{params['subid']}", v["id"], "EX", 60 * 60 * 24 * 30)
    resp = RedirectResponse(build_target(v["url"], params, v["id"]), status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/postback")
async def postback(subid: str, token: str | None = None, lp: str | None = None):
    """Call from your backend on signup/payment: /postback?subid=XXX&token=...
    (lp is optional if subid was seen on /go)."""
    check_token(token)
    variant = lp or await kv("GET", f"rot:sub:{subid}")
    if not variant:
        return {"ok": False, "reason": "unknown subid"}
    await kv("HINCRBY", "rot:conv", variant, 1)
    return {"ok": True, "variant": variant}


@app.get("/stats")
async def stats(token: str | None = None):
    check_token(token)

    def as_dict(flat):
        flat = flat or []
        return {flat[i]: int(flat[i + 1]) for i in range(0, len(flat), 2)}

    clicks = as_dict(await kv("HGETALL", "rot:clicks"))
    conv = as_dict(await kv("HGETALL", "rot:conv"))
    rows = []
    for v in load_variants():
        c, k = clicks.get(v["id"], 0), conv.get(v["id"], 0)
        rows.append({"id": v["id"], "url": v["url"], "weight": v["weight"],
                     "clicks": c, "conversions": k, "cr_%": round(100 * k / c, 2) if c else 0})
    return JSONResponse({"redis": bool(KV_URL and KV_TOKEN), "variants": rows})


@app.get("/")
async def root():
    return {"ok": True, "variants": len(load_variants())}
