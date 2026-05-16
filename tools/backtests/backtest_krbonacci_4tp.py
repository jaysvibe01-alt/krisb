"""크보나치 4단계 익절 (1.236 / 2.0 / 2.544 / 3.236) 백테스트.

PPT 슬라이드 19 (2.544) + 슬라이드 39 (3.236) 비율 반영.
서브에이전트 #1 (06_누락자료) C-1 발견사항.

비교:
  기존: TP1 50% @ 1.236R / TP2 30% @ 2.0R / Trail 20% (avg 2.5R)
  신규: TP1 50% @ 1.236R / TP2 20% @ 2.0R / TP3 20% @ 2.544R / TP4 10% @ 3.236R
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from statistics import median, mean
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import Collector, load, SLIPPAGE_BPS, FUNDING_PER_8H_PCT, BARS_PER_FUNDING
from backtest_realistic_pnl import (
    SYMBOLS, MAX_HOLD_BARS, ENTRY_FEE_PCT, EXIT_FEE_PCT,
)
from backtest_compound_returns import compound_simulation

# 신규 4단계 분할 (PPT 크보나치 비율 반영)
TP1_R_NEW, TP1_RATIO_NEW = 1.236, 0.50
TP2_R_NEW, TP2_RATIO_NEW = 2.0,   0.20
TP3_R_NEW, TP3_RATIO_NEW = 2.544, 0.20   # 슬라이드 19
TP4_R_NEW, TP4_RATIO_NEW = 3.236, 0.10   # 슬라이드 39
SL_R = 1.0


def simulate_trade_4tp(entry_price, sl_price, direction, path_klines, symbol):
    """4단계 분할 청산 + 본전 SL + 비용 반영."""
    sl_d_pct = abs(entry_price - sl_price) / entry_price
    if direction == "long":
        tp1 = entry_price + sl_d_pct * entry_price * TP1_R_NEW
        tp2 = entry_price + sl_d_pct * entry_price * TP2_R_NEW
        tp3 = entry_price + sl_d_pct * entry_price * TP3_R_NEW
        tp4 = entry_price + sl_d_pct * entry_price * TP4_R_NEW
    else:
        tp1 = entry_price - sl_d_pct * entry_price * TP1_R_NEW
        tp2 = entry_price - sl_d_pct * entry_price * TP2_R_NEW
        tp3 = entry_price - sl_d_pct * entry_price * TP3_R_NEW
        tp4 = entry_price - sl_d_pct * entry_price * TP4_R_NEW

    remaining = 1.0
    realized_r = 0.0
    exit_path = []
    funding_acc = 0.0
    tp1_hit = tp2_hit = tp3_hit = tp4_hit = False
    hold_bars = 0

    for bar_idx, k in enumerate(path_klines):
        if remaining <= 1e-9:
            break
        hold_bars = bar_idx + 1
        if bar_idx > 0 and bar_idx % BARS_PER_FUNDING == 0:
            funding_acc += FUNDING_PER_8H_PCT

        h, lo = k["high"], k["low"]

        if direction == "long":
            sl_breached = lo <= sl_price
            tp1_reached = h >= tp1 and not tp1_hit
            tp2_reached = h >= tp2 and tp1_hit and not tp2_hit
            tp3_reached = h >= tp3 and tp2_hit and not tp3_hit
            tp4_reached = h >= tp4 and tp3_hit and not tp4_hit
        else:
            sl_breached = h >= sl_price
            tp1_reached = lo <= tp1 and not tp1_hit
            tp2_reached = lo <= tp2 and tp1_hit and not tp2_hit
            tp3_reached = lo <= tp3 and tp2_hit and not tp3_hit
            tp4_reached = lo <= tp4 and tp3_hit and not tp4_hit

        if sl_breached and (tp1_reached or tp2_reached or tp3_reached or tp4_reached):
            # SL 우선 (보수적)
            realized_r += remaining * (-SL_R)
            exit_path.append((bar_idx, -SL_R, remaining))
            remaining = 0.0
            break

        if tp1_reached:
            realized_r += TP1_RATIO_NEW * TP1_R_NEW
            exit_path.append((bar_idx, TP1_R_NEW, TP1_RATIO_NEW))
            remaining -= TP1_RATIO_NEW
            tp1_hit = True
        if tp2_reached:
            realized_r += TP2_RATIO_NEW * TP2_R_NEW
            exit_path.append((bar_idx, TP2_R_NEW, TP2_RATIO_NEW))
            remaining -= TP2_RATIO_NEW
            tp2_hit = True
        if tp3_reached:
            realized_r += TP3_RATIO_NEW * TP3_R_NEW
            exit_path.append((bar_idx, TP3_R_NEW, TP3_RATIO_NEW))
            remaining -= TP3_RATIO_NEW
            tp3_hit = True
        if tp4_reached:
            realized_r += TP4_RATIO_NEW * TP4_R_NEW
            exit_path.append((bar_idx, TP4_R_NEW, TP4_RATIO_NEW))
            remaining -= TP4_RATIO_NEW
            tp4_hit = True

        if sl_breached:
            sl_r_signed = 0.0 if tp1_hit else -SL_R  # TP1 hit 후 본전 SL
            realized_r += remaining * sl_r_signed
            exit_path.append((bar_idx, sl_r_signed, remaining))
            remaining = 0.0
            break

    # 강제 청산 — 잔여
    if remaining > 1e-9 and path_klines:
        last_close = path_klines[min(hold_bars - 1, len(path_klines) - 1)]["close"]
        if direction == "long":
            r_exit = (last_close - entry_price) / (entry_price * sl_d_pct)
        else:
            r_exit = (entry_price - last_close) / (entry_price * sl_d_pct)
        realized_r += remaining * r_exit
        exit_path.append((hold_bars, r_exit, remaining))

    # 비용
    slip_pct = SLIPPAGE_BPS.get(symbol, 5.0) / 100
    cost_fee_pct = ENTRY_FEE_PCT + EXIT_FEE_PCT
    cost_pct = slip_pct * 2 + cost_fee_pct + funding_acc
    cost_r = cost_pct / (sl_d_pct * 100)
    final_r = realized_r - cost_r

    return {
        "raw_r": round(realized_r, 3),
        "costs_r": round(cost_r, 3),
        "final_r": round(final_r, 3),
        "tp1_hit": tp1_hit, "tp2_hit": tp2_hit,
        "tp3_hit": tp3_hit, "tp4_hit": tp4_hit,
        "hold_bars": hold_bars,
    }


def collect_4tp(symbol):
    """봇 시그널 수집 + 4단계 청산 시뮬."""
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
    if not k15: return [], 0
    bot.STATE[symbol] = bot.SymbolState()
    bot.RSI_STATE[symbol] = RSISymbolState()
    bot.SERIES_15M[symbol] = deque(maxlen=200); bot.SERIES_4H[symbol] = deque(maxlen=100); bot.SERIES_1D[symbol] = deque(maxlen=50)
    bot.ISOLATED_SR_CACHE[symbol] = []
    for k in k15[:50]: bot.SERIES_15M[symbol].append(k)
    for k in k4h[:8]: bot.SERIES_4H[symbol].append(k)
    for k in k1d[:10]: bot.SERIES_1D[symbol].append(k)
    last_4h_ot = bot.SERIES_4H[symbol][-1]["open_time"]; last_1d_ot = bot.SERIES_1D[symbol][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h_ot); i1d = sum(1 for k in k1d if k["open_time"] <= last_1d_ot)
    for i in range(50, len(k15)):
        kk = k15[i]; bot.SERIES_15M[symbol].append(kk)
        while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
            bot.SERIES_4H[symbol].append(k4h[i4]); i4 += 1
        while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
            bot.SERIES_1D[symbol].append(k1d[i1d]); i1d += 1
        atr = bot.calc_atr(list(bot.SERIES_15M[symbol]))
        c.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try: bot.evaluate_symbol_15m(symbol)
        except Exception: pass

    enriched = []
    for ev in c.events:
        confirm_idx = ev["bar_idx"]
        if confirm_idx + 1 >= len(k15): continue
        confirm = k15[confirm_idx]; next_bar = k15[confirm_idx + 1]
        direction = ev["direction"]; atr = ev["atr"]
        if direction == "long":
            zone_low, zone_high = confirm["low"], confirm["close"]
        else:
            zone_low, zone_high = confirm["close"], confirm["high"]
        if next_bar["low"] > zone_high or next_bar["high"] < zone_low: continue
        entry_price = (zone_low + zone_high) / 2
        sl_d = atr * 1.5
        sl_price = entry_price - sl_d if direction == "long" else entry_price + sl_d
        path = k15[confirm_idx + 1:confirm_idx + 1 + MAX_HOLD_BARS]
        sim = simulate_trade_4tp(entry_price, sl_price, direction, path, symbol)
        sim["direction"] = direction; sim["bar_ts"] = ev["bar_ts"]
        enriched.append(sim)

    days = (k15[-1]["close_time"] - k15[50]["close_time"]) / (1000 * 86400)
    return enriched, days


from collections import deque


def main():
    print("=== 크보나치 4단계 익절 백테스트 (PPT 슬라이드 19·39 반영) ===\n")
    print("기존: TP1 50% @ 1.236R / TP2 30% @ 2.0R / Trail 20% (avg 2.5R)")
    print("신규: TP1 50% @ 1.236R / TP2 20% @ 2.0R / TP3 20% @ 2.544R / TP4 10% @ 3.236R\n")

    all_trades = []
    total_days = 0
    for sym in SYMBOLS:
        trades, days = collect_4tp(sym)
        for t in trades: t["symbol"] = sym
        all_trades.extend(trades)
        total_days = max(total_days, days)
        if trades:
            n = len(trades)
            avg_r = sum(t["final_r"] for t in trades) / n
            wins = sum(1 for t in trades if t["final_r"] > 0)
            tp3 = sum(1 for t in trades if t["tp3_hit"]); tp4 = sum(1 for t in trades if t["tp4_hit"])
            print(f"  {sym}: {n}거래, avg R {avg_r:+.3f}, Win {100*wins/n:.1f}%, TP3 {tp3} ({100*tp3/n:.1f}%), TP4 {tp4} ({100*tp4/n:.1f}%)")
    all_trades.sort(key=lambda x: x["bar_ts"])

    if not all_trades:
        print("거래 없음"); return

    n_total = len(all_trades)
    final_rs = [t["final_r"] for t in all_trades]
    avg_r = mean(final_rs)
    wins = sum(1 for r in final_rs if r > 0)
    total_r = sum(final_rs)

    print(f"\n📊 4단계 분할 전체:")
    print(f"   거래수: {n_total}/년")
    print(f"   avg R: {avg_r:+.3f}R")
    print(f"   합계 R: {total_r:+.2f}R")
    print(f"   Win%: {100*wins/n_total:.1f}%")
    print(f"   TP3 hit: {sum(1 for t in all_trades if t['tp3_hit'])} ({100*sum(1 for t in all_trades if t['tp3_hit'])/n_total:.1f}%)")
    print(f"   TP4 hit: {sum(1 for t in all_trades if t['tp4_hit'])} ({100*sum(1 for t in all_trades if t['tp4_hit'])/n_total:.1f}%)")

    print(f"\n💰 복리 시나리오:")
    for label, risk in [("1% (보수)", 1.0), ("3.5% (사용자)", 3.5)]:
        comp = compound_simulation(all_trades, risk)
        print(f"   {label:>15}: 복리 {comp['return_pct']:+.1f}%, 배수 {comp['multiplier']:.2f}×, MaxDD {comp['max_dd_pct']:.1f}%")

    # 기존 (3단계) 결과 비교
    print(f"\n=== 비교: 3단계 (기존) vs 4단계 (PPT 반영) ===")
    print("=" * 90)
    print(f"{'지표':>20} | {'3단계 (기존)':>14} | {'4단계 (PPT)':>14} | {'변화':>14}")
    print("-" * 90)
    OLD = {"n": 256, "avg_r": 0.418, "total_r": 106.73, "win_pct": 75.0,
           "comp_1": 187.2, "comp_3_5": 3457}
    rows = [
        ("거래수/년", OLD["n"], n_total),
        ("avg R", OLD["avg_r"], avg_r),
        ("합계 R/년", OLD["total_r"], total_r),
        ("Win%", OLD["win_pct"], 100*wins/n_total),
        ("복리 1%", OLD["comp_1"], compound_simulation(all_trades, 1.0)["return_pct"]),
        ("복리 3.5% (사용자)", OLD["comp_3_5"], compound_simulation(all_trades, 3.5)["return_pct"]),
    ]
    for label, old, new in rows:
        diff = new - old
        diff_pct = diff / old * 100 if old else 0
        print(f"{label:>20} | {old:>13.2f} | {new:>13.2f} | {diff:>+8.2f} ({diff_pct:>+5.0f}%)")
    print("=" * 90)


if __name__ == "__main__":
    main()
