"""Compare executable fill models for the KRTKY next-bar entry.

The strategy signal is generated at the close of a 15m confirmation bar.
The next 15m candle is then used only to decide whether a pre-posted order
would have filled. This script separates actual limit-order models from the
"best wick" model that uses the next candle extreme as the entry price.

Models:
  old_mid_any_touch : old optimistic model; any zone touch enters at mid.
  fixed_zone_high   : one aggressive limit at the expensive edge of the zone.
  fixed_mid         : one limit at zone midpoint.
  fixed_zone_low    : one conservative limit at the best edge of the zone.
  ladder_3          : equal limits at expensive edge, mid, and best edge.
  best_wick_oracle  : audit only; uses the next-bar wick extreme after the fact.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any


os.environ.setdefault("KRTKY_SKIP_ICT_CREDS", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_IDS", "")
os.environ.setdefault(
    "KRTKY_LOG_PATH",
    str(Path(os.environ.get("TEMP", ".")) / "krtky_fill_models.log"),
)
logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = Path(__file__).resolve().parent
CACHE_DIR = TOOLS_DIR / "backtest_data"

# Keep src before tools so we use the v1.5 simulate_trade implementation.
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(ROOT / "src"))

import krtky_alert_bot as bot  # noqa: E402
import backtest_realistic_pnl as pnl  # noqa: E402
from backtest_user_model import Collector, load  # noqa: E402
from rsi_alert_core import RSISymbolState  # noqa: E402


MODELS = [
    "old_mid_any_touch",
    "fixed_zone_high",
    "fixed_mid",
    "fixed_zone_low",
    "ladder_3",
    "best_wick_oracle",
]

DEFAULT_SYMBOLS_29 = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT",
    "ADAUSDT", "AVAXUSDT", "SUIUSDT", "BNBUSDT", "TONUSDT", "HYPEUSDT",
    "BCHUSDT", "FILUSDT", "ARBUSDT", "LTCUSDT", "OPUSDT", "DOTUSDT",
    "ETCUSDT", "AAVEUSDT", "ENAUSDT", "PENGUUSDT", "NEARUSDT",
    "APTUSDT", "SEIUSDT", "INJUSDT", "TIAUSDT", "WIFUSDT", "ZECUSDT",
]
INTERVALS = ("15m", "4h", "1d")
INTERVAL_MINUTES = {"15m": 15, "4h": 240, "1d": 1440}
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BARS_PER_REQ = 1500


def cache_file(symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"binance_{symbol}_{interval}_1y.json"


def has_symbol_data(symbol: str) -> bool:
    return all(cache_file(symbol, interval).exists() for interval in INTERVALS)


def fetch_binance(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Any]:
    url = f"{BINANCE_KLINES}?{urllib.parse.urlencode({
        'symbol': symbol,
        'interval': interval,
        'startTime': start_ms,
        'endTime': end_ms,
        'limit': BARS_PER_REQ,
    })}"
    req = urllib.request.Request(url, headers={"User-Agent": "krtky-fill-models/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if isinstance(data, dict) and "code" in data:
        raise RuntimeError(str(data))
    return data


def download_symbol_data(symbol: str, now_ms: int) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_ms = now_ms - 365 * 86400 * 1000
    for interval in INTERVALS:
        out_path = cache_file(symbol, interval)
        if out_path.exists():
            continue
        mins = INTERVAL_MINUTES[interval]
        cur = start_ms
        rows: list[dict[str, Any]] = []
        while cur < now_ms:
            end = min(cur + BARS_PER_REQ * mins * 60 * 1000, now_ms)
            chunk = fetch_binance(symbol, interval, cur, end)
            if not chunk:
                break
            for k in chunk:
                rows.append({
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": int(k[6]),
                })
            next_cur = int(chunk[-1][0]) + mins * 60 * 1000
            if next_cur <= cur:
                break
            cur = next_cur
            time.sleep(0.08)
        dedup = {row["open_time"]: row for row in rows}
        ordered = [dedup[key] for key in sorted(dedup)]
        out_path.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")


def ensure_data(symbols: list[str], download_missing: bool) -> tuple[list[str], list[str]]:
    missing = [symbol for symbol in symbols if not has_symbol_data(symbol)]
    if download_missing and missing:
        now_ms = int(time.time() * 1000)
        for idx, symbol in enumerate(missing, 1):
            print(f"download missing {idx}/{len(missing)} {symbol}", flush=True)
            download_symbol_data(symbol, now_ms)
    ready = [symbol for symbol in symbols if has_symbol_data(symbol)]
    still_missing = [symbol for symbol in symbols if not has_symbol_data(symbol)]
    return ready, still_missing


def reset_bot(symbol: str) -> None:
    bot.PRE_ALERT_TIMEOUT_BARS = 8
    bot.RSI_OVERSOLD = 30
    bot.RSI_OVERBOUGHT = 70
    bot.SYMBOLS = [symbol]
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    bot.BTC_TIME_GATE_ENABLED = True
    bot.BTC_CT_GATE_ENABLED = True


def collect_signal_events(symbol: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    reset_bot(symbol)
    collector = Collector()
    bot.send_telegram = collector.send

    k15 = load(symbol, "15m")
    k4h = load(symbol, "4h")
    k1d = load(symbol, "1d")
    if not k15 or not k4h or not k1d:
        return [], [], 0.0

    bot.STATE[symbol] = bot.SymbolState()
    bot.RSI_STATE[symbol] = RSISymbolState()
    bot.SERIES_15M[symbol] = deque(maxlen=200)
    bot.SERIES_4H[symbol] = deque(maxlen=100)
    bot.SERIES_1D[symbol] = deque(maxlen=50)
    bot.ISOLATED_SR_CACHE[symbol] = []
    for k in k15[:50]:
        bot.SERIES_15M[symbol].append(k)
    for k in k4h[:8]:
        bot.SERIES_4H[symbol].append(k)
    for k in k1d[:10]:
        bot.SERIES_1D[symbol].append(k)

    last_4h_ot = bot.SERIES_4H[symbol][-1]["open_time"]
    last_1d_ot = bot.SERIES_1D[symbol][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h_ot)
    i1d = sum(1 for k in k1d if k["open_time"] <= last_1d_ot)

    for i in range(50, len(k15)):
        kk = k15[i]
        bot.SERIES_15M[symbol].append(kk)
        while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
            bot.SERIES_4H[symbol].append(k4h[i4])
            i4 += 1
        while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
            bot.SERIES_1D[symbol].append(k1d[i1d])
            i1d += 1
        atr = bot.calc_atr(list(bot.SERIES_15M[symbol]))
        collector.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try:
            bot.evaluate_symbol_15m(symbol)
        except Exception:
            pass

    days = (k15[-1]["close_time"] - k15[50]["close_time"]) / (1000 * 86400)
    events = [
        event for event in collector.events
        if event.get("direction") in ("long", "short") and event["bar_idx"] + 1 < len(k15)
    ]
    return events, k15, days


def fixed_entry(direction: str, next_bar: dict[str, float], price: float) -> bool:
    return next_bar["low"] <= price if direction == "long" else next_bar["high"] >= price


def fill_plan(
    mode: str,
    direction: str,
    zone_low: float,
    zone_high: float,
    next_bar: dict[str, float],
) -> list[tuple[float, float]]:
    mid = (zone_low + zone_high) / 2.0

    if mode == "old_mid_any_touch":
        if next_bar["low"] <= zone_high and next_bar["high"] >= zone_low:
            return [(mid, 1.0)]
        return []

    if mode == "fixed_zone_high":
        price = zone_high if direction == "long" else zone_low
        return [(price, 1.0)] if fixed_entry(direction, next_bar, price) else []

    if mode == "fixed_mid":
        return [(mid, 1.0)] if fixed_entry(direction, next_bar, mid) else []

    if mode == "fixed_zone_low":
        price = zone_low if direction == "long" else zone_high
        return [(price, 1.0)] if fixed_entry(direction, next_bar, price) else []

    if mode == "ladder_3":
        levels = (
            [zone_high, mid, zone_low]
            if direction == "long"
            else [zone_low, mid, zone_high]
        )
        return [(price, 1.0 / 3.0) for price in levels if fixed_entry(direction, next_bar, price)]

    if mode == "best_wick_oracle":
        if direction == "long":
            if next_bar["low"] > zone_high:
                return []
            return [(max(next_bar["low"], zone_low), 1.0)]
        if next_bar["high"] < zone_low:
            return []
        return [(min(next_bar["high"], zone_high), 1.0)]

    raise ValueError(f"unknown fill model: {mode}")


def simulate_event(
    event: dict[str, Any],
    k15: list[dict[str, Any]],
    symbol: str,
    mode: str,
) -> dict[str, Any] | None:
    confirm_idx = event["bar_idx"]
    confirm = k15[confirm_idx]
    next_bar = k15[confirm_idx + 1]
    direction = event["direction"]
    atr = event["atr"]

    if direction == "long":
        zone_low, zone_high = confirm["low"], confirm["close"]
    else:
        zone_low, zone_high = confirm["close"], confirm["high"]
    if zone_high < zone_low:
        zone_low, zone_high = zone_high, zone_low

    fills = fill_plan(mode, direction, zone_low, zone_high, next_bar)
    if not fills:
        return None

    path = k15[confirm_idx + 1:confirm_idx + 1 + pnl.MAX_HOLD_BARS]
    weighted_r = 0.0
    weighted_raw = 0.0
    weighted_costs = 0.0
    fill_ratio = 0.0
    hold_bars = 0
    outcomes: Counter[str] = Counter()
    entries: list[float] = []
    for entry_price, weight in fills:
        sl_d = atr * 1.5
        sl_price = entry_price - sl_d if direction == "long" else entry_price + sl_d
        sim = pnl.simulate_trade(entry_price, sl_price, direction, path, symbol)
        weighted_r += weight * float(sim["final_r"])
        weighted_raw += weight * float(sim["raw_r"])
        weighted_costs += weight * float(sim["costs_r"])
        fill_ratio += weight
        hold_bars = max(hold_bars, int(sim["hold_bars"]))
        outcomes[str(sim["outcome"])] += 1
        entries.append(entry_price)

    return {
        "symbol": symbol,
        "mode": mode,
        "direction": direction,
        "bar_ts": event["bar_ts"],
        "confirm_idx": confirm_idx,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "entry_price": sum(entries) / len(entries),
        "fill_ratio": round(fill_ratio, 4),
        "final_r": round(weighted_r, 4),
        "raw_r": round(weighted_raw, 4),
        "costs_r": round(weighted_costs, 4),
        "hold_bars": hold_bars,
        "outcome": "+".join(sorted(outcomes)),
    }


def compound_simulation(trades: list[dict[str, Any]], risk_pct: float, starting_capital: float = 100.0) -> dict[str, Any]:
    capital = starting_capital
    peak = capital
    max_dd = 0.0
    ruined = False
    for trade in sorted(trades, key=lambda x: x["bar_ts"]):
        factor = 1.0 + (risk_pct / 100.0) * float(trade["final_r"])
        if factor <= 0:
            capital = 0.0
            ruined = True
            max_dd = 100.0
            break
        capital *= factor
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100.0)
    return {
        "risk_pct": risk_pct,
        "final_capital": round(capital, 6),
        "return_pct": round((capital / starting_capital - 1.0) * 100.0, 3),
        "multiplier": round(capital / starting_capital, 6),
        "max_dd_pct": round(max_dd, 3),
        "ruined": ruined,
    }


def summarize(trades: list[dict[str, Any]], signal_count: int, days: float) -> dict[str, Any]:
    rs = [float(t["final_r"]) for t in trades]
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    return {
        "signals": signal_count,
        "filled_events": n,
        "fill_event_pct": round(n / signal_count * 100.0, 2) if signal_count else 0.0,
        "effective_fills": round(sum(float(t.get("fill_ratio", 1.0)) for t in trades), 2),
        "days": round(days, 2),
        "trades_per_day": round(n / days, 3) if days else 0.0,
        "sum_r": round(sum(rs), 3),
        "avg_r": round(sum(rs) / n, 4) if n else 0.0,
        "win_pct": round(wins / n * 100.0, 2) if n else 0.0,
        "compound_1pct": compound_simulation(trades, 1.0),
        "compound_3_5pct": compound_simulation(trades, 3.5),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    symbols = DEFAULT_SYMBOLS_29 if args.symbols == "default29" else [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    ready, missing = ensure_data(symbols, args.download_missing)
    if missing:
        print(f"missing data skipped: {', '.join(missing)}", flush=True)
    if not ready:
        raise RuntimeError("No symbols with complete cached data.")

    trades_by_mode: dict[str, list[dict[str, Any]]] = {mode: [] for mode in MODELS}
    signal_count = 0
    max_days = 0.0
    by_symbol: dict[str, Any] = {}

    for idx, symbol in enumerate(ready, 1):
        print(f"signals {idx}/{len(ready)} {symbol}", flush=True)
        events, k15, days = collect_signal_events(symbol)
        signal_count += len(events)
        max_days = max(max_days, days)
        by_symbol[symbol] = {"signals": len(events), "days": round(days, 2), "modes": {}}
        for mode in MODELS:
            symbol_trades: list[dict[str, Any]] = []
            for event in events:
                trade = simulate_event(event, k15, symbol, mode)
                if trade is not None:
                    symbol_trades.append(trade)
                    trades_by_mode[mode].append(trade)
            by_symbol[symbol]["modes"][mode] = summarize(symbol_trades, len(events), days)

    summary = {mode: summarize(trades, signal_count, max_days) for mode, trades in trades_by_mode.items()}
    result = {
        "symbols_requested": symbols,
        "symbols_tested": ready,
        "symbols_missing": missing,
        "model_notes": {
            "old_mid_any_touch": "Old optimistic model: any next-bar zone overlap enters at midpoint even if midpoint was not touched.",
            "fixed_zone_high": "Single aggressive limit: long at zone high, short at zone low.",
            "fixed_mid": "Single limit at midpoint; fill requires next bar to touch midpoint.",
            "fixed_zone_low": "Single conservative limit: long at zone low, short at zone high.",
            "ladder_3": "Three equal pre-posted limits at expensive edge, midpoint, and best edge; partial fills are scaled.",
            "best_wick_oracle": "Audit-only model using next-bar wick extreme as entry price after the fact.",
        },
        "summary": summary,
        "by_symbol": by_symbol,
    }

    out_path = CACHE_DIR / "fill_model_comparison.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=== Fill model comparison ===")
    print(f"symbols tested: {len(ready)} / requested {len(symbols)}")
    print(f"signals: {signal_count}")
    print("model, filled, fill%, sumR, avgR, win%, comp1%, dd1%, comp3.5%, dd3.5%")
    for mode in MODELS:
        s = summary[mode]
        c1 = s["compound_1pct"]
        c35 = s["compound_3_5pct"]
        print(
            f"{mode}, {s['filled_events']}, {s['fill_event_pct']:.1f}, "
            f"{s['sum_r']:+.2f}, {s['avg_r']:+.3f}, {s['win_pct']:.1f}, "
            f"{c1['return_pct']:+.1f}, {c1['max_dd_pct']:.1f}, "
            f"{c35['return_pct']:+.1f}, {c35['max_dd_pct']:.1f}"
        )
    print(f"saved: {out_path}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="default29", help="default29 or comma-separated symbols")
    parser.add_argument("--download-missing", action="store_true", help="download missing 1y Binance data")
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
