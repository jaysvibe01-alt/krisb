"""레버리지/포지션 사이즈별 실제 자본 수익률 — 봇 게이트 적용 후.

R 단위 → 실제 % 수익으로 변환.

핵심:
  거래당 자본 손실 (SL 시) = 포지션 비율 × 손절 거리 × 레버리지
  여기서:
    포지션 비율 = 자본 중 한 거래에 투입한 비율 (예: 자본의 50%)
    손절 거리 = 진입가 대비 SL 가격 거리 (보통 BTC 0.5~1%, 알트 1~2%)
    레버리지 = 5x, 10x, 20x ...

  거래당 리스크 R = (자본 손실 %) / 100

  실제 수익 % = 연R × R(자본%)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import median
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import verify_user_model, Collector, load

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def run_symbol(symbol):
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
    c = Collector()
    bot.send_telegram = c.send

    k15 = load(symbol, "15m"); k4h = load(symbol, "4h"); k1d = load(symbol, "1d")
    if not k15 or not k4h or not k1d:
        return [], 0
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
        c.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try: bot.evaluate_symbol_15m(symbol)
        except Exception: pass

    entered = []
    sl_distances = []
    for ev in c.events:
        v = verify_user_model(ev, k15, symbol, realistic=True)
        if not v or not v.get("entered"):
            continue
        entered.append({"direction": ev["direction"], "v": v})
        # 손절가 거리 계산 (entry vs sl)
        if "sl" in v and "entry" in v:
            dist_pct = abs(v["entry"] - v["sl"]) / v["entry"] * 100
            sl_distances.append(dist_pct)

    days = (k15[-1]["close_time"] - k15[50]["close_time"]) / (1000 * 86400)
    return entered, days, sl_distances


def expectancy(events, model="median"):
    if not events:
        return 0
    n = len(events)
    rrs = [e["v"]["rr"] for e in events if e["v"]["rr"] != float("inf") and e["v"]["rr"] < 100]
    rr_med = median(rrs) if rrs else 0
    sl_pct = sum(1 for e in events if e["v"]["sl_hit"]) / n
    tp1_pct = sum(1 for e in events if e["v"]["tp1_hit"]) / n
    tp2_pct = sum(1 for e in events if e["v"]["tp2_hit"]) / n

    if model == "median":
        return sl_pct * (-1.0) + (1 - sl_pct) * rr_med
    elif model == "tiered":
        tp2_only = tp2_pct
        tp1_only = max(0, tp1_pct - tp2_pct)
        nothing = max(0, 1 - sl_pct - tp1_pct)
        return sl_pct * (-1.0) + tp1_only * 1.0 + tp2_only * 2.0 + nothing * 0.0
    return 0


def main():
    print("=== 레버리지 적용 실제 자본 수익률 — 봇 게이트 적용 후 ===\n")

    # 종목별 R 단위 결과
    sym_results = {}
    total_annual_n = 0
    total_annual_r_med = 0
    total_annual_r_tier = 0
    avg_sl_distance = []
    for sym in SYMBOLS:
        ev, days, sl_dists = run_symbol(sym)
        if not ev:
            continue
        e_med = expectancy(ev, "median")
        e_tier = expectancy(ev, "tiered")
        annual_n = len(ev) / days * 365
        annual_r_med = e_med * annual_n
        annual_r_tier = e_tier * annual_n
        med_sl_dist = median(sl_dists) if sl_dists else 0
        sym_results[sym] = {
            "n": len(ev),
            "annual_n": annual_n,
            "med_sl_dist_pct": med_sl_dist,
            "annual_r_med": annual_r_med,
            "annual_r_tier": annual_r_tier,
        }
        total_annual_n += annual_n
        total_annual_r_med += annual_r_med
        total_annual_r_tier += annual_r_tier
        avg_sl_distance.extend(sl_dists)

    overall_med_sl = median(avg_sl_distance) if avg_sl_distance else 0.7

    print(f"📐 평균 손절 거리 (진입가 대비):")
    print(f"{'종목':>10} | {'중앙 손절거리':>13} | {'1년 거래수':>10} | {'연R(보수)':>9} | {'연R(tiered)':>11}")
    print("-" * 75)
    for sym, r in sym_results.items():
        print(f"{sym:>10} | {r['med_sl_dist_pct']:>12.2f}% | {r['annual_n']:>10.1f} | {r['annual_r_med']:>+8.2f}R | {r['annual_r_tier']:>+10.2f}R")
    print("-" * 75)
    print(f"{'전체 중앙':>10} | {overall_med_sl:>12.2f}% | {total_annual_n:>10.1f} | {total_annual_r_med:>+8.2f}R | {total_annual_r_tier:>+10.2f}R")
    print()

    # 시나리오:
    # 1) 단순 1배 (현물식): 자본 100% 투입, 레버리지 없음
    # 2) 5배 레버리지, 포지션 자본의 50% (보수)
    # 3) 10배 레버리지, 포지션 자본의 50% (Kris 표준)
    # 4) 20배 레버리지, 포지션 자본의 50% (공격적)
    # 5) Risk-fixed 1%: 자본의 1% 만 거래당 잃도록 (레버리지 자동)

    print("💰 시나리오별 1년 자본 수익률\n")
    scenarios = [
        ("1배 (현물식)",        1.0, 1.00),
        ("5배 × 자본50%",       5.0, 0.50),
        ("10배 × 자본50% (Kris표준)", 10.0, 0.50),
        ("20배 × 자본50% (공격)",  20.0, 0.50),
        ("Risk-Fixed 0.5%",   None, None),  # 거래당 자본의 0.5% 손실로 고정
        ("Risk-Fixed 1.0%",   None, None),
        ("Risk-Fixed 2.0%",   None, None),
    ]

    print(f"{'시나리오':>22} | {'SL시 자본손실':>12} | {'연수익(보수)':>11} | {'연수익(tiered)':>13} | {'예상 MaxDD':>11}")
    print("-" * 90)

    # MaxDD 추정: 연속 SL 5회 가정
    consec_sl = 5

    for name, lev, pos_frac in scenarios:
        if lev is not None:
            # 레버리지 모드
            risk_per_trade = pos_frac * overall_med_sl * lev  # 자본 % 손실 per SL
        else:
            # Risk-fixed 모드
            risk_per_trade = {"Risk-Fixed 0.5%": 0.5, "Risk-Fixed 1.0%": 1.0, "Risk-Fixed 2.0%": 2.0}[name]

        ret_med = total_annual_r_med * risk_per_trade
        ret_tier = total_annual_r_tier * risk_per_trade
        max_dd_est = risk_per_trade * consec_sl  # 연속 5회 SL 시
        print(f"{name:>22} | {risk_per_trade:>11.2f}% | {ret_med:>+10.1f}% | {ret_tier:>+12.1f}% | {max_dd_est:>10.1f}%")
    print("=" * 90)

    print("\n📌 해석:")
    print(f"  - 평균 손절 거리: {overall_med_sl:.2f}% (BTC/ETH/SOL/XRP 진입가 대비)")
    print(f"  - 1년 거래 빈도: 약 {total_annual_n:.0f}회 ({total_annual_n/12:.1f}회/월)")
    print(f"  - 'Risk-Fixed' 모델: 거래당 자본 손실율을 고정 → 레버리지는 자동 (손절거리에 반비례)")
    print(f"  - MaxDD 는 연속 SL 5회 단순 추정. 실제로는 시퀀스/회복 효과로 다름.")
    print(f"  - Slippage/Funding 미반영. 실수익은 -10~25% 감소 예상.")

    print("\n🎯 Kris 본인 표준 (10배 × 자본 50%) 기준:")
    name = "10배 × 자본50% (Kris표준)"
    risk = 0.50 * overall_med_sl * 10
    ret_med = total_annual_r_med * risk
    ret_tier = total_annual_r_tier * risk
    print(f"   SL 1회 = -{risk:.1f}% 자본 손실")
    print(f"   1년 보수 시나리오  : {ret_med:+.0f}%")
    print(f"   1년 tiered 시나리오: {ret_tier:+.0f}%")
    print(f"   실제 (slippage 반영, 0.8 배수): {ret_med*0.8:+.0f}% ~ {ret_tier*0.8:+.0f}%")


if __name__ == "__main__":
    main()
