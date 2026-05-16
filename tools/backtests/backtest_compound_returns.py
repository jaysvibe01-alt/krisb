"""크트키 봇 — 복리 1년 수익률 시뮬레이션.

backtest_realistic_pnl.py 의 실제 거래 시퀀스를 시간순으로 굴려서
각 거래마다 자본 % 변화 → 누적 복리 자본 증식.

크리스비 실제 매매 = 복리. 거래당 자본 % 리스크 고정 → 자본 늘면 포지션도 비례.

시뮬:
  자본_0 = 100
  거래마다: 자본_{i+1} = 자본_i × (1 + risk_per_trade × R_i)

비교: 단순 합산 vs 복리
  단순: Σ R_i × risk_per_trade
  복리: Π (1 + risk_per_trade × R_i) - 1
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import median, mean
from math import log, exp
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import Collector, load
from backtest_realistic_pnl import (
    simulate_trade, collect_entries, SYMBOLS, MAX_HOLD_BARS,
    ENTRY_FEE_PCT, EXIT_FEE_PCT, TP1_RATIO, TP2_RATIO, TRAIL_RATIO,
    TP1_R, TP2_R, TRAIL_TARGET_R, SL_R,
)


def collect_all_trades_sorted() -> list[dict]:
    """4종목 거래 모두 모아서 시간순(bar_ts) 정렬."""
    all_trades = []
    for sym in SYMBOLS:
        trades, days = collect_entries(sym)
        for t in trades:
            t["symbol"] = sym
            all_trades.append(t)
    # 시간순 정렬
    all_trades.sort(key=lambda x: x["bar_ts"])
    return all_trades, days


def compound_simulation(trades: list[dict], risk_per_trade_pct: float,
                       starting_capital: float = 100.0) -> dict:
    """거래 시퀀스를 복리로 굴림.

    risk_per_trade_pct:
      거래당 손절 시 자본의 몇 % 잃는지 (예: 1% 리스크면 1.0)
      자본 1% 리스크 = 자본 변화 = risk × R
        +1R 익절 → 자본 × (1 + risk/100)
        -1R 손절 → 자본 × (1 - risk/100)

    Returns:
      {final_capital, max_capital, min_capital, max_dd_pct, ...}
    """
    capital = starting_capital
    max_cap = capital
    min_cap_after_max = capital
    max_dd_pct = 0.0
    history = [capital]
    wins, losses = 0, 0

    for t in trades:
        r = t["final_r"]
        # 자본 % 변화 = risk × R
        capital *= (1 + (risk_per_trade_pct / 100) * r)
        history.append(capital)
        if r > 0: wins += 1
        elif r < 0: losses += 1

        max_cap = max(max_cap, capital)
        # MaxDD 갱신: 최고점 대비 현재 자본 하락 비율
        cur_dd = (max_cap - capital) / max_cap * 100
        max_dd_pct = max(max_dd_pct, cur_dd)

    final_return_pct = (capital - starting_capital) / starting_capital * 100
    multiplier = capital / starting_capital
    return {
        "starting": starting_capital,
        "final": round(capital, 2),
        "return_pct": round(final_return_pct, 1),
        "multiplier": round(multiplier, 2),
        "max_dd_pct": round(max_dd_pct, 1),
        "wins": wins,
        "losses": losses,
        "n_trades": len(trades),
        "history": history,
    }


def simple_sum(trades: list[dict], risk_per_trade_pct: float) -> float:
    """단순 합산 (비교용)."""
    total_r = sum(t["final_r"] for t in trades)
    return total_r * risk_per_trade_pct


def main():
    print("=== 크트키 봇 복리 1년 수익률 시뮬레이션 ===\n")
    print("복리 모델: 거래마다 자본 변화 = 자본 × (1 + risk% × R)")
    print("→ 자본 늘면 포지션도 비례 (실제 크리스비 매매 방식)\n")

    trades, days = collect_all_trades_sorted()
    n_total = len(trades)
    final_rs = [t["final_r"] for t in trades]
    sum_r = sum(final_rs)
    avg_r = sum_r / n_total if n_total else 0
    win_count = sum(1 for r in final_rs if r > 0)
    loss_count = sum(1 for r in final_rs if r < 0)
    annual_factor = 365 / max(days, 1)

    print(f"📊 거래 통계 (시간순 정렬):")
    print(f"   1년 거래수: {n_total}회")
    print(f"   거래당 평균 R: {avg_r:+.3f}R")
    print(f"   합계 R: {sum_r:+.2f}R")
    print(f"   Win: {win_count} ({100*win_count/n_total:.1f}%) / Loss: {loss_count} ({100*loss_count/n_total:.1f}%)")
    print()

    # 다양한 risk_per_trade 로 시뮬
    scenarios = [
        ("0.25% (초보수)", 0.25),
        ("0.5% (보수)", 0.5),
        ("1.0% (표준)", 1.0),
        ("1.75% (= 5배×50%)", 1.75),
        ("2.0% (= 일반 swing)", 2.0),
        ("3.5% (= 10배×50% Kris표준)", 3.5),
        ("5.0% (공격적)", 5.0),
        ("7.0% (= 20배×50% 위험)", 7.0),
    ]

    print("=" * 110)
    print(f"{'시나리오 (거래당 리스크)':>28} | {'단순합산':>10} | {'복리':>14} | {'배수':>7} | {'MaxDD':>8} | {'주의':>20}")
    print("-" * 110)
    for label, risk in scenarios:
        simple = simple_sum(trades, risk)
        comp = compound_simulation(trades, risk)
        warn = ""
        if comp["max_dd_pct"] > 30: warn = "⚠️ 위험"
        elif comp["max_dd_pct"] > 50: warn = "🚫 청산 가능"
        elif comp["return_pct"] > 1000: warn = "✨ 폭발 성장"
        elif comp["return_pct"] > 500: warn = "🔥 강한 성장"
        print(f"{label:>28} | {simple:>+9.1f}% | {comp['return_pct']:>+13.1f}% | {comp['multiplier']:>6.2f}× | {comp['max_dd_pct']:>7.1f}% | {warn:>20}")
    print("=" * 110)

    print(f"\n💡 단순합산 vs 복리 차이:")
    print(f"   - 단순합산: 시드 고정 (예: 항상 $100 리스크). 자본 늘어도 포지션 그대로.")
    print(f"   - 복리: 자본의 X% 리스크 — 자본 늘면 포지션도 비례. 실제 트레이더 방식.")
    print(f"   - {n_total} 거래에 평균 +{avg_r:+.3f}R → 복리 시 연 {compound_simulation(trades, 1.0)['return_pct']:+.0f}% (1% 리스크)")

    print(f"\n🎯 Kris 표준 (10배 × 자본 50%, 거래당 -3.5% 리스크) 복리 시:")
    kris_result = compound_simulation(trades, 3.5)
    print(f"   1년 수익률: {kris_result['return_pct']:+.0f}%")
    print(f"   자본 배수: {kris_result['multiplier']:.1f}×")
    print(f"   MaxDD: {kris_result['max_dd_pct']:.1f}%")
    if kris_result["history"]:
        print(f"   자본 추이: 100 → 최고 {max(kris_result['history']):.0f} → 최종 {kris_result['final']:.0f}")

    # 시간 흐름 (월별 자본)
    print(f"\n📈 Kris 표준 시나리오 월별 자본 추이 (시작 100, 1% 리스크):")
    cap_history = compound_simulation(trades, 1.0)["history"]
    if cap_history and trades:
        # 첫 거래 ts → 마지막 거래 ts 사이 12등분
        ts_first = trades[0]["bar_ts"]
        ts_last = trades[-1]["bar_ts"]
        ts_per_month = (ts_last - ts_first) / 12
        for m in range(1, 13):
            target_ts = ts_first + ts_per_month * m
            # target_ts 까지 누적된 trade 수
            n_until = sum(1 for t in trades if t["bar_ts"] <= target_ts)
            cap_at_month = cap_history[min(n_until, len(cap_history) - 1)]
            print(f"   M{m:>2}: {cap_at_month:>7.1f}  (+{cap_at_month - 100:+.0f}%)")

    # 저장
    out_path = ROOT / "backtest_data" / "compound_returns.json"
    out_path.write_text(json.dumps({
        "n_trades": n_total,
        "avg_r": round(avg_r, 3),
        "sum_r": round(sum_r, 2),
        "scenarios": {
            label: {
                "risk_pct": risk,
                "simple_sum_return_pct": round(simple_sum(trades, risk), 1),
                "compound": compound_simulation(trades, risk),
            }
            for label, risk in scenarios
        },
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
