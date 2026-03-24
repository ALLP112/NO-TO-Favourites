"""
Polymarket Scanner – NO on Favourites across ALL sports.

Covers: NBA, NCAAB, NHL, UFC, Football, Soccer/EPL/UCL, Esports,
Tennis, Cricket, Basketball, Baseball, Hockey, Rugby, Golf, F1,
Chess, Boxing, Pickleball — every category on Polymarket Sports.

Key behaviours:
  - Only pre-game markets (rejects anything already started / live)
  - Only markets resolving within 24 hours (soonest first)
  - $100k+ volume
  - Identifies the favourite, buys NO mechanically
  - Sorted by soonest resolution so the book turns over fast
"""

import json
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("edgebot.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# ── Every sport / league from Polymarket's sidebar ──────────────────
SPORTS_TAGS = [
    # Team leagues
    "sports", "nba", "ncaab", "ncaa", "nhl", "nfl", "mlb",
    "college-basketball", "college-football",
    "wnba", "mls",
    # Football / soccer
    "soccer", "football", "epl", "la-liga", "serie-a",
    "bundesliga", "ligue-1", "ucl", "champions-league",
    "copa-del-rey", "europa-league",
    # Combat / individual
    "ufc", "mma", "boxing", "tennis", "golf", "f1", "formula-1",
    "cricket", "rugby", "chess", "pickleball",
    # Esports
    "esports", "csgo", "dota", "league-of-legends", "valorant",
    # Baseball / hockey / basketball (generic)
    "baseball", "hockey", "basketball",
]

# Keywords to match in event titles / slugs (catches unlabelled markets)
SPORTS_KEYWORDS_TITLE = [
    "win", "beat", "defeat", "match", "game", "vs", "vs.",
    "championship", "series", "playoffs", "cup", "final",
    "nba", "nhl", "nfl", "mlb", "epl", "ufc", "ucl",
    "premier league", "la liga", "serie a", "champions league",
    "bundesliga", "ligue 1", "ncaab", "march madness",
    "grand prix", "grand slam", "open ", "masters",
    "cricket", "rugby", "boxing", "chess", "pickleball",
    "esports", "counter-strike", "dota", "league of legends",
]

SPORTS_KEYWORDS_SLUG = [
    "nba", "nhl", "nfl", "mlb", "epl", "ufc", "ucl",
    "premier-league", "la-liga", "serie-a", "champions-league",
    "bundesliga", "ncaab", "march-madness", "f1", "formula-1",
    "tennis", "golf", "cricket", "rugby", "boxing", "chess",
    "pickleball", "esports", "hockey", "baseball", "basketball",
    "soccer", "football", "mma",
]


class PolymarketScanner:
    def __init__(
        self,
        min_volume: float = 100_000,
        min_fav_price: float = 0.55,
        max_fav_price: float = 0.92,
        max_hours_until_resolve: float = 24.0,
    ):
        self.min_volume    = min_volume
        self.min_fav_price = min_fav_price
        self.max_fav_price = max_fav_price
        self.max_hours     = max_hours_until_resolve
        self._resolution_cache: dict[str, tuple[bool, str]] = {}

    # ── Public ──────────────────────────────────────────────────────────
    def find_no_opportunities(self) -> list[dict]:
        """
        Returns NO-on-favourite opportunities sorted by soonest
        resolution first. Only includes pre-game markets resolving
        within self.max_hours.
        """
        markets = self._fetch_sports_markets()
        opps = []

        now = datetime.now(timezone.utc)

        for m in markets:
            opp = self._evaluate_market(m, now)
            if opp:
                opps.append(opp)

        # Sort by soonest end time — we want fastest turnover
        opps.sort(key=lambda x: x["hours_until_resolve"])
        log.info(
            f"Found {len(opps)} pre-game NO targets from "
            f"{len(markets)} sports markets (within {self.max_hours}h)"
        )
        return opps

    def check_resolution(self, condition_id: str) -> tuple[bool, str]:
        """
        Check if a market has resolved.
        Returns (resolved, result) where result is
        'yes_wins' | 'no_wins' | 'void' | 'pending'.
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
            resolved = False
            result = "pending"

            if data.get("closed") or data.get("resolved"):
                resolved = True
                tokens = data.get("tokens", [])
                if tokens:
                    for tok in tokens:
                        price = float(tok.get("price", 0))
                        outcome = tok.get("outcome", "")
                        if price > 0.95:
                            result = "yes_wins" if outcome == "Yes" else "no_wins"
                            break
                    else:
                        result = "void"

            if not resolved:
                end_str = data.get("end_date_iso")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        )
                        if datetime.now(timezone.utc) > end_dt:
                            tokens = data.get("tokens", [])
                            for tok in tokens:
                                price = float(tok.get("price", 0))
                                outcome = tok.get("outcome", "")
                                if price > 0.90:
                                    resolved = True
                                    result = (
                                        "yes_wins"
                                        if outcome == "Yes"
                                        else "no_wins"
                                    )
                                    break
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
        seen_cids: set[str] = set()

        # ── Pass 1: Events endpoint (catches grouped markets) ───────
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     200,
                    "order":     "volume24hr",
                    "ascending": "false",
                },
                timeout=20,
            )
            resp.raise_for_status()
            events = resp.json()

            for event in events:
                if not self._is_sports_event(event):
                    continue

                event_start = (
                    event.get("startDate")
                    or event.get("start_date_iso")
                    or event.get("startDateIso")
                )
                event_end = (
                    event.get("endDate")
                    or event.get("end_date_iso")
                    or event.get("endDateIso")
                )

                for market in event.get("markets") or []:
                    cid = market.get("conditionId") or market.get("condition_id")
                    if not cid or cid in seen_cids:
                        continue

                    volume = float(market.get("volume", 0) or 0)
                    if volume < self.min_volume:
                        continue

                    if market.get("marketType") not in (None, "binary"):
                        continue

                    market["_event_title"] = event.get("title", "")
                    market["_event_start"] = (
                        market.get("startDate")
                        or market.get("start_date_iso")
                        or event_start
                    )
                    market["_event_end"] = (
                        market.get("endDate")
                        or market.get("end_date_iso")
                        or event_end
                    )
                    market["_event_category"] = (
                        event.get("category") or ""
                    )

                    all_markets.append(market)
                    seen_cids.add(cid)

        except Exception as e:
            log.error(f"Events fetch failed: {e}")

        # ── Pass 2: Direct markets endpoint (fallback / extra) ──────
        try:
            resp2 = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     200,
                    "order":     "volume24hr",
                    "ascending": "false",
                    "tag":       "sports",
                },
                timeout=20,
            )
            if resp2.status_code == 200:
                for m in resp2.json():
                    cid = m.get("conditionId") or m.get("condition_id")
                    vol = float(m.get("volume", 0) or 0)
                    if cid and cid not in seen_cids and vol >= self.min_volume:
                        all_markets.append(m)
                        seen_cids.add(cid)
        except Exception:
            pass

        log.info(
            f"Fetched {len(all_markets)} sports markets "
            f"with ${self.min_volume:,.0f}+ volume"
        )
        return all_markets

    def _is_sports_event(self, event: dict) -> bool:
        """Check whether an event is sports-related."""
        tags = [t.lower() for t in (event.get("tags") or [])]
        title = (event.get("title") or "").lower()
        slug  = (event.get("slug") or "").lower()
        category = (event.get("category") or "").lower()

        if category in ("sports", "sport"):
            return True

        if any(t in SPORTS_TAGS for t in tags):
            return True

        if any(kw in title for kw in SPORTS_KEYWORDS_TITLE):
            return True

        if any(kw in slug for kw in SPORTS_KEYWORDS_SLUG):
            return True

        return False

    def _parse_dt(self, raw) -> datetime | None:
        """Try to parse a datetime string from Polymarket."""
        if not raw or not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _evaluate_market(self, market: dict, now: datetime) -> dict | None:
        """
        Check if market qualifies:
          1. Has a favourite in the 55¢–92¢ range
          2. Resolves within max_hours
          3. Has NOT started yet (pre-game only)
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

            # ── Parse end date — must resolve within max_hours ──────
            end_raw = (
                market.get("_event_end")
                or market.get("endDate")
                or market.get("end_date_iso")
            )
            end_dt = self._parse_dt(end_raw)
            if not end_dt:
                return None  # No end date → can't verify timing

            hours_left = (end_dt - now).total_seconds() / 3600.0
            if hours_left <= 0:
                return None  # Already past
            if hours_left > self.max_hours:
                return None  # Too far out

            # ── Pre-game check: reject if event has started ─────────
            start_raw = (
                market.get("_event_start")
                or market.get("startDate")
                or market.get("start_date_iso")
            )
            start_dt = self._parse_dt(start_raw)

            if start_dt and start_dt <= now:
                return None  # Event has started — this is live, skip

            # If no explicit start time, use a heuristic:
            # If end is within 3 hours and no start field, it's likely
            # live or about to be. Only accept if end is 3h+ away.
            if not start_dt and hours_left < 3.0:
                return None

            # ── Prices / favourite detection ────────────────────────
            prices_raw = (
                market.get("outcomePrices")
                or market.get("outcome_prices")
            )
            outcomes_raw = market.get("outcomes")

            if not prices_raw:
                return None

            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)
            if isinstance(outcomes_raw, str):
                outcomes_raw = json.loads(outcomes_raw)
            if not outcomes_raw:
                outcomes_raw = ["Yes", "No"]

            prices = [float(p) for p in prices_raw]
            if len(prices) < 2 or len(outcomes_raw) < 2:
                return None

            fav_idx     = 0 if prices[0] >= prices[1] else 1
            fav_price   = prices[fav_idx]
            fav_outcome = outcomes_raw[fav_idx]

            if fav_price < self.min_fav_price or fav_price > self.max_fav_price:
                return None

            no_price = 1.0 - fav_price
            volume   = float(market.get("volume", 0) or 0)

            # Detect sport/league from category or title
            sport_label = self._detect_sport(market, question)

            return {
                "condition_id":         cid,
                "question":             question,
                "fav_outcome":          fav_outcome,
                "fav_price":            fav_price,
                "no_price":             no_price,
                "volume":               volume,
                "end_date":             end_raw[:16] if end_raw else "unknown",
                "start_date":           start_raw[:16] if start_raw else "unknown",
                "hours_until_resolve":  round(hours_left, 1),
                "market_type":          "binary",
                "sport":                sport_label,
                "driver": (
                    f"Favourite '{fav_outcome}' at {fav_price:.0%}. "
                    f"NO shares at {no_price:.0%}. "
                    f"Resolves in {hours_left:.1f}h. "
                    f"Volume ${volume:,.0f}."
                ),
            }

        except Exception as e:
            log.debug(f"Eval error: {e}")
            return None

    @staticmethod
    def _detect_sport(market: dict, question: str) -> str:
        """Best-effort detection of which sport this is."""
        q = question.lower()
        cat = (market.get("_event_category") or "").lower()

        mapping = [
            (["nba"],                            "NBA"),
            (["ncaab", "ncaa", "march madness"], "NCAAB"),
            (["nhl", "hockey"],                  "NHL"),
            (["nfl"],                            "NFL"),
            (["mlb", "baseball"],                "MLB"),
            (["ucl", "champions league"],         "UCL"),
            (["epl", "premier league"],           "EPL"),
            (["la liga"],                         "La Liga"),
            (["serie a"],                         "Serie A"),
            (["bundesliga"],                      "Bundesliga"),
            (["ligue 1"],                         "Ligue 1"),
            (["ufc", "mma"],                      "UFC"),
            (["boxing"],                          "Boxing"),
            (["pickleball"],                      "Pickleball"),
            (["tennis", "grand slam", "wimbledon", "roland garros", "us open tennis", "australian open tennis"], "Tennis"),
            (["cricket"],                         "Cricket"),
            (["rugby"],                           "Rugby"),
            (["golf", "masters", "pga"],          "Golf"),
            (["f1", "formula", "grand prix"],     "F1"),
            (["chess"],                           "Chess"),
            (["esport", "counter-strike", "dota", "valorant", "league of legends"], "Esports"),
            (["soccer", "football"],              "Soccer"),
        ]

        combined = f"{q} {cat}"
        for keywords, label in mapping:
            for kw in keywords:
                if kw in combined:
                    return label

        return "Sports"
