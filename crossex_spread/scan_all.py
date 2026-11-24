"""Bulk cross-venue scan: executable perp spreads (bid on rich venue vs ask on cheap venue)
with 24h volume filter and funding carry. Venues: Binance, OKX, Gate, Aster, Hyperliquid (all dexes),
Kraken. Bybit is geo-blocked here.
"""
import os
import re
import sys
import time

import pandas as pd

import venues as V

MIN_VOL = float(sys.argv[1]) if len(sys.argv) > 1 else 1e6
FEES_RT = 0.002


def norm(base):
    m = re.match(r"^(1000000|100000|10000|1000|1M|k)(.+)$", base)
    if m and m.group(2).isupper():
        mult = {"1000000": 1e6, "1M": 1e6, "100000": 1e5, "10000": 1e4, "1000": 1e3, "k": 1e3}[m.group(1)]
        return m.group(2), mult
    return base, 1.0


rows = []
# Binance / Aster
for host, venue in ((V.BINANCE, "BINANCE"), (V.ASTER, "ASTER")):
    bt = {x["symbol"]: x for x in V.get(f"{host}/fapi/v1/ticker/bookTicker")}
    pi = {x["symbol"]: x for x in V.get(f"{host}/fapi/v1/premiumIndex")}
    t24 = {x["symbol"]: x for x in V.get(f"{host}/fapi/v1/ticker/24hr")}
    fi = V._funding_info(host)
    for s, b in bt.items():
        if not s.endswith("USDT") or s not in pi or s not in t24:
            continue
        base, mult = norm(s[:-4])
        ih = float(fi[s]["fundingIntervalHours"]) if s in fi else 8.0
        try:
            rows.append(dict(venue=venue, base=base, bid=float(b["bidPrice"]) / mult, ask=float(b["askPrice"]) / mult,
                             vol=float(t24[s]["quoteVolume"]), fr_day=float(pi[s]["lastFundingRate"]) * 24 / ih))
        except (ValueError, KeyError):
            pass
# OKX
tk = V.get(f"{V.OKX}/market/tickers", instType="SWAP")["data"]
for x in tk:
    if not x["instId"].endswith("-USDT-SWAP") or not x["bidPx"] or not x["askPx"]:
        continue
    base, mult = norm(x["instId"].split("-")[0])
    rows.append(dict(venue="OKX", base=base, bid=float(x["bidPx"]) / mult, ask=float(x["askPx"]) / mult,
                     vol=float(x["volCcy24h"]) * float(x["last"]), fr_day=None))
# Gate
gt = V.get(f"{V.GATE}/futures/usdt/tickers")
gc = {x["name"]: x for x in V.get(f"{V.GATE}/futures/usdt/contracts")}
for x in gt:
    c = x["contract"]
    if not c.endswith("_USDT") or not x.get("highest_bid") or not x.get("lowest_ask"):
        continue
    base, mult = norm(c[:-5])
    ih = gc.get(c, {}).get("funding_interval", 28800) / 3600
    try:
        rows.append(dict(venue="GATE", base=base, bid=float(x["highest_bid"]) / mult, ask=float(x["lowest_ask"]) / mult,
                         vol=float(x.get("volume_24h_quote") or 0), fr_day=float(x["funding_rate"]) * 24 / ih))
    except ValueError:
        pass
# Hyperliquid (impact prices = executable at impact notional)
for d in V.post(V.HL, {"type": "perpDexs"}):
    dex = "" if d is None else d["name"]
    for name, (a, c) in V._hl_ctx(dex).items():
        if a.get("isDelisted") or not c.get("impactPxs"):
            continue
        base, mult = norm(name.split(":")[-1])
        rows.append(dict(venue="HL" + (f":{dex}" if dex else ""), base=base,
                         bid=float(c["impactPxs"][0]) / mult, ask=float(c["impactPxs"][1]) / mult,
                         vol=float(c["dayNtlVlm"]), fr_day=float(c["funding"]) * 24))
# Kraken futures
for x in V.get("https://futures.kraken.com/derivatives/api/v3/tickers")["tickers"]:
    s = x.get("symbol", "")
    if not s.startswith("PF_") or not x.get("bid") or not x.get("ask"):
        continue
    base, mult = norm(s[3:-3].replace("XBT", "BTC"))
    fr = x.get("fundingRate")
    rows.append(dict(venue="KRAKEN", base=base, bid=float(x["bid"]) / mult, ask=float(x["ask"]) / mult,
                     vol=float(x.get("volumeQuote") or 0),
                     fr_day=(float(fr) * 24 / float(x["markPrice"])) if fr not in (None, "") and x.get("markPrice") else None))

df = pd.DataFrame(rows)
df = df[(df.bid > 0) & (df.ask > 0) & (df.ask >= df.bid)]
print("rows per venue:", df.venue.value_counts().to_dict())

liq = df[df.vol >= MIN_VOL]
opps = []
for base, g in liq.groupby("base"):
    if g.venue.nunique() < 2:
        continue
    for _, a in g.iterrows():
        for _, b in g.iterrows():
            if a.venue == b.venue:
                continue
            sp = b.bid / a.ask - 1          # buy A at ask, sell B at bid
            if sp <= 0 or sp > 0.2:
                continue
            carry = (b.fr_day - a.fr_day) if pd.notna(a.fr_day) and pd.notna(b.fr_day) else float("nan")
            opps.append(dict(base=base, long=a.venue, short=b.venue, exec_spread=sp * 100,
                             net_after_fees=(sp - FEES_RT) * 100, carry_pct_day=carry * 100 if carry == carry else carry,
                             vol_long_m=a.vol / 1e6, vol_short_m=b.vol / 1e6,
                             page_apr_14d=sp * 365 / 14 * 100))
o = pd.DataFrame(opps).sort_values("exec_spread", ascending=False)
pd.set_option("display.width", 220)
print(f"\nassets on >=2 venues with 24h vol >= ${MIN_VOL/1e6:.0f}M each:",
      liq.groupby("base").venue.nunique().ge(2).sum())
print(f"pairs with positive executable spread: {len(o)}; > fees (0.20%): {(o.exec_spread > 0.2).sum()}; > 0.5%: {(o.exec_spread > 0.5).sum()}")
print(o.head(25).round(3).to_string(index=False))
o.to_csv(os.path.join(V.DATA, "scan_all.csv"), index=False)
