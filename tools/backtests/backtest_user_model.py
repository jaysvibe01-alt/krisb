"""크트키 자리 — 사용자 정의 정확 모델로 백테스트.

사용자 한 줄 정의:
  15분봉 장대 + 거래량 多 + RSI 과매도/과매수 →
  반대색 봉 마감 컨펌 →
  다음봉 밑꼬리(롱) / 윗꼬리(숏) 진입

기존 백테스트 오류:
  entry_price = ev["close"]  (컨펌봉 종가 즉시 진입)

수정 모델 (사용자 정의 ⑥):
  롱: 컨펌봉 다음봉의 low 가 [컨펌봉 low, 컨펌봉 open] 매수존 가로지르면 진입
      미도달 시 no_entry (가격이 안 빠지고 바로 올라가버린 케이스)
  숏: 컨펌봉 다음봉의 high 가 [컨펌봉 open, 컨펌봉 high] 매도존 가로지르면 진입
      미도달 시 no_entry

진입가 = 매수존(매도존) 중간값 (보수적). 진입 후 48봉 verify.

PPT 15분봉 4원칙 (RSI 30/70) 만 충실 평가. LAB 룰은 손대지 않음.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict, deque
from statistics import median, mean, stdev
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot   # noqa
from rsi_alert_core import RSISymbolState

CACHE_DIR = ROOT / "backtest_data"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
VERIFY_BARS = 48

SLIPPAGE_BPS = {"BTCUSDT": 1.0, "ETHUSDT": 2.0, "SOLUSDT": 3.0, "XRPUSDT": 4.0}
FUNDING_PER_8H_PCT = 0.01
BARS_PER_FUNDING = 32


def load(symbol: str, interval: str) -> list[dict]:
    return json.loads((CACHE_DIR / f"binance_{symbol}_{interval}_1y.json").read_text(encoding="utf-8"))


class Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._ctx: dict = {}
    def set(self, **kw): self._ctx = kw
    def send(self, text):
        kind = "krtky" if ("크트키" in text or "🔷" in text) else "rsi"
        level = 1 if "[1] 사전 알림" in text else (
            2 if "[2] 진입 신호" in text else (
            3 if "[3]" in text and "진입 신호" in text else (
            4 if "[4]" in text or "강력 진입" in text else 0)))
        direction = "long" if "LONG" in text else ("short" if "SHORT" in text else "?")
        if level >= 2 and kind == "krtky":
            self.events.append({**self._ctx, "level": level, "direction": direction, "text": text})


def verify_user_model(ev: dict, klines: list[dict],
                      symbol: str, n: int = VERIFY_BARS,
                      realistic: bool = True) -> dict | None:
    """사용자 정의 진입 모델 — 다음봉 wick 도달 시 진입.

    Returns:
      {entered: bool, entry_price, entry_bar_offset, mfe, mae, sl_hit, rr, ...}
      미진입(no_entry) 시 entered=False.
    """
    confirm_idx = ev["bar_idx"]
    if confirm_idx + n + 1 >= len(klines):
        return {"truncated": True}
    direction = ev["direction"]
    if direction not in ("long", "short"):
        return None
    atr = ev["atr"]
    if atr <= 0:
        return None

    confirm_bar = klines[confirm_idx]
    # 매수존 / 매도존 — ★ M2 (low~close / close~high) 백테스트 1위 정의
    if direction == "long":
        zone_low = confirm_bar["low"]
        zone_high = confirm_bar["close"]
        if zone_high < zone_low:
            zone_low, zone_high = zone_high, zone_low
    else:
        zone_low = confirm_bar["close"]
        zone_high = confirm_bar["high"]
        if zone_high < zone_low:
            zone_low, zone_high = zone_high, zone_low

    # 다음봉부터 N봉 안에 wick 가 매수존(매도존) 가로지르는지
    entered = False
    entry_idx = None
    entry_price = None
    # 사용자 정의는 "다음 캔들" — 보수적으로 다음봉(1봉) 만 트리거 후보로 사용
    # PPT 슬라이드 14 보수성 3단계도 "다음봉" 명시
    NEXT_BAR_ONLY = True
    search_range = 1 if NEXT_BAR_ONLY else min(n, 4)   # 1봉 only or 4봉 안

    for j in range(1, search_range + 1):
        k = klines[confirm_idx + j]
        if k["low"] <= zone_high and k["high"] >= zone_low:
            # wick 가로지름 → 매수존 중간값 진입 (보수적)
            entry_price = (zone_low + zone_high) / 2
            entry_idx = confirm_idx + j
            entered = True
            break

    if not entered:
        return {"entered": False, "no_entry_reason": "wick_no_reach"}

    # Realistic 보정 — 슬리피지
    if realistic:
        slip = SLIPPAGE_BPS.get(symbol, 5.0) / 10000
        entry_price *= (1 + slip) if direction == "long" else (1 - slip)

    # 진입 후 48봉 verify
    sl_d = atr * 1.5
    sl = entry_price - sl_d if direction == "long" else entry_price + sl_d
    sl_hit, mfe, mae = False, 0.0, 0.0
    funding_cost = 0.0
    for j in range(n):
        if entry_idx + j + 1 >= len(klines): break
        k = klines[entry_idx + j + 1]
        if realistic and j > 0 and j % BARS_PER_FUNDING == 0:
            funding_cost += FUNDING_PER_8H_PCT

        if direction == "long":
            up = (k["high"] - entry_price) / entry_price * 100 - funding_cost
            dn = (k["low"] - entry_price) / entry_price * 100 - funding_cost
            mfe = max(mfe, up)
            mae = min(mae, dn)
            if k["low"] <= sl: sl_hit = True
        else:
            dn = (entry_price - k["low"]) / entry_price * 100 - funding_cost
            up = (entry_price - k["high"]) / entry_price * 100 - funding_cost
            mfe = max(mfe, dn)
            mae = min(mae, up)
            if k["high"] >= sl: sl_hit = True

    rr = mfe / abs(mae) if mae < 0 else float("inf")
    return {
        "entered": True, "entry_price": entry_price,
        "entry_bar_offset": entry_idx - confirm_idx,
        "sl_hit": sl_hit, "mfe": mfe, "mae": mae, "rr": rr,
        "tp1_hit": mfe >= 1.236, "tp2_hit": mfe >= 2.0,
    }


def run(timeout: int = 8) -> tuple[list[dict], dict[str, list[dict]]]:
    bot.PRE_ALERT_TIMEOUT_BARS = timeout
    bot.SYMBOLS = list(SYMBOLS)
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    c = Collector()
    bot.send_telegram = c.send

    klines_cache = {}
    for symbol in SYMBOLS:
        k15 = load(symbol, "15m"); k4h = load(symbol, "4h"); k1d = load(symbol, "1d")
        klines_cache[symbol] = k15
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
            c.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr)
            try: bot.evaluate_symbol_15m(symbol)
            except: pass
    return c.events, klines_cache


def dist(vals: list[float]) -> dict:
    if not vals: return {"n": 0}
    vs = sorted(vals); n = len(vs)
    return {
        "n": n, "mean": round(mean(vals), 3), "median": round(median(vs), 3),
        "p25": round(vs[n//4], 3), "p75": round(vs[3*n//4], 3),
        "min": round(vs[0], 3), "max": round(vs[-1], 3),
        "std": round(stdev(vals), 3) if n > 1 else 0,
    }


def summary(verified: list[dict]) -> dict:
    if not verified: return {"n": 0}
    mfes = [v["mfe"] for v in verified]
    maes = [v["mae"] for v in verified]
    rrs_raw = [v["rr"] for v in verified]
    rrs = [r for r in rrs_raw if r != float("inf") and r < 100]
    win = sum(1 for v in verified if v["mfe"] > abs(v["mae"]))
    sl = sum(1 for v in verified if v["sl_hit"])
    tp1 = sum(1 for v in verified if v["tp1_hit"])
    tp2 = sum(1 for v in verified if v["tp2_hit"])
    return {
        "n": len(verified),
        "mfe": dist(mfes), "mae": dist(maes), "rr_finite": dist(rrs),
        "rr_outlier_pct": round(100*(len(rrs_raw)-len(rrs))/len(rrs_raw), 1),
        "sl_hit_pct": round(100*sl/len(verified), 1),
        "tp1_pct": round(100*tp1/len(verified), 1),
        "tp2_pct": round(100*tp2/len(verified), 1),
        "win_rate_pct": round(100*win/len(verified), 1),
    }


def main() -> int:
    print(f"=== 크트키 자리 — 사용자 정의 진입 모델 백테스트 ===")
    print(f"  RSI 임계: 30 / 70 (사용자 확정)")
    print(f"  timeout: 8 봉")
    print(f"  진입 모델: 다음봉 wick 가 매수존(매도존) 도달 시 → 매수존 중간값 진입")
    print(f"            미도달 시 no_entry\n")

    t0 = time.time()
    events, klines_cache = run(timeout=8)
    print(f"발사된 [2/3/4] 이벤트: {len(events)} ({time.time()-t0:.1f}s)\n")

    # 두 모델로 verify
    for ev in events:
        v_ideal = verify_user_model(ev, klines_cache[ev["symbol"]], ev["symbol"], realistic=False)
        v_real = verify_user_model(ev, klines_cache[ev["symbol"]], ev["symbol"], realistic=True)
        if v_ideal: ev["ideal"] = v_ideal
        if v_real: ev["realistic"] = v_real

    # 진입 vs 미진입 분리
    entered_ideal = [e["ideal"] for e in events if "ideal" in e and e["ideal"].get("entered")]
    no_entry_ideal = sum(1 for e in events if "ideal" in e and not e["ideal"].get("entered") and "no_entry_reason" in e["ideal"])
    entered_real = [e["realistic"] for e in events if "realistic" in e and e["realistic"].get("entered")]

    print(f"진입 통계 (사용자 룰 모델):")
    print(f"  발사 [2/3/4]: {len(events)}")
    print(f"  미진입 (다음봉 wick 미도달): {no_entry_ideal}")
    print(f"  실 진입 (ideal): {len(entered_ideal)}")
    print(f"  실 진입 (realistic): {len(entered_real)}")
    print(f"  진입률: {100*len(entered_ideal)/len(events):.1f}%\n")

    ideal_stats = summary(entered_ideal)
    real_stats = summary(entered_real)

    print("=" * 90)
    print(f"{'메트릭':>25} | {'IDEAL':>15} | {'REALISTIC':>15} | {'예전 모델 (참고)':>20}")
    print("-" * 90)
    metrics = [
        ("진입 N", lambda s: s.get("n", 0)),
        ("MFE 평균", lambda s: s["mfe"].get("mean", 0)),
        ("MFE 중앙값 ★", lambda s: s["mfe"].get("median", 0)),
        ("MFE 75%", lambda s: s["mfe"].get("p75", 0)),
        ("MAE 평균", lambda s: s["mae"].get("mean", 0)),
        ("MAE 중앙값", lambda s: s["mae"].get("median", 0)),
        ("RR 평균 (finite)", lambda s: s["rr_finite"].get("mean", 0)),
        ("RR 중앙값 ★", lambda s: s["rr_finite"].get("median", 0)),
        ("RR 75%", lambda s: s["rr_finite"].get("p75", 0)),
        ("RR outlier %", lambda s: s.get("rr_outlier_pct", 0)),
        ("SL hit %", lambda s: s.get("sl_hit_pct", 0)),
        ("TP1 (1.236%) hit %", lambda s: s.get("tp1_pct", 0)),
        ("TP2 (2%) hit %", lambda s: s.get("tp2_pct", 0)),
        ("Win rate %", lambda s: s.get("win_rate_pct", 0)),
    ]
    # 예전 모델 (참고) — backtest_realistic.json 에서
    try:
        prev = json.loads((CACHE_DIR / "realistic_backtest.json").read_text(encoding="utf-8"))["realistic"]
    except:
        prev = {}
    for label, getter in metrics:
        vi = getter(ideal_stats); vr = getter(real_stats); vp = getter(prev) if prev else "-"
        print(f"{label:>25} | {str(vi):>15} | {str(vr):>15} | {str(vp):>20}")
    print("=" * 90)

    # 종목 / 방향 분리
    by_dir_sym = defaultdict(list)
    for e in events:
        if "realistic" in e and e["realistic"].get("entered"):
            by_dir_sym[f"{e['direction']} / {e['symbol']}"].append(e["realistic"])
    print("\n【방향 × 종목 (realistic, 사용자 모델)】")
    print(f"{'그룹':>25} | {'N':>5} | {'RR 중앙':>8} | {'Win%':>6} | {'SL%':>6} | {'TP1%':>6}")
    print("-" * 70)
    rows = []
    for g, lst in by_dir_sym.items():
        s = summary(lst)
        if s.get("n", 0) < 5: continue
        rows.append((g, s))
    for g, s in sorted(rows, key=lambda x: -x[1]["rr_finite"].get("median", 0)):
        rr_med = s["rr_finite"].get("median", 0)
        star = " ★★" if rr_med > 1.5 else (" ★" if rr_med > 1.2 else (" ↓" if rr_med < 0.8 else ""))
        print(f"{g:>25} | {s['n']:>5} | {rr_med:>7.2f}{star} | {s['win_rate_pct']:>5.1f}% | {s['sl_hit_pct']:>5.1f}% | {s['tp1_pct']:>5.1f}%")

    out_path = CACHE_DIR / "user_model_backtest.json"
    out_path.write_text(json.dumps({
        "config": {"rsi": "30/70", "timeout": 8, "entry_model": "next_bar_wick_zone",
                   "slippage_bps": SLIPPAGE_BPS, "funding_per_8h_pct": FUNDING_PER_8H_PCT},
        "ideal": ideal_stats, "realistic": real_stats,
        "entry_rate_pct": round(100*len(entered_ideal)/len(events), 1) if events else 0,
        "by_dir_symbol": {g: summary(lst) for g, lst in by_dir_sym.items()},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
