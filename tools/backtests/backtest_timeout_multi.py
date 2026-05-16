"""크트키 봇 PRE_ALERT_TIMEOUT_BARS 비교 백테스트 — 멀티 종목 (Binance 1년).

종목: BTC/ETH/SOL/XRP × 15m·4h·1d (각 약 35,140봉 × 4 = 140K봉)
비교군: 4 vs 6 vs 8 vs 12 봉
측정: 발사 빈도 / 도달률 / MFE / MAE / SL hit / TP1·TP2 / RR
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot   # noqa: E402

CACHE_DIR = ROOT / "backtest_data"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
VERIFY_BARS = 48


def load(symbol: str, interval: str) -> list[dict]:
    return json.loads((CACHE_DIR / f"binance_{symbol}_{interval}_1y.json").read_text(encoding="utf-8"))


class AlertCollector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._ctx: dict = {}

    def set_context(self, **kwargs) -> None:
        self._ctx = kwargs

    def send(self, text: str) -> None:
        kind = "krtky" if "크트키" in text or "🔷" in text else (
            "rsi" if "RSI 단순" in text or "[ICT_SGBOT · RSI]" in text or "📊" in text and "[ICT_SGBOT · RSI]" in text else "other"
        )
        level = 0
        if "[1] 사전 알림" in text:
            level = 1
        elif "[2] 진입 신호" in text:
            level = 2
        elif "[3]" in text and "진입 신호" in text:
            level = 3
        elif "[4]" in text or "강력 진입" in text:
            level = 4
        direction = "long" if "LONG" in text or "롱" in text else (
            "short" if "SHORT" in text or "숏" in text else "?"
        )
        self.events.append({**self._ctx, "kind": kind, "level": level, "direction": direction})


def run_one_timeout(timeout_bars: int) -> dict[str, list[dict]]:
    """timeout 값 하나로 4종목 백테스트, 종목별 이벤트 dict 반환."""
    bot.PRE_ALERT_TIMEOUT_BARS = timeout_bars
    bot.SYMBOLS = list(SYMBOLS)
    bot.STATE = defaultdict(bot.SymbolState)
    if hasattr(bot, "RSI_STATE"):
        try:
            from rsi_alert_core import RSISymbolState
            bot.RSI_STATE = defaultdict(RSISymbolState)
        except Exception:
            pass
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_15M))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_4H))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_1D))
    bot.ISOLATED_SR_CACHE = defaultdict(list)

    collector = AlertCollector()
    bot.send_telegram = collector.send

    all_events: dict[str, list[dict]] = {s: [] for s in SYMBOLS}

    # 각 종목 독립 처리 (메모리 절약)
    for symbol in SYMBOLS:
        klines_15m = load(symbol, "15m")
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

        # 워밍업 50봉
        warmup = 50
        for k in klines_15m[:warmup]:
            bot.SERIES_15M[symbol].append(k)
        for k in klines_4h[: warmup // 16 + 5]:
            bot.SERIES_4H[symbol].append(k)
        for k in klines_1d[:10]:
            bot.SERIES_1D[symbol].append(k)

        last_4h_ot = bot.SERIES_4H[symbol][-1]["open_time"] if bot.SERIES_4H[symbol] else 0
        last_1d_ot = bot.SERIES_1D[symbol][-1]["open_time"] if bot.SERIES_1D[symbol] else 0
        i4 = sum(1 for k in klines_4h if k["open_time"] <= last_4h_ot)
        i1d = sum(1 for k in klines_1d if k["open_time"] <= last_1d_ot)

        events_before = len(collector.events)
        for i in range(warmup, len(klines_15m)):
            k15 = klines_15m[i]
            bot.SERIES_15M[symbol].append(k15)
            while i4 < len(klines_4h) and klines_4h[i4]["close_time"] <= k15["close_time"]:
                bot.SERIES_4H[symbol].append(klines_4h[i4]); i4 += 1
            while i1d < len(klines_1d) and klines_1d[i1d]["close_time"] <= k15["close_time"]:
                bot.SERIES_1D[symbol].append(klines_1d[i1d]); i1d += 1
            atr = bot.calc_atr(list(bot.SERIES_15M[symbol]))
            collector.set_context(symbol=symbol, bar_idx=i, close=k15["close"], atr=atr)
            try:
                bot.evaluate_symbol_15m(symbol)
            except Exception:
                pass

        # 이 종목의 이벤트 분리
        new_evs = collector.events[events_before:]
        # 진입 검증
        for ev in new_evs:
            if ev["level"] >= 2:
                v = verify_event(ev, klines_15m)
                ev.update(v)
        all_events[symbol] = new_evs
        print(f"  {symbol}: {len(new_evs)} events "
              f"(pre={sum(1 for e in new_evs if e['level']==1)}, "
              f"entry={sum(1 for e in new_evs if e['level']>=2)})")

    return all_events


def verify_event(ev: dict, klines: list[dict], n: int = VERIFY_BARS) -> dict:
    entry_idx = ev["bar_idx"] + 1
    if entry_idx + n >= len(klines):
        return {"truncated": True}
    ep = ev["close"]
    atr = ev["atr"]
    if atr <= 0:
        return {}
    sl_dist = atr * 1.5
    direction = ev["direction"]
    if direction not in ("long", "short"):
        return {}
    sl = ep - sl_dist if direction == "long" else ep + sl_dist
    sl_hit, mfe, mae = False, 0.0, 0.0
    for j in range(n):
        k = klines[entry_idx + j]
        if direction == "long":
            mfe = max(mfe, (k["high"] - ep) / ep * 100)
            mae = min(mae, (k["low"] - ep) / ep * 100)
            if k["low"] <= sl and not sl_hit:
                sl_hit = True
        else:
            mfe = max(mfe, (ep - k["low"]) / ep * 100)
            mae = min(mae, (ep - k["high"]) / ep * 100)
            if k["high"] >= sl and not sl_hit:
                sl_hit = True
    return {
        "sl_hit": sl_hit, "mfe_pct": round(mfe, 3), "mae_pct": round(mae, 3),
        "tp1_hit": mfe >= 1.236, "tp2_hit": mfe >= 2.0,
        "rr": round(mfe / abs(mae), 2) if mae < 0 else 999.0,
    }


def summarize(events_by_symbol: dict[str, list[dict]]) -> dict:
    """전 종목 합산 + 종목별 분포."""
    all_evs = [e for evs in events_by_symbol.values() for e in evs]
    pre = [e for e in all_evs if e["kind"] == "krtky" and e["level"] == 1]
    entry = [e for e in all_evs if e["kind"] == "krtky" and e["level"] >= 2]
    verified = [e for e in entry if "mfe_pct" in e and not e.get("truncated")]

    avg = lambda key: sum(v[key] for v in verified) / len(verified) if verified else 0
    pct = lambda key: 100 * sum(1 for v in verified if v[key]) / len(verified) if verified else 0
    rr_vals = [v["rr"] for v in verified if v["rr"] != 999.0]
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0

    by_sym = {}
    for sym, evs in events_by_symbol.items():
        p = sum(1 for e in evs if e["kind"] == "krtky" and e["level"] == 1)
        e = sum(1 for e in evs if e["kind"] == "krtky" and e["level"] >= 2)
        by_sym[sym] = (p, e, 100 * e / p if p else 0)

    return {
        "pre_total": len(pre), "entry_total": len(entry),
        "reach_pct": 100 * len(entry) / len(pre) if pre else 0,
        "verified_n": len(verified),
        "avg_mfe": round(avg("mfe_pct"), 2),
        "avg_mae": round(avg("mae_pct"), 2),
        "sl_hit_pct": round(pct("sl_hit"), 1),
        "tp1_pct": round(pct("tp1_hit"), 1),
        "tp2_pct": round(pct("tp2_hit"), 1),
        "avg_rr": round(avg_rr, 2),
        "by_symbol": by_sym,
    }


def main() -> int:
    print(f"=== 멀티 종목 (Binance 1년) timeout 비교 백테스트 ===")
    print(f"종목: {SYMBOLS}\n")

    results = {}
    for tb in [4, 6, 8, 12]:
        print(f"--- timeout = {tb}봉 ---")
        t0 = time.time()
        events = run_one_timeout(tb)
        elapsed = time.time() - t0
        stats = summarize(events)
        stats["elapsed_sec"] = round(elapsed, 1)
        results[tb] = stats
        print(f"  합산: 사전 {stats['pre_total']} → 진입 {stats['entry_total']} "
              f"· 도달률 {stats['reach_pct']:.1f}% · MFE +{stats['avg_mfe']}% · "
              f"MAE {stats['avg_mae']}% · SL {stats['sl_hit_pct']}% · "
              f"TP1 {stats['tp1_pct']}% · RR {stats['avg_rr']}\n")

    print("=" * 88)
    print(f"{'timeout':>8} | {'사전':>5} | {'진입':>5} | {'도달%':>6} | "
          f"{'MFE':>7} | {'MAE':>7} | {'SL%':>5} | {'TP1%':>5} | {'TP2%':>5} | {'RR':>5}")
    print("-" * 88)
    for tb, s in results.items():
        print(f"{tb:>8} | {s['pre_total']:>5} | {s['entry_total']:>5} | "
              f"{s['reach_pct']:>5.1f}% | "
              f"+{s['avg_mfe']:>5.2f}% | {s['avg_mae']:>6.2f}% | "
              f"{s['sl_hit_pct']:>4.1f}% | {s['tp1_pct']:>4.1f}% | {s['tp2_pct']:>4.1f}% | "
              f"{s['avg_rr']:>5.2f}")
    print("=" * 88)

    print("\n종목별 분포 (timeout=12 기준):")
    for sym, (p, e, pct) in results[12]["by_symbol"].items():
        print(f"  {sym:>8} : 사전 {p:>4} / 진입 {e:>3} ({pct:>4.1f}%)")

    out = ROOT / "backtest_data" / "timeout_multi_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
