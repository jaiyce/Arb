"""Robustness checks: mark vs last-trade spreads, HK session effect (XIAOMI), weekly funding."""
import json, numpy as np, pandas as pd
from analyze import load, hourly_funding, backtest, half_life, FEES_RT
pd.set_option("display.width", 220)

def spread(asset, A, B, kind="close"):
    a, b = load(A, asset), load(B, asset)
    pa, pb = a[kind], b[kind]
    idx = pa.index.intersection(pb.index)
    full = pd.RangeIndex(idx.min(), idx.max()+1, 3600)
    return (pb.reindex(full).ffill()/pa.reindex(full).ffill()-1), a, b, full

# 1) mark-based vs close-based for pairs where both venues publish mark candles
print("MARK vs CLOSE spread (pct): std, %h>0.5%, n trades @0.5% entry & mean gross capture")
for asset, A, B in [("CYBER","ASTER","BINANCE"),("CYBER","GATE","BINANCE"),("SAHARA","GATE","BINANCE"),("SAHARA","GATE","OKX"),
                    ("MOONSHOT","BINANCE","OKX"),("MOONSHOT","BINANCE","GATE"),("XIAOMI","GATE","OKX"),("XIAOMI","ASTER","OKX"),("SOPH","BINANCE","OKX")]:
    for kind in ("close","mark"):
        s, a, b, full = spread(asset, A, B, kind)
        tr = backtest(s, None, 0.005, 0.001, 0.0)
        g = np.mean([t["spread_pnl"] for t in tr])*100 if tr else float("nan")
        print(f"{asset:8} {A:8}->{B:8} {kind:5} mean={s.mean()*100:6.2f} std={s.std()*100:5.2f} >0.5%={100*(s>0.005).mean():5.1f}%  trades={len(tr):3d} gross/trade={g:5.2f}  hl={half_life(s):4.1f}h")

# 2) XIAOMI: HK market open vs closed
print("\nXIAOMI spread by HK session (UTC 01:30-04:00 & 05:00-08:00 Mon-Fri = open)")
for A, B in [("GATE","HYPERLIQUID:io-XIAOMI"),("OKX","HYPERLIQUID:io-XIAOMI"),("ASTER","BYBIT"),("ASTER","OKX"),("GATE","OKX")]:
    s, a, b, full = spread("XIAOMI", A, B)
    ts = pd.to_datetime(s.index, unit="s", utc=True) - pd.Timedelta(hours=1)  # bar start
    mins = ts.hour*60 + ts.minute
    open_ = (ts.dayofweek < 5) & (((mins >= 90) & (mins < 240)) | ((mins >= 300) & (mins < 480)))
    so, sc = s[open_], s[~open_]
    print(f"{A:6}->{B[:14]:14} open: mean={so.mean()*100:5.2f} |s|>0.5%={100*(so.abs()>0.005).mean():5.1f}%   closed: mean={sc.mean()*100:5.2f} |s|>0.5%={100*(sc.abs()>0.005).mean():5.1f}%")
    if B.startswith("HYPER"):
        fb = hourly_funding(b["funding"], full)*100
        print(f"        HL funding %/h  open: {fb[open_].mean():.4f}   closed: {fb[~open_].mean():.4f}   (x24 closed = {fb[~open_].mean()*24:.2f}%/day)")
        wk = (ts.dayofweek >= 5)
        print(f"        weekend share of total HL funding: {fb[wk].sum()/fb.sum()*100:.0f}% ; weekend mean spread {s[wk].mean()*100:.2f}%")

# 3) funding time profile
print("\nDaily funding (%/day) by week, selected legs")
for v, asset in [("HYPERLIQUID:io-XIAOMI","XIAOMI"),("HYPERLIQUID:xyz-JIO","JIO"),("HYPERLIQUID:SOPH","SOPH"),("BINANCE","SOPH"),("ASTER","SOPH"),("GATE","SAHARA"),("BINANCE","SAHARA"),("BINANCE","CYBER")]:
    d = load(v, asset)
    f = d["funding"]
    if f is None or not len(f): continue
    w = (f.groupby(pd.to_datetime(f.index, unit="s").to_period("W")).sum() / 7 * 100).round(3)
    print(f"{v[:22]:22} {asset:7}", " ".join(f"{x:+.2f}" for x in w.values))
