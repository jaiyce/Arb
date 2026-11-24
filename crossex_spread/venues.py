"""Public market-data adapters for the venues reachable from here.

Every snapshot function returns a dict with:
  venue, sym, bids/asks [(price, base_qty)], bid, ask, mark, index, last,
  fr (funding rate per interval), int_h (interval hours), next_ts (ms)
Bybit REST is geo-blocked from US IPs: `bybit()` uses the v5 API when reachable
(e.g. from Europe); otherwise callers fall back to Lighter's cross-exchange
funding feed (8h-normalised) and to public.bybit.com trade dumps for history.
"""
import functools
import os
import time

import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"
T = 20

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

GATE = "https://api.gateio.ws/api/v4"
BINANCE = "https://www.binance.com"
ASTER = "https://fapi.asterdex.com"
OKX = "https://www.okx.com/api/v5"
HL = "https://api.hyperliquid.xyz/info"
LIGHTER = "https://mainnet.zklighter.elliot.ai/api/v1"
BYBIT = "https://api.bybit.com/v5"


def get(url, **params):
    for i in range(4):
        try:
            r = S.get(url, params=params, timeout=T)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 3:
                raise
            time.sleep(1.0 * (i + 1))


def post(url, body):
    for i in range(4):
        try:
            r = S.post(url, json=body, timeout=T)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 3:
                raise
            time.sleep(1.0 * (i + 1))


def vwap(levels, notional):
    """Average fill price to trade `notional` (quote) through `levels`."""
    q_acc = b_acc = 0.0
    for p, s in levels:
        if q_acc + p * s >= notional:
            b_acc += (notional - q_acc) / p
            return notional / b_acc
        q_acc += p * s
        b_acc += s
    return None


def _pack(venue, sym, bids, asks, mark, index, last, fr, int_h, next_ts, **extra):
    d = dict(venue=venue, sym=sym, bids=bids, asks=asks,
             bid=bids[0][0] if bids else None, ask=asks[0][0] if asks else None,
             mark=mark, index=index, last=last, fr=fr, int_h=int_h, next_ts=next_ts)
    d.update(extra)
    return d


# ---------------------------------------------------------------- Gate
def gate(base, quote="USDT"):
    c = f"{base}_{quote}"
    t = get(f"{GATE}/futures/usdt/tickers", contract=c)[0]
    k = get(f"{GATE}/futures/usdt/contracts/{c}")
    ob = get(f"{GATE}/futures/usdt/order_book", contract=c, limit=50)
    m = float(k["quanto_multiplier"])
    bids = [(float(x["p"]), float(x["s"]) * m) for x in ob["bids"]]
    asks = [(float(x["p"]), float(x["s"]) * m) for x in ob["asks"]]
    return _pack("GATE", c, bids, asks, float(t["mark_price"]), float(t["index_price"]),
                 float(t["last"]), float(t["funding_rate"]), k["funding_interval"] / 3600,
                 int(k["funding_next_apply"]) * 1000,
                 vol24=float(t.get("volume_24h_quote") or 0))


# ------------------------------------------------- Binance / Aster (same API)
@functools.lru_cache(None)
def _funding_info(host):
    try:
        return {x["symbol"]: x for x in get(f"{host}/fapi/v1/fundingInfo")}
    except Exception:
        return {}


def _binance_like(host, venue, base, quote="USDT"):
    s = base + quote
    p = get(f"{host}/fapi/v1/premiumIndex", symbol=s)
    ob = get(f"{host}/fapi/v1/depth", symbol=s, limit=50)
    t = get(f"{host}/fapi/v1/ticker/24hr", symbol=s)
    fi = _funding_info(host).get(s)
    int_h = float(fi["fundingIntervalHours"]) if fi else 8.0
    bids = [(float(a), float(b)) for a, b in ob["bids"]]
    asks = [(float(a), float(b)) for a, b in ob["asks"]]
    return _pack(venue, s, bids, asks, float(p["markPrice"]), float(p["indexPrice"]),
                 float(t["lastPrice"]), float(p["lastFundingRate"]), int_h,
                 int(p["nextFundingTime"]), vol24=float(t.get("quoteVolume") or 0))


def binance(base, quote="USDT"):
    return _binance_like(BINANCE, "BINANCE", base, quote)


def aster(base, quote="USDT"):
    return _binance_like(ASTER, "ASTER", base, quote)


# ---------------------------------------------------------------- OKX
def okx(base, quote="USDT"):
    inst = f"{base}-{quote}-SWAP"
    ins = get(f"{OKX}/public/instruments", instType="SWAP", instId=inst)["data"][0]
    ct = float(ins["ctVal"])
    ob = get(f"{OKX}/market/books", instId=inst, sz=50)["data"][0]
    mk = float(get(f"{OKX}/public/mark-price", instType="SWAP", instId=inst)["data"][0]["markPx"])
    fr = get(f"{OKX}/public/funding-rate", instId=inst)["data"][0]
    tk = get(f"{OKX}/market/ticker", instId=inst)["data"][0]
    try:
        idx = float(get(f"{OKX}/market/index-tickers", instId=f"{base}-{quote}")["data"][0]["idxPx"])
    except Exception:
        idx = None
    int_h = (int(fr["nextFundingTime"]) - int(fr["fundingTime"])) / 3.6e6
    bids = [(float(r[0]), float(r[1]) * ct) for r in ob["bids"]]
    asks = [(float(r[0]), float(r[1]) * ct) for r in ob["asks"]]
    return _pack("OKX", inst, bids, asks, mk, idx, float(tk["last"]), float(fr["fundingRate"]),
                 int_h, int(fr["fundingTime"]),
                 vol24=float(tk.get("volCcy24h") or 0) * float(tk["last"]))


