"""
GET /api/momentum -> JSON with this month's NIFTY 500 momentum ranking; the page applies the rules to your holdings.

Rules (the winner of smallcase_momentum/tune.py; see smallcase_momentum/README.md):
  universe  NIFTY 500 members, EQ series, price >= Rs 20, traded on >= 230 of the last 252 days,
            60-day median turnover >= Rs 1 cr
  score     12-month return skipping the last month: growth from 273 to 21 trading days before the signal close
  portfolio top 20, equal weight. A stock you hold is kept while it ranks <= 40; empty places go to the
            best-ranked stocks you don't hold
  calendar  signal on the last close of each month, trade on the next trading day (monthly; weekly screening
            added nothing in the backtest). The latest close is also screened, as a watch list only.
  sizing    the page splits capital between the strategy and a liquid fund (default 60/40), reset monthly

The answer does not depend on anyone's holdings, so one cached copy serves every visitor. The page keeps your
holdings in the browser and applies the keep / sell / buy rule itself (decide() is the same rule, for the CLI).

Data, fetched live on each (cache-missed) request; no database, no keys, standard library only:
  * NIFTY 500 list: niftyindices.com (falls back to the bundled copy in data/)
  * daily prices: NSE UDiFF cash bhavcopies for the last ~15 months (nsearchives.nseindia.com)
Stocks are matched by ISIN. NSE's previous close is NOT adjusted for splits/bonuses: an opening gap beyond
-25%/+35% is treated as a corporate action and only that day's open->close move is counted. Holiday files
that repeat an earlier trading day are dropped.
"""
import csv
import io
import json
import os
import statistics
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

