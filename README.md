# NO-TO-Favourites

A Polymarket paper-trading bot that mechanically bets **NO** against the favourite in sports markets with **$100k+ volume**.

## Strategy

Pure mechanical: find a sports market on Polymarket, identify the favourite, buy NO shares against them. No edge modelling, no bias estimation — just fade every qualifying favourite.

This is structurally different from betting on the underdog:

- **Betting YES on underdog**: You're buying YES shares on a separate market outcome, profiting only if that specific underdog wins.
- **Betting NO on favourite**: You're buying NO shares on the favourite's own market, profiting if the favourite loses — regardless of *who* beats them.

## Configuration

All settings are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `BANKROLL` | 10000 | Total simulated bankroll ($) |
| `STAKE_PER_POS` | 750 | Flat stake per position ($) |
| `MIN_VOLUME` | 100000 | Minimum market volume ($) |
| `SCAN_INTERVAL` | 120 | Seconds between scans |
| `MIN_FAV_PRICE` | 0.55 | Minimum favourite price (must be a real favourite) |
| `MAX_FAV_PRICE` | 0.92 | Maximum favourite price (avoid near-certainties) |

## Local Development

```bash
pip install -r requirements.txt
python app.py
```

Dashboard at `http://localhost:5000`

## Deploy to Render

1. Push this repo to GitHub
2. Go to [Render](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render will auto-detect `render.yaml` and configure everything
5. Deploy

The `render.yaml` pre-configures all environment variables. Adjust them in Render's dashboard after deploy.

## Architecture

```
app.py          → Flask web server, state management, dashboard routes
scanner.py      → Polymarket API integration, market scanning
templates/
  dashboard.html → Real-time terminal-style dashboard
render.yaml     → Render deployment config
Procfile        → Gunicorn process config
```

## How It Works

1. **Scan**: Fetches active sports events from Polymarket's Gamma API
2. **Filter**: Keeps only markets with $100k+ volume and binary YES/NO structure
3. **Identify**: Finds the favourite (highest-priced outcome, 55¢–92¢ range)
4. **Execute**: Paper-buys NO shares at $750 per position
5. **Monitor**: Checks for market resolution and tracks P&L

## Paper Trading

All trades are simulated. No real money is used. No Polymarket API keys are needed — the bot uses public read-only endpoints for market data. To go live, you would need to integrate with the Polymarket CLOB API for order execution (requires API keys and USDC on Polygon).

## Important Notes

- This is a **research/paper-trading tool**, not financial advice
- Past performance does not guarantee future results
- Sports markets on Polymarket may have different characteristics than traditional bookmaker markets
- State is stored in memory and resets on deploy/restart
