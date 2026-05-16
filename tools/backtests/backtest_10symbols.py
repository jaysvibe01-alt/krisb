"""10종목 확장 백테스트 — 기존 4 + 신규 6.

코덱스 권장: 진입 늘리기 1순위 = 종목 확장.
ETH/XRP 형 빈도+품질 내는지 검증.
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
from backtest_user_model import Collector, load
from backtest_realistic_pnl import collect_entries
from backtest_compound_returns import compound_simulation

SYMBOLS_10 = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",      # 기존 4
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT",                # 신규 (확실한 EV)
    "LINKUSDT", "SUIUSDT", "BNBUSDT",                  # 신규 (검증 필요)
]


def main():
    print("=== 10종목 확장 백테스트 (4 + 6) ===\n")
    print("실측 청산 엔진 + 모든 비용 (fee/slippage/funding) 반영\n")

    all_trades = []
    by_symbol = {}
    for sym in SYMBOLS_10:
        try:
            trades, days = collect_entries(sym)
        except Exception as e:
            print(f"  {sym}: 데이터 없음 또는 에러 ({e})")
            continue
        if not trades:
            print(f"  {sym}: 진입 0건")
            continue
        n = len(trades)
        final_rs = [t["final_r"] for t in trades]
        avg_r = mean(final_rs)
        sum_r = sum(final_rs)
        wins = sum(1 for r in final_rs if r > 0)
        sl_only = sum(1 for t in trades if t["outcome"] == "SL_only")
        annual_n = n / days * 365 if days else 0
        annual_r = sum_r / days * 365 if days else 0

        by_symbol[sym] = {
            "n": n, "annual_n": round(annual_n, 1),
            "avg_r": round(avg_r, 3),
            "sum_r": round(sum_r, 2),
            "annual_r": round(annual_r, 2),
            "win_pct": round(100 * wins / n, 1),
            "sl_pct": round(100 * sl_only / n, 1),
            "days": days,
        }
        for t in trades:
            t["symbol"] = sym
            all_trades.append(t)
    all_trades.sort(key=lambda x: x["bar_ts"])

    # 종목별 결과
    print("=" * 105)
    print(f"{'종목':>10} | {'1년 N':>5} | {'avgR':>7} | {'sum R':>9} | {'Win%':>5} | {'SL%':>5} | {'일/거래':>8} | {'평가':>10}")
    print("-" * 105)
    for sym in SYMBOLS_10:
        if sym not in by_symbol: continue
        r = by_symbol[sym]
        gap_days = 365 / r["annual_n"] if r["annual_n"] else 999
        # 평가: ETH/XRP 수준 (>50 거래 + avg R > 0.3) = 좋음
        if r["annual_n"] >= 50 and r["avg_r"] >= 0.3:
            grade = "★★ 우수"
        elif r["annual_n"] >= 30 and r["avg_r"] >= 0.2:
            grade = "★ 채택"
        elif r["annual_n"] >= 20 and r["avg_r"] >= 0.1:
            grade = "🟡 보조"
        elif r["avg_r"] >= 0.0:
            grade = "△ 약함"
        else:
            grade = "❌ 손해"
        is_new = "(신규)" if sym in ("DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","SUIUSDT","BNBUSDT") else ""
        print(f"{sym:>10} | {r['annual_n']:>5.1f} | {r['avg_r']:>+6.3f}R | {r['annual_r']:>+8.2f}R | {r['win_pct']:>4.1f}% | {r['sl_pct']:>4.1f}% | {gap_days:>7.1f}일 | {grade:>10} {is_new}")
    print("=" * 105)

    # 전체 vs 기존 4종 비교
    new_4 = [t for t in all_trades if t["symbol"] in ("BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT")]
    new_6 = [t for t in all_trades if t["symbol"] not in ("BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT")]
    all_n = len(all_trades)

    print(f"\n📊 종합:")
    print(f"   전체 거래수: {all_n}/년 (기존 4종 {len(new_4)} + 신규 6종 {len(new_6)})")
    print(f"   일 평균: {all_n/365:.2f}건 (기존 {len(new_4)/365:.2f}건 + 신규 {len(new_6)/365:.2f}건)")
    print(f"   합계 R: {sum(t['final_r'] for t in all_trades):+.2f}R")

    print(f"\n💰 복리 시뮬:")
    for label, risk in [("1%", 1.0), ("3.5% (사용자)", 3.5)]:
        comp_all = compound_simulation(all_trades, risk)
        comp_4 = compound_simulation(new_4, risk) if new_4 else {"return_pct": 0, "multiplier": 1, "max_dd_pct": 0}
        print(f"   리스크 {label:>13}: 10종 {comp_all['return_pct']:>+8.1f}% (배수 {comp_all['multiplier']:>5.2f}×) MaxDD {comp_all['max_dd_pct']:>5.1f}% | 4종 {comp_4['return_pct']:>+8.1f}%")

    print(f"\n=== 신규 vs 기존 비교 ===")
    print(f"기존 4종: {len(new_4)} 거래/년, 합계 R {sum(t['final_r'] for t in new_4):+.2f}R")
    print(f"신규 6종: {len(new_6)} 거래/년, 합계 R {sum(t['final_r'] for t in new_6):+.2f}R")
    if new_6:
        new6_avg = mean(t["final_r"] for t in new_6)
        new6_win = sum(1 for t in new_6 if t["final_r"] > 0) / len(new_6) * 100
        print(f"  신규 6종 avg R: {new6_avg:+.3f}R, Win {new6_win:.1f}%")

    # 저장
    out = {
        "by_symbol": by_symbol,
        "summary": {
            "n_total": all_n,
            "n_existing_4": len(new_4),
            "n_new_6": len(new_6),
            "sum_r_total": round(sum(t["final_r"] for t in all_trades), 2),
            "compound_1pct": compound_simulation(all_trades, 1.0),
            "compound_3_5pct": compound_simulation(all_trades, 3.5),
        }
    }
    (ROOT / "backtest_data" / "ten_symbols.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: backtest_data/ten_symbols.json")


if __name__ == "__main__":
    main()
