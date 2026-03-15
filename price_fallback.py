"""
price_fallback.py
─────────────────
Drop-in replacement for fastloop_trader.get_momentum() that tries
multiple CEX sources in order before giving up.

Fallback chain for BTC:
  1. Binance global       api.binance.com          (blocked in some regions)
  2. Binance US           api.binance.us            (US CDN, often accessible from MY)
  3. OKX                  www.okx.com               (reliable from Southeast Asia)
  4. Kraken               api.kraken.com            (EU CDN, solid fallback)
  5. Bybit                api.bybit.com             (popular in SEA)

How it works:
  - Monkey-patches get_momentum() in fastloop_trader at import time
  - fastloop_trader.py is not modified
  - run.py imports this module before importing fastloop_trader logic

Usage (already wired into run.py):
  import price_fallback  # noqa — patches get_momentum on import
"""

import json
import time
import sys
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# ── Symbol maps per exchange ───────────────────────────────────────────────────
_BINANCE_US_SYMBOLS   = {"BTC": "BTCUSD",  "ETH": "ETHUSD",  "SOL": "SOLUSD"}
_OKX_SYMBOLS          = {"BTC": "BTC-USDT","ETH": "ETH-USDT","SOL": "SOL-USDT"}
_KRAKEN_SYMBOLS       = {"BTC": "XBTUSD",  "ETH": "ETHUSD",  "SOL": "SOLUSD"}
_BYBIT_SYMBOLS        = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


def _fetch(url, timeout=8):
    """Minimal HTTP GET → parsed JSON. Returns None on any error."""
    try:
        req = Request(url, headers={"User-Agent": "fastloop-fallback/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (HTTPError, URLError, Exception):
        return None


# ── Per-exchange fetchers ──────────────────────────────────────────────────────

def _from_binance(symbol="BTCUSDT", lookback=5):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit={lookback}"
    data = _fetch(url)
    if not data or isinstance(data, dict):
        return None
    return _candles_to_momentum(data, source="binance")


def _from_binance_us(asset="BTC", lookback=5):
    symbol = _BINANCE_US_SYMBOLS.get(asset, "BTCUSD")
    url = f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1m&limit={lookback}"
    data = _fetch(url)
    if not data or isinstance(data, dict):
        return None
    return _candles_to_momentum(data, source="binance.us")


def _from_okx(asset="BTC", lookback=5):
    symbol = _OKX_SYMBOLS.get(asset, "BTC-USDT")
    # OKX /api/v5/market/candles returns newest-first; limit gives last N candles
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit={lookback}"
    data = _fetch(url)
    if not data or data.get("code") != "0":
        return None
    candles_raw = data.get("data", [])
    if len(candles_raw) < 2:
        return None
    # OKX format: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    # Newest first — reverse so oldest is index 0
    candles_raw = list(reversed(candles_raw))
    try:
        price_then = float(candles_raw[0][1])   # open of oldest
        price_now  = float(candles_raw[-1][4])  # close of newest
        volumes    = [float(c[5]) for c in candles_raw]
        return _build_result(price_then, price_now, volumes, source="okx")
    except (IndexError, ValueError, KeyError):
        return None


def _from_kraken(asset="BTC", lookback=5):
    symbol = _KRAKEN_SYMBOLS.get(asset, "XBTUSD")
    since = int(time.time()) - lookback * 60 - 60
    url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval=1&since={since}"
    data = _fetch(url)
    if not data or data.get("error"):
        return None
    result_key = [k for k in data.get("result", {}) if k != "last"]
    if not result_key:
        return None
    candles_raw = data["result"][result_key[0]]
    if len(candles_raw) < 2:
        return None
    # Kraken OHLC: [time, open, high, low, close, vwap, volume, count]
    try:
        price_then = float(candles_raw[0][1])
        price_now  = float(candles_raw[-1][4])
        volumes    = [float(c[6]) for c in candles_raw]
        return _build_result(price_then, price_now, volumes, source="kraken")
    except (IndexError, ValueError, KeyError):
        return None


def _from_bybit(asset="BTC", lookback=5):
    symbol = _BYBIT_SYMBOLS.get(asset, "BTCUSDT")
    url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval=1&limit={lookback}"
    data = _fetch(url)
    if not data or data.get("retCode") != 0:
        return None
    candles_raw = data.get("result", {}).get("list", [])
    if len(candles_raw) < 2:
        return None
    # Bybit: newest first — [startTime, open, high, low, close, volume, turnover]
    candles_raw = list(reversed(candles_raw))
    try:
        price_then = float(candles_raw[0][1])
        price_now  = float(candles_raw[-1][4])
        volumes    = [float(c[5]) for c in candles_raw]
        return _build_result(price_then, price_now, volumes, source="bybit")
    except (IndexError, ValueError, KeyError):
        return None


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _candles_to_momentum(candles, source="binance"):
    """Convert Binance-format kline list to momentum dict."""
    if len(candles) < 2:
        return None
    try:
        price_then = float(candles[0][1])
        price_now  = float(candles[-1][4])
        volumes    = [float(c[5]) for c in candles]
        return _build_result(price_then, price_now, volumes, source=source)
    except (IndexError, ValueError, KeyError):
        return None


def _build_result(price_then, price_now, volumes, source="unknown"):
    avg_volume    = sum(volumes) / len(volumes) if volumes else 1
    latest_volume = volumes[-1] if volumes else 1
    momentum_pct  = ((price_now - price_then) / price_then) * 100
    return {
        "momentum_pct":   momentum_pct,
        "direction":      "up" if momentum_pct > 0 else "down",
        "price_now":      price_now,
        "price_then":     price_then,
        "avg_volume":     avg_volume,
        "latest_volume":  latest_volume,
        "volume_ratio":   latest_volume / avg_volume if avg_volume > 0 else 1.0,
        "candles":        len(volumes),
        "_source":        source,   # extra field for logging — ignored by trader
    }


# ── Main fallback function (replaces get_momentum) ────────────────────────────

def get_momentum_with_fallback(asset="BTC", source="binance", lookback=5):
    """
    Try each price source in order. Returns first successful result.
    Prints which source was used (or failed) to stdout for Railway logs.
    """
    from fastloop_trader import ASSET_SYMBOLS  # import here to avoid circular import

    attempts = [
        ("binance",     lambda: _from_binance(ASSET_SYMBOLS.get(asset, "BTCUSDT"), lookback)),
        ("binance.us",  lambda: _from_binance_us(asset, lookback)),
        ("okx",         lambda: _from_okx(asset, lookback)),
        ("kraken",      lambda: _from_kraken(asset, lookback)),
        ("bybit",       lambda: _from_bybit(asset, lookback)),
    ]

    for name, fetcher in attempts:
        try:
            result = fetcher()
            if result:
                if name != "binance":
                    print(f"  ℹ️  Price source: {name} (binance unavailable)", flush=True)
                return result
        except Exception as e:
            print(f"  ⚠️  {name} error: {e}", flush=True)

    print("  ❌ All price sources failed — cannot trade this cycle", flush=True)
    return None


# ── Monkey-patch fastloop_trader.get_momentum ─────────────────────────────────
# This runs at import time. fastloop_trader must already be importable.

def _apply_patch():
    try:
        import fastloop_trader
        fastloop_trader.get_momentum = get_momentum_with_fallback
        print("  ✅ price_fallback: multi-source patch applied", flush=True)
    except ImportError:
        # fastloop_trader not importable yet — patch will be applied by run.py manually
        pass

_apply_patch()
