"""TON / HYPE 추가 종목 데이터 다운로드 + 가용성 확인."""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / "backtest_data"

EXTRA = ["TONUSDT", "HYPEUSDT"]
INTERVALS = {"15m": "15m", "4h": "4h", "1d": "1d"}
BARS_PER_REQ = 1500

NOW_MS = int(time.time() * 1000)
YEAR_AGO_MS = NOW_MS - 365 * 86400 * 1000

BINANCE = "https://fapi.binance.com/fapi/v1/klines"


def fetch_binance(symbol, interval, start_ms, end_ms):
    url = f"{BINANCE}?{urllib.parse.urlencode({'symbol': symbol, 'interval': interval, 'startTime': start_ms, 'endTime': end_ms, 'limit': BARS_PER_REQ})}"
    req = urllib.request.Request(url, headers={"User-Agent": "krtky/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and "code" in data:
                return None, str(data)
            return data, None
    except Exception as e:
        return None, str(e)


def interval_minutes(i):
    if i == "1d": return 1440
    if i == "4h": return 240
    return int(i.replace("m", ""))


def download_full(symbol, interval):
    mins = interval_minutes(interval)
    out_path = CACHE / f"binance_{symbol}_{interval}_1y.json"
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if len(existing) >= 365 * 24 * 60 // mins * 0.5:
                print(f"  {symbol} {interval}: 이미 충분 ({len(existing)} 봉)")
                return True, len(existing)
        except Exception: pass

    print(f"  {symbol} {interval}: 다운로드 시작")
    all_kl = []
    cur = YEAR_AGO_MS
    while cur < NOW_MS:
        end = min(cur + BARS_PER_REQ * mins * 60 * 1000, NOW_MS)
        chunk, err = fetch_binance(symbol, interval, cur, end)
        if err is not None:
            print(f"    [ERR] Binance 에러: {err[:80]}")
            return False, 0
        if not chunk:
            break
        for k in chunk:
            all_kl.append({
                "open_time": int(k[0]), "open": float(k[1]),
                "high": float(k[2]), "low": float(k[3]),
                "close": float(k[4]), "volume": float(k[5]),
                "close_time": int(k[6]),
            })
        last_ts = int(chunk[-1][0])
        new_cur = last_ts + mins * 60 * 1000
        if new_cur <= cur: break
        cur = new_cur
        time.sleep(0.1)

    seen = set(); out = []
    for k in all_kl:
        if k["open_time"] in seen: continue
        seen.add(k["open_time"])
        out.append(k)
    out.sort(key=lambda x: x["open_time"])
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"    [OK] {len(out)} 봉 저장")
    return True, len(out)


def main():
    print("=== TON / HYPE 추가 종목 데이터 다운로드 ===\n")
    results = {}
    for sym in EXTRA:
        print(f"[{sym}]")
        all_ok = True
        bars = {}
        for interval in INTERVALS:
            ok, n = download_full(sym, interval)
            if not ok: all_ok = False
            bars[interval] = n
        results[sym] = {"ok": all_ok, "bars": bars}
        print()
    print("=== 요약 ===")
    for sym, r in results.items():
        status = "[OK] 가능" if r["ok"] else "[ERR] 불가"
        print(f"  {sym}: {status} (15m={r['bars'].get('15m',0)}, 4h={r['bars'].get('4h',0)}, 1d={r['bars'].get('1d',0)})")


if __name__ == "__main__":
    main()
