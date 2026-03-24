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
STAKE_PER_POS     = float(os.getenv("STAKE_PER_POS", 750))
MAX_OPEN          = int(os.getenv("MAX_OPEN", 13))         # floor(10000/750)
MIN_VOLUME        = float(os.getenv("MIN_VOLUME", 100_000))
MAX_FAV_PRICE     = float(os.getenv("MAX_FAV_PRICE", 0.92))
MIN_FAV_PRICE     = float(os.getenv("MIN_FAV_PRICE", 0.55))
MAX_HOURS         = float(os.getenv("MAX_HOURS", 168))      # endDate = resolution deadline, not game time. 168h = 7 days
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


def _check_resolutions():
    """Check if any open positions have resolved."""
    still_open = []
    for t in state["open_trades"]:
        resolved, result = scanner.check_resolution(t["condition_id"])
        if resolved:
            if result == "no_wins":
                t["result"] = "win"
                t["profit"] = t["potential_profit"]
                state["pnl"]["wins"] += 1
            elif result == "yes_wins":
                t["result"] = "loss"
                t["profit"] = -t["stake"]
                state["pnl"]["losses"] += 1
            elif result == "void":
                t["result"] = "void"
                t["profit"] = 0.0
            else:
                t["result"] = "unknown"
                t["profit"] = 0.0

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


# ── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
