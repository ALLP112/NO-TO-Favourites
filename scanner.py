"""
Polymarket Scanner – finds NO opportunities on sports favourites.

Uses the Polymarket Gamma API (public, no auth needed for reads)
to discover sports markets, then identifies the favourite outcome
and calculates the perceived edge on buying NO.
"""

import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("edgebot.scanner")

GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"

# Sports-related tags on Polymarket
SPORTS_TAGS = [
    "sports", "nba", "nhl", "nfl", "mlb", "soccer", "football",
    "epl", "la-liga", "serie-a", "bundesliga", "ligue-1", "mls",
    "tennis", "boxing", "mma", "ufc", "cricket", "f1", "formula-1",
    "ncaa", "college-basketball", "college-football", "wnba",
    "champions-league", "copa-del-rey", "rugby",
]


class PolymarketScanner:
    def __init__(
        self,
        min_volume: float = 100_000,
        min_fav_price: float = 0.55,
        max_fav_price: float = 0.92,
        min_edge_pp: float = 3.0,
    ):
        self.min_volume    = min_volume
        self.min_fav_price = min_fav_price
        self.max_fav_price = max_fav_price
        self.min_edge_pp   = min_edge_pp
        self._resolution_cache: dict[str, tuple[bool, str]] = {}

    # ── Public API ──────────────────────────────────────────────────────
    def find_no_opportunities(self) -> list[dict]:
        """
        Return a list of opportunity dicts, sorted best-edge-first.
        Each dict:
          condition_id, question, fav_outcome, fav_price,
          model_no_prob, edge_pp, volume, end_date, hold_hours, driver
        """
        markets = self._fetch_sports_markets()
        opps = []

        for m in markets:
            opp = self._evaluate_market(m)
            if opp:
                opps.append(opp)

        opps.sort(key=lambda x: x["edge_pp"], reverse=True)
        log.info(f"Found {len(opps)} NO opportunities from {len(markets)} sports markets")
        return opps

    def check_resolution(self, condition_id: str) -> tuple[bool, str]:
        """
        Check if a market has resolved.
        Returns (resolved: bool, result: 'yes_wins'|'no_wins'|'void'|'pending')
        """
        if condition_id in self._resolution_cache:
            return self._resolution_cache[condition_id]

        try:
            resp = requests.get(
                f"{CLOB_API}/markets/{condition_id}",
                timeout=15,
            )
            if resp.status_code != 200:
                return (False, "pending")

            data = resp.json()

            # Check multiple resolution indicators
            resolved = False
            result = "pending"

            # Check if market is closed/resolved
            if data.get("closed") or data.get("resolved"):
                resolved = True
                # Determine winner from final prices
                tokens = data.get("tokens", [])
                if tokens:
                    # Find the outcome with price ~1.0 (winner)
                    for tok in tokens:
                        price = float(tok.get("price", 0))
                        outcome = tok.get("outcome", "")
                        if price > 0.95:
                            result = "yes_wins" if outcome == "Yes" else "no_wins"
                            break
                    else:
                        # Prices near 0.5 might mean void
                        result = "void"

            # Check end_date_iso passed and prices have settled
            if not resolved:
                end_str = data.get("end_date_iso")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) > end_dt:
                            # Market should have resolved by now
                            tokens = data.get("tokens", [])
                            for tok in tokens:
                                price = float(tok.get("price", 0))
                                outcome = tok.get("outcome", "")
                                if price > 0.90:
                                    resolved = True
                                    result = "yes_wins" if outcome == "Yes" else "no_wins"
                                    break
                                elif price < 0.10:
                                    continue
                    except (ValueError, TypeError):
                        pass

            if resolved:
                self._resolution_cache[condition_id] = (True, result)

            return (resolved, result)

        except Exception as e:
            log.warning(f"Resolution check failed for {condition_id}: {e}")
            return (False, "pending")

    # ── Internal ────────────────────────────────────────────────────────
    def _fetch_sports_markets(self) -> list[dict]:
        """Fetch active sports markets from Polymarket Gamma API."""
        all_markets = []

        try:
            # Fetch events tagged as sports
            params = {
                "active":  "true",
                "closed":  "false",
                "limit":   100,
                "order":   "volume24hr",
                "ascending": "false",
            }

            resp = requests.get(
                f"{GAMMA_API}/events",
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            events = resp.json()

            for event in events:
                # Check if this is a sports event
                tags = [t.lower() for t in (event.get("tags") or [])]
                title = (event.get("title") or "").lower()
                slug  = (event.get("slug") or "").lower()
                category = (event.get("category") or "").lower()

                is_sports = (
                    category in ("sports", "sport")
                    or any(t in SPORTS_TAGS for t in tags)
                    or any(kw in title for kw in [
                        "win", "beat", "defeat", "match", "game", "vs",
                        "championship", "series", "playoffs", "cup",
                        "nba", "nhl", "nfl", "mlb", "epl", "ufc",
                        "premier league", "la liga", "serie a",
                        "champions league", "bundesliga",
                    ])
                    or any(kw in slug for kw in [
                        "nba", "nhl", "nfl", "mlb", "epl", "ufc",
                        "premier-league", "la-liga", "serie-a",
                        "champions-league", "bundesliga",
                    ])
                )

                if not is_sports:
                    continue

                # Process each market within the event
                for market in (event.get("markets") or []):
                    volume = float(market.get("volume", 0) or 0)
                    if volume < self.min_volume:
                        continue

                    # Only binary markets (YES/NO)
                    if market.get("marketType") not in (None, "binary"):
                        continue

                    market["_event_title"] = event.get("title", "")
                    market["_event_end"]   = event.get("endDate") or event.get("end_date_iso")
                    all_markets.append(market)

        except Exception as e:
            log.error(f"Failed to fetch markets: {e}")

        # Also try the direct markets endpoint as fallback
        try:
            resp2 = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     100,
                    "order":     "volume24hr",
                    "ascending": "false",
                    "tag":       "sports",
                },
                timeout=20,
            )
            if resp2.status_code == 200:
                markets2 = resp2.json()
                seen = {m.get("conditionId") or m.get("condition_id") for m in all_markets}
                for m in markets2:
                    cid = m.get("conditionId") or m.get("condition_id")
                    vol = float(m.get("volume", 0) or 0)
                    if cid not in seen and vol >= self.min_volume:
                        all_markets.append(m)
        except Exception:
            pass

        log.info(f"Fetched {len(all_markets)} sports markets with ${self.min_volume:,.0f}+ volume")
        return all_markets

    def _evaluate_market(self, market: dict) -> dict | None:
        """
        Evaluate a single market for a NO-on-favourite opportunity.
        Returns an opportunity dict or None.
        """
        try:
            cid = market.get("conditionId") or market.get("condition_id")
            if not cid:
                return None

            question = (
                market.get("question")
                or market.get("_event_title")
                or "Unknown market"
            )

            # Get current prices
            # outcomePrices is often a JSON string like "[\"0.65\",\"0.35\"]"
            prices_raw = market.get("outcomePrices") or market.get("outcome_prices")
            outcomes_raw = market.get("outcomes")

            if not prices_raw:
                return None

            if isinstance(prices_raw, str):
                import json
                prices_raw = json.loads(prices_raw)

            if isinstance(outcomes_raw, str):
                import json
                outcomes_raw = json.loads(outcomes_raw)

            if not outcomes_raw:
                outcomes_raw = ["Yes", "No"]

            prices = [float(p) for p in prices_raw]
            if len(prices) < 2 or len(outcomes_raw) < 2:
                return None

            # Identify the favourite (highest price = most likely)
            fav_idx = 0 if prices[0] >= prices[1] else 1
            fav_price   = prices[fav_idx]
            fav_outcome = outcomes_raw[fav_idx]

            # Filter: must be a real favourite but not a near-certainty
            if fav_price < self.min_fav_price or fav_price > self.max_fav_price:
                return None

            # ── Edge calculation ────────────────────────────────────────
            # The core thesis: favourites in sports on Polymarket are
            # systematically overpriced. The NO price is (1 - fav_price).
            # Our model probability of NO is based on historical
            # favourite-longshot bias — favourites at X% typically win
            # at a rate below X%.
            #
            # Conservative bias estimate: favourites are overpriced
            # by ~3-8pp depending on how heavy the favourite is.
            no_price = 1.0 - fav_price

            # Bias model: heavier favourites tend to be more overpriced
            # (the market loads onto them). Light favs ~3pp, heavy ~7pp.
            bias_pp = self._estimate_bias(fav_price)
            model_no_prob = no_price + (bias_pp / 100.0)
            model_no_prob = min(model_no_prob, 0.95)  # cap

            # Edge = model_prob - market_price (in percentage points)
            edge_pp = (model_no_prob - no_price) * 100.0

            if edge_pp < self.min_edge_pp:
                return None

            # Estimate hold time from end date
            hold_hours = 24.0
            end_date = market.get("_event_end") or market.get("endDate") or market.get("end_date_iso")
            if end_date:
                try:
                    if isinstance(end_date, str):
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        delta = end_dt - datetime.now(timezone.utc)
                        hold_hours = max(1.0, delta.total_seconds() / 3600)
                except (ValueError, TypeError):
                    pass

            volume = float(market.get("volume", 0) or 0)

            return {
                "condition_id":  cid,
                "question":      question,
                "fav_outcome":   fav_outcome,
                "fav_price":     fav_price,
                "no_price":      no_price,
                "model_no_prob": model_no_prob,
                "edge_pp":       edge_pp,
                "volume":        volume,
                "end_date":      (end_date or "unknown")[:16] if end_date else "unknown",
                "hold_hours":    round(hold_hours, 1),
                "market_type":   "binary",
                "driver":        (
                    f"Favourite '{fav_outcome}' priced at {fav_price:.0%}. "
                    f"Historical bias suggests ~{bias_pp:.1f}pp overpricing. "
                    f"NO shares at {no_price:.0%}, model fair value {model_no_prob:.0%}. "
                    f"Volume ${volume:,.0f}."
                ),
            }

        except Exception as e:
            log.debug(f"Eval error: {e}")
            return None

    @staticmethod
    def _estimate_bias(fav_price: float) -> float:
        """
        Estimate how many percentage points the favourite is overpriced,
        based on the favourite-longshot bias literature.

        Very rough calibration:
          55% fav → ~3pp overpriced
          65% fav → ~4.5pp
          75% fav → ~5.5pp
          85% fav → ~7pp
          90% fav → ~7.5pp

        This is conservative — some studies show larger biases on
        prediction markets vs. traditional books.
        """
        # Piecewise linear interpolation
        anchors = [
            (0.55, 3.0),
            (0.60, 3.5),
            (0.65, 4.5),
            (0.70, 5.0),
            (0.75, 5.5),
            (0.80, 6.0),
            (0.85, 7.0),
            (0.90, 7.5),
        ]

        if fav_price <= anchors[0][0]:
            return anchors[0][1]
        if fav_price >= anchors[-1][0]:
            return anchors[-1][1]

        for i in range(len(anchors) - 1):
            x0, y0 = anchors[i]
            x1, y1 = anchors[i + 1]
            if x0 <= fav_price <= x1:
                t = (fav_price - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)

        return 5.0  # fallback
