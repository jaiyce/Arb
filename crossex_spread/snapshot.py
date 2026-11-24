"""Live snapshot of the 8 assets from the Gate CrossEx spread screenshot.

For each asset/venue: top of book, VWAP for a given notional, mark, index,
funding per interval and per day. Then, for the long/short legs the Gate
page highlighted (red = cheapest, green = most expensive), compare the
displayed spread (mark-based) with the executable one.
"""
import json
import os
import time

import venues as V

NOTIONALS = (1_000, 5_000, 20_000)

# legs as shown on the screenshot: (asset, long_venue, short_venue)
SCREEN = [
    ("XIAOMI", "ASTER", "BYBIT", 37.5), ("JIO", "GATE", "HYPERLIQUID", 36.31),
    ("LAOPU", "BYBIT", "GATE", 33.13), ("SOPH", "ASTER", "HYPERLIQUID", 29.93),
    ("SAHARA", "GATE", "BYBIT", 25.84), ("DRV", "ASTER", "HYPERLIQUID", 25.73),
    ("MOONSHOT", "BINANCE", "BYBIT", 22.78), ("CYBER", "GATE", "BYBIT", 21.53),
]

SYMBOLS_FILE = os.path.join(V.DATA, "crossex_symbols.json")
if not os.path.exists(SYMBOLS_FILE):  # public endpoint, no key needed
    json.dump(V.get(f"{V.GATE}/crossex/rule/symbols"), open(SYMBOLS_FILE, "w"))
symbols = json.load(open(SYMBOLS_FILE))


def crossex_venues(base):
    out = {}
    for x in symbols:
        if x["business_type"] == "FUTURE" and x["state"] == "live":
            ex, _, b, q = x["symbol"].split("_", 3)
            if b == base:
                out.setdefault(ex, []).append(q)
    return out


def snap(base):
    rows = []
    cv = crossex_venues(base)
    for ex, quotes in sorted(cv.items()):
        for q in quotes:
            try:
                if ex == "GATE":
                    rows.append(V.gate(base, q))
                elif ex == "BINANCE":
                    rows.append(V.binance(base, q))
                elif ex == "ASTER":
                    rows.append(V.aster(base, q))
                elif ex == "OKX":
                    if q != "USDT":  # OKX USDC (UMX) contracts are not in OKX's public v5 API
                        rows.append(dict(venue=ex, sym=f"{base}_{q}", note="not in OKX public API"))
                    else:
                        rows.append(V.okx(base, q))
                elif ex == "HYPERLIQUID":
                    for coin in V.hl_find(base):
                        rows.append(V.hyperliquid(coin))
                elif ex == "LIGHTER":
                    rows.append(V.lighter(base))
                elif ex == "BYBIT":
                    try:
                        rows.append(V.bybit(base, q))
                    except Exception:  # geo-blocked: funding only, from Lighter's feed
                        fr8 = V.lighter_funding_feed().get(("bybit", base))
                        rows.append(dict(venue="BYBIT", sym=base + q, bids=[], asks=[], bid=None,
                                         ask=None, mark=None, index=None, last=None,
                                         fr=fr8, int_h=8.0 if fr8 is not None else None,
                                         next_ts=None, note="REST unreachable; funding via Lighter feed (8h-norm.)"))
                else:
                    rows.append(dict(venue=ex, sym=base + q, note="not fetched"))
            except Exception as e:  # keep going, report
                rows.append(dict(venue=ex, sym=f"{base}_{q}", note=f"error {type(e).__name__}: {e}"))
            time.sleep(0.15)
    return rows


def fmt(x, n=6):
    return "-" if x is None else f"{x:.{n}g}"


def main():
    out = {}
    for asset, lv, sv, apr in SCREEN:
        rows = snap(asset)
        out[asset] = rows
        print(f"\n=== {asset}  (screenshot: long {lv} / short {sv}, Max APR {apr}%)")
        print(f"{'venue':12} {'sym':18} {'bid':>10} {'ask':>10} {'mark':>10} {'index':>10} "
              f"{'spr_bp':>7} {'fr%/int':>9} {'int_h':>5} {'fr%/day':>8} {'vwapBid5k':>10} {'vwapAsk5k':>10} {'vol24k$':>8}")
        for r in rows:
            if r.get("note") and r.get("bid") is None and r.get("fr") is None:
                print(f"{r['venue']:12} {r['sym']:18} {r['note']}")
                continue
            bs = None
            if r.get("bid") and r.get("ask"):
                bs = (r["ask"] - r["bid"]) / ((r["ask"] + r["bid"]) / 2) * 1e4
            frday = r["fr"] * 24 / r["int_h"] * 100 if r.get("fr") is not None and r.get("int_h") else None
            vb = V.vwap(r.get("bids") or [], 5000)
            va = V.vwap(r.get("asks") or [], 5000)
            print(f"{r['venue']:12} {r['sym']:18} {fmt(r.get('bid')):>10} {fmt(r.get('ask')):>10} "
                  f"{fmt(r.get('mark')):>10} {fmt(r.get('index')):>10} {fmt(bs,3):>7} "
                  f"{fmt(r['fr']*100 if r.get('fr') is not None else None,3):>9} {fmt(r.get('int_h'),2):>5} "
                  f"{fmt(frday,3):>8} {fmt(vb):>10} {fmt(va):>10} {fmt((r.get('vol24') or 0)/1e3,4):>8}"
                  + (f"  [{r['note']}]" if r.get("note") else ""))
    slim = {a: [{k: v for k, v in r.items() if k not in ("bids", "asks")} | {
        "bids": (r.get("bids") or [])[:50], "asks": (r.get("asks") or [])[:50]} for r in rows]
        for a, rows in out.items()}
    json.dump({"ts": int(time.time()), "data": slim}, open(os.path.join(V.DATA, "snapshot.json"), "w"))


if __name__ == "__main__":
    main()
