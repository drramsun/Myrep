# Sector Rotation Picks — serverless web app

Opening the page runs the quarterly Sector Rotation rules (see `../STRATEGY.md`) on live NSE data and shows the
6 picks, the reasoning for each, warning flags, the full sector ranking and the next stocks in line.

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
- On each request the function downloads the NIFTY 500 list (with sectors) from niftyindices.com and about 135 daily
  NSE bhavcopy zips (the last ~190 days, 16 at a time). It matches stocks by ISIN, so renamed symbols keep their history.
- Split and bonus days: NSE's previous close is not adjusted, so an opening gap beyond −25% / +35% is treated as a
  corporate action and only that day's open-to-close move is counted. Holiday files that repeat an earlier day are dropped.
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
