"""Spread history, funding carry and a naive backtest of the Gate 'Max APR' signal.

Pair convention: (asset, A, B) -> long A (cheap leg), short B (rich leg).
spread s_t = P_B / P_A - 1 on hourly closes (last trade of the hour).
Funding P&L of the pair per settlement = + r_B (short receives) - r_A (long pays).
"""
import json
import math
import os

import numpy as np
import pandas as pd

import venues as V

H = os.path.join(V.DATA, "hist")
FEE_TAKER = 0.0005          # CrossEx VIP0 taker, per fill
FEES_RT = 4 * FEE_TAKER     # open + close, both legs
HORIZON_H = 14 * 24

PAIRS = [
    ("XIAOMI", "ASTER", "BYBIT"), ("XIAOMI", "GATE", "HYPERLIQUID:io-XIAOMI"),
    ("XIAOMI", "GATE", "OKX"), ("XIAOMI", "OKX", "HYPERLIQUID:io-XIAOMI"),
    ("XIAOMI", "ASTER", "OKX"), ("XIAOMI", "GATE", "BYBIT"),
    ("JIO", "GATE", "HYPERLIQUID:xyz-JIO"),
    ("LAOPU", "BYBIT", "GATE"),
    ("SOPH", "ASTER", "HYPERLIQUID:SOPH"), ("SOPH", "BINANCE", "HYPERLIQUID:SOPH"),
    ("SOPH", "BINANCE", "BYBIT"), ("SOPH", "BINANCE", "OKX"),
    ("SAHARA", "GATE", "BYBIT"), ("SAHARA", "GATE", "BINANCE"), ("SAHARA", "GATE", "OKX"),
    ("SAHARA", "BINANCE", "OKX"),
    ("DRV", "ASTER", "HYPERLIQUID:para-DRV"), ("DRV", "ASTER", "BYBIT"),
    ("MOONSHOT", "BINANCE", "BYBIT"), ("MOONSHOT", "BINANCE", "OKX"), ("MOONSHOT", "BINANCE", "GATE"),
    ("CYBER", "GATE", "BYBIT"), ("CYBER", "GATE", "BINANCE"), ("CYBER", "ASTER", "BINANCE"),
]


def load(venue, asset):
    if venue.startswith("HYPERLIQUID:"):
        fn = f"{H}/HYPERLIQUID_{venue.split(':', 1)[1]}.json"
    else:
        fn = f"{H}/{venue}_{asset}.json"
    if not os.path.exists(fn):
        return None
    d = json.load(open(fn))
    close = pd.Series({int(k): v for k, v in d["close"].items()}, dtype=float).sort_index()
    mark = pd.Series({int(k): v for k, v in (d.get("mark") or {}).items()}, dtype=float).sort_index()
    fund = None
    if d.get("funding") is not None:
        fund = pd.Series({int(t): r for t, r in d["funding"]}, dtype=float).sort_index()
        fund = fund.groupby(fund.index).last()
    return dict(close=close, mark=mark, funding=fund)


def half_life(s):
    s = s.dropna()
    if len(s) < 50:
        return float("nan")
    x, y = s.values[:-1], s.values[1:]
    x = x - x.mean()
    y = y - y.mean()
    phi = (x * y).sum() / (x * x).sum()
    if not 0 < phi < 1:
        return float("inf") if phi >= 1 else 0.0
    return -math.log(2) / math.log(phi)


def hourly_funding(f, idx):
    """Settlement series -> hourly vector aligned on idx (rate booked in the hour it settles)."""
    if f is None:
        return None
    out = pd.Series(0.0, index=idx)
    for t, r in f.items():
        h = (t + 3599) // 3600 * 3600  # settlement belongs to the hour ending at/after t
        if h in out.index:
            out[h] += r
    return out


def backtest(s, fcarry, entry, exit_, cost):
    """Enter short-spread when s >= entry, exit when s <= exit_ or after 14d or at data end."""
    trades, i, n = [], 0, len(s)
    vals, idx = s.values, s.index
    fc = fcarry.values if fcarry is not None else None
    while i < n:
        if not np.isnan(vals[i]) and vals[i] >= entry:
            j = i + 1
            while j < n and (np.isnan(vals[j]) or vals[j] > exit_) and j - i < HORIZON_H:
                j += 1
            j = min(j, n - 1)
            spread_pnl = (vals[i] - vals[j]) / (1 + vals[i])
            fund = float(np.nansum(fc[i + 1:j + 1])) if fc is not None else float("nan")
            trades.append(dict(t_in=idx[i], t_out=idx[j], hours=j - i, s_in=vals[i], s_out=vals[j],
                               spread_pnl=spread_pnl, funding=fund, cost=cost,
                               converged=bool(vals[j] <= exit_), net=spread_pnl + (0 if np.isnan(fund) else fund) - cost))
            i = j + 1
        else:
            i += 1
    return trades