# ---------------------------------------------------------- Hyperliquid
@functools.lru_cache(None)
def _hl_ctx(dex):
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    meta, ctx = post(HL, body)
    return {a["name"]: (a, c) for a, c in zip(meta["universe"], ctx)}


def hl_find(base):
    """Return live (non-delisted) HL coin names for a base, across all dexes."""
    out = []
    for d in post(HL, {"type": "perpDexs"}):
        dex = "" if d is None else d["name"]
        for name, (a, c) in _hl_ctx(dex).items():
            if name.split(":")[-1] == base and not a.get("isDelisted"):
                out.append(name)
    return out


def hyperliquid(coin):
    dex = coin.split(":")[0] if ":" in coin else ""
    a, c = _hl_ctx(dex)[coin]
    lv = post(HL, {"type": "l2Book", "coin": coin})["levels"]
    bids = [(float(x["px"]), float(x["sz"])) for x in lv[0]]
    asks = [(float(x["px"]), float(x["sz"])) for x in lv[1]]
    now = int(time.time() * 1000)
    return _pack("HYPERLIQUID", coin, bids, asks, float(c["markPx"]), float(c["oraclePx"]),
                 float(c["midPx"]) if c.get("midPx") else None, float(c["funding"]), 1.0,
                 now - now % 3_600_000 + 3_600_000, vol24=float(c.get("dayNtlVlm") or 0))


# --------------------------------------------------------------- Lighter
@functools.lru_cache(None)
def _lighter_markets():
    return {x["symbol"]: x for x in get(f"{LIGHTER}/orderBooks")["order_books"]}


@functools.lru_cache(None)
def lighter_funding_feed():
    """{(exchange, symbol): rate normalised to 8h} from Lighter's aggregated feed."""
    return {(x["exchange"], x["symbol"]): float(x["rate"])
            for x in get(f"{LIGHTER}/funding-rates")["funding_rates"]}


def lighter(base):
    m = _lighter_markets()[base]
    mid = m["market_id"]
    det = get(f"{LIGHTER}/orderBookDetails", market_id=mid)["order_book_details"][0]
    ob = get(f"{LIGHTER}/orderBookOrders", market_id=mid, limit=50)
    bids = sorted(((float(x["price"]), float(x["remaining_base_amount"])) for x in ob["bids"]), reverse=True)
    asks = sorted((float(x["price"]), float(x["remaining_base_amount"])) for x in ob["asks"])
    fr8 = lighter_funding_feed().get(("lighter", base))
    now = int(time.time() * 1000)
    return _pack("LIGHTER", base, bids, asks, float(det["mark_price"]), float(det["index_price"]),
                 float(det["last_trade_price"]), fr8 / 8 if fr8 is not None else None, 1.0,
                 now - now % 3_600_000 + 3_600_000,
                 vol24=float(det.get("daily_quote_token_volume") or 0))


# ----------------------------------------------------------------- Bybit
def bybit(base, quote="USDT"):
    """Bybit v5 linear perp. Not reachable from US IPs (CloudFront 403)."""
    s = base + quote
    t = get(f"{BYBIT}/market/tickers", category="linear", symbol=s)["result"]["list"][0]
    ob = get(f"{BYBIT}/market/orderbook", category="linear", symbol=s, limit=50)["result"]
    int_h = t.get("fundingIntervalHour")
    if not int_h:
        ins = get(f"{BYBIT}/market/instruments-info", category="linear", symbol=s)["result"]["list"][0]
        int_h = float(ins["fundingInterval"]) / 60
    bids = [(float(a), float(b)) for a, b in ob["b"]]
    asks = [(float(a), float(b)) for a, b in ob["a"]]
    return _pack("BYBIT", s, bids, asks, float(t["markPrice"]), float(t["indexPrice"]),
                 float(t["lastPrice"]), float(t["fundingRate"]), float(int_h),
                 int(t["nextFundingTime"]), vol24=float(t.get("turnover24h") or 0))


def bybit_funding_history(base, start_s, end_s, quote="USDT"):
    """[[ts_s, rate], ...] or None when the API is unreachable."""
    s, acc, end = base + quote, [], end_s * 1000
    try:
        while True:
            rows = get(f"{BYBIT}/market/funding/history", category="linear", symbol=s,
                       startTime=start_s * 1000, endTime=end, limit=200)["result"]["list"]
            acc += [[int(r["fundingRateTimestamp"]) // 1000, float(r["fundingRate"])] for r in rows]
            if len(rows) < 200:
                break
            end = min(int(r["fundingRateTimestamp"]) for r in rows) - 1
    except Exception:
        return None
    return sorted(acc)