TOP, KEEP_RANK, LONG, SKIP = 20, 40, 273, 21          # 12 months (252 + skip) back to 1 month back
MIN_PRICE, MIN_TURNOVER, TURNOVER_DAYS, HIST_DAYS, HIST_MIN = 20.0, 1e7, 60, 252, 230
SHOW = 100                                             # ranked stocks shown on the page
BENCH = "NIFTYBEES"                                    # Nifty 50 ETF, for the equity-curve comparison
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept": "*/*"}
BHAV = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{:%Y%m%d}_F_0000.csv.zip"
N500 = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
HERE = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))
_CACHE = {}


def _get(url, timeout=25, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                                   # holiday / not published yet
            time.sleep(0.5 * (i + 1))
        except Exception:
            time.sleep(0.5 * (i + 1))
    return None


def load_members():
    raw, source = _get(N500), "niftyindices.com (live)"
    if not raw or b"ISIN" not in raw[:300]:
        fb = os.path.join(HERE, "data", "nifty500_fallback.csv")
        if not os.path.exists(fb):
            raise RuntimeError("couldn't download the NIFTY 500 list from niftyindices.com and no fallback copy is deployed")
        raw, source = open(fb, "rb").read(), "bundled copy (29 Sep 2026)"
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    return {r["ISIN Code"].strip(): {"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(),
                                     "sector": r["Industry"].strip()} for r in rows}, source


def fetch_day(d, isins, sym_isin):
    """one day's EQ rows for the members, keyed by the member's CURRENT ISIN. A face-value split gives a stock
    a new ISIN (same symbol) and a rename gives a new symbol (same ISIN), so a row matches on either."""
    raw = _get(BHAV.format(d))
    if not raw:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        out = {}
        for r in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")):
            if r["TradDt"] != d.isoformat():
                return None                                   # a repeated older file served on a holiday
            if r["SctySrs"] != "EQ":                          # EQ only (BE/BZ trade-for-trade excluded)
                continue
            if r["TckrSymb"] == BENCH:
                out[BENCH] = (float(r["OpnPric"] or 0), float(r["ClsPric"]), float(r["PrvsClsgPric"] or 0), 0.0)
                continue
            key = r["ISIN"] if r["ISIN"] in isins else sym_isin.get(r["TckrSymb"])
            if key and (key not in out or r["ISIN"] == key):
                out[key] = (float(r["OpnPric"] or 0), float(r["ClsPric"]), float(r["PrvsClsgPric"] or 0),
                                  float(r["TtlTrfVal"] or 0))
        return d, out
    except Exception:
        return None


def day_return(o, c, pc):
    r = c / pc - 1 if pc > 0 else 0.0
    if o > 0 and pc > 0 and not (-0.25 <= o / pc - 1 <= 0.35):
        r = c / o - 1                                         # split / bonus / demerger ex-date
    return max(-0.6, min(1.5, r))


def last_weekday_of_month(d):
    nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    x = nxt - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def build_series(got, members):
    """per ISIN: daily returns, cumulative growth, closes, turnover (aligned to the trading days in got)."""
    n = len(got)
    series = {}
    for i, (d, day) in enumerate(got):
        for isin, (o, c, pc, tv) in day.items():
            s = series.setdefault(isin, {"ret": [None] * n, "close": [None] * n, "tv": [None] * n})
            s["ret"][i] = day_return(o, c, pc) if pc > 0 else None
            s["close"][i], s["tv"][i] = c, tv
    for s in series.values():
        g, path = 1.0, []
        for r in s["ret"]:
            g *= 1 + (r or 0.0)
            path.append(g)
        s["g"] = path
    return series


def rank_at(series, members, dates, k):
    """ranked eligible stocks using closes up to trading-day index k (inclusive)."""
    out = []
    if k - LONG < 0:
        return out
    for isin, s in series.items():
        if isin not in members:                                # the benchmark ETF
            continue
        hist = sum(r is not None for r in s["ret"][max(0, k - HIST_DAYS + 1): k + 1])
        close = next((c for c in reversed(s["close"][: k + 1]) if c), None)
        tv = [v for v in s["tv"][max(0, k - TURNOVER_DAYS + 1): k + 1] if v is not None]
        med_tv = statistics.median(tv) if len(tv) >= 40 else 0.0
        if s["close"][k] is None:                              # not traded in the EQ series on the signal day
            continue
        if hist < HIST_MIN or not close or close < MIN_PRICE or med_tv < MIN_TURNOVER:
            continue
        g = s["g"]
        score = g[k - SKIP] / g[k - LONG] - 1
        daily = [r for r in s["ret"][k - HIST_DAYS + 1: k + 1] if r is not None]
        win = g[k - HIST_DAYS + 1: k + 1]
        m = members[isin]
        out.append({"isin": isin, "symbol": m["symbol"], "name": m["name"], "sector": m["sector"],
                    "score": score, "r12": g[k] / g[k - HIST_DAYS] - 1, "r6": g[k] / g[k - 126] - 1,
                    "r3": g[k] / g[k - 63] - 1, "r1": g[k] / g[k - 21] - 1,
                    "off_high": g[k] / max(win) - 1,
                    "vol": statistics.pstdev(daily) * 252 ** 0.5 if len(daily) > 20 else None,
                    "turnover_cr": med_tv / 1e7, "close": close})
    out.sort(key=lambda x: -x["score"])
    for i, x in enumerate(out, 1):
        x["rank"] = i
    return out


def decide(ranked, holdings):
    """apply the hold-while-rank<=40 rule to the holdings; returns keep / sell / buy and the new portfolio."""
    by_sym = {x["symbol"]: x for x in ranked}
    keep, sell = [], []
    for sym in holdings:
        x = by_sym.get(sym)
        if x is None:
            sell.append({"symbol": sym, "rank": None,
                         "reason": "not eligible any more (left the NIFTY 500, moved out of the EQ series, "
                                   "too illiquid, under Rs 20, or not traded)"})
        elif x["rank"] > KEEP_RANK:
            sell.append(dict(x, reason=f"rank {x['rank']} is below {KEEP_RANK}"))
        else:
            keep.append(dict(x, reason=f"rank {x['rank']} is within the top {KEEP_RANK}"))
    keep.sort(key=lambda x: x["rank"])
    for x in keep[TOP:]:                                       # more than 20 entered: drop the weakest
        sell.append(dict(x, reason=f"more than {TOP} holdings entered; rank {x['rank']} is the weakest"))
    keep = keep[:TOP]
    held = {x["symbol"] for x in keep}
    buy = []
    for x in ranked:
        if len(keep) + len(buy) >= TOP:
            break
        if x["symbol"] not in held:
            buy.append(dict(x, reason=f"rank {x['rank']}: the best-ranked stock not held"))
    portfolio = sorted(keep + buy, key=lambda x: x["rank"])
    return keep, sell, buy, portfolio


def compute():
    """everything the page needs, independent of anyone's holdings (the page applies the rule to them),
    so one cached answer serves every visitor."""
    t0 = time.time()
    members, list_source = load_members()
    isins = set(members)
    sym_isin = {m["symbol"]: i for i, m in members.items()}
    today = datetime.now(IST).date()
    span = 460                                                 # ~15 months: 273 trading days before last month end
    days = [today - timedelta(days=i) for i in range(span) if (today - timedelta(days=i)).weekday() < 5]
    with ThreadPoolExecutor(max_workers=16) as ex:
        got = [g for g in ex.map(lambda d: fetch_day(d, isins, sym_isin), days) if g]
    got.sort(key=lambda g: g[0])
    dates = [g[0] for g in got]
    month_start = date(today.year, today.month, 1)
    k_off = max((i for i, d in enumerate(dates) if d < month_start), default=-1)
    if k_off < LONG:
        raise RuntimeError(f"only {k_off + 1} trading days up to last month end could be downloaded from NSE "
                           f"(need {LONG + 1}); NSE may be blocking this server, try again later")
    series = build_series(got, members)
    official = rank_at(series, members, dates, k_off)
    latest = rank_at(series, members, dates, len(dates) - 1)
    trade_day = dates[k_off + 1] if k_off + 1 < len(dates) else None

    # this month's portfolio always sits inside the top KEEP_RANK (kept stocks rank <= 40, buys fill from the top)
    for x in official[:SHOW]:
        s = series[x["isin"]]
        x["latest_close"] = next((c for c in reversed(s["close"]) if c), x["close"])
        x["since_trade"] = (s["g"][-1] / s["g"][k_off + 1] - 1) if trade_day else 0.0
    curve = equity_curve(series, dates, k_off, official[:KEEP_RANK], {x["isin"] for x in official})

    nxt = last_weekday_of_month(today)
    if nxt <= dates[-1]:
        nxt = last_weekday_of_month(date(today.year + today.month // 12, today.month % 12 + 1, 1))
    slim = lambda xs: [{k: (round(v, 5) if isinstance(v, float) else v) for k, v in x.items() if k != "isin"} for x in xs]
    return {
        "generated_at": datetime.now(IST).isoformat(timespec="minutes"), "list_source": list_source,
        "rules": {"top": TOP, "keep_rank": KEEP_RANK, "lookback_days": LONG, "skip_days": SKIP,
                  "min_price": MIN_PRICE, "min_turnover_cr": MIN_TURNOVER / 1e7},
        "signal_date": dates[k_off].isoformat(), "trade_date": trade_day.isoformat() if trade_day else None,
        "data_through": dates[-1].isoformat(), "next_signal": nxt.isoformat(), "trading_days": len(dates),
        "members": sorted(sym_isin),
        "official": {"universe": len(official), "ranking": slim(official[:SHOW]),
                     "ranks": {x["symbol"]: [x["rank"], round(x["score"], 4)] for x in official}},
        "latest": {"universe": len(latest), "ranking": slim(latest[:SHOW]),
                   "ranks": {x["symbol"]: x["rank"] for x in latest}},
        "curve": curve,
        "seconds": round(time.time() - t0, 1),
    }


def equity_curve(series, dates, k_off, candidates, universe):
    """growth since the trade-day close (the rebalance) for every stock a portfolio can hold this month, plus an
    equal-weight NIFTY 500 (the screened universe, rebalanced daily) and the Nifty 50 ETF. The page averages the
    stocks of the user's portfolio. Costs and the liquid share are left out; the page applies the split."""
    t0 = k_off + 1
    if t0 >= len(dates):
        return None
    idx = range(t0, len(dates))
    pos = {}
    for x in candidates:
        g = series[x["isin"]]["g"]
        pos[x["symbol"]] = [round(g[i] / g[t0], 6) for i in idx]
    ew, e = [], 1.0
    for i in idx:
        if i > t0:
            rs = [series[u]["ret"][i] for u in universe if series[u]["ret"][i] is not None]
            e *= 1 + (sum(rs) / len(rs) if rs else 0.0)
        ew.append(round(e, 6))
    b = series.get(BENCH)
    nifty = [round(b["g"][i] / b["g"][t0], 6) for i in idx] if b else None
    return {"dates": [dates[i].isoformat() for i in idx], "ew_nifty500": ew, "nifty50": nifty, "positions": pos}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if not _CACHE.get("body") or time.time() - _CACHE["t"] > 6 * 3600:
                _CACHE["body"], _CACHE["t"] = json.dumps(compute()).encode(), time.time()
            body, code = _CACHE["body"], 200
        except Exception as e:
            body, code = json.dumps({"error": str(e)}).encode(), 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if code == 200:   # Vercel's CDN serves the same answer for 6 h, then refreshes in the background
            self.send_header("Cache-Control", "public, max-age=0, s-maxage=21600, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":      # local check: python api/momentum.py "SYM1 SYM2 ..."
    import sys
    res = compute()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    hold = sorted({h.strip().upper() for h in arg.replace(" ", ",").split(",") if h.strip()})
    o = res["official"]
    rows = {x["symbol"]: x for x in o["ranking"]}
    full = [dict(rows.get(sym, {"symbol": sym}), rank=r, score=sc)
            for sym, (r, sc) in sorted(o["ranks"].items(), key=lambda kv: kv[1][0])]
    keep, sell, buy, portfolio = decide(full, hold)
    print("signal", res["signal_date"], "trade", res["trade_date"], "data through", res["data_through"],
          "| days", res["trading_days"], "| universe", o["universe"], "| seconds", res["seconds"])
    print("portfolio:", " ".join(f"{x['symbol']}({x['rank']})" for x in portfolio))
    print("sell:", [(x["symbol"], x["reason"]) for x in sell])
    print("buy: ", [x["symbol"] for x in buy])
    c = res["curve"]
    if c:
        port = [sum(c["positions"][x["symbol"]][j] for x in portfolio) / len(portfolio) for j in range(len(c["dates"]))]
        print("curve:", c["dates"][0], "->", c["dates"][-1], "| portfolio", round(port[-1] - 1, 4),
              "| EW N500", round(c["ew_nifty500"][-1] - 1, 4), "| Nifty 50", c["nifty50"] and round(c["nifty50"][-1] - 1, 4))
    print("ranking rows:", len(o["ranking"]), len(res["latest"]["ranking"]), "| json KB", len(json.dumps(res)) // 1024)
