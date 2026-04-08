"""
EdgeBot – NO-TO-Favourites
Mechanically buys NO on the favourite in pre-game sports markets
on Polymarket. Targets full bankroll deployment across all slots.
Paper trading mode. Deploys on Render.
"""

import os
import time
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, redirect, url_for, jsonify

from scanner import PolymarketScanner

# ── Config ──────────────────────────────────────────────────────────────────
BANKROLL          = float(os.getenv("BANKROLL", 10_000))
STAKE_PER_POS     = float(os.getenv("STAKE_PER_POS", 400))
MAX_OPEN          = int(os.getenv("MAX_OPEN", 25))         # floor(10000/400)
MIN_VOLUME        = float(os.getenv("MIN_VOLUME", 30_000))
MAX_FAV_PRICE     = float(os.getenv("MAX_FAV_PRICE", 0.95))
MIN_FAV_PRICE     = float(os.getenv("MIN_FAV_PRICE", 0.05))
MAX_HOURS         = float(os.getenv("MAX_HOURS", 168))      # endDate = resolution deadline, not game time. 168h = 7 days
MIN_HOURS         = float(os.getenv("MIN_HOURS", 3))        # skip markets resolving too soon (likely live)
SCAN_INTERVAL     = int(os.getenv("SCAN_INTERVAL", 90))     # faster cycle to fill book

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("edgebot")

app = Flask(__name__)

# ── State ───────────────────────────────────────────────────────────────────
state = {
    "running":              False,
    "dry_run":              True,
    "scan_count":           0,
    "last_scan":            None,
    "last_trade":           None,
    "current_step":         "idle",
    "open_positions_count": 0,
    "error":                None,
    "pnl": {
        "total":  0.0,
        "wins":   0,
        "losses": 0,
    },
    "open_trades":          [],
    "closed_trades":        [],
    "allocated_bankroll":   0.0,
    "free_bankroll":        BANKROLL,
}

settings = {
    "total_bankroll_usd":      BANKROLL,
    "reference_stake_amount":  STAKE_PER_POS,
    "max_open_positions":      MAX_OPEN,
    "min_volume":              MIN_VOLUME,
    "scan_interval_sec":       SCAN_INTERVAL,
    "max_hours":               MAX_HOURS,
}

scanner = PolymarketScanner(
    min_volume=MIN_VOLUME,
    min_fav_price=MIN_FAV_PRICE,
    max_fav_price=MAX_FAV_PRICE,
    max_hours_until_resolve=MAX_HOURS,
    min_hours_until_resolve=MIN_HOURS,
)

lock = threading.Lock()
scan_thread = None


# ── Helpers ─────────────────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _can_open():
    return (
        len(state["open_trades"]) < MAX_OPEN
        and state["free_bankroll"] >= STAKE_PER_POS
    )


