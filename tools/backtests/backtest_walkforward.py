"""크트키 봇 Walk-Forward 검증.

1년 데이터를 4분기로 분할, 각 분기별 EV 일관성 측정.
봇이 룰 기반(파라미터 학습 X)이라 in-sample/out-of-sample 학습 단계는 없음.
대신 "동일 룰을 다른 기간에 적용 시 결과 일관성" 으로 robustness 검증.

기각 신호:
  - 한 분기만 큰 양수, 나머지 음수/0 → 우연/regime 특이
  - MaxDD 분기별 편차 큼 → 변동성 위험
  - Win% / avg R 분기별 큰 차이 → overfit 의심

수용 신호:
  - 4분기 모두 EV 양수 + 합리적 일관성 → robust
  - Win%/avg R 분기별 ±15% 이내 변동
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from statistics import median, mean, stdev
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backtest_realistic_pnl import collect_entries, SYMBOLS
from backtest_compound_returns import compound_simulation

KST = timezone(timedelta(hours=9))


def fmt_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%Y-%m-%d")


def main():
    print("=== 크트키 봇 Walk-Forward 검증 (1년 → 4분기 분할) ===\n")

    # 1) 4종목 모든 거래 수집
    all_trades = []
    for sym in SYMBOLS:
        trades, days = collect_entries(sym)
        for t in trades:
            t["symbol"] = sym
            all_trades.append(t)
    all_trades.sort(key=lambda x: x["bar_ts"])

    if not all_trades:
        print("거래 없음")
        return

    ts_first = all_trades[0]["bar_ts"]
    ts_last = all_trades[-1]["bar_ts"]
    total_days = (ts_last - ts_first) / (1000 * 86400)

    # 4분기 경계 (시간순 4등분)
    q_duration = (ts_last - ts_first) / 4
    quarters = []
    for q in range(4):
        q_start = ts_first + q * q_duration
        q_end = ts_first + (q + 1) * q_duration if q < 3 else ts_last + 1
        q_trades = [t for t in all_trades if q_start <= t["bar_ts"] < q_end]
        quarters.append({
            "q": q + 1,
            "start_ts": q_start, "end_ts": q_end,
            "start_date": fmt_date(int(q_start)), "end_date": fmt_date(int(q_end)),
            "trades": q_trades,
        })

    # 2) 각 분기별 통계
    print(f"전체: {fmt_date(ts_first)} ~ {fmt_date(ts_last)} ({total_days:.0f}일, {len(all_trades)}거래)\n")
    print("=" * 110)
    print(f"{'분기':>4} | {'기간':>23} | {'N':>4} | {'avgR':>7} | {'Win%':>5} | {'SL%':>5} | {'rawΣR':>8} | {'cost':>8} | {'finalR':>8}")
    print("-" * 110)
    quarter_stats = []
    for q in quarters:
        qts = q["trades"]
        if not qts:
            print(f"{q['q']:>4} | {q['start_date']} ~ {q['end_date']} | (거래 없음)")
            continue
        n = len(qts)
        final_rs = [t["final_r"] for t in qts]
        raw_rs = [t["raw_r"] for t in qts]
        costs_rs = [t["costs_r"] for t in qts]
        wins = sum(1 for r in final_rs if r > 0)
        sl_count = sum(1 for t in qts if t["outcome"] in ("SL_only",))
        avg_r = mean(final_rs)
        win_pct = 100 * wins / n
        sl_pct = 100 * sl_count / n
        sum_raw = sum(raw_rs)
        sum_cost = sum(costs_rs)
        sum_final = sum(final_rs)
        quarter_stats.append({
            "q": q["q"], "n": n, "avg_r": avg_r,
            "win_pct": win_pct, "sl_pct": sl_pct,
            "sum_raw_r": sum_raw, "sum_cost_r": sum_cost, "sum_final_r": sum_final,
        })
        print(f"{q['q']:>4} | {q['start_date']} ~ {q['end_date']} | {n:>4} | {avg_r:>+6.3f}R | {win_pct:>4.1f}% | {sl_pct:>4.1f}% | {sum_raw:>+7.2f}R | -{sum_cost:>6.2f}R | {sum_final:>+7.2f}R")
    print("=" * 110)

    # 3) 일관성 검증
    if len(quarter_stats) >= 2:
        avgr_values = [s["avg_r"] for s in quarter_stats]
        winpct_values = [s["win_pct"] for s in quarter_stats]
        finalr_values = [s["sum_final_r"] for s in quarter_stats]
        avgr_mean = mean(avgr_values)
        avgr_stdev = stdev(avgr_values) if len(avgr_values) > 1 else 0
        winpct_mean = mean(winpct_values)
        winpct_stdev = stdev(winpct_values) if len(winpct_values) > 1 else 0
        finalr_mean = mean(finalr_values)
        finalr_stdev = stdev(finalr_values) if len(finalr_values) > 1 else 0

        pos_quarters = sum(1 for r in finalr_values if r > 0)

        print(f"\n📊 일관성 통계:")
        print(f"   avg R: 평균 {avgr_mean:+.3f}R, std ±{avgr_stdev:.3f}R, CV {abs(avgr_stdev/avgr_mean)*100:.0f}%")
        print(f"   Win%:  평균 {winpct_mean:.1f}%, std ±{winpct_stdev:.1f}%")
        print(f"   finalΣR: 평균 {finalr_mean:+.2f}R/분기, std ±{finalr_stdev:.2f}R")
        print(f"   양수 분기: {pos_quarters}/4")

        # 판정
        print(f"\n🔍 Walk-Forward 판정:")
        if pos_quarters == 4 and avgr_stdev / abs(avgr_mean) < 0.5:
            print("   ✅ ROBUST — 4분기 모두 양수 + 변동성 합리적. 봇 룰이 일관성 있다.")
        elif pos_quarters >= 3:
            print("   🟡 약한 ROBUST — 3/4 분기 양수. 1분기는 regime 특이? 신뢰 부분적.")
        elif pos_quarters == 2:
            print("   ⚠️ 변동성 큼 — 2/4 분기만 양수. 우연/regime 의존 가능성.")
        else:
            print("   🚫 FAIL — 다수 분기 음수. 봇 룰 신뢰 X. 과최적화 의심.")

        if avgr_stdev / max(abs(avgr_mean), 1e-9) > 1.0:
            print("   ⚠️ avg R 변동성 100% 이상 — 매우 불안정. 안정성 우려.")

    # 4) 각 분기별 복리 시뮬 (1% 리스크 + Kris 3.5% 리스크)
    print(f"\n📈 분기별 복리 시뮬:")
    print("=" * 100)
    print(f"{'분기':>4} | {'N':>4} | {'복리 1% 리스크':>15} | {'배수':>7} | {'MaxDD':>7} | {'복리 3.5% (Kris)':>17} | {'배수':>8} | {'MaxDD':>7}")
    print("-" * 100)
    for q in quarters:
        qts = q["trades"]
        if not qts:
            continue
        comp_1 = compound_simulation(qts, 1.0)
        comp_35 = compound_simulation(qts, 3.5)
        print(f"{q['q']:>4} | {len(qts):>4} | {comp_1['return_pct']:>+13.1f}% | {comp_1['multiplier']:>6.2f}× | {comp_1['max_dd_pct']:>6.1f}% | {comp_35['return_pct']:>+15.1f}% | {comp_35['multiplier']:>7.2f}× | {comp_35['max_dd_pct']:>6.1f}%")
    print("=" * 100)

    # 5) 종합 — 전체 1년 vs 분기별 평균 외삽 비교 (overfit 검출)
    full_year = compound_simulation(all_trades, 1.0)
    full_year_35 = compound_simulation(all_trades, 3.5)
    avg_quarter_return_1 = mean([compound_simulation(q["trades"], 1.0)["return_pct"] for q in quarters if q["trades"]])
    extrapolated_year_1 = ((1 + avg_quarter_return_1 / 100) ** 4 - 1) * 100

    print(f"\n🎯 종합 — 전체 1년 vs 분기 평균 외삽 (overfit 검출):")
    print(f"   전체 1년 (실측, 1% 리스크): {full_year['return_pct']:+.1f}%  (배수 {full_year['multiplier']:.2f}×)")
    print(f"   분기 평균 × 4 외삽: {extrapolated_year_1:+.1f}%")
    if full_year['return_pct'] > extrapolated_year_1 * 1.5:
        print("   ⚠️ 실측이 외삽보다 50% 이상 큼 — 특정 분기 우연 효과 가능성. overfit 의심.")
    elif full_year['return_pct'] < extrapolated_year_1 * 0.5:
        print("   ⚠️ 실측이 외삽보다 50% 이상 작음 — drawdown 누적 효과.")
    else:
        print("   ✅ 실측과 외삽 일치 — 분기별 결과 누적이 자연스러움.")

    # 저장
    out_path = ROOT / "backtest_data" / "walkforward.json"
    out_path.write_text(json.dumps({
        "quarters": [{
            "q": s["q"], "n": s["n"], "avg_r": round(s["avg_r"], 3),
            "win_pct": round(s["win_pct"], 1), "sl_pct": round(s["sl_pct"], 1),
            "sum_final_r": round(s["sum_final_r"], 2),
        } for s in quarter_stats],
        "consistency": {
            "positive_quarters": sum(1 for s in quarter_stats if s["sum_final_r"] > 0),
            "avg_r_mean": round(mean([s["avg_r"] for s in quarter_stats]), 3),
            "avg_r_stdev": round(stdev([s["avg_r"] for s in quarter_stats]) if len(quarter_stats) > 1 else 0, 3),
            "win_pct_mean": round(mean([s["win_pct"] for s in quarter_stats]), 1),
            "win_pct_stdev": round(stdev([s["win_pct"] for s in quarter_stats]) if len(quarter_stats) > 1 else 0, 1),
        },
        "full_year_compound_1pct": full_year,
        "extrapolated_year_1pct": round(extrapolated_year_1, 1),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
