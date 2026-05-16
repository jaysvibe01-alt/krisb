"""BTC 만 정밀 분석 — Long/BTC RR 0.47 의 진짜 원인 추적.

가설: 단순 Long/BTC 가 전체적으로 나쁜 게 아니라, 특정 컨디션에서만 나쁘다.
  - 시간대 (KST 0-6 vs 6-12 vs 12-18 vs 18-24)
  - Regime (BTC 일봉 추세 bull/bear/sideways)
  - 부스트 조합 (어떤 부스트가 떴을 때 BTC 가 작동)
  - RSI / ATR / 거래량 수준
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

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import verify_user_model, Collector, load, SYMBOLS

KST = timezone(timedelta(hours=9))

BOOST_KEYWORDS = {"흡수 누적":"ABSORB","다이버전스":"DIVER","SR Flip":"SRFLIP",
                  "크보나치":"KBONA","일봉 4분할":"QUART","고립 반전":"ISOSR",
                  "과매도 컨플루언스":"HTF_OS","과매수 컨플루언스":"HTF_OB",
                  "신저가 갱신":"BREAK_LO","신고가 갱신":"BREAK_HI","꼬리 50":"WICK50"}


def extract_boosts(text):
    return frozenset(code for kw, code in BOOST_KEYWORDS.items() if kw in text)


def calc_ema(values, period):
    if not values: return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def get_btc_regime(btc_1d_klines):
    closes = [k["close"] for k in btc_1d_klines]
    if len(closes) < 200: return {}
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    out = {}
    for i, k in enumerate(btc_1d_klines):
        if i < 200:
            out[k["open_time"]] = "warmup"
            continue
        ratio = (ema50[i] - ema200[i]) / ema200[i]
        if ratio > 0.05: out[k["open_time"]] = "bull"
        elif ratio < -0.05: out[k["open_time"]] = "bear"
        else: out[k["open_time"]] = "sideways"
    return out


def regime_at(ts_ms, regime_map):
    if not regime_map: return "?"
    sorted_ots = sorted(regime_map.keys())
    for i in range(len(sorted_ots) - 1, -1, -1):
        if sorted_ots[i] <= ts_ms:
            return regime_map[sorted_ots[i]]
    return "?"


def kst_hour(ts_ms):
    h = datetime.fromtimestamp(ts_ms/1000, tz=KST).hour
    if h < 6: return "00-06"
    if h < 12: return "06-12"
    if h < 18: return "12-18"
    return "18-24"


def stats(lst):
    if not lst: return None
    rrs_raw = [e["realistic"]["rr"] for e in lst]
    rrs = [r for r in rrs_raw if r != float("inf") and r < 100]
    mfes = [e["realistic"]["mfe"] for e in lst]
    win = sum(1 for e in lst if e["realistic"]["mfe"] > abs(e["realistic"]["mae"]))
    sl = sum(1 for e in lst if e["realistic"]["sl_hit"])
    tp1 = sum(1 for e in lst if e["realistic"]["tp1_hit"])
    return {
        "n": len(lst),
        "rr_med": round(median(rrs), 2) if rrs else 0,
        "rr_mean": round(mean(rrs), 2) if rrs else 0,
        "mfe_med": round(median(mfes), 2),
        "win_pct": round(100*win/len(lst), 1),
        "sl_pct": round(100*sl/len(lst), 1),
        "tp1_pct": round(100*tp1/len(lst), 1),
    }


def main():
    print("=== BTC 만 정밀 분석 (Long/BTC RR 0.47 원인 추적) ===\n")

    # 1) 백테스트 (BTC 만)
    bot.PRE_ALERT_TIMEOUT_BARS = 8
    bot.SYMBOLS = ["BTCUSDT"]
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    c = Collector()
    bot.send_telegram = c.send

    k15 = load("BTCUSDT", "15m"); k4h = load("BTCUSDT", "4h"); k1d = load("BTCUSDT", "1d")
    bot.STATE["BTCUSDT"] = bot.SymbolState()
    bot.RSI_STATE["BTCUSDT"] = RSISymbolState()
    bot.SERIES_15M["BTCUSDT"] = deque(maxlen=200)
    bot.SERIES_4H["BTCUSDT"] = deque(maxlen=100)
    bot.SERIES_1D["BTCUSDT"] = deque(maxlen=50)
    bot.ISOLATED_SR_CACHE["BTCUSDT"] = []
    for k in k15[:50]: bot.SERIES_15M["BTCUSDT"].append(k)
    for k in k4h[:8]: bot.SERIES_4H["BTCUSDT"].append(k)
    for k in k1d[:10]: bot.SERIES_1D["BTCUSDT"].append(k)
    last_4h_ot = bot.SERIES_4H["BTCUSDT"][-1]["open_time"]
    last_1d_ot = bot.SERIES_1D["BTCUSDT"][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h_ot)
    i1d = sum(1 for k in k1d if k["open_time"] <= last_1d_ot)
    for i in range(50, len(k15)):
        kk = k15[i]
        bot.SERIES_15M["BTCUSDT"].append(kk)
        while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
            bot.SERIES_4H["BTCUSDT"].append(k4h[i4]); i4 += 1
        while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
            bot.SERIES_1D["BTCUSDT"].append(k1d[i1d]); i1d += 1
        atr = bot.calc_atr(list(bot.SERIES_15M["BTCUSDT"]))
        c.set(symbol="BTCUSDT", bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try: bot.evaluate_symbol_15m("BTCUSDT")
        except: pass

    for ev in c.events:
        v = verify_user_model(ev, k15, "BTCUSDT", realistic=True)
        if v: ev["realistic"] = v
    events = [e for e in c.events if "realistic" in e and e["realistic"].get("entered")]
    print(f"BTC 진입 N: {len(events)}\n")

    # BTC regime
    regime_map = get_btc_regime(k1d)

    def print_table(title, groups):
        print("=" * 95)
        print(f"【 {title} 】")
        print(f"{'그룹':>25} | {'N':>4} | {'RR 중앙':>7} | {'MFE 중앙':>9} | {'Win%':>6} | {'SL%':>6} | {'TP1%':>6}")
        print("-" * 95)
        rows = [(g, lst, stats(lst)) for g, lst in groups.items() if stats(lst) and stats(lst)["n"] >= 3]
        for g, lst, s in sorted(rows, key=lambda x: -x[2]["rr_med"]):
            star = " ★★" if s["rr_med"] > 1.5 else (" ★" if s["rr_med"] > 1.2 else (" ↓↓" if s["rr_med"] < 0.5 else (" ↓" if s["rr_med"] < 0.8 else "")))
            print(f"{g:>25} | {s['n']:>4} | {s['rr_med']:>6.2f}{star} | +{s['mfe_med']:>6.2f}% | {s['win_pct']:>5.1f}% | {s['sl_pct']:>5.1f}% | {s['tp1_pct']:>5.1f}%")
        print("=" * 95)
        print()

    # A) 방향
    by_dir = defaultdict(list)
    for e in events:
        by_dir[e["direction"]].append(e)
    print_table("BTC 방향 (Long vs Short)", by_dir)

    # B) Regime
    by_regime = defaultdict(list)
    for e in events:
        r = regime_at(e["bar_ts"], regime_map)
        by_regime[r].append(e)
    print_table("BTC × Regime", by_regime)

    # C) 방향 × Regime (가장 결정적)
    by_dr = defaultdict(list)
    for e in events:
        r = regime_at(e["bar_ts"], regime_map)
        by_dr[f"{e['direction']} / {r}"].append(e)
    print_table("BTC 방향 × Regime ★", by_dr)

    # D) 시간대
    by_hr = defaultdict(list)
    for e in events:
        by_hr[kst_hour(e["bar_ts"])].append(e)
    print_table("BTC 시간대 (KST)", by_hr)

    # E) 방향 × 시간대
    by_dh = defaultdict(list)
    for e in events:
        by_dh[f"{e['direction']} / {kst_hour(e['bar_ts'])}"].append(e)
    print_table("BTC 방향 × 시간대 ★", by_dh)

    # F) 부스트 조합 (최소 5건 이상)
    by_boost = defaultdict(list)
    for e in events:
        boosts = extract_boosts(e["text"])
        key = "+".join(sorted(boosts - {"KBONA", "SRFLIP"})) or "(only KBONA+SRFLIP)"
        by_boost[key].append(e)
    print_table("BTC 부스트 조합 (KBONA·SRFLIP 제외)", by_boost)

    # G) RSI 수준
    by_rsi = defaultdict(list)
    for e in events:
        # 진입 시 ev["close"] = 컨펌봉 close. RSI 알 수 없음 — bar_idx 로 시리즈에서 재계산
        idx = e["bar_idx"]
        if idx < 14: continue
        closes = [k15[i]["close"] for i in range(max(0, idx-50), idx+1)]
        rsi = bot.calc_rsi(closes)
        bucket = ("RSI<25" if rsi < 25 else
                  "RSI 25-30" if rsi < 30 else
                  "RSI 30-35" if rsi < 35 else
                  "RSI 35-40" if rsi < 40 else
                  "RSI 40-50" if rsi < 50 else
                  "RSI 50-60" if rsi < 60 else
                  "RSI 60-65" if rsi < 65 else
                  "RSI 65-70" if rsi < 70 else
                  "RSI ≥70")
        by_rsi[bucket].append(e)
    print_table("BTC × RSI 구간", by_rsi)

    # 종합 저장
    out = {
        "n_total": len(events),
        "by_direction": {k: stats(v) for k, v in by_dir.items()},
        "by_regime": {k: stats(v) for k, v in by_regime.items()},
        "by_dir_regime": {k: stats(v) for k, v in by_dr.items()},
        "by_hour": {k: stats(v) for k, v in by_hr.items()},
        "by_dir_hour": {k: stats(v) for k, v in by_dh.items()},
        "by_boost_combo": {k: stats(v) for k, v in by_boost.items() if len(v) >= 5},
        "by_rsi_bucket": {k: stats(v) for k, v in by_rsi.items()},
    }
    out_path = ROOT / "backtest_data" / "btc_deep_analysis.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_path}")


if __name__ == "__main__":
    main()
