"""Realistic 백테스트 — 슬리피지 + 펀딩 + 분포 통계 보강.

기존 backtest_timeout_multi.py 의 ideal 가정 (close 진입, slippage 0, funding 0)
대비 현실적 가정으로 재산출:

  진입가 = 다음봉 open × (1 + slippage_bps[symbol] / 10000)
  펀딩비 = 매 8h × 0.01% (롱 추세는 +, 숏 추세는 -)
  Realistic MFE/MAE = 위 두 가지 보정한 결과

통계 보강:
  median / std / 25%·75% percentile / 95% CI / win rate / max RR
  outlier 처리: 999 RR cap 분리 보고
"""
from __future__ import annotations

import json
import sys
import time
import math
from collections import defaultdict, deque
from pathlib import Path
from statistics import mean, median, stdev

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot   # noqa

CACHE_DIR = ROOT / "backtest_data"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
VERIFY_BARS = 48

# 현실적 슬리피지 (왕복 spread, bps = 0.01%)
SLIPPAGE_BPS = {
    "BTCUSDT": 1.0,    # 0.01% (가장 깊은 유동성)
    "ETHUSDT": 2.0,    # 0.02%
    "SOLUSDT": 3.0,    # 0.03%
    "XRPUSDT": 4.0,    # 0.04%
}

# 펀딩비 — 8시간마다 (15m × 32 봉)
FUNDING_PER_8H_PCT = 0.01   # 0.01% (Bitget USDT-M 평균)
BARS_PER_FUNDING = 32       # 32 봉 = 8시간


def load(symbol: str, interval: str) -> list[dict]:
    return json.loads((CACHE_DIR / f"binance_{symbol}_{interval}_1y.json").read_text(encoding="utf-8"))


class Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._ctx: dict = {}
    def set(self, **kw): self._ctx = kw
    def send(self, text):
        kind = "krtky" if ("크트키" in text or "🔷" in text) else "rsi"
        level = 1 if "[1] 사전" in text else (
            2 if "[2] 진입" in text else (
            3 if "[3]" in text else (4 if "[4]" in text else 0)))
        direction = "long" if "LONG" in text else ("short" if "SHORT" in text else "?")
        if level >= 2 and kind == "krtky":
            self.events.append({**self._ctx, "level": level, "direction": direction, "text": text})


def verify_ideal(ev: dict, klines: list[dict], n: int = VERIFY_BARS) -> dict | None:
    """기존 ideal 검증 — close 진입, 슬리피지/펀딩 0."""
    idx = ev["bar_idx"] + 1
    if idx + n >= len(klines): return None
    ep = ev["close"]
    atr = ev["atr"]
    if atr <= 0: return None
    sl_d = atr * 1.5
    direction = ev["direction"]
    if direction not in ("long","short"): return None
    sl = ep - sl_d if direction == "long" else ep + sl_d
    sl_hit, mfe, mae = False, 0.0, 0.0
    for j in range(n):
        k = klines[idx + j]
        if direction == "long":
            mfe = max(mfe, (k["high"]-ep)/ep*100)
            mae = min(mae, (k["low"]-ep)/ep*100)
            if k["low"] <= sl: sl_hit = True
        else:
            mfe = max(mfe, (ep-k["low"])/ep*100)
            mae = min(mae, (ep-k["high"])/ep*100)
            if k["high"] >= sl: sl_hit = True
    rr = mfe / abs(mae) if mae < 0 else float("inf")
    return {"sl_hit": sl_hit, "mfe": mfe, "mae": mae, "rr": rr}


def verify_realistic(ev: dict, klines: list[dict], symbol: str,
                     n: int = VERIFY_BARS) -> dict | None:
    """현실적 검증 — 다음봉 open 진입 + 슬리피지 + 펀딩."""
    idx = ev["bar_idx"] + 1
    if idx + n >= len(klines): return None
    direction = ev["direction"]
    if direction not in ("long","short"): return None
    atr = ev["atr"]
    if atr <= 0: return None
    sl_d = atr * 1.5
    slip_pct = SLIPPAGE_BPS.get(symbol, 5.0) / 10000

    # 진입가 = 다음봉 open (long 은 ask 쪽으로 slip up, short 은 bid 쪽으로 slip down)
    entry_open = klines[idx]["open"]
    if direction == "long":
        ep = entry_open * (1 + slip_pct)
        sl = ep - sl_d
    else:
        ep = entry_open * (1 - slip_pct)
        sl = ep + sl_d

    sl_hit, mfe, mae = False, 0.0, 0.0
    funding_cost_pct = 0.0
    for j in range(n):
        k = klines[idx + j]
        # 펀딩비 누적: 8h마다 (32봉) 한 번씩
        if j > 0 and j % BARS_PER_FUNDING == 0:
            # 롱 추세 (가격이 올랐으면) → 펀딩 +, 롱이 지불 → ep 에 비용 추가
            # 단순화: 매 8h 0.01% 비용 (롱·숏 무관, 평균적)
            funding_cost_pct += FUNDING_PER_8H_PCT

        if direction == "long":
            # 펀딩 차감 → effective high/low 가 낮아짐
            move_up = (k["high"]-ep)/ep*100 - funding_cost_pct
            move_dn = (k["low"]-ep)/ep*100 - funding_cost_pct
            mfe = max(mfe, move_up)
            mae = min(mae, move_dn)
            if k["low"] <= sl: sl_hit = True
        else:
            move_dn = (ep-k["low"])/ep*100 - funding_cost_pct
            move_up = (ep-k["high"])/ep*100 - funding_cost_pct
            mfe = max(mfe, move_dn)
            mae = min(mae, move_up)
            if k["high"] >= sl: sl_hit = True

    rr = mfe / abs(mae) if mae < 0 else float("inf")
    return {"sl_hit": sl_hit, "mfe": mfe, "mae": mae, "rr": rr,
            "entry_slip_pct": slip_pct * 100,
            "total_funding_pct": funding_cost_pct}