def _slots_free():
    """How many more positions can we open."""
    by_bankroll = int(state["free_bankroll"] // STAKE_PER_POS)
    by_slots    = MAX_OPEN - len(state["open_trades"])
    return min(by_bankroll, by_slots)


def _already_in(condition_id: str) -> bool:
    for t in state["open_trades"]:
        if t["condition_id"] == condition_id:
            return True
    for t in state["closed_trades"]:
        if t["condition_id"] == condition_id:
            return True
    return False


def _open_position(opp: dict):
    """Paper-open a NO position on the favourite."""
    no_price = opp["no_price"]
    shares   = STAKE_PER_POS / no_price
    profit_if_win = shares - STAKE_PER_POS   # each NO share pays $1

    trade = {
        "condition_id":        opp["condition_id"],
        "market_question":     opp["question"],
        "selection":           f"NO on {opp['fav_outcome']}",
        "market_domain":       opp.get("sport", "Sports"),
        "market_structure":    opp.get("market_type", "binary"),
        "expected_settlement": opp.get("end_date", "unknown"),
        "hours_until_resolve": opp.get("hours_until_resolve", 0),
        "timing_confidence":   "pre-game",
        "stake":               STAKE_PER_POS,
        "price":               no_price,
        "expected_hold_hours": opp.get("hours_until_resolve", 24),
        "key_driver":          opp.get("driver", ""),
        "fav_price":           opp["fav_price"],
        "fav_outcome":         opp["fav_outcome"],
        "volume":              opp["volume"],
        "shares":              shares,
        "potential_profit":    profit_if_win,
        "opened_at":           _now(),
        "result":              None,
        "profit":              0.0,
    }

    state["open_trades"].append(trade)
    state["open_positions_count"] = len(state["open_trades"])
    state["allocated_bankroll"] += STAKE_PER_POS
    state["free_bankroll"]      -= STAKE_PER_POS
    state["last_trade"]          = _now()

    log.info(
        f"OPENED  NO on '{opp['fav_outcome']}' | "
        f"{opp['sport']} | {opp['question'][:50]} | "
        f"NO@{no_price:.2f} | resolves {opp['hours_until_resolve']:.1f}h | "
        f"vol ${opp['volume']:,.0f} | "
        f"slots {state['open_positions_count']}/{MAX_OPEN}"
    )


def _check_fav_won(winning_outcome: str, fav: str) -> bool:
    """
    Check if the favourite won, handling name mismatches between
    what we stored (fav_outcome) and what CLOB returns (winning team).

    Handles:
      - Exact match: "Cavaliers" == "Cavaliers"
      - Case mismatch: "cavaliers" == "Cavaliers"
      - fav is "Yes"/"No" leftover: always returns False (can't determine)
      - fav contains "vs": extract first team as favourite, compare
    """
    if not winning_outcome or not fav:
        return False

    w = winning_outcome.lower().strip()
    f = fav.lower().strip()

    # Exact match (case insensitive)
    if w == f:
        return True

    # If fav is "Yes"/"No" (unfixed legacy), we can't determine the winner
    # Better to return False (counts as WIN) than guess wrong
    if f in ("yes", "no"):
        log.warning(f"Cannot determine fav winner: fav='{fav}', winner='{winning_outcome}'")
        return False

    # If fav contains "vs", it's the full match name (legacy bug)
    # We can't reliably tell which team was the favourite
    # Log it and return False — manual review needed
    if " vs " in f or " vs. " in f:
        log.warning(
            f"fav_outcome contains 'vs' (legacy bug): fav='{fav}', "
            f"winner='{winning_outcome}' — marking as UNKNOWN"
        )
        return False

    # Fuzzy match: winner is substantially contained in fav or vice versa
    if len(w) > 3 and len(f) > 3:
        if w in f or f in w:
            return True

    return False


def _check_resolutions():
    """Check if any open positions have resolved."""
    still_open = []
    checked = 0
    resolved_count = 0
    for t in state["open_trades"]:
        checked += 1
        resolved, winning_outcome = scanner.check_resolution(t["condition_id"])
        if resolved:
            resolved_count += 1

            if winning_outcome == "void":
                t["result"] = "void"
                t["profit"] = 0.0
            else:
                # Compare winning outcome to the favourite we bet NO against
                fav = t.get("fav_outcome", "")

                # The favourite won → our NO position loses
                # The favourite lost → our NO position wins
                fav_won = _check_fav_won(winning_outcome, fav)

                if fav_won:
                    t["result"] = "loss"
                    t["profit"] = -t["stake"]
                    state["pnl"]["losses"] += 1
                else:
                    t["result"] = "win"
                    t["profit"] = t["potential_profit"]
                    state["pnl"]["wins"] += 1

                log.info(
                    f"RESULT: winner='{winning_outcome}' | "
                    f"we bet NO on '{fav}' | "
                    f"{'LOSS (fav won)' if fav_won else 'WIN (fav lost)'}"
                )

            state["pnl"]["total"] += t["profit"]
            state["allocated_bankroll"] -= t["stake"]
            state["free_bankroll"]      += t["stake"] + t["profit"]
            state["closed_trades"].insert(0, t)
            log.info(
                f"CLOSED  {t['result'].upper()} | "
                f"{t['market_question'][:50]} | "
                f"P&L ${t['profit']:+,.0f} | "
                f"cumulative ${state['pnl']['total']:+,.0f}"
            )
        else:
            still_open.append(t)

    state["open_trades"] = still_open
    state["open_positions_count"] = len(still_open)
    if checked > 0:
        log.info(f"Resolution check: {checked} checked, {resolved_count} resolved, {len(still_open)} still open")


# ── Scan loop ───────────────────────────────────────────────────────────────
def _scan_loop():
    while state["running"]:
        try:
            state["current_step"] = "checking resolutions"
            state["error"] = None

            # 1. Always check resolutions first — free up slots
            if state["open_trades"]:
                _check_resolutions()

            # 2. Try to fill empty slots
            slots = _slots_free()
            if slots > 0:
                state["current_step"] = (
                    f"scanning — {slots} slots to fill "
                    f"(${state['free_bankroll']:,.0f} free)"
                )
                opportunities = scanner.find_no_opportunities()
                state["scan_count"] += 1
                state["last_scan"] = _now()

                filled = 0
                state["current_step"] = (
                    f"found {len(opportunities)} candidates — filling"
                )
                for opp in opportunities:
                    if not _can_open():
                        break
                    if _already_in(opp["condition_id"]):
                        continue
                    with lock:
                        _open_position(opp)
                        filled += 1

                if filled > 0:
                    state["current_step"] = (
                        f"filled {filled} slots — "
                        f"{state['open_positions_count']}/{MAX_OPEN} open"
                    )
                elif len(opportunities) == 0:
                    state["current_step"] = (
                        f"no qualifying pre-game markets — "
                        f"{state['open_positions_count']}/{MAX_OPEN} open"
                    )
                else:
                    state["current_step"] = (
                        f"all candidates already held — "
                        f"{state['open_positions_count']}/{MAX_OPEN} open"
                    )
            else:
                state["scan_count"] += 1
                state["last_scan"] = _now()
                state["current_step"] = (
                    f"fully deployed ${state['allocated_bankroll']:,.0f} — "
                    f"{state['open_positions_count']}/{MAX_OPEN} open"
                )

            # Shorter sleep when we have empty slots (aggressive fill)
            sleep_secs = SCAN_INTERVAL if _slots_free() == 0 else min(SCAN_INTERVAL, 45)
            state["current_step"] += f" · next scan {sleep_secs}s"

        except Exception as e:
            state["error"] = str(e)
            state["current_step"] = "error – retrying"
            log.exception("Scan error")
            sleep_secs = SCAN_INTERVAL

        for _ in range(sleep_secs):
            if not state["running"]:
                break
            time.sleep(1)

    state["current_step"] = "idle"


# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/start", methods=["POST"])
def start():
    global scan_thread
    if not state["running"]:
        state["running"] = True
        state["current_step"] = "starting"
        scan_thread = threading.Thread(target=_scan_loop, daemon=True)
        scan_thread.start()
    return redirect(url_for("dashboard"))


@app.route("/stop", methods=["POST"])
def stop():
    state["running"] = False
    return redirect(url_for("dashboard"))


@app.route("/api/status")
def api_status():
    return jsonify({
        "running":              state["running"],
        "current_step":         state["current_step"],
        "scan_count":           state["scan_count"],
        "last_scan":            state["last_scan"],
        "last_trade":           state["last_trade"],
        "open_positions_count": state["open_positions_count"],
        "max_open_positions":   settings["max_open_positions"],
        "stake_per_pos":        settings["reference_stake_amount"],
        "bankroll":             settings["total_bankroll_usd"],
        "min_volume":           settings["min_volume"],
        "pnl":                  state["pnl"],
        "free_bankroll":        state["free_bankroll"],
        "allocated_bankroll":   state["allocated_bankroll"],
        "error":                state["error"],
    })


@app.route("/api/trades")
def api_trades():
    return jsonify({
        "open":   state["open_trades"],
        "closed": state["closed_trades"],
    })


@app.route("/api/debug_market")
def api_debug_market():
    """Check what the APIs return for open positions — helps debug resolution."""
    import requests as req
    results = []
    for t in state["open_trades"][:5]:  # check first 5 only
        cid = t["condition_id"]
        entry = {"condition_id": cid[:16], "question": t["market_question"][:50]}

        # Gamma API
        try:
            r = req.get(f"https://gamma-api.polymarket.com/markets/{cid}", timeout=10)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, list):
                    d = d[0] if d else {}
                entry["gamma"] = {
                    "status": r.status_code,
                    "closed": d.get("closed"),
                    "resolved": d.get("resolved"),
                    "active": d.get("active"),
                    "outcomes": d.get("outcomes"),
                    "outcomePrices": d.get("outcomePrices"),
                    "keys": list(d.keys())[:20],
                }
            else:
                # Try query param format
                r2 = req.get("https://gamma-api.polymarket.com/markets",
                            params={"id": cid}, timeout=10)
                if r2.status_code == 200:
                    d2 = r2.json()
                    if isinstance(d2, list):
                        d2 = d2[0] if d2 else {}
                    entry["gamma_query"] = {
                        "status": r2.status_code,
                        "closed": d2.get("closed"),
                        "resolved": d2.get("resolved"),
                        "active": d2.get("active"),
                        "outcomes": d2.get("outcomes"),
                        "outcomePrices": d2.get("outcomePrices"),
                        "keys": list(d2.keys())[:20],
                    }
                else:
                    entry["gamma"] = {"status": r.status_code, "gamma_query_status": r2.status_code}
        except Exception as e:
            entry["gamma_error"] = str(e)

        # CLOB API
        try:
            r3 = req.get(f"https://clob.polymarket.com/markets/{cid}", timeout=10)
            if r3.status_code == 200:
                d3 = r3.json()
                entry["clob"] = {
                    "status": r3.status_code,
                    "closed": d3.get("closed"),
                    "resolved": d3.get("resolved"),
                    "active": d3.get("active"),
                    "tokens": [{"outcome": tk.get("outcome"), "price": tk.get("price")}
                              for tk in (d3.get("tokens") or [])],
                    "keys": list(d3.keys())[:20],
                }
            else:
                entry["clob"] = {"status": r3.status_code}
        except Exception as e:
            entry["clob_error"] = str(e)

        results.append(entry)

    return jsonify(results)


# ── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
