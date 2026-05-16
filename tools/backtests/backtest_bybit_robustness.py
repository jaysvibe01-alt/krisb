"""Bybit 데이터로 같은 크트키 룰 백테스트 — Binance 결과와 비교 (robustness 검증).

코덱스 권고 1순위: 거래소 다른 데이터로도 같은 결과 나오면 robustness 최종 확증.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# load 함수 monkey-patch — Binance 대신 Bybit 데이터 사용
import backtest_user_model

CACHE = ROOT / "backtest_data"


def load_bybit(symbol: str, interval: str) -> list[dict]:
    return json.loads((CACHE / f"bybit_{symbol}_{interval}_1y.json").read_text(encoding="utf-8"))


# Monkey-patch
backtest_user_model.load = load_bybit

# 이제 backtest_realistic_pnl 호출하면 자동으로 Bybit 데이터 사용
from backtest_realistic_pnl import collect_entries, SYMBOLS
from backtest_compound_returns import compound_simulation
from statistics import mean, stdev


def main():
    print("=== Bybit 데이터 robustness 검증 ===\n")
    print("(Binance 1년 백테스트 결과와 비교)\n")
    print("Binance 결과 (이전):")
    print("  - 256 거래/년, avg R +0.418, Win 75%, MaxDD 9.7% (1% risk)")
    print("  - 1% 복리: +187%/년, 3.5% 복리: +3457%/년")
    print()
    print("Bybit 결과:")

    all_trades = []
    total_days = 0
    for sym in SYMBOLS:
        trades, days = collect_entries(sym)
        for t in trades:
            t["symbol"] = sym
            all_trades.append(t)
        total_days = max(total_days, days)
        if trades:
            n = len(trades)
            avg_r = sum(t["final_r"] for t in trades) / n
            wins = sum(1 for t in trades if t["final_r"] > 0)
            print(f"  {sym}: {n}거래, avg R {avg_r:+.3f}, Win {100*wins/n:.1f}%")
    all_trades.sort(key=lambda x: x["bar_ts"])
    n_total = len(all_trades)
    if not n_total:
        print("거래 없음 — 데이터 또는 호출 오류")
        return

    # 전체 통계
    final_rs = [t["final_r"] for t in all_trades]
    avg_r = mean(final_rs)
    wins = sum(1 for r in final_rs if r > 0)
    sl_count = sum(1 for t in all_trades if t["outcome"] == "SL_only")
    sum_r = sum(final_rs)

    print(f"\n📊 Bybit 전체 (1년):")
    print(f"   거래수: {n_total}")
    print(f"   avg R: {avg_r:+.3f}R")
    print(f"   합계 R: {sum_r:+.2f}R")
    print(f"   Win%: {100*wins/n_total:.1f}%")
    print(f"   SL%: {100*sl_count/n_total:.1f}%")

    # 복리 시뮬
    print(f"\n💰 복리 시나리오 (Bybit):")
    for label, risk in [("1% (보수)", 1.0), ("1.75% (5x×50%)", 1.75),
                       ("3.5% (Kris 10x×50%)", 3.5), ("5% (공격)", 5.0)]:
        comp = compound_simulation(all_trades, risk)
        print(f"   {label:>22}: 복리 {comp['return_pct']:+.1f}%, 배수 {comp['multiplier']:.2f}×, MaxDD {comp['max_dd_pct']:.1f}%")

    # 비교 — Binance 결과 하드코딩
    BINANCE = {
        "n": 256, "avg_r": 0.418, "win_pct": 75.0, "sl_pct": 22.3,
        "sum_r": 106.73,
        "compound": {"1%": 187.2, "3.5%": 3457}
    }
    bybit = {
        "n": n_total, "avg_r": avg_r,
        "win_pct": 100*wins/n_total, "sl_pct": 100*sl_count/n_total,
        "sum_r": sum_r,
        "compound_1pct": compound_simulation(all_trades, 1.0)["return_pct"],
        "compound_3_5pct": compound_simulation(all_trades, 3.5)["return_pct"],
    }

    print(f"\n=== 거래소 비교: Binance vs Bybit ===")
    print("=" * 90)
    print(f"{'지표':>20} | {'Binance':>15} | {'Bybit':>15} | {'차이':>15} | 일치?")
    print("-" * 90)
    rows = [
        ("거래수/년", BINANCE["n"], bybit["n"]),
        ("avg R", BINANCE["avg_r"], bybit["avg_r"]),
        ("Win%", BINANCE["win_pct"], bybit["win_pct"]),
        ("SL%", BINANCE["sl_pct"], bybit["sl_pct"]),
        ("합계 R/년", BINANCE["sum_r"], bybit["sum_r"]),
        ("복리 1% 리스크", BINANCE["compound"]["1%"], bybit["compound_1pct"]),
        ("복리 3.5% Kris", BINANCE["compound"]["3.5%"], bybit["compound_3_5pct"]),
    ]
    consistent_count = 0
    for label, b_val, by_val in rows:
        diff = by_val - b_val
        diff_pct = abs(diff / b_val * 100) if b_val else 0
        # 일치 판정 ±25% 안
        if diff_pct < 25:
            verdict = "✅ 일치"
            consistent_count += 1
        elif diff_pct < 50:
            verdict = "🟡 약일치"
        else:
            verdict = "❌ 불일치"
        print(f"{label:>20} | {b_val:>14.2f} | {by_val:>14.2f} | {diff:>+13.2f} ({diff_pct:>4.0f}%) | {verdict}")
    print("=" * 90)

    print(f"\n🔍 Robustness 판정:")
    if consistent_count >= 5:
        print("   ✅ STRONG ROBUST — 두 거래소 결과 거의 일치. 봇 룰 거래소 무관 robust.")
    elif consistent_count >= 4:
        print("   🟡 PARTIAL ROBUST — 일부 지표 불일치. 추가 분석 필요.")
    else:
        print("   ⚠️ WEAK ROBUST — 거래소별 결과 큰 차이. 데이터 특이성 의심.")

    # 저장
    out = {
        "binance": BINANCE,
        "bybit": bybit,
        "consistent_count": consistent_count,
        "total_metrics": len(rows),
    }
    out_path = CACHE / "bybit_robustness.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
