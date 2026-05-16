"""롱/숏 분리 + Regime 분기 + 시간대·종목별 분석.

같은 471 event 데이터를 4개 차원으로 재분류:
  1) 방향 (long/short)
  2) Regime (BTC 강세/약세/횡보)  — BTC 1d close 의 EMA50 vs EMA200
  3) 시간대 (KST 0-6 / 6-12 / 12-18 / 18-24)
  4) 종목 (BTC/ETH/SOL/XRP)

각 그루핑에서 realistic RR 중앙값·Win rate·SL hit 비교.
50/50 동전 던지기에서 벗어난 sub-segment 발견 목적.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import median, mean
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot   # noqa
from rsi_alert_core import RSISymbolState
from backtest_realistic import (verify_realistic, Collector, load,
                                  SYMBOLS, SLIPPAGE_BPS, FUNDING_PER_8H_PCT)

KST = timezone(timedelta(hours=9))


def calc_ema(values: list[float], period: int) -> list[float]:
    if not values: return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def get_btc_regime(btc_1d_klines: list[dict]) -> dict[int, str]:
    """BTC 일봉 시점별 regime — EMA50 vs EMA200 비교.
    Returns: {open_time_ms: 'bull'/'bear'/'sideways'}
    """
    closes = [k["close"] for k in btc_1d_klines]
    if len(closes) < 200: return {}
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    out = {}
    for i, k in enumerate(btc_1d_klines):
        if i < 200:
            out[k["open_time"]] = "warmup"
            continue
        e50, e200 = ema50[i], ema200[i]
        ratio = (e50 - e200) / e200
        if ratio > 0.05: out[k["open_time"]] = "bull"
        elif ratio < -0.05: out[k["open_time"]] = "bear"
        else: out[k["open_time"]] = "sideways"
    return out


def regime_at(ts_ms: int, regime_map: dict[int, str]) -> str:
    """ts_ms 가 속한 일봉 regime."""
    if not regime_map: return "?"
    sorted_ots = sorted(regime_map.keys())
    for i in range(len(sorted_ots) - 1, -1, -1):
        if sorted_ots[i] <= ts_ms:
            return regime_map[sorted_ots[i]]
    return regime_map[sorted_ots[0]]


def kst_hour_bucket(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=KST)
    h = dt.hour
    if h < 6: return "00-06 KST"
    if h < 12: return "06-12 KST"
    if h < 18: return "12-18 KST"
    return "18-24 KST"


def stats(lst: list[dict]) -> dict:
    if not lst: return {"n": 0}
    rrs_raw = [e["realistic"]["rr"] for e in lst]
    rrs = [r for r in rrs_raw if r != float("inf") and r < 100]
    mfes = [e["realistic"]["mfe"] for e in lst]
    maes = [e["realistic"]["mae"] for e in lst]
    win = sum(1 for e in lst if e["realistic"]["mfe"] > abs(e["realistic"]["mae"]))
    sl = sum(1 for e in lst if e["realistic"]["sl_hit"])
    tp1 = sum(1 for e in lst if e["realistic"]["mfe"] >= 1.236)
    return {
        "n": len(lst),
        "rr_med": round(median(rrs), 2) if rrs else 0,
        "rr_mean": round(mean(rrs), 2) if rrs else 0,
        "mfe_med": round(median(mfes), 2),
        "mae_med": round(median(maes), 2),
        "win_pct": round(100 * win / len(lst), 1),
        "sl_pct": round(100 * sl / len(lst), 1),
        "tp1_pct": round(100 * tp1 / len(lst), 1),
    }


def main() -> int:
    print("=== 롱/숏 + Regime + 시간대 + 종목 분기 백테스트 ===\n")

    # 1) 백테스트 실행
    bot.PRE_ALERT_TIMEOUT_BARS = 8
    bot.SYMBOLS = list(SYMBOLS)
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    c = Collector()
    bot.send_telegram = c.send

    klines_cache = {}
    for symbol in SYMBOLS:
        k15 = load(symbol, "15m"); k4h = load(symbol, "4h"); k1d = load(symbol, "1d")
        klines_cache[symbol] = k15
        bot.STATE[symbol] = bot.SymbolState()
        bot.RSI_STATE[symbol] = RSISymbolState()
        bot.SERIES_15M[symbol] = deque(maxlen=200)
        bot.SERIES_4H[symbol] = deque(maxlen=100)
        bot.SERIES_1D[symbol] = deque(maxlen=50)
        bot.ISOLATED_SR_CACHE[symbol] = []
        for k in k15[:50]: bot.SERIES_15M[symbol].append(k)
        for k in k4h[:8]: bot.SERIES_4H[symbol].append(k)
        for k in k1d[:10]: bot.SERIES_1D[symbol].append(k)
        last_4h_ot = bot.SERIES_4H[symbol][-1]["open_time"]
        last_1d_ot = bot.SERIES_1D[symbol][-1]["open_time"]
        i4 = sum(1 for k in k4h if k["open_time"] <= last_4h_ot)
        i1d = sum(1 for k in k1d if k["open_time"] <= last_1d_ot)
        for i in range(50, len(k15)):
            kk = k15[i]
            bot.SERIES_15M[symbol].append(kk)
            while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
                bot.SERIES_4H[symbol].append(k4h[i4]); i4 += 1
            while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
                bot.SERIES_1D[symbol].append(k1d[i1d]); i1d += 1
            atr = bot.calc_atr(list(bot.SERIES_15M[symbol]))
            c.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr,
                  bar_ts=kk["close_time"])
            try: bot.evaluate_symbol_15m(symbol)
            except: pass

    # 2) realistic verify
    for ev in c.events:
        v = verify_realistic(ev, klines_cache[ev["symbol"]], ev["symbol"])
        if v: ev["realistic"] = v
    events = [e for e in c.events if "realistic" in e]
    print(f"총 진입: {len(events)}\n")

    # 3) BTC regime 계산
    btc_1d = load("BTCUSDT", "1d")
    regime_map = get_btc_regime(btc_1d)

    def print_table(title: str, groups: dict):
        print("=" * 100)
        print(f"【 {title} 】")
        print(f"{'그룹':>20} | {'N':>5} | {'RR 중앙':>8} | {'RR 평균':>8} | {'MFE 중앙':>9} | {'Win%':>6} | {'SL%':>6} | {'TP1%':>6}")
        print("-" * 100)
        sorted_groups = sorted(groups.items(), key=lambda x: -stats(x[1]).get("rr_med", 0))
        for g, lst in sorted_groups:
            s = stats(lst)
            if s["n"] < 5: continue
            star = " ★" if s["rr_med"] > 1.3 else ("" if s["rr_med"] > 1.0 else " ↓")
            print(f"{g:>20} | {s['n']:>5} | {s['rr_med']:>7.2f}{star} | {s['rr_mean']:>7.2f} | +{s['mfe_med']:>6.2f}% | {s['win_pct']:>5.1f}% | {s['sl_pct']:>5.1f}% | {s['tp1_pct']:>5.1f}%")
        print("=" * 100)

    # 4) 차원별 그루핑
    # (a) 방향
    by_dir = defaultdict(list)
    for e in events:
        by_dir[e["direction"]].append(e)
    print_table("방향 (Long vs Short)", by_dir)
    print()

    # (b) Regime
    by_regime = defaultdict(list)
    for e in events:
        r = regime_at(e["bar_ts"], regime_map)
        by_regime[r].append(e)
    print_table("BTC Regime (EMA50 vs EMA200, ±5% 임계)", by_regime)
    print()

    # (c) 방향 × Regime (가장 가치 있을 가능성)
    by_dir_regime = defaultdict(list)
    for e in events:
        r = regime_at(e["bar_ts"], regime_map)
        by_dir_regime[f"{e['direction']} / {r}"].append(e)
    print_table("방향 × Regime 교차", by_dir_regime)
    print()

    # (d) 시간대 (KST)
    by_hour = defaultdict(list)
    for e in events:
        by_hour[kst_hour_bucket(e["bar_ts"])].append(e)
    print_table("KST 시간대", by_hour)
    print()

    # (e) 종목
    by_sym = defaultdict(list)
    for e in events:
        by_sym[e["symbol"]].append(e)
    print_table("종목", by_sym)
    print()

    # (f) 방향 × 종목
    by_ds = defaultdict(list)
    for e in events:
        by_ds[f"{e['direction']} / {e['symbol']}"].append(e)
    print_table("방향 × 종목", by_ds)

    # 저장
    out = {
        "by_direction": {k: stats(v) for k, v in by_dir.items()},
        "by_regime": {k: stats(v) for k, v in by_regime.items()},
        "by_dir_regime": {k: stats(v) for k, v in by_dir_regime.items()},
        "by_hour_kst": {k: stats(v) for k, v in by_hour.items()},
        "by_symbol": {k: stats(v) for k, v in by_sym.items()},
        "by_dir_symbol": {k: stats(v) for k, v in by_ds.items()},
    }
    out_path = ROOT / "backtest_data" / "regime_analysis.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n저장: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
