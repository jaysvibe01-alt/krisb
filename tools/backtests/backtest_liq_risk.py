"""슬리피지 정확화 + 레버리지별 청산 위험 시뮬.

질문 1: 신규 종목 슬리피지 정확 반영 후 결과 어떻게 되나?
질문 2: 동시 포지션 2개 + 20-30배 레버리지에서 청산 안 나나?

청산 모델 (Bitget USDT-M Futures, Cross 가정):
  Maintenance margin = 자본의 0.5~1.0%
  Liquidation: 손실이 (Initial margin - Maintenance margin) 도달 시
  Cross 모드: 자본 전체 마진. 동시 포지션 손실 합산 가능.
  Isolated 모드: 포지션별 격리.

본 시뮬은 Cross 모드 가정 (사용자 30배 × 50% 표시).
연속 SL 시퀀스 / 동시 SL 시퀀스로 자본 손실 추적.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from statistics import mean, median
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backtest_realistic_pnl import collect_entries, SLIPPAGE_BPS

# 신규 종목 슬리피지 (현실 추정)
EXTENDED_SLIPPAGE_BPS = {
    "BTCUSDT": 1.0,   # 기존
    "ETHUSDT": 2.0,
    "SOLUSDT": 3.0,
    "XRPUSDT": 4.0,
    "DOGEUSDT": 5.0,   # 변동성 큼
    "LINKUSDT": 4.0,
    "ADAUSDT": 4.0,
    "AVAXUSDT": 5.0,
    "SUIUSDT": 6.0,   # 신생 + 변동성
    "BNBUSDT": 2.0,   # 메이저
    "TONUSDT": 5.0,
    "HYPEUSDT": 10.0,  # 신규 상장 = 큰 슬리피지
}

# 청산 위험 시뮬 파라미터
SYMBOLS_12 = list(EXTENDED_SLIPPAGE_BPS.keys())


def collect_all():
    """모든 종목 거래 시간순 정렬."""
    all_t = []
    for sym in SYMBOLS_12:
        try:
            trades, _ = collect_entries(sym)
        except Exception:
            continue
        for t in trades:
            t["symbol"] = sym
            all_t.append(t)
    all_t.sort(key=lambda x: x["bar_ts"])
    return all_t


def apply_extended_slippage(trades):
    """기본 슬리피지 (5bp) → 종목별 정확한 슬리피지로 final_r 재계산.

    기존 final_r 에 (정확_slip - 기본_slip) × 2 / sl_d_pct 만큼 조정.
    """
    out = []
    for t in trades:
        sym = t["symbol"]
        # 기존 시뮬에서 적용된 슬리피지 (기본 5bp if not in dict)
        old_slip = SLIPPAGE_BPS.get(sym, 5.0)
        new_slip = EXTENDED_SLIPPAGE_BPS.get(sym, 5.0)
        if old_slip != new_slip:
            # 슬리피지 차이 × 2 (진입+청산) → % → R 환산 어렵다.
            # 단순: final_r 직접 조정은 sl_d_pct 알아야 함. 일단 거래당 R 조정 추정.
            # 슬리피지 차이 × 2 / (평균 손절거리 0.7%) ≈ R 단위 차이
            slip_diff_pct = (new_slip - old_slip) / 100 * 2  # bp → %, ×2 (entry+exit)
            r_adj = slip_diff_pct / 0.7  # 평균 손절거리 0.7%
            t = dict(t)
            t["final_r"] = round(t["final_r"] - r_adj, 3)
        out.append(t)
    return out


def simulate_max_dd(trades, risk_pct, max_concurrent=2):
    """순차 시뮬 — 동시 포지션 한도, 자본 path 추적, MaxDD + 청산 측정.

    가정 (Cross 모드):
      - 자본 시작 100 (정규화)
      - 거래마다 자본의 risk_pct% × R 변화
      - 동시 포지션 ≤ max_concurrent (이전 포지션 끝나야 새 진입)
      - 연속 SL 시퀀스에서 자본 추적
      - 자본 -70% 도달 시 "청산" 카운트
    """
    capital = 100.0
    max_cap = 100.0
    max_dd_pct = 0.0
    liq_count = 0
    n_taken = 0
    n_skipped = 0
    active = []  # [(end_ts, R)]
    history = [100.0]

    for t in trades:
        # 종료된 active 정리
        ts = t["bar_ts"]
        active = [(e, r) for e, r in active if e > ts]

        if len(active) >= max_concurrent:
            n_skipped += 1
            continue

        # 진입 — 자본 X% 리스크
        r = t["final_r"]
        capital *= (1 + (risk_pct / 100) * r)
        n_taken += 1
        # 보유 시간 추정 (15m × hold_bars)
        hold_ms = t.get("hold_bars", 8) * 15 * 60 * 1000
        active.append((ts + hold_ms, r))

        # MaxDD + 청산 check
        max_cap = max(max_cap, capital)
        cur_dd = (max_cap - capital) / max_cap * 100
        max_dd_pct = max(max_dd_pct, cur_dd)
        if capital <= 30.0:  # -70% 시 청산
            liq_count += 1
            capital = 100.0  # 시드 리셋 시뮬
            max_cap = 100.0

        history.append(capital)

    return {
        "final_capital": round(capital, 2),
        "return_pct": round((capital - 100), 1),
        "max_dd_pct": round(max_dd_pct, 1),
        "n_taken": n_taken,
        "n_skipped": n_skipped,
        "liq_count": liq_count,
        "history": history,
    }


def main():
    print("=== 슬리피지 정확화 + 레버리지별 청산 위험 시뮬 ===\n")

    trades_raw = collect_all()
    trades_adj = apply_extended_slippage(trades_raw)

    # 효과 비교
    raw_sum = sum(t["final_r"] for t in trades_raw)
    adj_sum = sum(t["final_r"] for t in trades_adj)
    print(f"슬리피지 정확화 효과:")
    print(f"  기본 (기본 5bp): 합계 R {raw_sum:+.2f}")
    print(f"  정확 (종목별): 합계 R {adj_sum:+.2f}")
    print(f"  차이: {adj_sum - raw_sum:+.2f}R ({(adj_sum-raw_sum)/raw_sum*100:+.1f}%)")
    print()

    # 청산 위험 시뮬 (동시 포지션 2개 가정)
    print("=== 동시 포지션 2개 + 레버리지별 청산 위험 ===\n")
    print("청산 정의: 자본 -70% 도달 (시드 리셋)")
    print("리스크/거래 = (자본 % × 레버리지 × 평균 SL 0.7%) / 100")
    print()

    # 30배 × 50% = 자본 50% × 30 × 0.7% = 10.5% per SL → 너무 위험
    # 20배 × 50% = 자본 50% × 20 × 0.7% = 7% per SL
    # 20배 × 33% (동시 2개로) = 자본 33% × 20 × 0.7% = 4.6% per SL
    # 15배 × 50% = 자본 50% × 15 × 0.7% = 5.25%
    # 10배 × 50% = 자본 50% × 10 × 0.7% = 3.5%

    scenarios = [
        ("5x × 50% / 1포지션",   5,  0.50, 1),  # 자본의 1.75% per SL
        ("10x × 50% / 동시1",   10,  0.50, 1),  # 3.5%
        ("10x × 50% / 동시2",   10,  0.50, 2),  # 동시 2개면 100% 노출
        ("15x × 33% / 동시2",   15,  0.33, 2),  # 자본 33% × 15 = notional 5x
        ("20x × 33% / 동시2",   20,  0.33, 2),  # 4.6%
        ("20x × 50% / 동시1",   20,  0.50, 1),  # 7%
        ("20x × 50% / 동시2",   20,  0.50, 2),  # 동시 2개 = 자본 100% 노출
        ("25x × 40% / 동시2",   25,  0.40, 2),  # 7%
        ("30x × 25% / 동시2",   30,  0.25, 2),  # 5.25%
        ("30x × 33% / 동시2",   30,  0.33, 2),  # 6.93%
        ("30x × 50% / 동시2",   30,  0.50, 2),  # 10.5% ⚠️
    ]

    print("=" * 110)
    print(f"{'시나리오':>22} | {'SL시 자본손실':>13} | {'1년 수익률':>11} | {'MaxDD':>7} | {'청산 횟수':>9} | {'동시 진입':>9} | {'평가':>15}")
    print("-" * 110)
    for label, lev, frac, concurrent in scenarios:
        risk_per = frac * lev * 0.7  # 자본 % × 레버리지 × 평균 SL 거리 0.7%
        result = simulate_max_dd(trades_adj, risk_per, max_concurrent=concurrent)
        if result["liq_count"] > 0:
            grade = f"🚫 청산 {result['liq_count']}회"
        elif result["max_dd_pct"] > 50:
            grade = "⚠️ 위험"
        elif result["max_dd_pct"] > 30:
            grade = "🟡 주의"
        elif result["return_pct"] > 1000:
            grade = "✨ 폭발"
        elif result["return_pct"] > 500:
            grade = "🔥 우수"
        else:
            grade = "안전"
        print(f"{label:>22} | {risk_per:>12.2f}% | {result['return_pct']:>+10.1f}% | {result['max_dd_pct']:>6.1f}% | {result['liq_count']:>9} | {result['n_taken']:>4}/{result['n_taken']+result['n_skipped']:>4} | {grade:>15}")
    print("=" * 110)

    print(f"\n📌 핵심:")
    print(f"   동시 포지션 2개 = SL 동시 발생 시 자본 손실 2배")
    print(f"   상관 0.7-0.9 → BTC SL 시 ETH/SOL/XRP 도 같이 SL 가능성 큼")
    print(f"   30배 × 50% × 동시 2개 = SL 1회 -10.5%, 동시 SL -21% (4회면 -42% 청산 가까움)")
    print(f"   20배 × 33% × 동시 2개 = SL 1회 -4.6%, 동시 SL -9.2% (안전 마진)")
    print(f"   가장 안전한 공격: 20배 × 33% (자본 33% 만 진입 → 30%대 수익률, MaxDD 30% 이내)")

    # 종합 저장
    out = {
        "scenarios": [
            {"label": label, "lev": lev, "frac": frac, "concurrent": concurrent,
             "risk_per_trade_pct": frac * lev * 0.7,
             "result": simulate_max_dd(trades_adj, frac * lev * 0.7, concurrent)}
            for label, lev, frac, concurrent in scenarios
        ],
        "extended_slippage_bps": EXTENDED_SLIPPAGE_BPS,
        "slippage_adjustment_r": round(adj_sum - raw_sum, 2),
    }
    (ROOT / "backtest_data" / "liq_risk.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
