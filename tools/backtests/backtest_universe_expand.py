"""유니버스 확장 백테스트 — 12종 + 신규 후보 (가용한 것만).

★★ 우수 (avg R ≥ 0.30 + Win ≥ 75%): SYMBOLS + PREMIUM 채택
★ 채택 (avg R ≥ 0.20 + Win ≥ 70%): SYMBOLS 채택
△/❌: 채택 X
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

# 데이터 가용 확인
CACHE = ROOT / "backtest_data"
EXISTING_12 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
               "DOGEUSDT", "LINKUSDT", "ADAUSDT", "AVAXUSDT",
               "SUIUSDT", "BNBUSDT", "TONUSDT", "HYPEUSDT"]
NEW_CANDIDATES = ["LTCUSDT", "TRXUSDT", "BCHUSDT", "DOTUSDT",
                  "ETCUSDT", "FILUSDT", "ARBUSDT", "OPUSDT",
                  "NEARUSDT", "APTUSDT", "SEIUSDT", "AAVEUSDT",
                  "INJUSDT", "TIAUSDT", "PEPEUSDT", "WIFUSDT"]


def has_data(sym):
    return (CACHE / f"binance_{sym}_15m_1y.json").exists()


def main():
    print("=== 유니버스 확장 백테스트 ===\n")

    # 기존 12종은 결과 있음 (twelve_symbols.json)
    print("기존 12종 결과 (참고):")
    try:
        existing = json.loads((CACHE / "twelve_symbols.json").read_text(encoding="utf-8"))
        print(f"  거래 {existing['n_total']}, 합계 R {existing['sum_r_total']:+.2f}\n")
    except Exception: pass

    # 신규 후보 백테스트
    print("신규 후보 백테스트:")
    print("=" * 110)
    print(f"{'종목':>10} | {'데이터':>6} | {'1년 N':>5} | {'avgR':>7} | {'sum R':>9} | {'Win%':>5} | {'SL%':>5} | {'평가':>15}")
    print("-" * 110)
    accepted = []
    accepted_premium = []
    all_new_trades = []
    for sym in NEW_CANDIDATES:
        if not has_data(sym):
            print(f"{sym:>10} | {'NO':>6} | {'데이터 없음 — Rate limit 또는 미상장':>60}")
            continue
        try:
            trades, days = collect_entries(sym)
        except Exception as e:
            print(f"{sym:>10} | {'ERR':>6} | {str(e)[:60]}")
            continue
        if not trades:
            print(f"{sym:>10} | {'OK':>6} | {'진입 0건':>20}")
            continue
        n = len(trades)
        final_rs = [t["final_r"] for t in trades]
        avg_r = mean(final_rs)
        sum_r = sum(final_rs)
        wins = sum(1 for r in final_rs if r > 0)
        sl_only = sum(1 for t in trades if t["outcome"] == "SL_only")
        win_pct = 100 * wins / n
        sl_pct = 100 * sl_only / n

        # 등급
        if avg_r >= 0.35 and win_pct >= 76 and n >= 50:
            grade = "★★ 우수 (PREMIUM)"; accepted.append(sym); accepted_premium.append(sym)
        elif avg_r >= 0.30 and win_pct >= 73 and n >= 50:
            grade = "★★ 우수"; accepted.append(sym); accepted_premium.append(sym)
        elif avg_r >= 0.20 and win_pct >= 70 and n >= 30:
            grade = "★ 채택"; accepted.append(sym)
        elif avg_r >= 0.10:
            grade = "🟡 보조"
        elif avg_r >= 0:
            grade = "△ 약함"
        else:
            grade = "❌ 손해"

        print(f"{sym:>10} | {'OK':>6} | {n:>5} | {avg_r:>+6.3f}R | {sum_r:>+8.2f}R | {win_pct:>4.1f}% | {sl_pct:>4.1f}% | {grade:>20}")

        for t in trades:
            t["symbol"] = sym
            all_new_trades.append(t)
    print("=" * 110)

    print(f"\n📊 자동 채택 결과:")
    print(f"   ★★ PREMIUM 후보: {accepted_premium}")
    print(f"   ★ 채택: {[s for s in accepted if s not in accepted_premium]}")
    print(f"   전체 채택: {len(accepted)}개")

    # 통합 효과 시뮬 (12종 + 신규 채택)
    if accepted:
        print(f"\n📈 12종 + 신규 채택 통합 시뮬:")
        all_trades = []
        for sym in EXISTING_12 + accepted:
            try:
                trades, _ = collect_entries(sym)
                for t in trades:
                    t["symbol"] = sym
                    all_trades.append(t)
            except Exception: pass
        all_trades.sort(key=lambda x: x["bar_ts"])
        n_total = len(all_trades)
        sum_r = sum(t["final_r"] for t in all_trades)
        print(f"   거래수: {n_total}/년 (일 {n_total/365:.2f}건)")
        print(f"   합계 R: {sum_r:+.2f}R")

        for label, risk in [("1%", 1.0), ("3.5%", 3.5)]:
            comp = compound_simulation(all_trades, risk)
            print(f"   복리 {label}: {comp['return_pct']:+.1f}% (배수 {comp['multiplier']:.2f}×, MaxDD {comp['max_dd_pct']:.1f}%)")

    # 저장
    out = {
        "accepted": accepted,
        "accepted_premium": accepted_premium,
        "new_trades_count": len(all_new_trades),
    }
    (CACHE / "universe_expand.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
