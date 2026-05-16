"""12종목 확장 백테스트 — TON + HYPE 추가.

기존 10종 + TONUSDT (THE OPEN NETWORK) + HYPEUSDT (Hyperliquid).
TON 1년 풀 데이터, HYPE 11.5개월 (신규 상장).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from statistics import mean
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backtest_realistic_pnl import collect_entries
from backtest_compound_returns import compound_simulation

SYMBOLS_12 = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",      # 코어 4
    "DOGEUSDT", "LINKUSDT",                            # 신규 ★★
    "ADAUSDT", "AVAXUSDT", "SUIUSDT", "BNBUSDT",       # 신규 ★
    "TONUSDT", "HYPEUSDT",                             # 신규 (사용자 요청)
]


def main():
    print("=== 12종 확장 백테스트 (10종 + TON + HYPE) ===\n")

    all_trades = []
    by_symbol = {}
    for sym in SYMBOLS_12:
        try:
            trades, days = collect_entries(sym)
        except Exception as e:
            print(f"  {sym}: 에러 ({e})")
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
            "avg_r": round(avg_r, 3), "sum_r": round(sum_r, 2),
            "annual_r": round(annual_r, 2),
            "win_pct": round(100 * wins / n, 1),
            "sl_pct": round(100 * sl_only / n, 1),
            "days": days,
        }
        for t in trades:
            t["symbol"] = sym
            all_trades.append(t)
    all_trades.sort(key=lambda x: x["bar_ts"])

    print("=" * 110)
    print(f"{'종목':>10} | {'1년 N':>5} | {'avgR':>7} | {'sum R':>9} | {'Win%':>5} | {'SL%':>5} | {'일/거래':>7} | {'평가':>15}")
    print("-" * 110)
    for sym in SYMBOLS_12:
        if sym not in by_symbol: continue
        r = by_symbol[sym]
        gap_days = 365 / r["annual_n"] if r["annual_n"] else 999
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
        is_new = "(NEW)" if sym in ("TONUSDT", "HYPEUSDT") else ""
        print(f"{sym:>10} | {r['annual_n']:>5.1f} | {r['avg_r']:>+6.3f}R | {r['annual_r']:>+8.2f}R | {r['win_pct']:>4.1f}% | {r['sl_pct']:>4.1f}% | {gap_days:>6.1f}일 | {grade:>10} {is_new}")
    print("=" * 110)

    # 신규 vs 기존 10
    new_2 = [t for t in all_trades if t["symbol"] in ("TONUSDT", "HYPEUSDT")]
    old_10 = [t for t in all_trades if t["symbol"] not in ("TONUSDT", "HYPEUSDT")]
    all_n = len(all_trades)

    print(f"\n📊 종합:")
    print(f"   전체 거래수: {all_n}/년")
    print(f"   기존 10종: {len(old_10)} 거래")
    print(f"   신규 2종 (TON+HYPE): {len(new_2)} 거래")
    print(f"   일 평균: {all_n/365:.2f}건")
    print(f"   합계 R: {sum(t['final_r'] for t in all_trades):+.2f}R")

    print(f"\n💰 복리 시뮬:")
    for label, risk in [("1% (보수)", 1.0), ("1.75% (5x×50%)", 1.75), ("3.5% (사용자)", 3.5)]:
        comp_all = compound_simulation(all_trades, risk)
        comp_10 = compound_simulation(old_10, risk) if old_10 else None
        c10 = f"{comp_10['return_pct']:>+9.1f}%" if comp_10 else "      —"
        print(f"   리스크 {label:>18}: 12종 {comp_all['return_pct']:>+9.1f}% (배수 {comp_all['multiplier']:>6.2f}×) MaxDD {comp_all['max_dd_pct']:>5.1f}% | 10종 {c10}")

    if new_2:
        new2_avg = mean(t["final_r"] for t in new_2)
        new2_win = sum(1 for t in new_2 if t["final_r"] > 0) / len(new_2) * 100
        new2_sum = sum(t["final_r"] for t in new_2)
        print(f"\n📈 신규 2종 (TON+HYPE) 합산: {len(new_2)} 거래, avg R {new2_avg:+.3f}, Win {new2_win:.1f}%, sum R {new2_sum:+.2f}R")

    # 저장
    out = {
        "by_symbol": by_symbol,
        "n_total": all_n,
        "n_old_10": len(old_10),
        "n_new_2": len(new_2),
        "sum_r_total": round(sum(t["final_r"] for t in all_trades), 2),
        "compound_1pct": compound_simulation(all_trades, 1.0),
        "compound_3_5pct": compound_simulation(all_trades, 3.5),
    }
    (ROOT / "backtest_data" / "twelve_symbols.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: backtest_data/twelve_symbols.json")


if __name__ == "__main__":
    main()
