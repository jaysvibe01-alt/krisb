"""크트키 자리 — 매수존(매도존) 정의 5가지 비교 백테스트.

사용자 정의 한 줄:
  15분봉 장대 + 거래량 多 + RSI 과매도/과매수 →
  반대색 봉 마감 컨펌 →
  다음봉 wick 가 "매수존" 가로지를 때 진입

매수존 정의 5가지 (롱):
  M1 — low~open       (사용자 정의, 가장 좁음)
  M2 — low~close      (양봉 컨펌이면 더 넓음)
  M3 — low~mid        ((low+close)/2 까지)
  M4 — low + ATR*0.5  (ATR 기반 적응형 천장)
  M5 — close 즉시     (예전 모델, 100% 진입)

매도존 정의 5가지 (숏): 대칭 — high~open / high~close / mid~high /
                                high - ATR*0.5 / close 즉시

같은 events(367건 추정) 위에서 5가지 zone_def 로 verify_user_model 변형 실행,
진입률 / RR 중앙 / Win / SL / TP1 / TP2 비교.

기본 가정:
  - 진입가 = 매수존 중간값 (보수적 — 사용자 정의와 동일)
  - M5 는 매수존 무관, ev["close"] 즉시 진입
  - SL = entry - ATR*1.5 (롱), entry + ATR*1.5 (숏)
  - 48봉 verify, realistic 슬리피지 + 펀딩 포함
  - 다음봉(1봉) 만 트리거 후보 (사용자 NEXT_BAR_ONLY=True 유지)
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

ZONE_DEFS = ["low_open", "low_close", "low_mid", "low_atr05", "close_now"]
ZONE_LABEL = {
    "low_open":   "M1 (low~open) 사용자 정의",
    "low_close":  "M2 (low~close)",
    "low_mid":    "M3 (low~mid)",
    "low_atr05":  "M4 (low + ATR*0.5)",
    "close_now":  "M5 (close 즉시)",
}


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


def compute_zone(direction: str, confirm_bar: dict, atr: float, zone_def: str) -> tuple[float, float] | None:
    """매수존(매도존) [low, high] 계산. M5(close_now)는 None 반환 (즉시 진입 → zone 미사용)."""
    o = confirm_bar["open"]; c = confirm_bar["close"]
    lo = confirm_bar["low"]; hi = confirm_bar["high"]
    mid = (lo + c) / 2 if direction == "long" else (hi + c) / 2

    if zone_def == "close_now":
        return None  # 즉시 진입 → wick 도달 검사 skip

    if direction == "long":
        if zone_def == "low_open":
            zl, zh = lo, o
        elif zone_def == "low_close":
            zl, zh = lo, c
        elif zone_def == "low_mid":
            zl, zh = lo, mid
        elif zone_def == "low_atr05":
            zl, zh = lo, lo + atr * 0.5
        else:
            return None
    else:  # short — 위쪽 매도존
        if zone_def == "low_open":   # M1 대칭: open~high
            zl, zh = o, hi
        elif zone_def == "low_close":  # M2 대칭: close~high
            zl, zh = c, hi
        elif zone_def == "low_mid":    # M3 대칭: mid~high
            zl, zh = mid, hi
        elif zone_def == "low_atr05":  # M4 대칭: high - ATR*0.5 ~ high
            zl, zh = hi - atr * 0.5, hi
        else:
            return None

    if zh < zl:
        zl, zh = zh, zl
    return (zl, zh)


def verify_with_zone(ev: dict, klines: list[dict], symbol: str,
                     zone_def: str, n: int = VERIFY_BARS,
                     realistic: bool = True) -> dict | None:
    """매수존 정의를 파라미터로 받아서 verify.

    M5 (close_now): 100% 진입 (ev["close"]).
    M1~M4: 다음봉 wick 가 매수존 도달 시 매수존 중간값 진입.
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
    zone = compute_zone(direction, confirm_bar, atr, zone_def)

    entered = False
    entry_idx = None
    entry_price = None
    bars_to_reach = None  # 매수존 도달까지 걸린 봉 수 (1봉 only 이지만 분포용으로 4봉까지 확장 추적)

    # M5: 즉시 진입
    if zone is None:
        entered = True
        entry_idx = confirm_idx + 1
        if entry_idx >= len(klines):
            return {"truncated": True}
        entry_price = klines[entry_idx]["open"]  # 다음봉 open 으로 즉시 진입 (현실적)
        bars_to_reach = 0
    else:
        zone_low, zone_high = zone
        # NEXT_BAR_ONLY = True (사용자 정의 충실 — 다음봉만)
        # 다만 "걸린 봉 수 분포" 분석을 위해 4봉까지 추적 (실제 진입은 1봉 만)
        for j in range(1, 5):
            if confirm_idx + j >= len(klines):
                break
            k = klines[confirm_idx + j]
            if k["low"] <= zone_high and k["high"] >= zone_low:
                if j == 1:
                    entry_price = (zone_low + zone_high) / 2
                    entry_idx = confirm_idx + j
                    entered = True
                bars_to_reach = j
                break
        if not entered:
            return {"entered": False, "no_entry_reason": "wick_no_reach", "bars_to_reach": None}

    # Realistic 보정
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
        "bars_to_reach": bars_to_reach,
        "sl_hit": sl_hit, "mfe": mfe, "mae": mae, "rr": rr,
        "tp1_hit": mfe >= 1.236, "tp2_hit": mfe >= 2.0,
    }