def main():
    snap = json.load(open(os.path.join(V.DATA, "snapshot.json")))["data"]

    def half_spread(asset, venue):
        vname = venue.split(":")[0]
        for r in snap.get(asset, []):
            if r["venue"] == vname and r.get("bid") and r.get("ask"):
                if vname == "HYPERLIQUID" and r["sym"].replace(":", "-") != venue.split(":", 1)[1]:
                    continue
                return (r["ask"] - r["bid"]) / (r["ask"] + r["bid"])
        return float("nan")

    rows, all_trades = [], []
    for asset, A, B in PAIRS:
        a, b = load(A, asset), load(B, asset)
        if a is None or b is None:
            rows.append(dict(pair=f"{asset} {A}->{B}", note="missing data"))
            continue
        idx = a["close"].index.intersection(b["close"].index)
        if len(idx) < 48:
            rows.append(dict(pair=f"{asset} {A}->{B}", note=f"only {len(idx)} common hours"))
            continue
        full = pd.RangeIndex(idx.min(), idx.max() + 1, 3600)
        pa = a["close"].reindex(full).ffill()
        pb = b["close"].reindex(full).ffill()
        s = pb / pa - 1
        sm = None
        if len(a["mark"]) and len(b["mark"]):
            sm = (b["mark"].reindex(full) / a["mark"].reindex(full) - 1)
        fa, fb = hourly_funding(a["funding"], full), hourly_funding(b["funding"], full)
        carry = (fb - fa) if (fa is not None and fb is not None) else None
        hs_a, hs_b = half_spread(asset, A), half_spread(asset, B)
        slip = 2 * (np.nan_to_num(hs_a, nan=0.002) + np.nan_to_num(hs_b, nan=0.002))  # cross half-spread in & out
        cost = FEES_RT + slip
        days = len(full) / 24
        r = dict(pair=f"{asset} {A.split(':')[0][:5]}->{B.split(':')[0][:5]}", days=round(days, 1),
                 s_now=s.iloc[-1], s_mean=s.mean(), s_med=s.median(), s_std=s.std(),
                 p5=s.quantile(.05), p95=s.quantile(.95),
                 pct_gt_05=(s > 0.005).mean(), pct_gt_1=(s > 0.01).mean(),
                 hl_h=half_life(s), s_mark_mean=sm.mean() if sm is not None else float("nan"),
                 carry_day=(carry.sum() / days) if carry is not None else float("nan"),
                 fa_day=(fa.sum() / days) if fa is not None else float("nan"),
                 fb_day=(fb.sum() / days) if fb is not None else float("nan"),
                 cost_rt=cost)
        for entry in (0.005, 0.01):
            tr = backtest(s, carry, entry, 0.001, cost)
            for t in tr:
                t.update(pair=r["pair"], entry=entry)
            all_trades += tr
            if tr:
                r[f"n@{entry:.1%}"] = len(tr)
                r[f"conv@{entry:.1%}"] = np.mean([t["converged"] for t in tr])
                r[f"net@{entry:.1%}"] = np.mean([t["net"] for t in tr])
                r[f"hrs@{entry:.1%}"] = np.mean([t["hours"] for t in tr])
        # 'page signal' forward test: every hour with s >= 0.8%, hold exactly 14d (or to data end)
        sig = s[s >= 0.008].index
        fw = []
        for t0 in sig:
            i = full.get_loc(t0)
            j = min(i + HORIZON_H, len(full) - 1)
            if j - i < 72:
                continue
            spnl = (s.iloc[i] - s.iloc[j]) / (1 + s.iloc[i])
            fpnl = float(carry.iloc[i + 1:j + 1].sum()) if carry is not None else float("nan")
            fw.append((spnl, fpnl))
        if fw:
            sp, fp = np.array(fw).T
            r["fw_n"] = len(fw)
            r["fw_spread_med"] = np.median(sp)
            r["fw_fund_med"] = np.nanmedian(fp) if not np.all(np.isnan(fp)) else float("nan")
            r["fw_net_med"] = np.median(sp + np.nan_to_num(fp) - cost)
            r["fw_win"] = np.mean(sp + np.nan_to_num(fp) - cost > 0)
        rows.append(r)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 50)
    pct = lambda x: f"{x*100:.2f}" if isinstance(x, (float, np.floating)) and not np.isnan(x) else "-"
    cols1 = ["pair", "days", "s_now", "s_mean", "s_med", "s_std", "p5", "p95", "pct_gt_05", "pct_gt_1", "hl_h", "s_mark_mean"]
    cols2 = ["pair", "fa_day", "fb_day", "carry_day", "cost_rt"]
    cols3 = ["pair", "n@0.5%", "conv@0.5%", "hrs@0.5%", "net@0.5%", "n@1.0%", "conv@1.0%", "hrs@1.0%", "net@1.0%",
             "fw_n", "fw_spread_med", "fw_fund_med", "fw_net_med", "fw_win"]
    for cols, title in ((cols1, "SPREAD STATS (%; s = P_short/P_long - 1, hl_h = AR(1) half-life in hours)"),
                        (cols2, "FUNDING (% per day; carry = short-leg rate - long-leg rate, + = pair earns)"),
                        (cols3, "BACKTESTS (net % of notional per trade after fees+half-spreads; fw = 14d forward from every hour s>=0.8%)")):
        print("\n" + title)
        sub = df[[c for c in cols if c in df.columns]].copy()
        for c in sub.columns:
            if c in ("pair", "days", "n@0.5%", "n@1.0%", "fw_n"):
                continue
            if c in ("hl_h", "hrs@0.5%", "hrs@1.0%"):
                sub[c] = sub[c].map(lambda x: f"{x:.1f}" if isinstance(x, (float, np.floating)) and not np.isnan(x) else "-")
            else:
                sub[c] = sub[c].map(pct)
        print(sub.to_string(index=False))
    pd.DataFrame(all_trades).to_csv(os.path.join(V.DATA, "trades.csv"), index=False)
    df.to_csv(os.path.join(V.DATA, "pair_stats.csv"), index=False)


if __name__ == "__main__":
    main()
