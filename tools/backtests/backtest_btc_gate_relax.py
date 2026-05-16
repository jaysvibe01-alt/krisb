"""BTC 게이트 완화 시뮬 — 신호 빈도 vs EV 트레이드오프.

현재: Long 06-12 KST, Short 06-18 KST, CT 1개 필수 (Long)
시나리오:
  S1. 시간 확장: Long 06-15, Short 04-20
  S2. 시간 더 확장: Long 06-18, Short 00-22
  S3. 시간 게이트 제거 (CT 만 유지)
  S4. CT 게이트 제거 (시간만 유지)
  S5. 둘 다 제거 (baseline)
  S6. 시간 게이트 제거 + RSI hard 만
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import mean, median
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import verify_user_model, Collector, load
from backtest_realistic_pnl import simulate_trade, MAX_HOLD_BARS

KST = timezone(timedelta(hours=9))


def kst_hour(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=KST).hour


# 시나리오 정의
SCENARIOS = {
    "현재": {"long_hours": range(6, 12), "short_hours": range(6, 18), "ct_required": True},
    "S1_시간확장": {"long_hours": range(6, 15), "short_hours": range(4, 20), "ct_required": True},
    "S2_시간더확장": {"long_hours": range(6, 18), "short_hours": range(0, 22), "ct_required": True},
    "S3_시간게이트제거": {"long_hours": None, "short_hours": None, "ct_required": True},
    "S4_CT게이트제거": {"long_hours": range(6, 12), "short_hours": range(6, 18), "ct_required": False},
    "S5_둘다제거": {"long_hours": None, "short_hours": None, "ct_required": False},
}


def collect_btc_raw():
    """BTC baseline — 모든 게이트 끄고 4원칙만 통과한 진입 수집."""
    bot.PRE_ALERT_TIMEOUT_BARS = 8
    bot.RSI_OVERSOLD = 30
    bot.RSI_OVERBOUGHT = 70
    bot.SYMBOLS = ["BTCUSDT"]
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    bot.BTC_TIME_GATE_ENABLED = False  # 게이트 OFF
    bot.BTC_CT_GATE_ENABLED = False
    c = Collector()
    bot.send_telegram = c.send

    k15 = load("BTCUSDT", "15m"); k4h = load("BTCUSDT", "4h"); k1d = load("BTCUSDT", "1d")
    bot.STATE["BTCUSDT"] = bot.SymbolState()
    bot.RSI_STATE["BTCUSDT"] = RSISymbolState()
    bot.SERIES_15M["BTCUSDT"] = deque(maxlen=200); bot.SERIES_4H["BTCUSDT"] = deque(maxlen=100); bot.SERIES_1D["BTCUSDT"] = deque(maxlen=50)
    bot.ISOLATED_SR_CACHE["BTCUSDT"] = []
    for k in k15[:50]: bot.SERIES_15M["BTCUSDT"].append(k)
    for k in k4h[:8]: bot.SERIES_4H["BTCUSDT"].append(k)
    for k in k1d[:10]: bot.SERIES_1D["BTCUSDT"].append(k)
    last_4h = bot.SERIES_4H["BTCUSDT"][-1]["open_time"]; last_1d = bot.SERIES_1D["BTCUSDT"][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h); i1d = sum(1 for k in k1d if k["open_time"] <= last_1d)
    for i in range(50, len(k15)):
        kk = k15[i]; bot.SERIES_15M["BTCUSDT"].append(kk)
        while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
            bot.SERIES_4H["BTCUSDT"].append(k4h[i4]); i4 += 1
        while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
            bot.SERIES_1D["BTCUSDT"].append(k1d[i1d]); i1d += 1
        atr = bot.calc_atr(list(bot.SERIES_15M["BTCUSDT"]))
        c.set(symbol="BTCUSDT", bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try: bot.evaluate_symbol_15m("BTCUSDT")
        except Exception: pass

    # 게이트 다시 ON (다른 스크립트 영향 X)
    bot.BTC_TIME_GATE_ENABLED = True
    bot.BTC_CT_GATE_ENABLED = True

    # CT 시그널 / 시간 정보 + 청산 시뮬
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

        # CT 시그널 (다이버/흡수/SR/HTF) — ev["text"] 에 있음
        text = ev.get("text", "")
        has_ct = any(kw in text for kw in ["다이버전스", "흡수 누적", "SR Flip", "과매도 컨플루언스", "과매수 컨플루언스"])

        sim = simulate_trade(entry_price, sl_price, direction, path, "BTCUSDT")
        sim["direction"] = direction; sim["bar_ts"] = ev["bar_ts"]
        sim["hour_kst"] = kst_hour(ev["bar_ts"])
        sim["has_ct"] = has_ct
        enriched.append(sim)
    return enriched


def apply_scenario(trades, scenario):
    """시나리오 룰 적용 → 통과 거래만 반환."""
    out = []
    for t in trades:
        # 시간 게이트
        if scenario["long_hours"] is not None and t["direction"] == "long":
            if t["hour_kst"] not in scenario["long_hours"]: continue
        if scenario["short_hours"] is not None and t["direction"] == "short":
            if t["hour_kst"] not in scenario["short_hours"]: continue
        # CT 게이트 (Long 만)
        if scenario["ct_required"] and t["direction"] == "long" and not t["has_ct"]:
            continue
        out.append(t)
    return out


def stats(trades):
    if not trades: return None
    n = len(trades)
    rrs = []
    for t in trades:
        r = t["final_r"]
        rrs.append(r)
    avg_r = mean(rrs)
    sum_r = sum(rrs)
    wins = sum(1 for r in rrs if r > 0)
    sl_count = sum(1 for t in trades if t["outcome"] == "SL_only")
    long_count = sum(1 for t in trades if t["direction"] == "long")
    short_count = n - long_count
    return {
        "n": n, "avg_r": round(avg_r, 3), "sum_r": round(sum_r, 2),
        "win_pct": round(100 * wins / n, 1),
        "sl_pct": round(100 * sl_count / n, 1),
        "long_n": long_count, "short_n": short_count,
    }


def main():
    print("=== BTC 게이트 완화 시뮬 (사용자 지적: BTC 진입 너무 빡빡) ===\n")
    print("Baseline (게이트 X): 4원칙만 적용한 BTC 진입 수집 중...")
    trades = collect_btc_raw()
    print(f"4원칙 통과 BTC 거래: {len(trades)}건\n")

    # 시간대 분포
    print("📊 시간대별 자연 분포 (모든 게이트 OFF):")
    hour_dist = defaultdict(lambda: defaultdict(int))
    for t in trades:
        bucket = f"{t['hour_kst']//6*6:02d}-{t['hour_kst']//6*6+6:02d}"
        hour_dist[bucket][t["direction"]] += 1
    for bucket in sorted(hour_dist.keys()):
        long_n = hour_dist[bucket]["long"]
        short_n = hour_dist[bucket]["short"]
        print(f"   {bucket} KST: Long {long_n}건, Short {short_n}건")
    print()

    # 시나리오 비교
    print("=" * 110)
    print(f"{'시나리오':>20} | {'N':>4} | {'Long':>4} | {'Short':>4} | {'avgR':>7} | {'sumR':>9} | {'Win%':>5} | {'SL%':>5} | {'일/건':>7} | {'평가':>20}")
    print("-" * 110)
    base_stats = None
    for name, scen in SCENARIOS.items():
        sub = apply_scenario(trades, scen)
        s = stats(sub)
        if not s:
            print(f"{name:>20} | 거래 없음")
            continue
        days_per = 365 / s["n"] if s["n"] else 999
        rating = ""
        if s["avg_r"] >= 0.4 and s["n"] >= 50: rating = "★★ 강추"
        elif s["avg_r"] >= 0.3 and s["n"] >= 30: rating = "★ 채택"
        elif s["avg_r"] >= 0.2: rating = "🟡 보조"
        elif s["avg_r"] >= 0.0: rating = "△ 약함"
        else: rating = "❌ 손해"
        if name == "현재":
            base_stats = s
            rating = f"기준 {rating}"
        print(f"{name:>20} | {s['n']:>4} | {s['long_n']:>4} | {s['short_n']:>4} | {s['avg_r']:>+6.3f}R | {s['sum_r']:>+8.2f}R | {s['win_pct']:>4.1f}% | {s['sl_pct']:>4.1f}% | {days_per:>6.1f}일 | {rating:>20}")
    print("=" * 110)
    print()

    # 추천 (1% 리스크 가정 + 동시 포지션 1 가정)
    print("💡 1% 리스크 기준 연 R 환산:")
    for name, scen in SCENARIOS.items():
        sub = apply_scenario(trades, scen)
        s = stats(sub)
        if not s: continue
        # 단순 합산 (복리 X)
        annual = s["sum_r"]
        marker = "★" if s["avg_r"] >= 0.3 and s["n"] >= base_stats["n"] * 1.5 else " "
        print(f"   {marker} {name:>20}: N={s['n']:>3}, 연 R {annual:>+7.2f}R ({s['avg_r']:>+5.3f}R/거래)")
    print()

    print("📌 핵심 발견:")
    s_cur = stats(apply_scenario(trades, SCENARIOS["현재"]))
    s_s1 = stats(apply_scenario(trades, SCENARIOS["S1_시간확장"]))
    s_s2 = stats(apply_scenario(trades, SCENARIOS["S2_시간더확장"]))
    s_s3 = stats(apply_scenario(trades, SCENARIOS["S3_시간게이트제거"]))
    print(f"   현재 N={s_cur['n']} avgR {s_cur['avg_r']:+.3f}R")
    if s_s1: print(f"   S1 시간 확장 N={s_s1['n']} avgR {s_s1['avg_r']:+.3f}R (현재 대비 N {(s_s1['n']/s_cur['n']-1)*100:+.0f}%, avgR {(s_s1['avg_r']-s_cur['avg_r']):+.3f})")
    if s_s2: print(f"   S2 시간 더 확장 N={s_s2['n']} avgR {s_s2['avg_r']:+.3f}R (현재 대비 N {(s_s2['n']/s_cur['n']-1)*100:+.0f}%, avgR {(s_s2['avg_r']-s_cur['avg_r']):+.3f})")
    if s_s3: print(f"   S3 시간 게이트 제거 N={s_s3['n']} avgR {s_s3['avg_r']:+.3f}R (현재 대비 N {(s_s3['n']/s_cur['n']-1)*100:+.0f}%, avgR {(s_s3['avg_r']-s_cur['avg_r']):+.3f})")

    # 저장
    out = {name: stats(apply_scenario(trades, scen)) for name, scen in SCENARIOS.items()}
    (ROOT / "backtest_data" / "btc_gate_relax.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
