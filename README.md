# EdgeBot — NO on Favourites

A Polymarket paper-trading bot that exploits the **favourite-longshot bias** in sports markets.

## Strategy

The bot buys **NO shares** on the favourite outcome in sports markets with **$100k+ volume**. This is structurally different from betting on the underdog:

- **Betting YES on underdog**: You're buying YES shares on a separate market outcome, paying (1 - fav_price) and profiting if the underdog wins.
- **Betting NO on favourite**: You're buying NO shares on the *favourite's own market*, paying (1 - fav_price) and profiting if the favourite loses — regardless of *who* beats them.

The favourite-longshot bias is well-documented: favourites on prediction markets are systematically overpriced by 3-8 percentage points. Buying NO captures this edge.

## Configuration

All settings are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `BANKROLL` | 10000 | Total simulated bankroll ($) |
| `STAKE_PER_POS` | 750 | Flat stake per position ($) |
| `MIN_VOLUME` | 100000 | Minimum market volume ($) |
| `SCAN_INTERVAL` | 120 | Seconds between scans |
| `MIN_EDGE_PP` | 3.0 | Minimum edge in percentage points |
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
scanner.py      → Polymarket API integration, market scanning, edge calculation
templates/
  dashboard.html → Real-time terminal-style dashboard
render.yaml     → Render deployment config
Procfile        → Gunicorn process config
```

## How It Works

1. **Scan**: Fetches active sports events from Polymarket's Gamma API
2. **Filter**: Keeps only markets with $100k+ volume and binary YES/NO structure
3. **Identify**: Finds the favourite (highest-priced outcome, 55¢–92¢ range)
4. **Calculate**: Estimates the favourite-longshot bias based on price level
5. **Execute**: Paper-buys NO shares at $750 per position if edge ≥ 3pp
6. **Monitor**: Checks for market resolution and tracks P&L

## Paper Trading

All trades are simulated. No real money is used. No Polymarket API keys are needed — the bot uses public read-only endpoints for market data. To go live, you would need to integrate with the Polymarket CLOB API for order execution (requires API keys and USDC on Polygon).

## Important Notes

- This is a **research/paper-trading tool**, not financial advice
- The favourite-longshot bias estimates are conservative calibrations from academic literature
- Past performance of any bias does not guarantee future results
- Sports markets on Polymarket may have different characteristics than traditional bookmaker markets
- State is stored in memory and resets on deploy/restart
