# Sector Rotation Picks — serverless web app

Opening the page runs the quarterly Sector Rotation rules (see `../STRATEGY.md`) on NSE data and shows **this
quarter's portfolio**: the 6 picks set from the closes before the quarter's first trading day, the reasoning for each,
warning flags, the sector ranking and the next stocks in line. The portfolio stays the same until the next rebalance.
A sector-by-sector table lists every screened stock (1m/3m/6m returns, distance from the 6-month high, turnover,
volatility), at the rebalance or today. Below it, a clearly marked preview shows what the rules would pick on today's closes (for information, not trading).

```
index.html        the page (static)
api/picks.py      serverless function: downloads NSE data, applies the rules, returns JSON (standard library only)
api/data/         fallback NIFTY 500 list, used if niftyindices.com can't be reached
vercel.json       Mumbai region (bom1), 60 s time limit
dev_server.py     local preview
```

## Run locally
```bash
python webapp/dev_server.py
```
Then open http://localhost:8787. The first load takes about 5–10 seconds.

## Deploy to Vercel (free Hobby plan is enough)
1. Install the CLI: `npm i -g vercel`
2. Run `cd webapp`, then `vercel` (log in, accept the defaults), then `vercel --prod`.
3. Open the URL it prints.

Or push the `webapp` folder to GitHub and import the repo at vercel.com/new (root directory: `webapp`).

## How it works
- On each request the function downloads the NIFTY 500 list (with sectors) from niftyindices.com and about 135–200 daily
  NSE bhavcopy zips (from ~200 days before the quarter start to today, 16 at a time). It matches stocks by ISIN, so renamed symbols keep their history.
- Split and bonus days: NSE's previous close is not adjusted, so an opening gap beyond −25% / +35% is treated as a
  corporate action and only that day's open-to-close move is counted. Holiday files that repeat an earlier day are dropped.
- Only EQ-series stocks are used, as in the backtest; trade-for-trade (BE/BZ) stocks are left out.
- Results are cached by Vercel's CDN for 6 hours (then refreshed in the background), so NSE is hit at most a few
  times a day.

## Limits
- **NSE may block or rate-limit cloud servers.** If that happens the page shows an error; try again later. The Mumbai
  region usually works best.
- Uses today's NIFTY 500 list. Changes NSE has announced but not yet made are not applied (the local
  `sector_rotation_signal.py` does apply them). This only matters in the few days around the March and September reviews.
- Sectors with fewer than 8 stocks, or ranked high because of one stock, are flagged on the page. The rules still pick
  them, as in the backtest.
- Not investment advice.

---

# Momentum Top 20 (`/momentum.html`, `api/momentum.py`)

The live version of the rule picked in `../smallcase_momentum/` (tune.py and sizing.py):
- **Universe:** NIFTY 500, EQ series only, price ≥ ₹20, traded on 230 of the last 252 days, 60-day median turnover ≥ ₹1 cr.
- **Score:** 12-month return skipping the last month, i.e. growth from 273 to 21 trading days before the signal.
- **Portfolio:** buy the top 20 in equal amounts, and keep a holding while it ranks 40 or better.
- **Calendar:** signal on the last close of each month, trade on the next trading day.
- **Watch list:** the latest close is also ranked, for information only.
- **Sizing:** the page splits capital between the strategy and a liquid fund (default 60/40), with share counts at the latest close.

**Holdings.** The function has no database:
- Type your holdings on the page; they are kept in your browser's localStorage and sent as `?hold=SYM1,SYM2`.
- After trading, press *Save the new portfolio as my holdings*, so next month's check starts from them.
- With no holdings, it returns the plain top 20 (a fresh start).

**P&L since the rebalance.** The page draws an equity curve from the trade-day close to the latest close:
- This month's portfolio (equal amounts, left to drift), against an equal-weight NIFTY 500 and NIFTYBEES.
- Pick any date with the slider (or click the chart) to see the portfolio's return, P&L per stock, and P&L in ₹ for your capital and split. The liquid share is assumed to earn 6% a year.
- The curve is before costs and tax.

**Downloads (CSV):** the full ranking (all ~470 eligible stocks, month-end or latest), this month's trades, and the daily P&L since the rebalance (portfolio, benchmarks, and each stock).

**Data.** Each cache miss downloads about 310 daily bhavcopies (about 15 months), which takes about 16 s locally. The function is allowed 60 s. Responses are cached for 6 h per holdings list.

**Rows are matched on ISIN *or* symbol.** A face-value split gives a stock a new ISIN, and a rename gives it a new symbol, so either one is enough to keep its history (MCX's split, for example).

**Limits:**
- It uses today's NIFTY 500 list (no point-in-time membership).
- Stocks moved to trade-for-trade (BE) are not eligible.
- Not investment advice.

Test locally: `python webapp/api/momentum.py "MCX,NETWEB"`, or run `dev_server.py` and open http://localhost:8787/momentum.html.
