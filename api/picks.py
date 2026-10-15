"""
GET /api/picks -> JSON with this quarter's Sector Rotation picks and the reasoning behind each one.

Two results are returned:
  official  the portfolio for the current quarter: the rules run on closes up to the trading day before the
            quarter's first trading day (STRATEGY.md). It stays the same until the next rebalance.
  preview   the same rules on the latest closes ("if you rebalanced today"). Changes daily; not traded.

Rules (identical to STRATEGY.md / sector_rotation_test.py):
  universe  NIFTY 500 members, price >= Rs 20, >= 6 months of history, top 300 by 60-day median turnover
  sectors   NSE "Industry"; ranked by equal-weight 6-month (126 trading days) return; need >= 5 stocks
  picks     top 3 sectors x top 2 stocks by relative strength (stock 6m return - sector 6m return)

Data, fetched live on each (cache-missed) request — no database, no keys, standard library only:
  * NIFTY 500 list + sectors: niftyindices.com (falls back to the bundled copy in data/)
  * daily prices: NSE UDiFF cash bhavcopies for the last ~190 calendar days (nsearchives.nseindia.com)
Stocks are matched by ISIN, so symbol renames don't break the price history.
NSE's previous close is NOT adjusted for splits/bonuses: an opening gap beyond -25%/+35% is treated as a
corporate action and only that day's open->close move is counted. Holiday files that repeat an earlier
trading day are dropped (their trade-date field doesn't match).
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

LOOKBACK_DAYS, N_SECTORS, PER_SECTOR, UNIVERSE, MIN_MEMBERS, MIN_PRICE, TURNOVER_DAYS = 126, 3, 2, 300, 5, 20.0, 60
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept": "*/*"}
BHAV = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{:%Y%m%d}_F_0000.csv.zip"
N500 = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
HERE = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))
_CACHE = {"t": 0, "body": None}


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


def fetch_day(d, isins):
    raw = _get(BHAV.format(d))
    if not raw:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        out = {}
        for r in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")):
            if r["TradDt"] != d.isoformat():
                return None                                   # a repeated older file served on a holiday
            if r["ISIN"] in isins and r["SctySrs"] == "EQ":    # EQ only, as in the backtest
                out[r["ISIN"]] = (float(r["OpnPric"] or 0), float(r["ClsPric"]), float(r["PrvsClsgPric"] or 0),
                                  float(r["TtlTrfVal"] or 0), r["TckrSymb"])
        return d, out
    except Exception:
        return None


def quarter_start(today):
    return date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)


def next_rebalance(today):
    for y, m in [(today.year, mm) for mm in (1, 4, 7, 10)] + [(today.year + 1, 1)]:
        d = date(y, m, 1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d > today:
            return d


def pct(x):
    return f"{x * 100:+.1f}%"


def compute():
    t0 = time.time()
    members, list_source = load_members()
    isins = set(members)
    today = datetime.now(IST).date()
    qs = quarter_start(today)
    span = (today - qs).days + 200                            # enough history for the quarter's signal date too
    days = [today - timedelta(days=i) for i in range(0, span) if (today - timedelta(days=i)).weekday() < 5]
    with ThreadPoolExecutor(max_workers=16) as ex:
        got = [g for g in ex.map(lambda d: fetch_day(d, isins), days) if g]
    got.sort(key=lambda g: g[0])
    before = [g for g in got if g[0] < qs]                    # closes up to the day before the rebalance
    if len(before) < LOOKBACK_DAYS + 1:
        raise RuntimeError(f"only {len(before)} trading days before {qs} could be downloaded from NSE (need "
                           f"{LOOKBACK_DAYS + 1}); NSE may be blocking this server — try again later")
    official, preview = run_rules(before, members), run_rules(got, members)
    reb_day = next((g[0] for g in got if g[0] >= qs), None)

    # how each official pick has done since the signal close (corporate-action safe, from daily returns)
    after = [g for g in got if g[0] > before[-1][0]]
    for p in official["picks"]:
        g, last = 1.0, None
        for d, day in after:
            if p["isin"] in day:
                o, c, pc, _, _ = day[p["isin"]]
                g *= 1 + day_return(o, c, pc); last = c
        p["since_signal"] = g - 1 if after else 0.0
        p["latest_close"] = last if last is not None else p["close"]
    keep = {p["symbol"] for p in official["picks"]}
    for p in preview["picks"]:
        p["in_portfolio"] = p["symbol"] in keep

    official.update(rebalance_date=(reb_day or next_rebalance(qs - timedelta(days=1))).isoformat(),
                    quarter_start=qs.isoformat())
    return {
        "generated_at": datetime.now(IST).isoformat(timespec="minutes"),
        "next_rebalance": next_rebalance(today).isoformat(), "list_source": list_source,
        "rules": {"lookback_days": LOOKBACK_DAYS, "sectors": N_SECTORS, "per_sector": PER_SECTOR,
                  "universe": UNIVERSE, "min_members": MIN_MEMBERS, "min_price": MIN_PRICE},
        "official": official, "preview": preview, "seconds": round(time.time() - t0, 1),
    }


def day_return(o, c, pc):
    r = c / pc - 1 if pc > 0 else 0.0
    if o > 0 and pc > 0 and not (-0.25 <= o / pc - 1 <= 0.35):
        r = c / o - 1                                         # split / bonus / demerger ex-date
    return r


def run_rules(got, members):
    """Applies the rules to the trading days in `got` (oldest first); the last day is the signal close."""
    dates = [g[0] for g in got]
    n = len(dates)

    # per-stock daily returns (corporate-action safe), closes, turnover
    series = {}
    for i, (d, day) in enumerate(got):
        for isin, (o, c, pc, tv, sym) in day.items():
            s = series.setdefault(isin, {"ret": [None] * n, "close": [None] * n, "tv": [None] * n, "sym": sym})
            r = day_return(o, c, pc) if pc > 0 else None
            s["ret"][i], s["close"][i], s["tv"][i], s["sym"] = r, c, tv, sym

    def window_growth(rets, k):
        g = 1.0
        for r in rets[-k:]:
            g *= 1 + max(-0.6, min(1.5, r or 0.0))
        return g - 1

    stats = {}
    for isin, s in series.items():
        valid = sum(r is not None for r in s["ret"][-LOOKBACK_DAYS:])
        last_close = next((c for c in reversed(s["close"]) if c), None)
        if valid < LOOKBACK_DAYS * 0.9 or s["close"][n - 1 - LOOKBACK_DAYS] is None or not last_close:
            continue
        if last_close < MIN_PRICE:
            continue
        tv = [v for v in s["tv"][-TURNOVER_DAYS:] if v]
        # price path rebuilt from returns (so splits don't distort the 6-month high)
        path, g = [], 1.0
        for r in s["ret"][-LOOKBACK_DAYS:]:
            g *= 1 + max(-0.6, min(1.5, r or 0.0)); path.append(g)
        daily = [r for r in s["ret"][-LOOKBACK_DAYS:] if r is not None]
        stats[isin] = {
            "isin": isin, "symbol": members[isin]["symbol"], "name": members[isin]["name"], "sector": members[isin]["sector"],
            "r6": window_growth(s["ret"], LOOKBACK_DAYS), "r3": window_growth(s["ret"], 63),
            "r1": window_growth(s["ret"], 21), "off_high": path[-1] / max(path) - 1,
            "vol": statistics.pstdev(daily) * (252 ** 0.5) if len(daily) > 20 else None,
            "turnover_cr": statistics.median(tv) / 1e7 if tv else 0.0, "close": last_close,
        }

    uni = sorted(stats.values(), key=lambda x: -x["turnover_cr"])[:UNIVERSE]
    by_sec = {}
    for x in uni:
        by_sec.setdefault(x["sector"], []).append(x)
    ranking = []
    for sec, xs in by_sec.items():
        if len(xs) < MIN_MEMBERS:
            continue
        r6 = statistics.mean(x["r6"] for x in xs)
        top = max(xs, key=lambda x: x["r6"])
        without_top = statistics.mean([x["r6"] for x in xs if x is not top]) if len(xs) > 1 else r6
        ranking.append({"sector": sec, "r6": r6, "r3": statistics.mean(x["r3"] for x in xs), "n": len(xs),
                        "breadth": sum(x["r6"] > 0 for x in xs) / len(xs),
                        "median_r6": statistics.median(x["r6"] for x in xs),
                        "top_stock": top["symbol"], "without_top_r6": without_top})
    ranking.sort(key=lambda x: -x["r6"])
    for i, r in enumerate(ranking, 1):
        r["rank"] = i
    rank_wo = {r["sector"]: i for i, r in enumerate(sorted(ranking, key=lambda x: -x["without_top_r6"]), 1)}

    picks, runners = [], []
    for sr in ranking[:N_SECTORS]:
        xs = sorted(by_sec[sr["sector"]], key=lambda x: -(x["r6"] - sr["r6"]))
        for j, x in enumerate(xs[:PER_SECTOR + 2]):
            item = dict(x, rs=x["r6"] - sr["r6"], rank_in_sector=j + 1, sector_size=len(xs), sector_rank=sr["rank"],
                        sector_r6=sr["r6"])
            if j < PER_SECTOR:
                item["reasons"], item["flags"] = explain(item, sr, len(ranking), rank_wo[sr["sector"]])
                picks.append(item)
            else:
                item["gap_to_pick"] = item["rs"] - picks[-1]["rs"]     # points behind the sector's last pick
                runners.append(item)

    # closest calls: the sectors just below the cut, with the stocks they would have contributed
    cut = ranking[N_SECTORS - 1]["r6"] if len(ranking) >= N_SECTORS else 0.0
    next_sectors = []
    for sr in ranking[N_SECTORS:N_SECTORS + 2]:
        xs = sorted(by_sec[sr["sector"]], key=lambda x: -(x["r6"] - sr["r6"]))[:PER_SECTOR]
        next_sectors.append({"sector": sr["sector"], "rank": sr["rank"], "r6": sr["r6"], "n": sr["n"],
                             "gap": sr["r6"] - cut,
                             "stocks": [{"symbol": x["symbol"], "r6": x["r6"], "rs": x["r6"] - sr["r6"]} for x in xs]})

    sec_notes = []
    for sr in ranking[:N_SECTORS]:
        note = (f"{sr['sector']} ranks #{sr['rank']} of {len(ranking)} sectors: its {sr['n']} stocks averaged "
                f"{pct(sr['r6'])} over 6 months (median {pct(sr['median_r6'])}), {sr['breadth']:.0%} of them are up, "
                f"and the last 3 months averaged {pct(sr['r3'])}.")
        if rank_wo[sr["sector"]] > N_SECTORS:
            note += (f" Caution: without its best stock ({sr['top_stock']}) the sector average would be "
                     f"{pct(sr['without_top_r6'])} and it would rank #{rank_wo[sr['sector']]}, so one stock is "
                     "carrying this sector.")
        sec_notes.append(note)

    # every screened stock, for the sector-by-sector table (unranked small sectors included)
    rank_of = {r["sector"]: r["rank"] for r in ranking}
    picked = {p["symbol"] for p in picks}
    screened = []
    for sec, xs in by_sec.items():
        avg = statistics.mean(x["r6"] for x in xs)
        for x in sorted(xs, key=lambda x: -x["r6"]):
            screened.append({k: x[k] for k in ("symbol", "name", "sector", "r6", "r3", "r1", "off_high", "vol",
                                               "turnover_cr", "close")}
                            | {"rs": x["r6"] - avg, "sector_rank": rank_of.get(sec), "picked": x["symbol"] in picked})

    return {"data_through": dates[-1].isoformat(), "trading_days": n, "universe_size": len(uni),
            "sectors_ranked": len(ranking), "sector_ranking": ranking, "sector_notes": sec_notes,
            "picks": picks, "runners_up": runners, "next_sectors": next_sectors, "screened": screened}


def explain(x, sr, n_sectors, rank_without_top):
    reasons = [
        f"Sector: {x['sector']} is #{x['sector_rank']} of {n_sectors} sectors ({pct(x['sector_r6'])} average 6-month return).",
        f"Relative strength: up {pct(x['r6'])} in 6 months against {pct(x['sector_r6'])} for its sector, "
        f"beating it by {x['rs'] * 100:.0f} points; #{x['rank_in_sector']} of {x['sector_size']} in the sector.",
        f"Trend: {pct(x['r3'])} over 3 months and {pct(x['r1'])} over 1 month; "
        + ("at its 6-month high." if x["off_high"] > -0.02 else f"{abs(x['off_high']) * 100:.0f}% below its 6-month high."),
        f"Liquidity: median daily turnover ₹{x['turnover_cr']:,.0f} cr; volatility {x['vol'] * 100:.0f}% a year."
        if x["vol"] else f"Liquidity: median daily turnover ₹{x['turnover_cr']:,.0f} cr.",
    ]
    flags = []
    if x["r6"] > 1.5:
        flags.append(f"Very extended: up {x['r6'] * 100:.0f}% in 6 months. Sharp reversals are common after runs like this.")
    if x["off_high"] < -0.15:
        flags.append(f"Momentum fading: {abs(x['off_high']) * 100:.0f}% below its 6-month high.")
    if x["r1"] < -0.08:
        flags.append(f"Weak last month ({pct(x['r1'])}).")
    if x["vol"] and x["vol"] > 0.6:
        flags.append(f"High volatility ({x['vol'] * 100:.0f}% a year): expect big swings.")
    if sr["n"] < 8:
        flags.append(f"Small sector: only {sr['n']} stocks, so its ranking is fragile.")
    if rank_without_top > N_SECTORS:
        flags.append(f"The sector's rank depends on one stock ({sr['top_stock']}).")
    return reasons, flags


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if not _CACHE["body"] or time.time() - _CACHE["t"] > 6 * 3600:
                _CACHE["body"], _CACHE["t"] = json.dumps(compute()).encode(), time.time()
            body, code = _CACHE["body"], 200
        except Exception as e:
            body, code = json.dumps({"error": str(e)}).encode(), 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if code == 200:   # let Vercel's CDN serve the same answer for 6 h, then refresh in the background
            self.send_header("Cache-Control", "public, s-maxage=21600, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":      # local check: python api/picks.py
    res = compute()
    for k in ("official", "preview"):
        r = res[k]
        print(k, "| closes through", r["data_through"], "|", r.get("rebalance_date", ""))
        print("  sectors:", [(s["sector"], round(s["r6"] * 100, 1), s["n"]) for s in r["sector_ranking"][:5]])
        print("  picks:  ", [p["symbol"] for p in r["picks"]])
    print("seconds", res["seconds"])
