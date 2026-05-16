"""Bybit USDT-M Futures 15m/4h/1d 1년 데이터 다운로드 — robustness 검증용.

Bybit V5 API: GET /v5/market/kline
  category=linear (USDT perpetual)
  symbol=BTCUSDT etc.
  interval=15 / 240 / D
  limit max 1000
  start/end (ms)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.parse

ROOT = Path(__file__).parent
CACHE = ROOT / "backtest_data"
CACHE.mkdir(exist_ok=True)

BASE = "https://api.bybit.com/v5/market/kline"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
INTERVALS = {"15m": "15", "4h": "240", "1d": "D"}
BARS_PER_REQ = 1000

# 1년 = 365일
NOW_MS = int(time.time() * 1000)
YEAR_AGO_MS = NOW_MS - 365 * 86400 * 1000


def fetch_klines(symbol: str, interval_str: str, start_ms: int, end_ms: int) -> list[dict]:
    """Bybit V5 GET /market/kline. Returns oldest-first list of dicts."""
    url = f"{BASE}?{urllib.parse.urlencode({'category': 'linear', 'symbol': symbol, 'interval': interval_str, 'start': start_ms, 'end': end_ms, 'limit': BARS_PER_REQ})}"
    req = urllib.request.Request(url, headers={"User-Agent": "krtky/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  Error: {e}")
        return []
    if data.get("retCode") != 0:
        print(f"  API error: {data.get('retMsg')}")
        return []
    rows = data.get("result", {}).get("list", [])
    out = []
    for r in rows:
        # Bybit: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
        ts = int(r[0])
        out.append({
            "open_time": ts,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "close_time": ts + interval_minutes(interval_str) * 60 * 1000 - 1,
        })
    # Bybit returns newest-first → reverse
    out.sort(key=lambda x: x["open_time"])
    return out


def interval_minutes(interval_str: str) -> int:
    if interval_str == "D":
        return 1440
    return int(interval_str)


def download_full(symbol: str, interval_name: str):
    interval_str = INTERVALS[interval_name]
    mins = interval_minutes(interval_str)
    bars_per_year = 365 * 24 * 60 // mins
    out_path = CACHE / f"bybit_{symbol}_{interval_name}_1y.json"
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if len(existing) >= bars_per_year * 0.9:
                print(f"  {out_path.name}: 이미 충분 ({len(existing)} 봉) — 스킵")
                return
        except Exception:
            pass

    print(f"  {symbol} {interval_name}: 다운로드 시작 (예상 {bars_per_year} 봉)")
    all_klines = []
    cur_start = YEAR_AGO_MS
    while cur_start < NOW_MS:
        cur_end = min(cur_start + BARS_PER_REQ * mins * 60 * 1000, NOW_MS)
        chunk = fetch_klines(symbol, interval_str, cur_start, cur_end)
        if not chunk:
            break
        all_klines.extend(chunk)
        # 다음 청크 — 마지막 open_time 다음
        last_ts = chunk[-1]["open_time"]
        new_start = last_ts + mins * 60 * 1000
        if new_start <= cur_start:
            break
        cur_start = new_start
        time.sleep(0.1)  # rate limit 보호

    # 중복 제거 + 정렬
    seen = set()
    dedup = []
    for k in all_klines:
        ts = k["open_time"]
        if ts in seen:
            continue
        seen.add(ts)
        dedup.append(k)
    dedup.sort(key=lambda x: x["open_time"])

    out_path.write_text(json.dumps(dedup, ensure_ascii=False), encoding="utf-8")
    print(f"    → {len(dedup)} 봉 저장: {out_path.name}")


def main():
    print("=== Bybit USDT-M Futures 1년 데이터 다운로드 ===\n")
    for sym in SYMBOLS:
        print(f"[{sym}]")
        for interval in INTERVALS:
            download_full(sym, interval)
        print()
    print("완료.")


if __name__ == "__main__":
    main()
