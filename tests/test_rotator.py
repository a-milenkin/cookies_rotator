# pip install fastapi httpx "fakeredis[lua]" && python tests/test_rotator.py
import asyncio
import importlib
import os
import sys
from urllib.parse import parse_qs, urlparse

import fakeredis.aioredis
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["STATS_TOKEN"] = "secret"
UA = {"user-agent": "Mozilla/5.0"}


def make_client(targets: str):
    os.environ["TARGETS"] = targets
    import index
    index = importlib.reload(index)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_kv(*cmd):
        return await redis.execute_command(*[str(c) for c in cmd])

    index.kv = fake_kv
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=index.app), base_url="https://rot.test")
    return index, client


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def test_even_split_and_tracking():
    index, c = make_client("https://x1.com,https://x2.com,https://x3.com")
    locs = await asyncio.gather(*[c.get(f"/?utm_source=p&subid={i}", headers=UA) for i in range(900)])
    locs = [r.headers["location"] for r in locs]
    assert query(locs[0])["utm_source"] == "p" and len(query(locs[0])["cid"]) == 32

    for i, loc in enumerate(locs[:600]):
        host = "evil.com" if i == 7 else urlparse(loc).hostname
        await c.get(f"/t/arrive?cid={query(loc)['cid']}&host={host}")
    await c.get(f"/t/arrive?cid={query(locs[0])['cid']}&host=x1.com")  # дубль
    await c.get("/t/arrive?cid=garbage")
    for _ in range(5):
        await c.get("/", headers={"user-agent": "TelegramBot"})
    assert (await c.head("/", headers=UA)).status_code == 200

    assert (await c.get("/stats")).status_code == 403
    s = (await c.get("/stats?token=secret")).json()
    assert [t["sent"] for t in s["targets"].values()] == [300, 300, 300]
    assert [t["arrived"] for t in s["targets"].values()] == [200, 200, 200]
    assert s["distribution_spread"] == 0 and s["bots_skipped"] == 5
    assert sum(t["host_mismatch"] for t in s["targets"].values()) == 1

    assert (await c.get("/api/index/health")).json()["redis"] is True
    await c.post("/reset?token=secret")
    assert (await c.get("/stats?token=secret")).json()["total_sent"] == 0

    async def down(*_):
        raise index.KVError("down")
    index.kv = down
    r = await c.get("/", headers=UA)
    assert r.status_code == 302 and r.headers["x-rotator-fallback"] == "random"


async def test_full_urls():
    _, c = make_client("https://a.com/landing?q=hello\nhttps://b.com/new\nhttps://c.com")
    locs = [(await c.get("/?subid=7&q=zzz", headers=UA)).headers["location"] for _ in range(3)]
    a, b, cc = (urlparse(u) for u in locs)
    assert (a.hostname, a.path, query(locs[0])["q"], query(locs[0])["subid"]) == ("a.com", "/landing", "hello", "7")
    assert (b.hostname, b.path, query(locs[1])["q"]) == ("b.com", "/new", "zzz")
    assert (cc.hostname, cc.path) == ("c.com", "/")


async def test_go_alias_and_ai_block():
    index, c = make_client("https://chatgpt.com/?q=x\nhttps://claude.ai/new?q=x\nhttps://www.perplexity.ai/?q=x\nhttps://mnogo-reels.ru/\nhttps://my-site.com/p")
    assert index.TARGETS == ["https://mnogo-reels.ru", "https://my-site.com/p"] and len(index.BLOCKED_TARGETS) == 3
    hosts = {urlparse((await c.get("/go?subid=1", headers=UA)).headers["location"]).hostname for _ in range(6)}
    assert hosts == {"mnogo-reels.ru", "my-site.com"}
    _, c2 = make_client("https://chatgpt.com/?q=x,https://claude.ai/new?q=y")
    r = await c2.get("/go", headers=UA)
    assert r.status_code == 503 and "location" not in r.headers


async def main():
    await test_even_split_and_tracking()
    await test_full_urls()
    await test_go_alias_and_ai_block()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
