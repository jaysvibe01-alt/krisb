"""Backtest the KRTKY logic on a daily Binance top-40 volume universe.

This is a research wrapper around the existing backtest engine. It does not
modify the bot rules. It downloads Binance USDT-M futures candles, builds a
daily top-40 universe by 1d quote volume, then filters the generated trades.

Two universe modes are reported:
  - same_day_top40: uses that UTC day's final 1d volume. This can be look-ahead.
  - prev_day_top40: uses the previous UTC day's final 1d volume. This is closer
    to a live universe known at the start of the day.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


os.environ.setdefault("KRTKY_SKIP_ICT_CREDS", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_IDS", "")
os.environ.setdefault(
    "KRTKY_LOG_PATH",
    str(Path(os.environ.get("TEMP", ".")) / "krtky_dynamic_top40.log"),
)

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = Path(__file__).resolve().parent
CACHE_DIR = TOOLS_DIR / "backtest_data"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TOOLS_DIR))

import backtest_realistic_pnl as pnl  # noqa: E402


BASE_URL = "https://fapi.binance.com"
INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}
INTERVALS = ("15m", "4h", "1d")
BARS_PER_REQ = 1500
USER_AGENT = "krtky-top40-backtest/1.0"


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def date_to_ms(d: date) -> int:
    return dt_to_ms(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))


def ms_to_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def request_json(path: str, params: dict[str, Any] | None = None, retries: int = 6) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (418, 429):
                time.sleep(min(60, 4 * (attempt + 1)))
            else:
                time.sleep(min(10, 1 + attempt))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(min(10, 1 + attempt))
    raise RuntimeError(f"Binance request failed: {path} {params} ({last_error})")


def fetch_exchange_symbols(limit_symbols: int | None = None) -> list[str]:
    info = request_json("/fapi/v1/exchangeInfo")
    symbols: list[str] = []
    for item in info.get("symbols", []):
        if item.get("status") != "TRADING":
            continue
        if item.get("contractType") != "PERPETUAL":
            continue
        if item.get("quoteAsset") != "USDT":
            continue
        symbol = item.get("symbol")
        if not symbol or "_" in symbol:
            continue
        symbols.append(symbol)
    symbols = sorted(set(symbols))
    if limit_symbols is not None:
        symbols = symbols[:limit_symbols]
    return symbols


def parse_kline(k: list[Any]) -> dict[str, Any]:
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": int(k[6]),
        "quote_volume": float(k[7]),
    }


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    step = INTERVAL_MS[interval]
    out: list[dict[str, Any]] = []
    cur = start_ms
    while cur < end_ms:
        data = request_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms - 1,
                "limit": BARS_PER_REQ,
            },
        )
        if not data:
            break
        chunk = [parse_kline(k) for k in data]
        out.extend(chunk)
        next_cur = chunk[-1]["open_time"] + step
        if next_cur <= cur:
            break
        cur = next_cur
        time.sleep(0.03)

    dedup: dict[int, dict[str, Any]] = {}
    for kline in out:
        dedup[kline["open_time"]] = kline
    return [dedup[k] for k in sorted(dedup)]


def cache_path(symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"dynamic_top40_{symbol}_{interval}.json"


def ranking_cache_path(start: date, end: date) -> Path:
    return CACHE_DIR / f"dynamic_top40_ranking_{start}_{end}.json"


def load_cached_klines(path: Path, start_ms: int, end_ms: int) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data:
        return None
    if data[0].get("open_time", 10**18) <= start_ms and data[-1].get("close_time", 0) >= end_ms - 1:
        return data
    return None


def ensure_symbol_data(symbol: str, start_ms: int, end_ms: int, refresh: bool = False) -> bool:
    for interval in INTERVALS:
        path = cache_path(symbol, interval)
        if not refresh and load_cached_klines(path, start_ms, end_ms) is not None:
            continue
        data = fetch_klines(symbol, interval, start_ms, end_ms)
        if not data:
            return False
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return True


def dynamic_load(symbol: str, interval: str) -> list[dict[str, Any]]:
    return json.loads(cache_path(symbol, interval).read_text(encoding="utf-8"))


def build_daily_rankings(
    symbols: list[str],
    start: date,
    end: date,
    refresh: bool = False,
) -> tuple[dict[str, list[str]], dict[str, dict[str, float]], list[str]]:
    cache = ranking_cache_path(start, end)
    if not refresh and cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        return payload["top40_by_day"], payload["quote_volume_by_day"], payload.get("errors", [])

    start_ms = date_to_ms(start)
    end_ms = date_to_ms(end + timedelta(days=1))
    quote_volume_by_day: dict[str, dict[str, float]] = defaultdict(dict)
    errors: list[str] = []

    for idx, symbol in enumerate(symbols, 1):
        if idx == 1 or idx % 50 == 0 or idx == len(symbols):
            print(f"ranking candles: {idx}/{len(symbols)}", flush=True)
        try:
            klines = fetch_klines(symbol, "1d", start_ms, end_ms)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")
            continue
        for kline in klines:
            day = ms_to_day(kline["open_time"])
            if start.isoformat() <= day <= end.isoformat():
                quote_volume_by_day[day][symbol] = float(kline.get("quote_volume", 0.0))

    top40_by_day: dict[str, list[str]] = {}
    for day, volumes in quote_volume_by_day.items():
        ranked = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
        top40_by_day[day] = [symbol for symbol, _ in ranked[:40]]

    payload = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "top40_by_day": top40_by_day,
        "quote_volume_by_day": quote_volume_by_day,
        "errors": errors,
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return top40_by_day, quote_volume_by_day, errors


def universe_for_trade(day: str, top40_by_day: dict[str, list[str]], mode: str) -> set[str]:
    if mode == "same_day_top40":
        return set(top40_by_day.get(day, []))
    if mode == "prev_day_top40":
        prev = (parse_date(day) - timedelta(days=1)).isoformat()
        return set(top40_by_day.get(prev, []))
    raise ValueError(f"unknown mode: {mode}")


def collect_symbol_trades(symbol: str) -> list[dict[str, Any]]:
    trades, _days = pnl.collect_entries(symbol)
    for trade in trades:
        trade["symbol"] = symbol
        trade["exit_ts"] = trade["bar_ts"] + int(trade.get("hold_bars", pnl.MAX_HOLD_BARS)) * INTERVAL_MS["15m"]
    return trades


def filter_dynamic_trades(
    trades: list[dict[str, Any]],
    top40_by_day: dict[str, list[str]],
    start: date,
    end: date,
    mode: str,
) -> list[dict[str, Any]]:
    start_key = start.isoformat()
    end_key = end.isoformat()
    filtered: list[dict[str, Any]] = []
    for trade in trades:
        day = ms_to_day(int(trade["bar_ts"]))
        if not (start_key <= day <= end_key):
            continue
        if trade["symbol"] not in universe_for_trade(day, top40_by_day, mode):
            continue
        trade = dict(trade)
        trade["trade_day_utc"] = day
        trade["universe_mode"] = mode
        filtered.append(trade)
    filtered.sort(key=lambda x: (x["bar_ts"], x["symbol"]))
    return filtered


def max_drawdown_pct(values: list[float]) -> float:
    peak = values[0] if values else 0.0
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return max_dd


def trade_stats(trades: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    rs = [float(t["final_r"]) for t in trades]
    n = len(rs)
    days = (end - start).days + 1
    wins = sum(1 for r in rs if r > 0)
    by_symbol = Counter(t["symbol"] for t in trades)
    by_day = Counter(t["trade_day_utc"] for t in trades)
    return {
        "n": n,
        "days": days,
        "trades_per_day": round(n / days, 2) if days else 0,
        "sum_r": round(sum(rs), 3),
        "avg_r": round(sum(rs) / n, 3) if n else 0,
        "win_pct": round(wins / n * 100, 1) if n else 0,
        "best_r": round(max(rs), 3) if rs else 0,
        "worst_r": round(min(rs), 3) if rs else 0,
        "active_trade_days": len(by_day),
        "top_symbols": by_symbol.most_common(20),
    }


def compound_sequential(
    trades: list[dict[str, Any]],
    risk_pct: float,
    starting_capital: float = 100.0,
) -> dict[str, Any]:
    capital = starting_capital
    curve = [capital]
    ruined = False
    for trade in trades:
        factor = 1 + (risk_pct / 100.0) * float(trade["final_r"])
        if factor <= 0:
            capital = 0.0
            ruined = True
            curve.append(capital)
            break
        capital *= factor
        curve.append(capital)
    return {
        "risk_pct": risk_pct,
        "mode": "all_signals_sequential",
        "n_taken": len(curve) - 1,
        "final_capital": round(capital, 6),
        "return_pct": round((capital / starting_capital - 1) * 100, 3),
        "multiplier": round(capital / starting_capital, 6),
        "max_dd_pct": round(max_drawdown_pct(curve), 3),
        "ruined": ruined,
    }


def compound_with_position_limit(
    trades: list[dict[str, Any]],
    risk_pct: float,
    max_concurrent: int,
    starting_capital: float = 100.0,
) -> dict[str, Any]:
    capital = starting_capital
    curve = [capital]
    open_positions: list[dict[str, Any]] = []
    taken = 0
    skipped = 0

    def close_due(ts: int | float) -> None:
        nonlocal capital, open_positions
        due = [p for p in open_positions if p["exit_ts"] <= ts]
        if not due:
            return
        remaining = [p for p in open_positions if p["exit_ts"] > ts]
        for pos in sorted(due, key=lambda x: x["exit_ts"]):
            capital += pos["pnl"]
            curve.append(capital)
        open_positions = remaining

    for trade in trades:
        start_ts = int(trade["bar_ts"])
        close_due(start_ts)
        if len(open_positions) >= max_concurrent:
            skipped += 1
            continue
        risk_amount = capital * risk_pct / 100.0
        pnl_amount = risk_amount * float(trade["final_r"])
        open_positions.append(
            {
                "exit_ts": int(trade["exit_ts"]),
                "pnl": pnl_amount,
                "symbol": trade["symbol"],
            }
        )
        taken += 1

    for pos in sorted(open_positions, key=lambda x: x["exit_ts"]):
        capital += pos["pnl"]
        curve.append(capital)

    return {
        "risk_pct": risk_pct,
        "mode": f"max_concurrent_{max_concurrent}",
        "max_concurrent": max_concurrent,
        "n_taken": taken,
        "n_skipped": skipped,
        "final_capital": round(capital, 6),
        "return_pct": round((capital / starting_capital - 1) * 100, 3),
        "multiplier": round(capital / starting_capital, 6),
        "max_dd_pct": round(max_drawdown_pct(curve), 3),
    }


def summarize_mode(trades: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    summary = trade_stats(trades, start, end)
    summary["compound"] = {
        "1pct_all": compound_sequential(trades, 1.0),
        "3_5pct_all": compound_sequential(trades, 3.5),
        "1pct_one_position": compound_with_position_limit(trades, 1.0, 1),
        "3_5pct_one_position": compound_with_position_limit(trades, 3.5, 1),
        "1pct_two_positions": compound_with_position_limit(trades, 1.0, 2),
        "3_5pct_two_positions": compound_with_position_limit(trades, 3.5, 2),
    }
    return summary


def print_mode_summary(mode: str, summary: dict[str, Any]) -> None:
    print()
    print(f"[{mode}]")
    print(
        "trades={n}, avgR={avg_r:+.3f}, sumR={sum_r:+.2f}, win={win_pct:.1f}%, "
        "per_day={trades_per_day:.2f}".format(**summary)
    )
    for key in (
        "1pct_all",
        "3_5pct_all",
        "1pct_one_position",
        "3_5pct_one_position",
        "1pct_two_positions",
        "3_5pct_two_positions",
    ):
        item = summary["compound"][key]
        print(
            f"  {key}: return={item['return_pct']:+.1f}%, "
            f"x{item['multiplier']:.2f}, DD={item['max_dd_pct']:.1f}%, "
            f"taken={item['n_taken']}"
            + (f", skipped={item['n_skipped']}" if "n_skipped" in item else "")
        )


def run(args: argparse.Namespace) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    end = parse_date(args.end_date) if args.end_date else datetime.now(timezone.utc).date() - timedelta(days=1)
    start = parse_date(args.start_date) if args.start_date else end - timedelta(days=args.days - 1)
    warmup_start = start - timedelta(days=args.warmup_days)
    start_ms = date_to_ms(start)
    end_ms = date_to_ms(end + timedelta(days=1))
    warmup_ms = date_to_ms(warmup_start)

    print(f"period UTC: {start} to {end} ({(end - start).days + 1} days)")
    print(f"warmup UTC: {warmup_start} to {start - timedelta(days=1)}")

    symbols = fetch_exchange_symbols(limit_symbols=args.limit_symbols)
    print(f"active Binance USDT perpetual symbols: {len(symbols)}")

    top40_by_day, quote_volume_by_day, ranking_errors = build_daily_rankings(
        symbols, start, end, refresh=args.refresh_rankings
    )
    days_with_rank = len(top40_by_day)
    if days_with_rank == 0:
        raise RuntimeError("No daily ranking data was built.")
    top40_union = sorted({symbol for values in top40_by_day.values() for symbol in values})
    print(f"ranked days: {days_with_rank}, top40 union symbols: {len(top40_union)}")

    ready_symbols: list[str] = []
    failed_symbols: list[str] = []
    for idx, symbol in enumerate(top40_union, 1):
        print(f"market data: {idx}/{len(top40_union)} {symbol}", flush=True)
        try:
            ok = ensure_symbol_data(symbol, warmup_ms, end_ms, refresh=args.refresh_symbol_data)
        except Exception as exc:  # noqa: BLE001
            failed_symbols.append(f"{symbol}: {exc}")
            continue
        if ok:
            ready_symbols.append(symbol)
        else:
            failed_symbols.append(f"{symbol}: no candles")

    pnl.load = dynamic_load
    all_trades: list[dict[str, Any]] = []
    failed_backtests: list[str] = []
    for idx, symbol in enumerate(ready_symbols, 1):
        print(f"backtest: {idx}/{len(ready_symbols)} {symbol}", flush=True)
        try:
            all_trades.extend(collect_symbol_trades(symbol))
        except Exception as exc:  # noqa: BLE001
            failed_backtests.append(f"{symbol}: {exc}")

    all_trades.sort(key=lambda x: (x["bar_ts"], x["symbol"]))
    same_day_trades = filter_dynamic_trades(all_trades, top40_by_day, start, end, "same_day_top40")
    prev_day_trades = filter_dynamic_trades(all_trades, top40_by_day, start, end, "prev_day_top40")

    result = {
        "source": "Binance USDT-M futures klines",
        "period_utc": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1},
        "ranking": {
            "method": "1d quoteVolume top 40 by UTC day",
            "days_with_rank": days_with_rank,
            "active_symbols_scanned": len(symbols),
            "top40_union_symbols": len(top40_union),
            "top40_union": top40_union,
            "ranking_errors": ranking_errors[:20],
            "ranking_error_count": len(ranking_errors),
        },
        "data": {
            "ready_symbols": len(ready_symbols),
            "failed_symbols": failed_symbols[:30],
            "failed_symbol_count": len(failed_symbols),
            "failed_backtests": failed_backtests[:30],
            "failed_backtest_count": len(failed_backtests),
        },
        "modes": {
            "same_day_top40": summarize_mode(same_day_trades, start, end),
            "prev_day_top40": summarize_mode(prev_day_trades, start, end),
        },
    }

    out_path = CACHE_DIR / f"dynamic_top40_3m_result_{start}_{end}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["output_path"] = str(out_path)

    print_mode_summary("same_day_top40", result["modes"]["same_day_top40"])
    print_mode_summary("prev_day_top40", result["modes"]["prev_day_top40"])
    print()
    print(f"saved: {out_path}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="UTC start date, YYYY-MM-DD")
    parser.add_argument("--end-date", help="UTC end date, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=90, help="closed UTC days to test when start-date is omitted")
    parser.add_argument("--warmup-days", type=int, default=30)
    parser.add_argument("--limit-symbols", type=int, help="debug only: scan only the first N exchange symbols")
    parser.add_argument("--refresh-rankings", action="store_true")
    parser.add_argument("--refresh-symbol-data", action="store_true")
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
