"""유니버스 확장 — Binance Futures 시총/거래량 상위 16종 데이터 다운로드.

기존 12종에 추가:
  메이저:  LTC, TRX, BCH, DOT, ETC, FIL
  L2/L1:  ARB, OP, NEAR, APT, SEI
  DeFi:   AAVE, INJ, TIA
  밈:     PEPE, WIF
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / "backtest_data"

CANDIDATES = [
    # 메이저 (안정)
    "LTCUSDT", "TRXUSDT", "BCHUSDT", "DOTUSDT", "ETCUSDT", "FILUSDT",
    # L2/L1 (트렌드)
    "ARBUSDT", "OPUSDT", "NEARUSDT", "APTUSDT", "SEIUSDT",
    # DeFi
    "AAVEUSDT", "INJUSDT", "TIAUSDT",
    # 밈 (변동성 큼)
    "PEPEUSDT", "WIFUSDT",
]
INTERVALS = {"15m": "15m", "4h": "4h", "1d": "1d"}
BARS_PER_REQ = 1500

NOW_MS = int(time.time() * 1000)
YEAR_AGO_MS = NOW_MS - 365 * 86400 * 1000
BASE = "https://fapi.binance.com/fapi/v1/klines"


def fetch(symbol, interval, start_ms, end_ms):
    url = f"{BASE}?{urllib.parse.urlencode({'symbol': symbol, 'interval': interval, 'startTime': start_ms, 'endTime': end_ms, 'limit': BARS_PER_REQ})}"
    req = urllib.request.Request(url, headers={"User-Agent": "krtky/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
            if isinstance(d, dict) and "code" in d:
                return None, str(d)
            return d, None
    except Exception as e:
        return None, str(e)


def imin(i):
    if i == "1d": return 1440
    if i == "4h": return 240
    return int(i.replace("m", ""))


def download(sym, interval):
    mins = imin(interval)
    out_path = CACHE / f"binance_{sym}_{interval}_1y.json"
    if out_path.exists():
        try:
            ex = json.loads(out_path.read_text(encoding="utf-8"))
            if len(ex) >= 365 * 24 * 60 // mins * 0.5:
                return True, len(ex)
        except Exception: pass
    all_kl = []
    cur = YEAR_AGO_MS
    while cur < NOW_MS:
        end = min(cur + BARS_PER_REQ * mins * 60 * 1000, NOW_MS)
        chunk, err = fetch(sym, interval, cur, end)
        if err is not None:
            return False, 0
        if not chunk: break
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
        seen.add(k["open_time"]); out.append(k)
    out.sort(key=lambda x: x["open_time"])
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return True, len(out)


def main():
    print(f"=== Binance Futures 16종 후보 데이터 다운로드 ===\n")
    summary = {}
    for sym in CANDIDATES:
        ok_all = True
        bars = {}
        for interval in INTERVALS:
            ok, n = download(sym, interval)
            if not ok: ok_all = False
            bars[interval] = n
        summary[sym] = {"ok": ok_all, "bars": bars}
        status = "OK" if ok_all else "FAIL"
        print(f"  {sym}: {status} (15m={bars.get('15m',0)}, 4h={bars.get('4h',0)}, 1d={bars.get('1d',0)})")
    print(f"\n완료 — 가용 종목: {sum(1 for s in summary.values() if s['ok'])}/{len(CANDIDATES)}")


if __name__ == "__main__":
    main()
