# Локальный тест: pip install fastapi httpx "fakeredis[lua]" && python tests/test_rotator.py
import asyncio, os, sys, random
from urllib.parse import urlparse, parse_qs
os.environ["STATS_TOKEN"] = "secret"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import index
import fakeredis.aioredis as far

r = far.FakeRedis(decode_responses=True)
async def fake_kv(*cmd):
    cmd = [str(c) for c in cmd]
    res = await r.execute_command(*cmd)
    if cmd[0] == "PING": return "PONG"
    return [x for kv_ in res.items() for x in kv_] if isinstance(res, dict) else res
index.kv = fake_kv
import httpx

async def main():
    tr = httpx.ASGITransport(app=index.app)
    async with httpx.AsyncClient(transport=tr, base_url="https://rot.test") as c:
        UA = {"user-agent": "Mozilla/5.0 (iPhone)"}
        async def click(i):
            resp = await c.get(f"/?utm_source=propeller&subid={i}", headers=UA)
            assert resp.status_code == 302, resp.status_code
            return resp.headers["location"]
        locs = await asyncio.gather(*[click(i) for i in range(900)])
        # боты и HEAD
        for _ in range(5): assert (await c.get("/", headers={"user-agent":"TelegramBot"})).status_code == 302
        assert (await c.head("/", headers=UA)).status_code == 200
        # пиксели: доходит ~90%, у одного клика дубль, один с чужого хоста
        sample = locs[0]
        q = parse_qs(urlparse(sample).query); print("пример:", sample)
        assert q["utm_source"] == ["propeller"] and len(q["cid"][0]) == 32
        random.seed(1); arrived = 0
        for i, loc in enumerate(locs):
            if random.random() < 0.9:
                cid = parse_qs(urlparse(loc).query)["cid"][0]
                host = urlparse(loc).hostname if i != 7 else "evil.com"
                await c.get(f"/t/arrive?cid={cid}&host={host}"); arrived += 1
                if i == 3: await c.get(f"/t/arrive?cid={cid}&host={host}")
        assert (await c.get("/t/arrive?cid=garbage")).status_code == 200
        assert (await c.get("/stats")).status_code == 403
        s = (await c.get("/stats?token=secret")).json()
        import json; print(json.dumps(s, indent=1, ensure_ascii=False))
        assert [v["sent"] for v in s["targets"].values()] == [300, 300, 300]
        assert s["total_arrived"] == arrived and s["bots_skipped"] == 5
        assert sum(v["host_mismatch"] for v in s["targets"].values()) == 1
        js = (await c.get("/t.js")).text; assert "https://rot.test/t/arrive" in js
        assert (await c.get("/api/index/health")).json()["redis"] is True
        assert (await c.post("/reset?token=secret")).json()["ok"]
        assert (await c.get("/stats?token=secret")).json()["total_sent"] == 0
        # Redis лёг -> fallback рандом, трафик не теряется
        async def broken(*a): raise index.KVError("down")
        index.kv = broken
        resp = await c.get("/?a=1", headers=UA)
        assert resp.status_code == 302 and resp.headers["x-rotator-fallback"] == "random"
        print("ALL OK")
asyncio.run(main())

# Полные ссылки с путём и своими параметрами
def test_full_urls():
    os.environ["TARGETS"] = "https://a.com/landing?lp=a&q=hello\nhttps://b.com/new\nhttps://c.com"
    import importlib; importlib.reload(index)
    assert index.TARGETS == ["https://a.com/landing?lp=a&q=hello", "https://b.com/new", "https://c.com"]
    index.kv = fake_kv
    async def run():
        await r.flushall()
        tr = httpx.ASGITransport(app=index.app)
        async with httpx.AsyncClient(transport=tr, base_url="https://rot.test") as c:
            UA = {"user-agent": "Mozilla/5.0"}
            locs = [(await c.get("/?subid=7&lp=zzz", headers=UA)).headers["location"] for _ in range(6)]
            for l in locs: print(l)
            a = urlparse(locs[0]); qa = parse_qs(a.query)
            assert a.hostname == "a.com" and a.path == "/landing"
            assert qa["lp"] == ["a"] and qa["q"] == ["hello"] and qa["subid"] == ["7"] and len(qa["cid"][0]) == 32
            assert urlparse(locs[1]).path == "/new" and urlparse(locs[2]).path == "/"
            assert [urlparse(l).hostname for l in locs] == ["a.com", "b.com", "c.com"] * 2
            cid = parse_qs(a.query)["cid"][0]
            await c.get(f"/t/arrive?cid={cid}&host=a.com")
            s = (await c.get("/stats?token=secret")).json()
            t = s["targets"]["https://a.com/landing?lp=a&q=hello"]
            assert t["sent"] == 2 and t["arrived"] == 1 and t["host_mismatch"] == 0
            print("FULL URLS OK")
    asyncio.run(run())

test_full_urls()
