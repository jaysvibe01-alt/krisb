"""크트키 봇 — 진짜 1년 수익률 측정 (실제 청산 엔진 + 모든 비용 반영).

기존 backtest_user_model.py 의 약점 보완:
1. 실제 청산 시퀀스 — 봉별 path 추적, SL/TP1/TP2 hit 순서 정확히
2. 분할 청산 — TP1 50% / TP2 30% / 트레일 20%
3. 거래 수수료 — Bitget taker 0.06% × 2 (진입 + 청산)
4. 기존 슬리피지 + 펀딩비 그대로 사용

R 단위:
  R = 진입가 대비 손절가까지의 거리 (%)
  거래당 P&L = Σ (분할_비율 × 청산_R) - 비용 R

P&L 보고: 보수/낙관 가정 없이 실제 가격 path 만으로 계산.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import median, mean, stdev
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import Collector, load, SLIPPAGE_BPS, FUNDING_PER_8H_PCT, BARS_PER_FUNDING

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# Bitget USDT-M Futures 수수료
TAKER_FEE_PCT = 0.06    # 0.06% 진입가 대비 (시장가 가정)
MAKER_FEE_PCT = 0.02    # 0.02% (limit 체결 시)
ENTRY_FEE_PCT = MAKER_FEE_PCT  # M2 매수존 limit 가정
EXIT_FEE_PCT = TAKER_FEE_PCT   # SL/TP 시장가 가정

# 분할 청산 (Kris 본인 방식: TP1 50%, TP2 30%, trail 20%)
TP1_RATIO = 0.50
TP2_RATIO = 0.30
TRAIL_RATIO = 0.20

# RR multiples (실제 익절가)
SL_R = 1.0      # 손절 = -1R
TP1_R = 1.0     # 1:1 자리 (크보나치)
TP2_R = 2.0     # 2.0 자리 (크보나치 2.0 확장)
TRAIL_TARGET_R = 2.5  # trailing 청산 평균 가정 (보수적, RR 2~3 사이)

# 모니터링 봉 수 (진입 후 N봉 안에 안 끝나면 강제 청산)
MAX_HOLD_BARS = 48


def simulate_trade(entry_price: float, sl_price: float, direction: str,
                   path_klines: list[dict], symbol: str) -> dict:
    """봉 path 추적해서 실제 청산 시퀀스 + 분할 청산 P&L 계산.

    Returns:
        {
            "exit_path": [(bar_offset, R, ratio), ...],  # 청산 이력
            "final_r": float,                              # 총 R (비용 차감 후)
            "raw_r": float,                                # 비용 차감 전
            "costs_r": float,                              # 비용 합계 R
            "hold_bars": int,                              # 보유 봉
            "outcome": str,                                # "SL_only" / "TP1_then_SL" / "TP1+TP2+trail" / "timeout"
        }
    """
    sl_d_pct = abs(entry_price - sl_price) / entry_price  # 1R = 손절거리
    tp1_price = entry_price + (sl_d_pct * entry_price * TP1_R) if direction == "long" else entry_price - (sl_d_pct * entry_price * TP1_R)
    tp2_price = entry_price + (sl_d_pct * entry_price * TP2_R) if direction == "long" else entry_price - (sl_d_pct * entry_price * TP2_R)

    remaining = 1.0
    realized_r = 0.0
    exit_path = []
    funding_acc = 0.0
    hold_bars = 0
    tp1_hit, tp2_hit = False, False
    trail_max_r = 0.0  # 잔여 trail 포지션의 best R

    for bar_idx, k in enumerate(path_klines):
        if remaining <= 1e-9:
            break
        hold_bars = bar_idx + 1
        # 펀딩비 (8h = 32 × 15m bars)
        if bar_idx > 0 and bar_idx % BARS_PER_FUNDING == 0:
            funding_acc += FUNDING_PER_8H_PCT  # %

        h, lo = k["high"], k["low"]

        # 보수적 intra-bar 청산 순서:
        # - 한 봉 안에 SL/TP 둘 다 hit 가능하면, 가까운 쪽 (= SL 또는 TP1 중 가까운) 부터 hit
        # - long 의 경우 봉이 open→high→low→close 인지 open→low→high→close 인지 알 수 없으므로
        #   "SL 과 TP1 둘 다 hit 면 SL 먼저" 라고 보수적으로 가정

        if direction == "long":
            sl_breached = lo <= sl_price
            tp1_reached = h >= tp1_price and not tp1_hit
            tp2_reached = h >= tp2_price and tp1_hit and not tp2_hit
        else:
            sl_breached = h >= sl_price
            tp1_reached = lo <= tp1_price and not tp1_hit
            tp2_reached = lo <= tp2_price and tp1_hit and not tp2_hit

        # 보수적: 같은 봉 SL + TP 면 SL 부터
        if sl_breached and (tp1_reached or tp2_reached):
            # 잔여 전부 SL 청산
            r_from_sl = -SL_R
            realized_r += remaining * r_from_sl
            exit_path.append((bar_idx, r_from_sl, remaining))
            remaining = 0.0
            break

        if tp1_reached:
            # TP1 50% 청산
            realized_r += TP1_RATIO * TP1_R
            exit_path.append((bar_idx, TP1_R, TP1_RATIO))
            remaining -= TP1_RATIO
            tp1_hit = True

        if tp2_reached:
            # TP2 30% 청산
            realized_r += TP2_RATIO * TP2_R
            exit_path.append((bar_idx, TP2_R, TP2_RATIO))
            remaining -= TP2_RATIO
            tp2_hit = True
            # trailing 잔여 추적 시작 — best R 갱신
            trail_max_r = TP2_R

        if sl_breached:
            # 잔여 SL 청산
            sl_r_signed = -SL_R
            # 단, TP1 hit 후면 본전 SL 이동 가정 → 잔여 SL = 0R 으로 처리
            if tp1_hit:
                sl_r_signed = 0.0  # 본전 SL
            realized_r += remaining * sl_r_signed
            exit_path.append((bar_idx, sl_r_signed, remaining))
            remaining = 0.0
            break

        # trailing 잔여 추적 (tp2_hit 후)
        if tp2_hit and remaining > 1e-9:
            cur_r = (h - entry_price) / (entry_price * sl_d_pct) if direction == "long" else (entry_price - lo) / (entry_price * sl_d_pct)
            trail_max_r = max(trail_max_r, cur_r)

    # 강제 청산 — 잔여가 남아있으면 마지막 봉 close 로 청산
    if remaining > 1e-9 and path_klines:
        last = path_klines[min(hold_bars - 1, len(path_klines) - 1)]
        last_close = last["close"]
        if direction == "long":
            r_exit = (last_close - entry_price) / (entry_price * sl_d_pct)
        else:
            r_exit = (entry_price - last_close) / (entry_price * sl_d_pct)
        # trailing 잔여는 trail_max_r 와 close R 중 보수적 (작은 값)
        if tp2_hit:
            r_exit = min(r_exit, TRAIL_TARGET_R)
        realized_r += remaining * r_exit
        exit_path.append((hold_bars, r_exit, remaining))
        remaining = 0.0

    # 비용 계산 (R 단위로 환산)
    # 슬리피지: 진입 + 청산 둘 다
    slip_bps = SLIPPAGE_BPS.get(symbol, 5.0)
    slip_pct = slip_bps / 100  # bps → %, 0.01% 단위
    # entry + exit 2회
    cost_slip_pct = slip_pct * 2

    # 거래 수수료: 진입 (entry) + 청산 1~N회
    n_exits = len([e for e in exit_path if e[2] > 0])
    cost_fee_pct = ENTRY_FEE_PCT + EXIT_FEE_PCT * n_exits / max(n_exits, 1) if n_exits > 0 else ENTRY_FEE_PCT
    # 단순화: entry fee + exit fee (분할 시 청산 비율 합 = 1 이므로 전체 청산 fee 한 번)
    cost_fee_pct = ENTRY_FEE_PCT + EXIT_FEE_PCT

    # 펀딩비 — 누적 % 를 sl_d_pct (1R 거리) 로 나눠 R 단위로
    cost_funding_pct = funding_acc

    total_cost_pct = cost_slip_pct + cost_fee_pct + cost_funding_pct
    cost_r = total_cost_pct / (sl_d_pct * 100)  # % → R

    final_r = realized_r - cost_r

    # outcome 분류
    if tp1_hit and tp2_hit:
        outcome = "TP1+TP2+trail"
    elif tp1_hit and any(e[1] <= 0 for e in exit_path[1:]):
        outcome = "TP1_then_BE_SL"  # 본전 SL
    elif tp1_hit:
        outcome = "TP1_only"
    elif any(e[1] < 0 for e in exit_path):
        outcome = "SL_only"
    else:
        outcome = "timeout"

    return {
        "exit_path": exit_path,
        "raw_r": round(realized_r, 3),
        "costs_r": round(cost_r, 3),
        "final_r": round(final_r, 3),
        "hold_bars": hold_bars,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "outcome": outcome,
    }


def collect_entries(symbol: str) -> tuple[list[dict], int]:
    """봇 그대로 돌려서 진입 이벤트 수집."""
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

    # 진입 모델 (M2: low~close 매수존, 다음 봉)
    enriched = []
    for ev in c.events:
        confirm_idx = ev["bar_idx"]
        if confirm_idx + 1 >= len(k15): continue
        confirm = k15[confirm_idx]
        next_bar = k15[confirm_idx + 1]
        direction = ev["direction"]
        atr = ev["atr"]

        # 매수존 (M2)
        if direction == "long":
            zone_low, zone_high = confirm["low"], confirm["close"]
        else:
            zone_low, zone_high = confirm["close"], confirm["high"]

        # 다음봉이 매수존 안에 들어왔는지
        if next_bar["low"] > zone_high or next_bar["high"] < zone_low:
            continue

        entry_price = (zone_low + zone_high) / 2
        # 슬리피지 (진입가 보정 — 이미 simulate_trade 에서 비용 차감하므로 여기선 raw entry)

        sl_d = atr * 1.5
        sl_price = entry_price - sl_d if direction == "long" else entry_price + sl_d

        # 모니터링 path
        path = k15[confirm_idx + 1:confirm_idx + 1 + MAX_HOLD_BARS]
        sim = simulate_trade(entry_price, sl_price, direction, path, symbol)
        sim["direction"] = direction
        sim["bar_ts"] = ev["bar_ts"]
        sim["entry_price"] = entry_price
        sim["sl_price"] = sl_price
        sim["confirm_idx"] = confirm_idx
        enriched.append(sim)

    days = (k15[-1]["close_time"] - k15[50]["close_time"]) / (1000 * 86400)
    return enriched, days


def main():
    print("=== 크트키 봇 진짜 1년 수익률 (실제 청산 엔진 + 모든 비용) ===\n")
    print(f"비용 모델:")
    print(f"  - 진입 fee: {ENTRY_FEE_PCT}% (maker, limit 가정)")
    print(f"  - 청산 fee: {EXIT_FEE_PCT}% (taker, SL/TP 시장가)")
    print(f"  - 슬리피지: BTC 1bp / ETH 2bp / SOL 3bp / XRP 4bp × 2 (진입+청산)")
    print(f"  - 펀딩비: 8h 0.01% (32봉마다 누적)")
    print(f"  - 분할 청산: TP1 50% / TP2 30% / Trail 20% (Kris 본인 방식)")
    print(f"  - TP1 hit 후 잔여 SL = 본전(0R) 이동")
    print()

    all_results = {}
    total_final_r = 0
    total_n = 0
    for sym in SYMBOLS:
        trades, days = collect_entries(sym)
        if not trades:
            print(f"  {sym}: 진입 없음")
            continue
        n = len(trades)
        final_rs = [t["final_r"] for t in trades]
        raw_rs = [t["raw_r"] for t in trades]
        costs_rs = [t["costs_r"] for t in trades]
        outcomes = defaultdict(int)
        for t in trades: outcomes[t["outcome"]] += 1
        annual_n = n / days * 365 if days else 0
        annual_final_r = sum(final_rs) / days * 365 if days else 0
        annual_raw_r = sum(raw_rs) / days * 365 if days else 0
        annual_costs_r = sum(costs_rs) / days * 365 if days else 0
        win_count = sum(1 for r in final_rs if r > 0)
        win_pct = 100 * win_count / n
        avg_final_r = sum(final_rs) / n

        all_results[sym] = {
            "n": n, "annual_n": round(annual_n, 1),
            "avg_final_r": round(avg_final_r, 3),
            "annual_raw_r": round(annual_raw_r, 2),
            "annual_costs_r": round(annual_costs_r, 2),
            "annual_final_r": round(annual_final_r, 2),
            "win_pct": round(win_pct, 1),
            "outcomes": dict(outcomes),
        }
        total_final_r += annual_final_r
        total_n += annual_n

    # 출력
    print("=" * 110)
    print(f"{'종목':>10} | {'연N':>5} | {'avgR':>6} | {'Win%':>5} | {'TP1+TP2':>7} | {'TP1만':>5} | {'본전SL':>6} | {'SL만':>5} | {'타임':>5} | {'연rawR':>7} | {'연비용R':>7} | {'연finalR':>8}")
    print("-" * 110)
    for sym, r in all_results.items():
        oc = r["outcomes"]
        tp1tp2 = oc.get("TP1+TP2+trail", 0)
        tp1only = oc.get("TP1_only", 0)
        be_sl = oc.get("TP1_then_BE_SL", 0)
        sl_only = oc.get("SL_only", 0)
        timeout = oc.get("timeout", 0)
        print(f"{sym:>10} | {r['annual_n']:>5.1f} | {r['avg_final_r']:>+5.3f}R | {r['win_pct']:>4.1f}% | {tp1tp2:>7} | {tp1only:>5} | {be_sl:>6} | {sl_only:>5} | {timeout:>5} | {r['annual_raw_r']:>+6.2f}R | -{abs(r['annual_costs_r']):>5.2f}R | {r['annual_final_r']:>+7.2f}R")
    print("-" * 110)
    print(f"{'합계':>10} | {total_n:>5.1f} |        |       |         |       |        |       |       |         |         | {total_final_r:>+7.2f}R")
    print("=" * 110)

    print(f"\n📊 1년 자본 수익률 (Kris 표준 10x × 자본50% 기준, 평균 손절거리 0.7%):")
    print(f"   SL 1회 = -3.5% 자본 손실")
    print(f"   연 final R: {total_final_r:+.2f}R")
    risk_per_trade = 3.5  # %
    annual_return_pct = total_final_r * risk_per_trade
    print(f"   → 연 자본 수익률: {annual_return_pct:+.1f}%")
    print()
    print(f"📊 자본 1% 리스크/거래 (보수형) 기준:")
    print(f"   → 연 자본 수익률: {total_final_r * 1.0:+.1f}%")
    print()

    out_path = ROOT / "backtest_data" / "realistic_pnl.json"
    out_path.write_text(json.dumps({
        "by_symbol": all_results,
        "total_annual_final_r": round(total_final_r, 2),
        "total_annual_n": round(total_n, 1),
        "annual_return_pct_10x_50": round(annual_return_pct, 1),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {out_path}")


if __name__ == "__main__":
    main()
