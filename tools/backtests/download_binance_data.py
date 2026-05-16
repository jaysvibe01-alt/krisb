"""Binance USDT-M Futures 1년 데이터 다운로드 — 신규 종목 추가용.

기존 4종목 (BTC/ETH/SOL/XRP) 외 추가 종목:
  DOGE, ADA, AVAX, LINK, SUI, BNB
"""
from __future__ import annotations

import json
import time
from pathlib import Path
import urllib.request
import urllib.parse

ROOT = Path(__file__).parent
CACHE = ROOT / "backtest_data"

BASE = "https://fapi.binance.com/fapi/v1/klines"
INTERVALS = {"15m": "15m", "4h": "4h", "1d": "1d"}
BARS_PER_REQ = 1500

NEW_SYMBOLS = ["DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT", "BNBUSDT"]

NOW_MS = int(time.time() * 1000)
YEAR_AGO_MS = NOW_MS - 365 * 86400 * 1000


def fetch_klines(symbol, interval, start_ms, end_ms):
    url = f"{BASE}?{urllib.parse.urlencode({'symbol': symbol, 'interval': interval, 'startTime': start_ms, 'endTime': end_ms, 'limit': BARS_PER_REQ})}"
    req = urllib.request.Request(url, headers={"User-Agent": "krtky/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  Error: {e}")
        return []
    out = []
    for k in data:
        out.append({
            "open_time": int(k[0]),
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]), "close_time": int(k[6]),
        })
    return out


def interval_minutes(interval):
    if interval == "1d": return 1440
    if interval == "4h": return 240
    return int(interval.replace("m", ""))


def download_full(symbol, interval):
    mins = interval_minutes(interval)
    out_path = CACHE / f"binance_{symbol}_{interval}_1y.json"
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if len(existing) >= 365 * 24 * 60 // mins * 0.9:
                print(f"  {out_path.name}: 이미 충분 ({len(existing)} 봉) — 스킵")
                return
        except Exception: pass

    print(f"  {symbol} {interval}: 다운로드 시작")
    all_kl = []
    cur = YEAR_AGO_MS
    while cur < NOW_MS:
        end = min(cur + BARS_PER_REQ * mins * 60 * 1000, NOW_MS)
        chunk = fetch_klines(symbol, interval, cur, end)
        if not chunk: break
        all_kl.extend(chunk)
        last_ts = chunk[-1]["open_time"]
        new_cur = last_ts + mins * 60 * 1000
        if new_cur <= cur: break
        cur = new_cur
        time.sleep(0.1)

    # dedup
    seen = set(); out = []
    for k in all_kl:
        if k["open_time"] in seen: continue
        seen.add(k["open_time"])
        out.append(k)
    out.sort(key=lambda x: x["open_time"])
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"    → {len(out)} 봉 저장")


def main():
    print("=== Binance USDT-M Futures 신규 6종 1년 데이터 다운로드 ===\n")
    for sym in NEW_SYMBOLS:
        print(f"[{sym}]")
        for interval in INTERVALS:
            download_full(sym, interval)
        print()
    print("완료.")


if __name__ == "__main__":
    main()