def run(timeout: int = 8) -> dict:
    """timeout=8, 4종목 백테스트 — 각 진입에 ideal + realistic 검증 동시 적용."""
    bot.PRE_ALERT_TIMEOUT_BARS = timeout
    bot.SYMBOLS = list(SYMBOLS)
    bot.STATE = defaultdict(bot.SymbolState)
    if hasattr(bot, "RSI_STATE"):
        from rsi_alert_core import RSISymbolState
        bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_15M))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_4H))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_1D))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    c = Collector()
    bot.send_telegram = c.send

    klines_cache: dict[str, list[dict]] = {}
    for symbol in SYMBOLS:
        klines_15m = load(symbol, "15m")
        klines_cache[symbol] = klines_15m
        klines_4h = load(symbol, "4h")
        klines_1d = load(symbol, "1d")
        bot.STATE[symbol] = bot.SymbolState()
        if hasattr(bot, "RSI_STATE"):
            from rsi_alert_core import RSISymbolState
            bot.RSI_STATE[symbol] = RSISymbolState()
        bot.SERIES_15M[symbol] = deque(maxlen=bot.SERIES_MAX_15M)
        bot.SERIES_4H[symbol] = deque(maxlen=bot.SERIES_MAX_4H)
        bot.SERIES_1D[symbol] = deque(maxlen=bot.SERIES_MAX_1D)
        bot.ISOLATED_SR_CACHE[symbol] = []
        warmup = 50
        for k in klines_15m[:warmup]: bot.SERIES_15M[symbol].append(k)
        for k in klines_4h[:warmup//16+5]: bot.SERIES_4H[symbol].append(k)
        for k in klines_1d[:10]: bot.SERIES_1D[symbol].append(k)
        last_4h_ot = bot.SERIES_4H[symbol][-1]["open_time"]
        last_1d_ot = bot.SERIES_1D[symbol][-1]["open_time"]
        i4 = sum(1 for k in klines_4h if k["open_time"] <= last_4h_ot)
        i1d = sum(1 for k in klines_1d if k["open_time"] <= last_1d_ot)
        for i in range(warmup, len(klines_15m)):
            k15 = klines_15m[i]
            bot.SERIES_15M[symbol].append(k15)
            while i4 < len(klines_4h) and klines_4h[i4]["close_time"] <= k15["close_time"]:
                bot.SERIES_4H[symbol].append(klines_4h[i4]); i4 += 1
            while i1d < len(klines_1d) and klines_1d[i1d]["close_time"] <= k15["close_time"]:
                bot.SERIES_1D[symbol].append(klines_1d[i1d]); i1d += 1
            atr = bot.calc_atr(list(bot.SERIES_15M[symbol]))
            c.set(symbol=symbol, bar_idx=i, close=k15["close"], atr=atr)
            try: bot.evaluate_symbol_15m(symbol)
            except: pass

    # 두 가지 검증
    for ev in c.events:
        v_ideal = verify_ideal(ev, klines_cache[ev["symbol"]])
        v_real = verify_realistic(ev, klines_cache[ev["symbol"]], ev["symbol"])
        if v_ideal: ev["ideal"] = v_ideal
        if v_real: ev["realistic"] = v_real

    return {"events": c.events}


def dist_stats(vals: list[float], label: str) -> dict:
    """분포 통계 — Lopez de Prado 권장 보강."""
    if not vals:
        return {"label": label, "n": 0}
    sorted_v = sorted(vals)
    n = len(sorted_v)
    return {
        "label": label,
        "n": n,
        "mean": round(mean(vals), 3),
        "median": round(median(vals), 3),
        "std": round(stdev(vals), 3) if n > 1 else 0,
        "min": round(sorted_v[0], 3),
        "p25": round(sorted_v[n//4], 3),
        "p75": round(sorted_v[3*n//4], 3),
        "max": round(sorted_v[-1], 3),
        # 95% CI of mean (approx normal)
        "ci95": round(1.96 * stdev(vals) / math.sqrt(n), 3) if n > 1 else 0,
    }


def summarize(events: list[dict], scenario: str) -> dict:
    """scenario = 'ideal' or 'realistic'."""
    verified = [e for e in events if scenario in e]
    if not verified:
        return {"n": 0}

    mfes = [e[scenario]["mfe"] for e in verified]
    maes = [e[scenario]["mae"] for e in verified]
    # RR 처리: inf cap 분리 + finite 만 통계
    rrs_raw = [e[scenario]["rr"] for e in verified]
    rrs_finite = [r for r in rrs_raw if r != float("inf") and r < 100]   # 100배 이상은 outlier
    rrs_outlier = sum(1 for r in rrs_raw if r == float("inf") or r >= 100)

    sl_hit_n = sum(1 for e in verified if e[scenario]["sl_hit"])
    tp1_n = sum(1 for e in verified if e[scenario]["mfe"] >= 1.236)
    tp2_n = sum(1 for e in verified if e[scenario]["mfe"] >= 2.0)
    win_n = sum(1 for e in verified if e[scenario]["mfe"] > abs(e[scenario]["mae"]))

    return {
        "n": len(verified),
        "mfe": dist_stats(mfes, "MFE %"),
        "mae": dist_stats(maes, "MAE %"),
        "rr_finite": dist_stats(rrs_finite, "RR (finite, <100)"),
        "rr_outlier_pct": round(100 * rrs_outlier / len(verified), 1),
        "sl_hit_pct": round(100 * sl_hit_n / len(verified), 1),
        "tp1_pct": round(100 * tp1_n / len(verified), 1),
        "tp2_pct": round(100 * tp2_n / len(verified), 1),
        "win_rate_pct": round(100 * win_n / len(verified), 1),
    }


def main() -> int:
    print(f"=== Realistic 백테스트 (timeout=8) ===")
    print(f"슬리피지: {SLIPPAGE_BPS}")
    print(f"펀딩: 매 8h × {FUNDING_PER_8H_PCT}%\n")
    t0 = time.time()
    out = run(timeout=8)
    print(f"\n실행 시간: {time.time()-t0:.1f}s")
    print(f"총 진입 이벤트: {len(out['events'])}\n")

    ideal = summarize(out["events"], "ideal")
    real = summarize(out["events"], "realistic")

    print("=" * 90)
    print(f"{'메트릭':>25} | {'IDEAL':>12} | {'REALISTIC':>12} | {'차이':>10}")
    print("-" * 90)
    metrics = [
        ("진입 N", lambda d: d["n"]),
        ("MFE 평균", lambda d: d["mfe"]["mean"] if "mean" in d.get("mfe",{}) else 0),
        ("MFE 중앙값 ★", lambda d: d["mfe"]["median"] if "median" in d.get("mfe",{}) else 0),
        ("MFE 표준편차", lambda d: d["mfe"]["std"] if "std" in d.get("mfe",{}) else 0),
        ("MFE 25%", lambda d: d["mfe"]["p25"] if "p25" in d.get("mfe",{}) else 0),
        ("MFE 75%", lambda d: d["mfe"]["p75"] if "p75" in d.get("mfe",{}) else 0),
        ("MAE 평균", lambda d: d["mae"]["mean"] if "mean" in d.get("mae",{}) else 0),
        ("MAE 중앙값", lambda d: d["mae"]["median"] if "median" in d.get("mae",{}) else 0),
        ("RR 평균 (finite)", lambda d: d["rr_finite"]["mean"] if "mean" in d.get("rr_finite",{}) else 0),
        ("RR 중앙값 ★", lambda d: d["rr_finite"]["median"] if "median" in d.get("rr_finite",{}) else 0),
        ("RR 25%", lambda d: d["rr_finite"]["p25"] if "p25" in d.get("rr_finite",{}) else 0),
        ("RR 75%", lambda d: d["rr_finite"]["p75"] if "p75" in d.get("rr_finite",{}) else 0),
        ("RR outlier (inf,>100) %", lambda d: d.get("rr_outlier_pct", 0)),
        ("SL hit %", lambda d: d.get("sl_hit_pct", 0)),
        ("TP1 (1.236%) hit %", lambda d: d.get("tp1_pct", 0)),
        ("TP2 (2%) hit %", lambda d: d.get("tp2_pct", 0)),
        ("Win rate (MFE > |MAE|) %", lambda d: d.get("win_rate_pct", 0)),
    ]
    for label, getter in metrics:
        vi = getter(ideal); vr = getter(real)
        try:
            diff = vr - vi if isinstance(vi, (int, float)) else "-"
            diff_s = f"{diff:>+10.2f}" if isinstance(diff, (int, float)) else f"{diff:>10}"
        except:
            diff_s = ""
        print(f"{label:>25} | {str(vi):>12} | {str(vr):>12} | {diff_s}")
    print("=" * 90)

    out_path = CACHE_DIR / "realistic_backtest.json"
    out_path.write_text(json.dumps({
        "config": {"timeout": 8, "slippage_bps": SLIPPAGE_BPS,
                   "funding_per_8h_pct": FUNDING_PER_8H_PCT,
                   "verify_bars": VERIFY_BARS},
        "ideal": ideal, "realistic": real,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
