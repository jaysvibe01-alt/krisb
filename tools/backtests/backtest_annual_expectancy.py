"""1년 기대수익 계산 — 전체 종목, 모든 게이트 적용 후.

봇 코드 그대로 돌려서 각 종목별:
  - 거래 횟수 (1년)
  - SL hit%, TP1%, TP2%
  - 거래당 기대 R (보수적/낙관적)
  - 연간 기대 R

자본 리스크 정책 (Kris 방식):
  - 거래당 리스크 = R = 1R = 손절가까지의 거리 (자본의 1% 가정)
  - 자본의 1% 리스크/거래 → 1R = 1% 자본
  - 자본의 0.5% 리스크/거래 → 1R = 0.5% 자본 (보수적)

기대값 모델:
  P&L per trade = SL% × (-1R) + (1-SL%) × E[non-SL R]
  여기서 E[non-SL R] 은 RR 중앙값을 보수적으로 사용 (mean 은 outlier 영향 큼)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import median, mean
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import verify_user_model, Collector, load

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def run_symbol(symbol):
    """봇 그대로 돌려서 한 종목 진입 수집 (모든 게이트 ON)."""
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
    # 게이트 ON 확인
    bot.BTC_TIME_GATE_ENABLED = True
    bot.BTC_CT_GATE_ENABLED = True
    c = Collector()
    bot.send_telegram = c.send

    k15 = load(symbol, "15m")
    k4h = load(symbol, "4h")
    k1d = load(symbol, "1d")
    if not k15 or not k4h or not k1d:
        return [], 0

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
        c.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try:
            bot.evaluate_symbol_15m(symbol)
        except Exception:
            pass

    entered = []
    for ev in c.events:
        v = verify_user_model(ev, k15, symbol, realistic=True)
        if v and v.get("entered"):
            entered.append({"direction": ev["direction"], "v": v, "text": ev.get("text", "")})

    # 1년 기간 측정
    days = (k15[-1]["close_time"] - k15[50]["close_time"]) / (1000 * 86400)
    return entered, days


def expectancy_per_trade(events, model="median"):
    """거래당 기대 R 계산.

    model="median": E = SL% × (-1) + (1-SL%) × RR_median   (보수적)
    model="mean"  : E = SL% × (-1) + (1-SL%) × RR_mean     (낙관적, outlier 영향)
    model="tiered": SL% × (-1) + TP2_only% × 2 + TP1_only% × 1 + 나머지 × 0  (실현 청산)
    """
    if not events:
        return 0, 0
    n = len(events)
    rrs = [e["v"]["rr"] for e in events if e["v"]["rr"] != float("inf") and e["v"]["rr"] < 100]
    rr_med = median(rrs) if rrs else 0
    rr_mean = mean(rrs) if rrs else 0
    sl_pct = sum(1 for e in events if e["v"]["sl_hit"]) / n
    tp1_pct = sum(1 for e in events if e["v"]["tp1_hit"]) / n
    tp2_pct = sum(1 for e in events if e["v"]["tp2_hit"]) / n
    win_pct = sum(1 for e in events if e["v"]["mfe"] > abs(e["v"]["mae"])) / n

    if model == "median":
        e_r = sl_pct * (-1.0) + (1 - sl_pct) * rr_med
    elif model == "mean":
        e_r = sl_pct * (-1.0) + (1 - sl_pct) * rr_mean
    elif model == "tiered":
        tp2_only = tp2_pct  # TP2 hit 거래는 +2R 가정
        tp1_only = max(0, tp1_pct - tp2_pct)  # TP1만 hit
        nothing = max(0, 1 - sl_pct - tp1_pct)  # SL 도 TP1 도 안 맞음 → 0R 가정
        e_r = sl_pct * (-1.0) + tp1_only * 1.0 + tp2_only * 2.0 + nothing * 0.0
    else:
        e_r = 0

    return e_r, {
        "n": n,
        "rr_med": round(rr_med, 2),
        "rr_mean": round(rr_mean, 2),
        "win_pct": round(100 * win_pct, 1),
        "sl_pct": round(100 * sl_pct, 1),
        "tp1_pct": round(100 * tp1_pct, 1),
        "tp2_pct": round(100 * tp2_pct, 1),
        "e_r": round(e_r, 3),
    }


def main():
    print("=== 1년 기대수익 계산 — 전체 종목, 모든 게이트 적용 후 ===\n")
    all_events = []
    rows = []
    for sym in SYMBOLS:
        ev, days = run_symbol(sym)
        all_events.extend(ev)
        if not ev:
            print(f"  {sym}: 진입 없음")
            continue
        e_med, stats_med = expectancy_per_trade(ev, "median")
        e_mean, _ = expectancy_per_trade(ev, "mean")
        e_tier, _ = expectancy_per_trade(ev, "tiered")
        annual_n = round(stats_med["n"] / days * 365, 1) if days else 0
        annual_r_med = round(e_med * annual_n, 2)
        annual_r_mean = round(e_mean * annual_n, 2)
        annual_r_tier = round(e_tier * annual_n, 2)
        rows.append({
            "sym": sym,
            "days": round(days, 0),
            "n": stats_med["n"],
            "annual_n": annual_n,
            "rr_med": stats_med["rr_med"],
            "sl_pct": stats_med["sl_pct"],
            "win_pct": stats_med["win_pct"],
            "e_med": round(e_med, 3),
            "e_mean": round(e_mean, 3),
            "e_tier": round(e_tier, 3),
            "annual_r_med": annual_r_med,
            "annual_r_mean": annual_r_mean,
            "annual_r_tier": annual_r_tier,
        })

    print("=" * 115)
    print(f"{'종목':>10} | {'기간일':>5} | {'N':>4} | {'연N':>5} | {'RR중앙':>6} | {'SL%':>5} | {'Win%':>5} | {'E중앙':>6} | {'E기단':>6} | {'연R중앙':>7} | {'연R기단':>7}")
    print("-" * 115)
    total_n = 0
    total_annual_n = 0
    total_r_med = 0
    total_r_mean = 0
    total_r_tier = 0
    for r in rows:
        print(f"{r['sym']:>10} | {r['days']:>5.0f} | {r['n']:>4} | {r['annual_n']:>5.1f} | {r['rr_med']:>5.2f} | {r['sl_pct']:>4.1f}% | {r['win_pct']:>4.1f}% | {r['e_med']:>+6.3f} | {r['e_tier']:>+6.3f} | {r['annual_r_med']:>+7.2f}R | {r['annual_r_tier']:>+7.2f}R")
        total_n += r["n"]
        total_annual_n += r["annual_n"]
        total_r_med += r["annual_r_med"]
        total_r_mean += r["annual_r_mean"]
        total_r_tier += r["annual_r_tier"]
    print("-" * 115)
    print(f"{'합계':>10} |       | {total_n:>4} | {total_annual_n:>5.1f} |        |       |       |        |        | {total_r_med:>+7.2f}R | {total_r_tier:>+7.2f}R")
    print("=" * 115)

    print("\n📊 1년 기대수익 (자본 대비):")
    for risk_pct in [0.25, 0.5, 1.0, 2.0]:
        ret_med = total_r_med * risk_pct
        ret_tier = total_r_tier * risk_pct
        print(f"  거래당 리스크 {risk_pct:>4.2f}% : 보수적(중앙) {ret_med:>+6.1f}% / 실현청산(tiered) {ret_tier:>+6.1f}%")

    print("\n주: ")
    print("  - '보수적(중앙)' = SL hit 시 -1R, 그 외 모두 RR 중앙값으로 청산 가정")
    print("  - '실현청산(tiered)' = SL hit 시 -1R, TP2 hit 시 +2R, TP1만 hit 시 +1R, 나머지 0R")
    print("  - Slippage/funding 미반영. 실제는 -10~20% 수익 감소 예상.")
    print("  - 게이트로 거래 빈도 감소 → 분산 효과 줄어듦 (월 1-3건 BTC, 월 3-5건 알트)")

    out_path = ROOT / "backtest_data" / "annual_expectancy.json"
    out_path.write_text(json.dumps({"rows": rows, "total_r_med": total_r_med, "total_r_tier": total_r_tier}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
