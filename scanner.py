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
        max_hours_until_resolve: float = 168.0,
        min_hours_until_resolve: float = 3.0,
    ):
        self.min_volume    = min_volume
        self.min_fav_price = min_fav_price
        self.max_fav_price = max_fav_price
        self.max_hours     = max_hours_until_resolve
        self.min_hours     = min_hours_until_resolve
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

        # Diagnostic counters
        no_dates = 0
        already_past = 0
        too_soon = 0
        too_far = 0
        not_sports = 0
        not_fixture = 0
        derivative = 0
        no_fav = 0
        accepted = 0

        for m in markets:
            opp = self._evaluate_market_debug(m, now)
            if isinstance(opp, dict):
                opps.append(opp)
                accepted += 1
            elif opp in ("no_end", "no_dates"):
                no_dates += 1
            elif opp == "already_past":
                already_past += 1
            elif opp == "too_soon":
                too_soon += 1
            elif opp == "too_far":
                too_far += 1
            elif opp == "not_sports":
                not_sports += 1
            elif opp == "not_fixture":
                not_fixture += 1
            elif opp == "derivative":
                derivative += 1
            else:
                no_fav += 1

        # Sort by soonest resolution — fastest turnover
        opps.sort(key=lambda x: x["hours_until_resolve"])
        log.info(
            f"Found {len(opps)} fixture NO targets from "
            f"{len(markets)} markets ({self.min_hours}h-{self.max_hours}h) | "
            f"Rejected: not_sports={not_sports} no_vs={not_fixture} "
            f"derivative={derivative} no_dates={no_dates} past={already_past} "
            f"too_soon={too_soon} too_far={too_far} no_fav={no_fav}"
        )
        return opps

    def check_resolution(self, condition_id: str) -> tuple[bool, str]:
        """
        Check if a market has DEFINITIVELY resolved.
        Only triggers when prices are at 99¢+ on one side,
        meaning Polymarket has actually settled the market.
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

            # Only resolve if the market is explicitly marked resolved
            # AND one outcome's price is at 0.99+ (definitive settlement)
            if not data.get("resolved"):
                return (False, "pending")

            tokens = data.get("tokens", [])
            if not tokens:
                return (False, "pending")

            for tok in tokens:
                price = float(tok.get("price", 0))
                outcome = tok.get("outcome", "")
                if price >= 0.99:
                    result = "yes_wins" if outcome == "Yes" else "no_wins"
                    self._resolution_cache[condition_id] = (True, result)
                    return (True, result)

            # Resolved but no clear winner (void / ambiguous)
            # Check if all prices are near zero (void scenario)
            all_low = all(float(t.get("price", 0)) < 0.10 for t in tokens)
            if all_low:
                self._resolution_cache[condition_id] = (True, "void")
                return (True, "void")

            # Market says resolved but prices haven't settled to 0/1 yet
            # Wait for definitive settlement
            return (False, "pending")

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
                        self._safe_str(event.get("category"))
                    )

                    all_markets.append(market)
                    seen_cids.add(cid)

        except Exception as e:
            log.error(f"Events fetch failed: {e}")

        events_count = len(all_markets)

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
                        if "_event_end" not in m:
                            m["_event_end"] = (
                                m.get("endDate")
                                or m.get("end_date_iso")
                                or m.get("endDateIso")
                                or m.get("expirationDate")
                                or m.get("game_start_time")
                            )
                        if "_event_start" not in m:
                            m["_event_start"] = (
                                m.get("startDate")
                                or m.get("start_date_iso")
                                or m.get("startDateIso")
                                or m.get("gameStartTime")
                                or m.get("game_start_time")
                            )
                        if "_event_title" not in m:
                            m["_event_title"] = m.get("question", "")
                        all_markets.append(m)
                        seen_cids.add(cid)
        except Exception as e:
            log.error(f"Markets fetch failed: {e}")

        fallback_count = len(all_markets) - events_count
        log.info(
            f"Fetched {len(all_markets)} sports markets "
            f"({events_count} from events, {fallback_count} from fallback) "
            f"with ${self.min_volume:,.0f}+ volume"
        )
        return all_markets

    def _is_sports_event(self, event: dict) -> bool:
        """Check whether an event is sports-related."""
        # Tags can be strings OR dicts like {"label": "sports"}
        raw_tags = event.get("tags") or []
        tags = []
        for t in raw_tags:
            if isinstance(t, str):
                tags.append(t.lower())
            elif isinstance(t, dict):
                label = t.get("label") or t.get("slug") or t.get("name") or ""
                if isinstance(label, str):
                    tags.append(label.lower())

        # All of these can be strings, dicts, or None from the API
        title = self._safe_str(event.get("title"))
        slug  = self._safe_str(event.get("slug"))
        category = self._safe_str(event.get("category"))

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

    @staticmethod
    def _safe_str(val) -> str:
        """Safely convert an API field to lowercase string.
        Polymarket sometimes returns dicts where you'd expect strings."""
        if isinstance(val, str):
            return val.lower()
        if isinstance(val, dict):
            # Try common dict shapes: {"label": "...", "slug": "..."}
            for key in ("label", "slug", "name", "value"):
                v = val.get(key)
                if isinstance(v, str):
                    return v.lower()
        return ""

    def _evaluate_market(self, market: dict, now: datetime) -> dict | None:
        """Wrapper for compatibility."""
        result = self._evaluate_market_debug(market, now)
        return result if isinstance(result, dict) else None

    def _evaluate_market_debug(self, market: dict, now: datetime):
        """
        Check if market qualifies. Returns opportunity dict on success,
        or a rejection reason string for diagnostics.

        TIMING LOGIC:
          Polymarket's startDate = when the market was CREATED (always in the past)
          Polymarket's endDate   = resolution deadline (hours to days after the game)

          There is NO reliable "game kick-off time" field in the API.
          So we use endDate as a proxy — if a market resolves within
          max_hours, the underlying fixture is likely happening soon.
          We sort by soonest endDate to get the fastest turnover.
        """
        try:
            cid = market.get("conditionId") or market.get("condition_id")
            if not cid:
                return "no_cid"

            question = (
                market.get("question")
                or market.get("_event_title")
                or "Unknown market"
            )

            q_lower = question.lower()

            # ── Reject non-sports markets (finance, politics, geo, etc) ──
            non_sports = [
                # Finance / crypto
                "s&p", "spx", "spy", "nasdaq", "dow jones", "bitcoin",
                "btc", "eth", "ethereum", "crypto", "stock", "index",
                "price above", "price below", "fed ", "interest rate",
                "crude oil", "gold price", "silver price", "oil price",
                "treasury", "inflation", "gdp", "recession",
                # Politics / geopolitics
                "election", "president", "congress", "senate",
                "iran", "israel", "ukraine", "russia", "china",
                "war ", "conflict", "invasion", "forces enter",
                "ceasefire", "peace deal", "sanctions",
                "trump", "biden", "tariff",
                "democrat", "republican", "parliament",
                # Entertainment / media
                "temperature", "weather", "box office", "imdb",
                "twitter", "follower", "subscriber", "viewership",
                "emmy", "oscar", "grammy", "golden globe",
                "tiktok", "youtube", "netflix", "spotify",
                # Generic non-fixture patterns
                "by end of", "by march", "by april", "by may",
                "by june", "by july", "by august", "by september",
                "by october", "by november", "by december", "by january",
                "by february", "before ", "by the end",
                "hit $", "reach $", "above $", "below $",
                "will the ", "will there", "will any",
                "how many", "more than", "less than", "at least",
            ]
            if any(kw in q_lower for kw in non_sports):
                return "not_sports"

            # ── Must be a fixture: require "vs" in the question ─────
            # Real match-winners: "Kings vs. Hornets", "Team A vs Team B"
            # Non-fixtures: "Will X happen?", "Iran conflict ends?"
            has_vs = " vs " in q_lower or " vs. " in q_lower
            if not has_vs:
                return "not_fixture"

            # ── Only match-winner markets (reject spreads, O/U, totals) ─
            derivative_markers = [
                "spread", "o/u ", "over/under", "over or under",
                "total points", "total goals", "total runs",
                "total score", "total combined",
                "handicap", "margin", "first to score",
                "first half", "second half", "1st half", "2nd half",
                "first quarter", "first period", "first set",
                "most ", "highest ", "lowest ", "exact score",
                "both teams to score", "btts",
                "anytime ", "player prop", "mvp",
                "assists", "rebounds", "strikeouts",
                "passing yards", "rushing yards", "touchdowns",
                "hits, runs", "home runs",
                "aces", "double faults",
                "corners", "cards", "bookings",
                "top ", "finish position",
                "game 1 winner", "game 2 winner", "game 3 winner",
                "map 1", "map 2", "map 3",
            ]
            if any(kw in q_lower for kw in derivative_markers):
                return "derivative"

            # ── Find market end / resolution date ───────────────────
            end_raw = (
                market.get("_event_end")
                or market.get("endDate")
                or market.get("end_date_iso")
                or market.get("endDateIso")
                or market.get("expirationDate")
            )
            end_dt = self._parse_dt(end_raw)
            if not end_dt:
                return "no_dates"

            hours_to_end = (end_dt - now).total_seconds() / 3600.0

            if hours_to_end <= 0:
                return "already_past"  # already resolved

            if hours_to_end < self.min_hours:
                return "too_soon"  # likely live or in-play

            if hours_to_end > self.max_hours:
                return "too_far"  # resolves too far out

            hours_until_resolve = hours_to_end

            # ── Prices / favourite detection ────────────────────────
            prices_raw = (
                market.get("outcomePrices")
                or market.get("outcome_prices")
            )
            outcomes_raw = market.get("outcomes")

            if not prices_raw:
                return "no_fav"

            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)
            if isinstance(outcomes_raw, str):
                outcomes_raw = json.loads(outcomes_raw)
            if not outcomes_raw:
                outcomes_raw = ["Yes", "No"]

            prices = [float(p) for p in prices_raw]
            if len(prices) < 2 or len(outcomes_raw) < 2:
                return "no_fav"

            fav_idx     = 0 if prices[0] >= prices[1] else 1
            fav_price   = prices[fav_idx]
            fav_outcome = outcomes_raw[fav_idx]

            if fav_price < self.min_fav_price or fav_price > self.max_fav_price:
                return "no_fav"

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
                "hours_until_resolve":  round(hours_until_resolve, 1),
                "market_type":          "binary",
                "sport":                sport_label,
                "driver": (
                    f"Favourite '{fav_outcome}' at {fav_price:.0%}. "
                    f"NO shares at {no_price:.0%}. "
                    f"Resolves in {hours_until_resolve:.1f}h. "
                    f"Volume ${volume:,.0f}."
                ),
            }

        except Exception as e:
            log.debug(f"Eval error: {e}")
            return "error"

    @staticmethod
    def _detect_sport(market: dict, question: str) -> str:
        """Best-effort detection of which sport this is."""
        q = question.lower()
        raw_cat = market.get("_event_category")
        cat = PolymarketScanner._safe_str(raw_cat) if raw_cat else ""

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
