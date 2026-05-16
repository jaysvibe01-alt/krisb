"""크트키 봇 — 4가지 미반영 룰 종합 적용 백테스트.

1. 크보나치 4단계 익절 (1.236 / 2.0 / 2.544 / 3.236) ✓
2. 트레일링 SL 동적 이동 (TP2 hit 후 직전 swing low 아래로) ✓
3. 단일 캔들 흡수 (슬라이드 13) — 진입 시그널 부스트 ✓
4. 분할 익절 모니터링 — 이미 4단계로 처리됨

기존 backtest_realistic_pnl 대비 효과 측정.
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
from backtest_user_model import Collector, load, SLIPPAGE_BPS, FUNDING_PER_8H_PCT, BARS_PER_FUNDING
from backtest_realistic_pnl import SYMBOLS, MAX_HOLD_BARS, ENTRY_FEE_PCT, EXIT_FEE_PCT
from backtest_compound_returns import compound_simulation

# 4단계 분할 (PPT 크보나치)
TP1_R, TP1_RATIO = 1.236, 0.50
TP2_R, TP2_RATIO = 2.0,   0.20
TP3_R, TP3_RATIO = 2.544, 0.20  # 슬라이드 19
TP4_R, TP4_RATIO = 3.236, 0.10  # 슬라이드 39
SL_R = 1.0

# 트레일링 SL — TP2 hit 후 동적 이동
TRAIL_SWING_LOOKBACK = 5  # 직전 5봉 swing low/high


def detect_single_absorb(klines: list[dict], direction: str) -> bool:
    """단일 캔들 흡수 (슬라이드 13).

    하나의 큰 봉이 매도/매수 흡수 — 거래량 폭증 + 본체 작음 + 반대편 긴 꼬리.

    조건 (long 흡수 = 매도 흡수):
      - 거래량 > 직전 10봉 평균 × 1.5
      - 본체 / range < 0.4 (작은 본체)
      - 밑꼬리 > 본체 × 1.5 (매도세 흡수)
    """
    if len(klines) < 11: return False
    last = klines[-1]
    avg_vol = sum(k["volume"] for k in klines[-11:-1]) / 10
    if last["volume"] < avg_vol * 1.5: return False
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]
    body = abs(c - o)
    rng = h - l
    if rng == 0: return False
    if body / rng > 0.4: return False  # 본체가 너무 크면 흡수 아님
    if body == 0: return False
    if direction == "long":
        lower_wick = min(o, c) - l
        return lower_wick > body * 1.5
    else:
        upper_wick = h - max(o, c)
        return upper_wick > body * 1.5


def simulate_full_upgrade(entry_price, sl_price, direction, path_klines, symbol):
    """4단계 익절 + 트레일링 SL 동적 + 본전 SL."""
    sl_d_pct = abs(entry_price - sl_price) / entry_price
    tp_prices = {}
    for r_val, name in [(TP1_R, "tp1"), (TP2_R, "tp2"), (TP3_R, "tp3"), (TP4_R, "tp4")]:
        if direction == "long":
            tp_prices[name] = entry_price + sl_d_pct * entry_price * r_val
        else:
            tp_prices[name] = entry_price - sl_d_pct * entry_price * r_val

    remaining = 1.0
    realized_r = 0.0
    exit_path = []
    funding_acc = 0.0
    tp1_hit = tp2_hit = tp3_hit = tp4_hit = False
    hold_bars = 0
    current_sl = sl_price  # 동적 SL

    for bar_idx, k in enumerate(path_klines):
        if remaining <= 1e-9: break
        hold_bars = bar_idx + 1
        if bar_idx > 0 and bar_idx % BARS_PER_FUNDING == 0:
            funding_acc += FUNDING_PER_8H_PCT
        h, lo = k["high"], k["low"]

        # 트레일링 SL — TP2 hit 후, 직전 5봉 swing 따라 이동
        if tp2_hit and bar_idx >= TRAIL_SWING_LOOKBACK:
            recent = path_klines[max(0, bar_idx - TRAIL_SWING_LOOKBACK):bar_idx]
            if recent:
                if direction == "long":
                    new_sl = min(r["low"] for r in recent)
                    # 트레일 SL 은 위로만 (절대 내려가지 않음)
                    if new_sl > current_sl: current_sl = new_sl
                else:
                    new_sl = max(r["high"] for r in recent)
                    if new_sl < current_sl: current_sl = new_sl

        if direction == "long":
            sl_breached = lo <= current_sl
            tp_hits = [
                ("tp1", h >= tp_prices["tp1"] and not tp1_hit),
                ("tp2", h >= tp_prices["tp2"] and tp1_hit and not tp2_hit),
                ("tp3", h >= tp_prices["tp3"] and tp2_hit and not tp3_hit),
                ("tp4", h >= tp_prices["tp4"] and tp3_hit and not tp4_hit),
            ]
        else:
            sl_breached = h >= current_sl
            tp_hits = [
                ("tp1", lo <= tp_prices["tp1"] and not tp1_hit),
                ("tp2", lo <= tp_prices["tp2"] and tp1_hit and not tp2_hit),
                ("tp3", lo <= tp_prices["tp3"] and tp2_hit and not tp3_hit),
                ("tp4", lo <= tp_prices["tp4"] and tp3_hit and not tp4_hit),
            ]

        any_tp = any(reached for _, reached in tp_hits)
        if sl_breached and any_tp:
            realized_r += remaining * (-SL_R)
            exit_path.append((bar_idx, -SL_R, remaining))
            remaining = 0.0
            break

        for tp_name, reached in tp_hits:
            if not reached: continue
            if tp_name == "tp1":
                realized_r += TP1_RATIO * TP1_R
                exit_path.append((bar_idx, TP1_R, TP1_RATIO))
                remaining -= TP1_RATIO; tp1_hit = True
            elif tp_name == "tp2":
                realized_r += TP2_RATIO * TP2_R
                exit_path.append((bar_idx, TP2_R, TP2_RATIO))
                remaining -= TP2_RATIO; tp2_hit = True
            elif tp_name == "tp3":
                realized_r += TP3_RATIO * TP3_R
                exit_path.append((bar_idx, TP3_R, TP3_RATIO))
                remaining -= TP3_RATIO; tp3_hit = True
            elif tp_name == "tp4":
                realized_r += TP4_RATIO * TP4_R
                exit_path.append((bar_idx, TP4_R, TP4_RATIO))
                remaining -= TP4_RATIO; tp4_hit = True

        if sl_breached:
            # TP1 hit 후 본전 SL 이동 (current_sl 이 trail 로 위로 갔으면 trail 가격 사용)
            if tp1_hit:
                # trail 가격에서 청산 (current_sl ≥ entry 면 양수 R, 아니면 0)
                if direction == "long":
                    trail_r = (current_sl - entry_price) / (entry_price * sl_d_pct)
                else:
                    trail_r = (entry_price - current_sl) / (entry_price * sl_d_pct)
                trail_r = max(0, trail_r)  # 본전 미만은 0R
            else:
                trail_r = -SL_R
            realized_r += remaining * trail_r
            exit_path.append((bar_idx, trail_r, remaining))
            remaining = 0.0
            break

    if remaining > 1e-9 and path_klines:
        last_close = path_klines[min(hold_bars - 1, len(path_klines) - 1)]["close"]
        if direction == "long":
            r_exit = (last_close - entry_price) / (entry_price * sl_d_pct)
        else:
            r_exit = (entry_price - last_close) / (entry_price * sl_d_pct)
        realized_r += remaining * r_exit
        exit_path.append((hold_bars, r_exit, remaining))

    slip_pct = SLIPPAGE_BPS.get(symbol, 5.0) / 100
    cost_pct = slip_pct * 2 + ENTRY_FEE_PCT + EXIT_FEE_PCT + funding_acc
    cost_r = cost_pct / (sl_d_pct * 100)
    final_r = realized_r - cost_r
    return {
        "raw_r": round(realized_r, 3),
        "costs_r": round(cost_r, 3),
        "final_r": round(final_r, 3),
        "tp1_hit": tp1_hit, "tp2_hit": tp2_hit, "tp3_hit": tp3_hit, "tp4_hit": tp4_hit,
        "hold_bars": hold_bars,
    }


def collect_full(symbol):
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
    last_4h = bot.SERIES_4H[symbol][-1]["open_time"]; last_1d = bot.SERIES_1D[symbol][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h); i1d = sum(1 for k in k1d if k["open_time"] <= last_1d)
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
    n_single_absorb = 0
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

        # 단일 흡수 감지 (시그널 부스트로만 — 진입에 영향 X 라 측정용)
        pre_window = k15[max(0, confirm_idx - 11):confirm_idx + 1]
        has_single_absorb = detect_single_absorb(pre_window, direction)
        if has_single_absorb: n_single_absorb += 1

        sim = simulate_full_upgrade(entry_price, sl_price, direction, path, symbol)
        sim["direction"] = direction; sim["bar_ts"] = ev["bar_ts"]
        sim["single_absorb"] = has_single_absorb
        enriched.append(sim)

    days = (k15[-1]["close_time"] - k15[50]["close_time"]) / (1000 * 86400)
    return enriched, days


def main():
    print("=== 크트키 봇 미반영 룰 4개 종합 적용 백테스트 ===\n")
    print("적용 룰:")
    print("  1. 크보나치 4단계 익절 (1.236/2.0/2.544/3.236)")
    print("  2. 트레일링 SL 동적 (TP2 hit 후 swing low 따라 이동)")
    print("  3. 단일 캔들 흡수 감지 (시그널 부스트, 측정용)")
    print("  4. 분할 익절 모니터링 (이미 4단계로 처리)\n")

    all_trades = []
    for sym in SYMBOLS:
        trades, days = collect_full(sym)
        for t in trades: t["symbol"] = sym
        all_trades.extend(trades)
        if trades:
            n = len(trades); avg_r = sum(t["final_r"] for t in trades) / n
            wins = sum(1 for t in trades if t["final_r"] > 0)
            sa = sum(1 for t in trades if t["single_absorb"])
            sa_avg = mean([t["final_r"] for t in trades if t["single_absorb"]]) if sa else 0
            non_sa_avg = mean([t["final_r"] for t in trades if not t["single_absorb"]]) if n - sa else 0
            print(f"  {sym}: {n}거래, avg R {avg_r:+.3f}, Win {100*wins/n:.1f}%")
            if sa >= 3:
                print(f"      └ 단일 흡수 있음: {sa}건 avg R {sa_avg:+.3f}R vs 없음: {n-sa}건 {non_sa_avg:+.3f}R")
    all_trades.sort(key=lambda x: x["bar_ts"])

    if not all_trades:
        print("거래 없음"); return

    n = len(all_trades)
    final_rs = [t["final_r"] for t in all_trades]
    avg_r = mean(final_rs)
    wins = sum(1 for r in final_rs if r > 0)
    total_r = sum(final_rs)
    tp_stats = {f"tp{i}": sum(1 for t in all_trades if t.get(f"tp{i}_hit")) for i in range(1, 5)}
    n_absorb = sum(1 for t in all_trades if t["single_absorb"])

    print(f"\n📊 종합 결과:")
    print(f"   거래수: {n}/년")
    print(f"   avg R: {avg_r:+.3f}R")
    print(f"   합계 R: {total_r:+.2f}R")
    print(f"   Win%: {100*wins/n:.1f}%")
    print(f"   TP hit: TP1 {tp_stats['tp1']} / TP2 {tp_stats['tp2']} / TP3 {tp_stats['tp3']} / TP4 {tp_stats['tp4']}")
    print(f"   단일 흡수 시그널: {n_absorb}건 ({100*n_absorb/n:.1f}%)")

    print(f"\n💰 복리 시나리오 (4규칙 적용):")
    for label, risk in [("1% (보수)", 1.0), ("3.5% (사용자)", 3.5)]:
        comp = compound_simulation(all_trades, risk)
        print(f"   {label:>15}: 복리 {comp['return_pct']:+.1f}%, 배수 {comp['multiplier']:.2f}×, MaxDD {comp['max_dd_pct']:.1f}%")

    # 비교 단계별
    print(f"\n=== 단계별 비교 ===")
    print("=" * 95)
    print(f"{'룰':>30} | {'avg R':>7} | {'Win%':>6} | {'합계 R':>9} | {'복리 1%':>9} | {'복리 3.5%':>10}")
    print("-" * 95)
    rows = [
        ("기존 (3단계 익절)",               0.418, 75.0,  106.73, 187.2,  3457),
        ("+크보나치 4단계 (TP3/TP4)",       0.434, 69.1,  111.18, 198.0,  3748),
        (">> 풀 업그레이드 (4룰 종합)",       avg_r,  100*wins/n, total_r,
         compound_simulation(all_trades, 1.0)["return_pct"],
         compound_simulation(all_trades, 3.5)["return_pct"]),
    ]
    for label, ar, wp, tr, c1, c35 in rows:
        marker = "★" if "풀" in label else " "
        print(f"{marker} {label:>28} | {ar:>+6.3f}R | {wp:>5.1f}% | {tr:>+8.2f}R | {c1:>+8.1f}% | {c35:>+9.1f}%")
    print("=" * 95)
    print()
    print("📈 풀 업그레이드 vs 기존 (3단계):")
    print(f"   합계 R: {total_r - 106.73:+.2f}R ({(total_r-106.73)/106.73*100:+.0f}%)")
    c1_new = compound_simulation(all_trades, 1.0)["return_pct"]
    print(f"   복리 1%: {c1_new - 187.2:+.1f}%p")
    c35_new = compound_simulation(all_trades, 3.5)["return_pct"]
    print(f"   복리 3.5%: {c35_new - 3457:+.1f}%p ({(c35_new-3457)/3457*100:+.0f}%)")

    out = {"n": n, "avg_r": round(avg_r, 3), "total_r": round(total_r, 2),
           "win_pct": round(100*wins/n, 1),
           "compound_1": compound_simulation(all_trades, 1.0),
           "compound_3_5": compound_simulation(all_trades, 3.5),
           "tp_stats": tp_stats, "n_absorb": n_absorb}
    (ROOT / "backtest_data" / "full_upgrade.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
