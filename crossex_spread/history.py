"""Fetch ~45 days of hourly prices (+ funding) per venue for the screenshot assets.

Output: data/hist/<VENUE>_<ASSET>.json  {"close": {ts: px}, "mark": {...}, "funding": [[ts, rate], ...]}
ts = hour END (unix seconds). Funding rates are per settlement (fraction, longs pay if > 0).
"""
import concurrent.futures as cf
import gzip
import io
import json
import os
import sys
import time

import venues as V

DAYS = int(os.environ.get("DAYS", 45))
END = int(time.time()) // 3600 * 3600
START = END - DAYS * 86400
OUT = os.path.join(V.DATA, "hist")
os.makedirs(OUT, exist_ok=True)

JOBS = [
    ("GATE", a) for a in ["XIAOMI", "JIO", "LAOPU", "SOPH", "SAHARA", "MOONSHOT", "CYBER"]
] + [
    ("BINANCE", a) for a in ["SOPH", "SAHARA", "MOONSHOT", "CYBER"]
] + [
    ("ASTER", a) for a in ["XIAOMI", "SOPH", "SAHARA", "DRV", "CYBER"]
] + [
    ("OKX", a) for a in ["XIAOMI", "SOPH", "SAHARA", "MOONSHOT"]
] + [
    ("HYPERLIQUID", c) for c in ["io:XIAOMI", "xyz:JIO", "SOPH", "para:DRV"]
] + [
    ("BYBIT", a) for a in ["XIAOMI", "LAOPU", "SOPH", "SAHARA", "DRV", "MOONSHOT", "CYBER"]
]


def gate(a):
    c = f"{a}_USDT"
    out = {}
    for key, cc in (("close", c), ("mark", f"mark_{c}")):
        rows = V.get(f"{V.GATE}/futures/usdt/candlesticks", contract=cc, interval="1h",
                     **{"from": START, "to": END})
        out[key] = {int(r["t"]) + 3600: float(r["c"]) for r in rows}
    fr = V.get(f"{V.GATE}/futures/usdt/funding_rate", contract=c, limit=1000,
               **{"from": START, "to": END})
    out["funding"] = sorted([int(r["t"]), float(r["r"])] for r in fr)
    return out


def binance_like(host, a):
    s = a + "USDT"
    out = {}
    for key, path in (("close", "klines"), ("mark", "markPriceKlines")):
        acc = {}
        st = START * 1000
        while st < END * 1000:
            rows = V.get(f"{host}/fapi/v1/{path}", symbol=s, interval="1h", startTime=st,
                         endTime=END * 1000, limit=1500)
            if not rows:
                break
            for r in rows:
                acc[int(r[0]) // 1000 + 3600] = float(r[4])
            st = int(rows[-1][0]) + 3_600_000
            if len(rows) < 1500:
                break
        out[key] = acc
    acc, st = [], START * 1000
    while True:
        rows = V.get(f"{host}/fapi/v1/fundingRate", symbol=s, startTime=st, endTime=END * 1000, limit=1000)
        acc += [[int(r["fundingTime"]) // 1000, float(r["fundingRate"])] for r in rows]
        if len(rows) < 1000:
            break
        st = int(rows[-1]["fundingTime"]) + 1
    out["funding"] = sorted(acc)
    return out


def okx(a):
    inst = f"{a}-USDT-SWAP"
    out = {}
    for key, path in (("close", "market/history-candles"), ("mark", "market/history-mark-price-candles")):
        acc, after = {}, None
        for _ in range(40):
            p = dict(instId=inst, bar="1H", limit=100)
            if after:
                p["after"] = after
            rows = V.get(f"{V.OKX}/{path}", **p)["data"]
            if not rows:
                break
            for r in rows:
                t = int(r[0]) // 1000
                if START <= t < END:
                    acc[t + 3600] = float(r[4])
            after = rows[-1][0]
            if int(after) // 1000 < START:
                break
            time.sleep(0.12)
        out[key] = acc
    acc, after = [], None
    for _ in range(20):
        p = dict(instId=inst, limit=100)
        if after:
            p["after"] = after
        rows = V.get(f"{V.OKX}/public/funding-rate-history", **p)["data"]
        if not rows:
            break
        acc += [[int(r["fundingTime"]) // 1000, float(r["realizedRate"] or r["fundingRate"])] for r in rows]
        after = rows[-1]["fundingTime"]
        if int(after) // 1000 < START:
            break
        time.sleep(0.12)
    out["funding"] = sorted(x for x in acc if START <= x[0] <= END)
    return out


def hyperliquid(coin):
    rows = V.post(V.HL, {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h", "startTime": START * 1000, "endTime": END * 1000}})
    out = {"close": {int(r["t"]) // 1000 + 3600: float(r["c"]) for r in rows}}
    acc, st = [], START * 1000
    for _ in range(30):
        fr = V.post(V.HL, {"type": "fundingHistory", "coin": coin, "startTime": st, "endTime": END * 1000})
        if not fr:
            break
        acc += [[int(r["time"]) // 1000, float(r["fundingRate"])] for r in fr]
        st = int(fr[-1]["time"]) + 1
        if len(fr) < 500:
            break
    out["funding"] = sorted(acc)
    return out


def bybit(a):
    s = a + "USDT"
    close, vwap_num, vwap_den = {}, {}, {}

    def day(d):
        url = f"https://public.bybit.com/trading/{s}/{s}{d}.csv.gz"
        r = V.S.get(url, timeout=60)
        if r.status_code != 200:
            return []
        txt = gzip.decompress(r.content).decode()
        res = []
        for line in txt.splitlines()[1:]:
            f = line.split(",")
            res.append((float(f[0]), float(f[4]), float(f[3])))
        return res

    days = sorted({time.strftime("%Y-%m-%d", time.gmtime(t)) for t in range(START, END, 86400)})
    with cf.ThreadPoolExecutor(6) as ex:
        for trades in ex.map(day, days):
            trades.sort()
            for ts, px, sz in trades:
                h = int(ts) // 3600 * 3600 + 3600
                close[h] = px  # last trade of the hour wins (sorted)
                vwap_num[h] = vwap_num.get(h, 0) + px * sz
                vwap_den[h] = vwap_den.get(h, 0) + sz
    return {"close": close, "vwap": {h: vwap_num[h] / vwap_den[h] for h in vwap_num},
            "funding": V.bybit_funding_history(a, START, END)}


def run(job):
    venue, a = job
    fn = f"{OUT}/{venue}_{a.replace(':', '-')}.json"
    if os.path.exists(fn):
        return job, "cached"
    t0 = time.time()
    if venue == "GATE":
        d = gate(a)
    elif venue == "BINANCE":
        d = binance_like(V.BINANCE, a)
    elif venue == "ASTER":
        d = binance_like(V.ASTER, a)
    elif venue == "OKX":
        d = okx(a)
    elif venue == "HYPERLIQUID":
        d = hyperliquid(a)
    elif venue == "BYBIT":
        d = bybit(a)
    json.dump(d, open(fn, "w"))
    n = len(d.get("close") or {})
    nf = len(d.get("funding") or [])
    return job, f"{n} closes, {nf} funding pts, {time.time() - t0:.0f}s"


if __name__ == "__main__":
    jobs = JOBS if len(sys.argv) == 1 else [j for j in JOBS if j[0] in sys.argv[1:]]
    with cf.ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(run, j): j for j in jobs}
        for f in cf.as_completed(futs):
            try:
                job, msg = f.result()
                print(job, msg, flush=True)
            except Exception as e:
                print(futs[f], "ERROR", type(e).__name__, e, flush=True)
