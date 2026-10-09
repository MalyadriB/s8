# S8 · Straddle

A research and paper-trading lab for NIFTY 50 options strategies, built around short ATM straddles: same-day (S1), iron flies and expiry wings (S2–S4), and the overnight family (S5 held to the next 09:30, S8 = S5 without Friday entries, S6/S7 held two/three nights, S10 = S8 without December).

- **Backtests** on 1-minute option candles, with the full brokerage/STT/exchange/GST cost model.
- **Paper trading** on the live Upstox market feed. It is simulated only: no real order is ever placed. Fills are the real bid (sells) and ask (buy-backs).
- **Today**: a one-screen live dashboard centred on S5. It shows the session timeline, live mark-to-market, the trade closed this morning and NIFTY.
- **Calendar**: every day and month, backtest and paper, for any strategy.

## Layout

```
backend/    FastAPI service: Upstox auth and market stream, SQLite storage, strategy lab, paper engine (Python)
frontend/   React + TypeScript + Vite UI (lightweight-charts)
research/   offline studies: risk rules, stops, walk-forward, NSE end-of-day model (scripts, reports, result tables)
```

`backend/BACKEND_GUIDE.md` documents the backend in detail.

## Running it

Requirements: Python 3.12+, Node 20+, and an Upstox API app (client id/secret) for market data.

```bash
# backend - copy the settings template and fill in your own Upstox app details
cd backend
cp .env.example .env
python -m venv .venv && .venv/Scripts/activate   # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000

# frontend (a second terminal)
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Tests: `cd backend && MARKET_HOLIDAYS_FETCH=false python -m unittest discover -p "test_*.py"`

## Not in this repository

- **Secrets.** `backend/.env` is git-ignored. `backend/.env.example` lists the settings with placeholders only.
- **Market data.** The SQLite database (`backend/data/`) and the research caches stay local. The database is rebuilt from the Upstox feed and its historical-candle downloads.

## Disclaimer

This is research software for studying strategies. It is not investment advice, and the paper engine never places real orders.