def run_events(timeout: int = 8) -> tuple[list[dict], dict[str, list[dict]]]:
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
        "rr_outlier_pct": round(100*(len(rrs_raw)-len(rrs))/len(rrs_raw), 1) if rrs_raw else 0,
        "sl_hit_pct": round(100*sl/len(verified), 1),
        "tp1_pct": round(100*tp1/len(verified), 1),
        "tp2_pct": round(100*tp2/len(verified), 1),
        "win_rate_pct": round(100*win/len(verified), 1),
    }


def main() -> int:
    print(f"=== 크트키 자리 — 매수존 5가지 비교 백테스트 ===")
    print(f"  RSI 임계: 30 / 70")
    print(f"  timeout: 8 봉, verify: 48봉")
    print(f"  슬리피지 / 펀딩 적용 (realistic)\n")

    t0 = time.time()
    events, klines_cache = run_events(timeout=8)
    n_events = len(events)
    print(f"발사된 [2/3/4] 이벤트: {n_events} ({time.time()-t0:.1f}s)\n")
    if n_events == 0:
        print("이벤트가 0건이라 종료.")
        return 1

    # 5가지 zone_def 동시 검증
    results: dict[str, list[dict]] = {z: [] for z in ZONE_DEFS}
    no_entry_count: dict[str, int] = {z: 0 for z in ZONE_DEFS}
    bars_dist: dict[str, list[int]] = {z: [] for z in ZONE_DEFS}  # 1=다음봉, 2=2번째, ...
    bars_no_reach: dict[str, int] = {z: 0 for z in ZONE_DEFS}

    for ev in events:
        symbol = ev["symbol"]; klines = klines_cache[symbol]
        for z in ZONE_DEFS:
            v = verify_with_zone(ev, klines, symbol, z, realistic=True)
            if v is None:
                continue
            if v.get("truncated"):
                continue
            if v.get("entered"):
                results[z].append({**v, "symbol": symbol, "direction": ev["direction"]})
                if v.get("bars_to_reach") is not None:
                    bars_dist[z].append(v["bars_to_reach"])
            else:
                no_entry_count[z] += 1
                # 도달 못한 케이스 — 4봉까지 추적해도 미도달
                if v.get("bars_to_reach") is None:
                    bars_no_reach[z] += 1

    # 출력 비교 매트릭스
    print("=" * 110)
    print(f"{'매수존 정의':<30} | {'진입N':>5} | {'진입률':>7} | {'RR중앙':>7} | {'Win%':>6} | {'SL%':>6} | {'TP1%':>6} | {'TP2%':>6} | {'점수':>7}")
    print("-" * 110)
    matrix = []
    for z in ZONE_DEFS:
        lst = results[z]
        s = summary(lst)
        entry_rate = 100 * s["n"] / n_events if n_events else 0
        rr_med = s["rr_finite"].get("median", 0) if s.get("n", 0) else 0
        score = entry_rate * rr_med / 100  # 0~∞ 점수 (진입률 * RR 중앙)
        row = {
            "zone": z, "label": ZONE_LABEL[z],
            "n": s.get("n", 0), "entry_rate_pct": round(entry_rate, 1),
            "rr_median": rr_med, "win_pct": s.get("win_rate_pct", 0),
            "sl_pct": s.get("sl_hit_pct", 0),
            "tp1_pct": s.get("tp1_pct", 0), "tp2_pct": s.get("tp2_pct", 0),
            "score": round(score, 3),
            "summary": s,
        }
        matrix.append(row)
        print(f"{ZONE_LABEL[z]:<30} | {row['n']:>5} | {row['entry_rate_pct']:>6.1f}% | {row['rr_median']:>7.2f} | {row['win_pct']:>5.1f}% | {row['sl_pct']:>5.1f}% | {row['tp1_pct']:>5.1f}% | {row['tp2_pct']:>5.1f}% | {row['score']:>7.3f}")
    print("=" * 110)

    # 매수존 도달까지 걸린 봉 수 분포
    print("\n【매수존 도달까지 걸린 봉 수 분포 (M1~M4)】")
    print(f"{'매수존':<30} | {'1봉 안':>8} | {'2봉':>5} | {'3봉':>5} | {'4봉':>5} | {'미도달':>8}")
    print("-" * 75)
    bars_dist_summary = {}
    for z in ZONE_DEFS:
        if z == "close_now": continue
        # 1봉만 실제 진입했지만, bars_to_reach 는 4봉까지 추적
        # 다만 verify_with_zone 은 1봉 도달 시 즉시 break — 따라서 분포는 전체 이벤트 중 4봉 안에 도달한 봉 수로 재계산해야
        # 위 로직은 1봉 진입한 케이스만 bars_dist 에 들어가므로, 사실상 모두 "1"임. 별도 추적 필요.
        bars_dist_summary[z] = {"need_separate_track": True}
    # 별도 추적: 전 이벤트에 대해 1~4봉 도달 여부
    extended_track: dict[str, dict[str, int]] = {z: {"1": 0, "2": 0, "3": 0, "4": 0, "miss": 0} for z in ZONE_DEFS if z != "close_now"}
    for ev in events:
        symbol = ev["symbol"]; klines = klines_cache[symbol]
        confirm_idx = ev["bar_idx"]
        direction = ev["direction"]
        atr = ev["atr"]
        if confirm_idx + 5 >= len(klines) or atr <= 0 or direction not in ("long", "short"):
            continue
        confirm_bar = klines[confirm_idx]
        for z in ZONE_DEFS:
            if z == "close_now": continue
            zone = compute_zone(direction, confirm_bar, atr, z)
            if zone is None: continue
            zl, zh = zone
            reached = None
            for j in range(1, 5):
                if confirm_idx + j >= len(klines):
                    break
                k = klines[confirm_idx + j]
                if k["low"] <= zh and k["high"] >= zl:
                    reached = j
                    break
            if reached is None:
                extended_track[z]["miss"] += 1
            else:
                extended_track[z][str(reached)] += 1

    for z in ZONE_DEFS:
        if z == "close_now": continue
        t = extended_track[z]
        total = sum(t.values())
        if total == 0: continue
        p1 = 100 * t["1"] / total
        p2 = 100 * t["2"] / total
        p3 = 100 * t["3"] / total
        p4 = 100 * t["4"] / total
        pm = 100 * t["miss"] / total
        print(f"{ZONE_LABEL[z]:<30} | {p1:>7.1f}% | {p2:>4.1f}% | {p3:>4.1f}% | {p4:>4.1f}% | {pm:>7.1f}%")

    # 종목별 매수존 도달률 (1봉 안)
    print("\n【종목별 매수존 1봉 도달률 (각 M)】")
    print(f"{'종목':<10} | " + " | ".join(f"{ZONE_LABEL[z]:<28}" for z in ZONE_DEFS))
    by_sym_reach = {sym: {z: {"reach": 0, "total": 0} for z in ZONE_DEFS} for sym in SYMBOLS}
    for ev in events:
        symbol = ev["symbol"]; klines = klines_cache[symbol]
        confirm_idx = ev["bar_idx"]; direction = ev["direction"]; atr = ev["atr"]
        if confirm_idx + 2 >= len(klines) or atr <= 0 or direction not in ("long", "short"):
            continue
        confirm_bar = klines[confirm_idx]
        next_bar = klines[confirm_idx + 1]
        for z in ZONE_DEFS:
            by_sym_reach[symbol][z]["total"] += 1
            if z == "close_now":
                by_sym_reach[symbol][z]["reach"] += 1
                continue
            zone = compute_zone(direction, confirm_bar, atr, z)
            if zone is None: continue
            zl, zh = zone
            if next_bar["low"] <= zh and next_bar["high"] >= zl:
                by_sym_reach[symbol][z]["reach"] += 1

    sym_reach_rows = {}
    for sym in SYMBOLS:
        row = {}
        cells = []
        for z in ZONE_DEFS:
            t = by_sym_reach[sym][z]["total"]
            r = by_sym_reach[sym][z]["reach"]
            pct = 100 * r / t if t else 0
            row[z] = {"reach": r, "total": t, "pct": round(pct, 1)}
            cells.append(f"{pct:>5.1f}% ({r:>3}/{t:>3})")
        sym_reach_rows[sym] = row
        print(f"{sym:<10} | " + " | ".join(f"{c:<28}" for c in cells))

    # 방향별 매수존 도달률
    print("\n【방향별 매수존 1봉 도달률】")
    by_dir_reach = {"long": {z: {"reach": 0, "total": 0} for z in ZONE_DEFS},
                    "short": {z: {"reach": 0, "total": 0} for z in ZONE_DEFS}}
    for ev in events:
        symbol = ev["symbol"]; klines = klines_cache[symbol]
        confirm_idx = ev["bar_idx"]; direction = ev["direction"]; atr = ev["atr"]
        if direction not in ("long", "short"): continue
        if confirm_idx + 2 >= len(klines) or atr <= 0: continue
        confirm_bar = klines[confirm_idx]
        next_bar = klines[confirm_idx + 1]
        for z in ZONE_DEFS:
            by_dir_reach[direction][z]["total"] += 1
            if z == "close_now":
                by_dir_reach[direction][z]["reach"] += 1
                continue
            zone = compute_zone(direction, confirm_bar, atr, z)
            if zone is None: continue
            zl, zh = zone
            if next_bar["low"] <= zh and next_bar["high"] >= zl:
                by_dir_reach[direction][z]["reach"] += 1

    dir_reach_rows = {}
    print(f"{'방향':<8} | " + " | ".join(f"{ZONE_LABEL[z]:<28}" for z in ZONE_DEFS))
    for d in ("long", "short"):
        row = {}
        cells = []
        for z in ZONE_DEFS:
            t = by_dir_reach[d][z]["total"]
            r = by_dir_reach[d][z]["reach"]
            pct = 100 * r / t if t else 0
            row[z] = {"reach": r, "total": t, "pct": round(pct, 1)}
            cells.append(f"{pct:>5.1f}% ({r:>3}/{t:>3})")
        dir_reach_rows[d] = row
        print(f"{d:<8} | " + " | ".join(f"{c:<28}" for c in cells))

    # 최적 매수존 선정 — score (진입률 * RR 중앙 / 100) 최댓값
    best = max(matrix, key=lambda r: r["score"])
    print(f"\n>>> 가장 합리적 매수존: {best['label']}")
    print(f"    진입률 {best['entry_rate_pct']}%, RR 중앙 {best['rr_median']:.2f}, 점수 {best['score']:.3f}")

    # JSON 저장
    out_path = CACHE_DIR / "zone_compare_backtest.json"
    out_path.write_text(json.dumps({
        "config": {"rsi": "30/70", "timeout": 8, "verify_bars": VERIFY_BARS,
                   "slippage_bps": SLIPPAGE_BPS, "funding_per_8h_pct": FUNDING_PER_8H_PCT,
                   "next_bar_only": True},
        "n_events": n_events,
        "matrix": matrix,
        "extended_track": extended_track,
        "by_sym_reach": sym_reach_rows,
        "by_dir_reach": dir_reach_rows,
        "best": {"zone": best["zone"], "label": best["label"],
                 "entry_rate_pct": best["entry_rate_pct"], "rr_median": best["rr_median"],
                 "score": best["score"]},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
