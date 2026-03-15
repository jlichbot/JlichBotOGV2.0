#!/usr/bin/env python3
"""
run.py — Railway entrypoint for FastLoop Trader
- Runs fastloop_trader.py with full diagnostic output
- Sends alerts via both custom Telegram AND Simmer native clawdbot
- Clearly explains every skip/trade/error in Railway logs
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

try:
    import price_fallback  # noqa — patches get_momentum at import
except Exception as e:
    print(f"⚠️  price_fallback patch warning: {e}", flush=True)

from telegram_notify import notify_trade, notify_error, notify_skip, notify_budget_warning

# ── Config ─────────────────────────────────────────────────────────────────────
LIVE_TRADING  = os.environ.get("LIVE_TRADING", "0") == "1"
SMART_SIZING  = os.environ.get("SMART_SIZING", "0") == "1"
DAILY_BUDGET  = float(os.environ.get("DAILY_BUDGET_USD", "20"))
ASSET         = os.environ.get("SIMMER_SPRINT_ASSET", "BTC")
WINDOW        = os.environ.get("SIMMER_SPRINT_WINDOW", "5m")
ENTRY_THRESH  = os.environ.get("SIMMER_FASTLOOP_ENTRY_THRESHOLD", "0.05")
MOMENTUM_MIN  = os.environ.get("SIMMER_FASTLOOP_MOMENTUM_THRESHOLD", "0.3")
MAX_POS       = os.environ.get("SIMMER_FASTLOOP_MAX_POSITION_USD", "5")
SIMMER_KEY    = os.environ.get("SIMMER_API_KEY", "")

os.environ["AUTOMATON_MANAGED"] = "1"

# ── Simmer API health check (diagnose unreachable issue) ──────────────────────
def check_simmer_reachable():
    """Quick no-auth health check. Tells us if Railway IP can reach Simmer."""
    try:
        req = Request("https://api.simmer.markets/api/sdk/health")
        with urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            return True, data.get("version", "?")
    except Exception as e:
        return False, str(e)

# ── Simmer native Telegram via clawdbot ───────────────────────────────────────
def simmer_notify_trade(market_id, side, amount, reasoning="FastLoop signal"):
    """Tell Simmer to push a native trade alert via clawdbot."""
    if not SIMMER_KEY:
        return
    try:
        # Use troubleshoot endpoint to surface the alert — lightweight
        payload = {
            "error_text": f"[FASTLOOP ALERT] {side.upper()} ${amount:.2f} on {market_id[:16]}",
            "message": reasoning,
        }
        req = Request(
            "https://api.simmer.markets/api/sdk/troubleshoot",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {SIMMER_KEY}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=8) as r:
            pass
    except Exception:
        pass  # Never crash the cycle over an alert failure

# ── Cycle header ──────────────────────────────────────────────────────────────
ts         = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
mode_label = "LIVE" if LIVE_TRADING else "DRY RUN"

print("", flush=True)
print("=" * 60, flush=True)
print(f"  FASTLOOP  |  {ts}  |  {mode_label}", flush=True)
print("=" * 60, flush=True)
print(f"  Asset: {ASSET}  Window: {WINDOW}  Budget: ${DAILY_BUDGET}", flush=True)
print(f"  Entry: {ENTRY_THRESH}  Momentum min: {MOMENTUM_MIN}%  Max pos: ${MAX_POS}", flush=True)
print("-" * 60, flush=True)

# ── Simmer API reachability check ─────────────────────────────────────────────
reachable, version = check_simmer_reachable()
if reachable:
    print(f"  Simmer API: ✅ reachable (v{version})", flush=True)
else:
    print(f"  Simmer API: ⚠️  unreachable from Railway IP — using Gamma fallback", flush=True)
    print(f"  Detail: {version}", flush=True)
print("-" * 60, flush=True)

# ── Build subprocess command (no --quiet = full diagnostic output) ────────────
cmd = [sys.executable, "fastloop_trader.py"]
if LIVE_TRADING:
    cmd.append("--live")
if SMART_SIZING:
    cmd.append("--smart-sizing")

_here = os.path.dirname(os.path.abspath(__file__))
_env  = os.environ.copy()
_env["PYTHONPATH"]    = _here + (":" + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
_env["PYTHONSTARTUP"] = os.path.join(_here, "price_fallback.py")

# ── Run trader ────────────────────────────────────────────────────────────────
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=_env)
except subprocess.TimeoutExpired:
    print("RESULT: TIMEOUT — killed after 90s", flush=True)
    notify_error("FastLoop timed out after 90s on Railway")
    sys.exit(1)

stdout = result.stdout or ""
stderr = result.stderr or ""

print(stdout, flush=True)
if stderr.strip():
    print(f"[STDERR]\n{stderr}", flush=True)

# ── Parse automaton JSON ───────────────────────────────────────────────────────
automaton_data = None
for line in stdout.splitlines():
    if line.strip().startswith('{"automaton"'):
        try:
            automaton_data = json.loads(line.strip()).get("automaton", {})
        except json.JSONDecodeError:
            pass

# ── Extract context from output ───────────────────────────────────────────────
lines        = stdout.splitlines()
momentum_val = 0.0
price_val    = 0.0
side_val     = "YES"
price_source = "binance"
market_name  = ""
market_id    = ""

for l in lines:
    if "Momentum:" in l:
        try: momentum_val = float(l.split("Momentum:")[1].strip().split("%")[0].replace("+",""))
        except Exception: pass
    if "YES price:" in l or "YES $" in l:
        try: price_val = float(l.split("$")[1].strip().split()[0])
        except Exception: pass
    if "Signal: YES" in l: side_val = "YES"
    elif "Signal: NO" in l: side_val = "NO"
    if "Price source:" in l:
        try: price_source = l.split("Price source:")[1].strip().split()[0]
        except Exception: pass
    if "Selected:" in l:
        market_name = l.replace("Selected:", "").replace("🎯","").strip()
    if "Market ID:" in l or "Market ready:" in l:
        try: market_id = l.split(":")[-1].strip().split("...")[0]
        except Exception: pass

# ── Results summary ────────────────────────────────────────────────────────────
print("-" * 60, flush=True)

if result.returncode != 0:
    err = (stderr or stdout)[:300]
    print(f"RESULT: CRASH  exit={result.returncode}", flush=True)
    print(f"  {err[:200]}", flush=True)
    notify_error(f"Trader crashed (exit {result.returncode})\n{err}")
    print("=" * 60, flush=True)
    sys.exit(result.returncode)

if automaton_data:
    trades_executed  = automaton_data.get("trades_executed", 0)
    trades_attempted = automaton_data.get("trades_attempted", 0)
    amount_usd       = automaton_data.get("amount_usd", 0.0)
    skip_reason      = automaton_data.get("skip_reason", "")
    signals          = automaton_data.get("signals", 0)
    exec_errors      = automaton_data.get("execution_errors", [])

    if trades_executed > 0:
        tag = "PAPER" if not LIVE_TRADING else "LIVE"
        print(f"RESULT: TRADE EXECUTED [{tag}]", flush=True)
        print(f"  Side:     {side_val}", flush=True)
        print(f"  Amount:   ${amount_usd:.2f}", flush=True)
        print(f"  Price:    ${price_val:.3f}", flush=True)
        print(f"  Market:   {market_name[:55]}", flush=True)
        print(f"  Momentum: {momentum_val:+.3f}%  Feed: {price_source}", flush=True)

        # Alert via custom Telegram
        notify_trade(side=side_val, market=market_name or "BTC Fast Market",
                     amount=amount_usd, price=price_val, momentum=momentum_val,
                     dry_run=not LIVE_TRADING)
        print(f"  Alert: custom Telegram sent", flush=True)

        # Alert via Simmer native clawdbot
        simmer_notify_trade(
            market_id=market_id or market_name,
            side=side_val,
            amount=amount_usd,
            reasoning=f"BTC momentum {momentum_val:+.3f}% via {price_source}"
        )

        if DAILY_BUDGET > 0 and amount_usd / DAILY_BUDGET > 0.8:
            notify_budget_warning(amount_usd, DAILY_BUDGET)

    elif trades_attempted > 0 and exec_errors:
        print(f"RESULT: TRADE FAILED (attempted, not executed)", flush=True)
        for e in exec_errors:
            print(f"  Error: {e}", flush=True)
        notify_error("Trade attempted but failed:\n" + "\n".join(exec_errors))

    else:
        low = stdout.lower()

        # Determine specific skip reason from output
        if "no active fast markets" in low or "found 0 active" in low or "found 0" in low:
            if not reachable:
                why = "NO MARKETS — Simmer API unreachable + Gamma found 0 live BTC markets"
                hint = "Normal outside US market hours (9:30AM-7PM ET / 13:30-23:00 UTC weekdays)"
            else:
                why = "NO MARKETS — 0 live BTC 5m markets found on Polymarket right now"
                hint = "Check polymarket.com for active BTC fast markets"
        elif "no fast markets with" in low or "no tradeable markets" in low:
            why = "NO MARKETS — Markets found but all expired or insufficient time remaining"
            hint = "Try reducing SIMMER_FASTLOOP_MIN_TIME_BETWEEN_TRADES_SEC"
        elif "momentum" in low and "< minimum" in low:
            actual_line = next((l.strip() for l in lines if "Momentum" in l and "minimum" in l), "")
            why = f"WEAK SIGNAL — {actual_line or 'BTC momentum below ' + MOMENTUM_MIN + '%'}"
            hint = f"Lower SIMMER_FASTLOOP_MOMENTUM_THRESHOLD from {MOMENTUM_MIN} to try 0.2"
        elif "divergence" in low and "minimum" in low:
            why = f"WEAK SIGNAL — Price divergence below entry threshold {ENTRY_THRESH}"
            hint = "Lower SIMMER_FASTLOOP_ENTRY_THRESHOLD to 0.03"
        elif "already holding" in low:
            why = "SKIPPED — Already holding a position on this market (dedup)"
            hint = "Normal — prevents doubling up"
        elif "wide spread" in low:
            why = "SKIPPED — Order book spread too wide (illiquid market)"
            hint = "Normal — protecting against bad fills"
        elif "fees eat" in low:
            why = "SKIPPED — Edge insufficient to cover Polymarket 10% fee"
            hint = "Need stronger momentum signal to overcome fee drag"
        elif "daily budget" in low and "exhausted" in low:
            why = f"BUDGET — Daily limit ${DAILY_BUDGET} reached"
            hint = "Resets at UTC midnight"
        elif "all price sources failed" in low or "failed to fetch price" in low:
            why = "PRICE FEED ERROR — All 5 sources failed (Binance/BinanceUS/OKX/Kraken/Bybit)"
            hint = "Serious network issue on Railway — check service status"
            notify_error(why)
        elif "clob price unavailable" in low:
            why = "PRICE FEED ERROR — Cannot fetch live Polymarket CLOB price"
            hint = "Polymarket API may be down"
        else:
            why = f"NO SIGNAL — {skip_reason or 'no qualifying conditions met'}"
            hint = ""

        print(f"RESULT: NO TRADE", flush=True)
        print(f"  Why:  {why}", flush=True)
        if hint:
            print(f"  Hint: {hint}", flush=True)

        if os.environ.get("NOTIFY_SKIPS") == "1":
            notify_skip(why)

else:
    low = stdout.lower()
    if not stdout.strip():
        msg = "EMPTY OUTPUT — SDK silent exit. Check SIMMER_API_KEY validity."
        print(f"RESULT: ERROR — {msg}", flush=True)
        notify_error(msg)
    elif "api key" in low or "authorization" in low:
        msg = "AUTH ERROR — SIMMER_API_KEY rejected. Regenerate at simmer.markets/dashboard"
        print(f"RESULT: ERROR — {msg}", flush=True)
        notify_error(msg)
    elif "import" in low and "error" in low:
        print(f"RESULT: ERROR — simmer-sdk import failed. Check requirements.txt.", flush=True)
    else:
        print(f"RESULT: UNKNOWN — no structured report. Full output above.", flush=True)

print("=" * 60, flush=True)
print(f"  Done  |  {ts}", flush=True)
print("", flush=True)
sys.exit(0)
