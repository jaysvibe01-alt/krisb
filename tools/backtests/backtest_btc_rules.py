"""BTC 만의 정밀 크트키 룰 — 5가지 비교 백테스트.

BTC 가 시장 중심 → BTC 진입 자리를 잘 맞추면 나머지 종목도 따라옴.
방금 정밀 분석에서 발견:
  - 기존 룰 (RSI≤30 모든 시간대): Long RR 0.46 (catching falling knife)
  - 06-12 KST: Long RR 1.37, Win 80% ★
  - RSI 50-60 구간: RR 1.53, Win 68%
  - 다이버+4분할: RR 7.64, Win 80%

5가지 룰 비교:
  R1 — 기존 (RSI 30/70, 모든 시간, 모든 부스트)         BASELINE
  R2 — RSI 30/70 + 06-12 KST 시간대만 (오전 필터)
  R3 — RSI 30/70 + Short only (BTC 의 best 방향)
  R4 — RSI 35-50 (모멘텀 추종 모드, 극단 RSI 회피)
  R5 — 다이버+4분할 컨플루언스 필수 (최강 부스트)
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
from backtest_user_model import verify_user_model, Collector, load

KST = timezone(timedelta(hours=9))

BOOST_KEYWORDS = {"흡수 누적":"ABSORB","다이버전스":"DIVER","SR Flip":"SRFLIP",
                  "크보나치":"KBONA","일봉 4분할":"QUART","고립 반전":"ISOSR",
                  "과매도 컨플루언스":"HTF_OS","과매수 컨플루언스":"HTF_OB"}


def extract_boosts(text):
    return frozenset(code for kw, code in BOOST_KEYWORDS.items() if kw in text)


def kst_hour(ts_ms):
    h = datetime.fromtimestamp(ts_ms/1000, tz=KST).hour
    if h < 6: return "00-06"
    if h < 12: return "06-12"
    if h < 18: return "12-18"
    return "18-24"


def run_btc_baseline():
    """기존 룰 (RSI 30/70) 로 BTC 백테스트 — 모든 진입 수집."""
    bot.PRE_ALERT_TIMEOUT_BARS = 8
    bot.RSI_OVERSOLD = 30
    bot.RSI_OVERBOUGHT = 70
    bot.SYMBOLS = ["BTCUSDT"]
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    c = Collector()
    bot.send_telegram = c.send

    k15 = load("BTCUSDT", "15m")
    k4h = load("BTCUSDT", "4h")
    k1d = load("BTCUSDT", "1d")
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

    # verify + enrich
    enriched = []
    for ev in c.events:
        v = verify_user_model(ev, k15, "BTCUSDT", realistic=True)
        if not v or not v.get("entered"): continue
        # RSI 계산 (진입 시점)
        idx = ev["bar_idx"]
        if idx < 14: continue
        closes = [k15[i]["close"] for i in range(max(0, idx-50), idx+1)]
        rsi = bot.calc_rsi(closes)
        boosts = extract_boosts(ev["text"])
        enriched.append({
            "direction": ev["direction"],
            "bar_ts": ev["bar_ts"],
            "bar_idx": ev["bar_idx"],
            "close": ev["close"],
            "atr": ev["atr"],
            "rsi_at_entry": rsi,
            "hour_kst": kst_hour(ev["bar_ts"]),
            "boosts": boosts,
            "realistic": v,
        })
    return enriched


def apply_rule(events, rule):
    """룰별 필터링 — 통과한 진입만 반환."""
    out = []
    for e in events:
        if rule == "R1":   # 기존 모든 진입
            out.append(e)
        elif rule == "R2":  # 06-12 KST 만
            if e["hour_kst"] == "06-12":
                out.append(e)
        elif rule == "R3":  # Short only
            if e["direction"] == "short":
                out.append(e)
        elif rule == "R4":  # RSI 35-50 모멘텀 (극단 RSI 회피)
            r = e["rsi_at_entry"]
            if e["direction"] == "long" and 35 <= r <= 50:
                out.append(e)
            elif e["direction"] == "short" and 50 <= r <= 65:
                out.append(e)
        elif rule == "R5":  # 다이버 + 4분할 필수
            if "DIVER" in e["boosts"] and "QUART" in e["boosts"]:
                out.append(e)
        elif rule == "R6":  # Short / 06-18 KST (best Short 시간대)
            if e["direction"] == "short" and e["hour_kst"] in ("06-12", "12-18"):
                out.append(e)
        elif rule == "R7":  # Long / 06-12 KST (best Long 시간대)
            if e["direction"] == "long" and e["hour_kst"] == "06-12":
                out.append(e)
        elif rule == "R8":  # R6 + R7 합집합 (시간대 best 조합)
            ok = ((e["direction"] == "short" and e["hour_kst"] in ("06-12", "12-18")) or
                  (e["direction"] == "long" and e["hour_kst"] == "06-12"))
            if ok: out.append(e)
    return out


def stats(lst):
    if not lst: return None
    rrs_raw = [e["realistic"]["rr"] for e in lst]
    rrs = [r for r in rrs_raw if r != float("inf") and r < 100]
    mfes = [e["realistic"]["mfe"] for e in lst]
    win = sum(1 for e in lst if e["realistic"]["mfe"] > abs(e["realistic"]["mae"]))
    sl = sum(1 for e in lst if e["realistic"]["sl_hit"])
    tp1 = sum(1 for e in lst if e["realistic"]["tp1_hit"])
    tp2 = sum(1 for e in lst if e["realistic"]["tp2_hit"])
    return {
        "n": len(lst),
        "rr_med": round(median(rrs), 2) if rrs else 0,
        "rr_mean": round(mean(rrs), 2) if rrs else 0,
        "mfe_med": round(median(mfes), 2),
        "win_pct": round(100*win/len(lst), 1),
        "sl_pct": round(100*sl/len(lst), 1),
        "tp1_pct": round(100*tp1/len(lst), 1),
        "tp2_pct": round(100*tp2/len(lst), 1),
    }


def main():
    print("=== BTC 만의 정밀 크트키 룰 — 8가지 비교 ===\n")
    events = run_btc_baseline()
    print(f"BTC baseline 진입: {len(events)}\n")

    rules = {
        "R1": "기존 (BASELINE) — RSI 30/70 + 모든 시간 + 모든 부스트",
        "R2": "06-12 KST 시간대만 (한국 오전 — 미국 마감 후)",
        "R3": "Short only (BTC best 방향)",
        "R4": "RSI 35-50/50-65 모멘텀 모드 (극단 RSI 회피)",
        "R5": "다이버 + 일봉4분할 필수 (최강 컨플루언스)",
        "R6": "Short + 06-18 KST (best Short 시간대)",
        "R7": "Long + 06-12 KST (best Long 시간대 — Kris 본인이 슬라이드 9·14·17 잡은 자리)",
        "R8": "R6 + R7 합집합 (시간대별 best 조합)",
    }

    results = {}
    print("=" * 100)
    print(f"{'룰':>4} | {'N':>4} | {'RR 중앙':>7} | {'RR 평균':>7} | {'MFE 중앙':>9} | {'Win%':>6} | {'SL%':>6} | {'TP1%':>6} | {'TP2%':>6}")
    print("-" * 100)
    for rule, desc in rules.items():
        filtered = apply_rule(events, rule)
        s = stats(filtered)
        results[rule] = {**(s or {"n": 0}), "desc": desc}
        if not s:
            print(f"{rule:>4} | {0:>4} | (표본 없음)")
            continue
        star = ""
        if s["rr_med"] > 1.5: star = " ★★"
        elif s["rr_med"] > 1.2: star = " ★"
        elif s["rr_med"] < 0.7: star = " ↓"
        print(f"{rule:>4} | {s['n']:>4} | {s['rr_med']:>6.2f}{star} | {s['rr_mean']:>6.2f} | +{s['mfe_med']:>6.2f}% | {s['win_pct']:>5.1f}% | {s['sl_pct']:>5.1f}% | {s['tp1_pct']:>5.1f}% | {s['tp2_pct']:>5.1f}%")
    print("=" * 100)

    print("\n룰 설명:")
    for rule, desc in rules.items():
        print(f"  {rule}: {desc}")

    # 최고 룰 강조
    best = max(results.items(), key=lambda x: x[1].get("rr_med", 0))
    print(f"\n🏆 최고 룰: {best[0]} (RR 중앙 {best[1].get('rr_med', 0)}, N={best[1].get('n', 0)})")
    print(f"   {best[1].get('desc', '')}")

    out_path = ROOT / "backtest_data" / "btc_rules_compare.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
