"""
EdgeBot – Polymarket NO-on-Favourites Scanner
Bets NO against the favourite in sports markets with $100k+ volume.
Paper trading mode. Deploys on Render.
"""

import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, redirect, url_for, jsonify

from scanner import PolymarketScanner

# ── Config ──────────────────────────────────────────────────────────────────
BANKROLL          = float(os.getenv("BANKROLL", 10_000))
STAKE_PER_POS     = float(os.getenv("STAKE_PER_POS", 750))
MAX_OPEN          = int(os.getenv("MAX_OPEN", 13))        # floor(10000/750)
MIN_VOLUME        = float(os.getenv("MIN_VOLUME", 100_000))
SCAN_INTERVAL     = int(os.getenv("SCAN_INTERVAL", 120))  # seconds
MAX_FAV_PRICE     = float(os.getenv("MAX_FAV_PRICE", 0.92))  # don't short near-certs
MIN_FAV_PRICE     = float(os.getenv("MIN_FAV_PRICE", 0.55))  # must actually be a fav

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
}

scanner = PolymarketScanner(
    min_volume=MIN_VOLUME,
    min_fav_price=MIN_FAV_PRICE,
    max_fav_price=MAX_FAV_PRICE,
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
    no_price = 1.0 - opp["fav_price"]          # price of NO share
    shares   = STAKE_PER_POS / no_price         # how many NO shares we get
    payout   = shares                           # each NO share pays $1 if fav loses
    profit_if_win = payout - STAKE_PER_POS

    trade = {
        "condition_id":        opp["condition_id"],
        "market_question":     opp["question"],
        "selection":           f"NO on {opp['fav_outcome']}",
        "market_domain":       "sports",
        "market_structure":    opp.get("market_type", "binary"),
        "expected_settlement": opp.get("end_date", "unknown"),
        "timing_confidence":   "medium",
        "stake":               STAKE_PER_POS,
        "price":               no_price,
        "expected_hold_hours": opp.get("hold_hours", 24),
        "key_driver":          opp.get("driver", ""),
        "fav_price":           opp["fav_price"],
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
    log.info(f"OPENED  NO on '{opp['fav_outcome']}' | {opp['question'][:60]} | "
             f"NO@{no_price:.2f} | vol ${opp['volume']:,.0f}")


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
            log.info(f"CLOSED  {t['result'].upper()} | {t['market_question'][:50]} | "
                     f"P&L ${t['profit']:+,.0f}")
        else:
            still_open.append(t)

    state["open_trades"] = still_open
    state["open_positions_count"] = len(still_open)


# ── Scan loop ───────────────────────────────────────────────────────────────
def _scan_loop():
    while state["running"]:
        try:
            state["current_step"] = "scanning markets"
            state["error"] = None

            # 1. Check resolutions on open positions
            if state["open_trades"]:
                state["current_step"] = "checking resolutions"
                _check_resolutions()

            # 2. Find new opportunities
            if _can_open():
                state["current_step"] = "fetching sports markets"
                opportunities = scanner.find_no_opportunities()
                state["scan_count"] += 1
                state["last_scan"] = _now()

                state["current_step"] = f"evaluating {len(opportunities)} candidates"
                for opp in opportunities:
                    if not _can_open():
                        break
                    if _already_in(opp["condition_id"]):
                        continue
                    with lock:
                        _open_position(opp)
            else:
                state["scan_count"] += 1
                state["last_scan"] = _now()
                state["current_step"] = "at capacity – waiting"

            state["current_step"] = f"sleeping {SCAN_INTERVAL}s"
        except Exception as e:
            state["error"] = str(e)
            state["current_step"] = "error – retrying"
            log.exception("Scan error")

        # Sleep in 1s chunks so stop is responsive
        for _ in range(SCAN_INTERVAL):
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
