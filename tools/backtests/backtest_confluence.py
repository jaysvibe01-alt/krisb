"""부스트 × 크보나치 컨플루언스 조합별 RR 매트릭스.

각 진입 알림의 메시지 본문에서 부스트 종류 추출 → 조합별 그루핑 → 평균 RR/MFE 측정.
목적: '어떤 컨플루언스가 같이 떴을 때 수익률이 가장 좋은가' 객관 결정.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot   # noqa

CACHE_DIR = ROOT / "backtest_data"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
VERIFY_BARS = 48

# 부스트 메시지 본문에 등장하는 키워드 → 짧은 코드
BOOST_KEYWORDS = {
    "흡수 누적": "ABSORB",
    "다이버전스": "DIVER",
    "SR Flip": "SRFLIP",
    "크보나치": "KBONA",   # 1.236/1:1/2.0/2.26/0.764
    "일봉 4분할": "QUART",
    "고립 반전": "ISOSR",
    "과매도 컨플루언스": "HTF_OS",
    "과매수 컨플루언스": "HTF_OB",
    "신저가 갱신": "BREAK_LO",
    "신고가 갱신": "BREAK_HI",
    "꼬리 50": "WICK50",
}


def load(symbol: str, interval: str) -> list[dict]:
    return json.loads((CACHE_DIR / f"binance_{symbol}_{interval}_1y.json").read_text(encoding="utf-8"))


def extract_boosts(text: str) -> list[str]:
    found = []
    for keyword, code in BOOST_KEYWORDS.items():
        if keyword in text and code not in found:
            found.append(code)
    return sorted(found)


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
            boosts = extract_boosts(text)
            self.events.append({**self._ctx, "level": level, "direction": direction,
                                "boosts": boosts, "n_boosts": len(boosts), "text": text})


def verify(ev, klines, n=VERIFY_BARS):
    idx = ev["bar_idx"] + 1
    if idx + n >= len(klines): return None
    ep, atr = ev["close"], ev["atr"]
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
    return {"sl_hit": sl_hit, "mfe": mfe, "mae": mae,
            "rr": mfe/abs(mae) if mae < 0 else 999.0}


def run() -> dict:
    """timeout=8 + 4종목 백테스트로 부스트별 결과 수집."""
    bot.PRE_ALERT_TIMEOUT_BARS = 8
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
        print(f"  {symbol} 완료: events={len([e for e in c.events if e['symbol']==symbol])}")

    # 검증
    for ev in c.events:
        v = verify(ev, klines_cache[ev["symbol"]])
        if v: ev.update(v)
    return {"events": c.events}


def main() -> int:
    print("=== 부스트 × 크보나치 조합별 RR 매트릭스 ===")
    print(f"종목: {SYMBOLS} · timeout=8 · 1년치\n")
    t0 = time.time()
    out = run()
    events = [e for e in out["events"] if "mfe" in e]
    print(f"\n검증 완료된 진입 신호: {len(events)} ({time.time()-t0:.1f}s)\n")

    # ─── 그룹 1: 부스트 개수별 ───
    by_n: dict[int, list] = defaultdict(list)
    for e in events:
        by_n[e["n_boosts"]].append(e)
    print("=" * 82)
    print("【부스트 개수별 RR 분포】")
    print(f"{'#부스트':>7} | {'N':>5} | {'MFE':>7} | {'MAE':>7} | {'SL%':>5} | {'TP1%':>5} | {'TP2%':>5} | {'평균 RR':>7}")
    print("-" * 82)
    for n in sorted(by_n.keys()):
        lst = by_n[n]
        mfe = sum(e["mfe"] for e in lst)/len(lst)
        mae = sum(e["mae"] for e in lst)/len(lst)
        sl = 100*sum(1 for e in lst if e["sl_hit"])/len(lst)
        tp1 = 100*sum(1 for e in lst if e["mfe"]>=1.236)/len(lst)
        tp2 = 100*sum(1 for e in lst if e["mfe"]>=2.0)/len(lst)
        rrs = [e["rr"] for e in lst if e["rr"]<999]
        rr_avg = sum(rrs)/len(rrs) if rrs else 0
        print(f"{n:>7} | {len(lst):>5} | +{mfe:>5.2f}% | {mae:>6.2f}% | {sl:>4.1f}% | {tp1:>4.1f}% | {tp2:>4.1f}% | {rr_avg:>7.2f}")
    print("=" * 82)

    # ─── 그룹 2: 개별 부스트 RR ───
    by_b: dict[str, list] = defaultdict(list)
    for e in events:
        for b in (e["boosts"] or ["(없음)"]):
            by_b[b].append(e)
    print("\n【개별 부스트별 RR (해당 부스트가 떴을 때)】")
    print(f"{'부스트':>10} | {'N':>5} | {'MFE':>7} | {'SL%':>5} | {'TP2%':>5} | {'평균 RR':>7}")
    print("-" * 60)
    for b in sorted(by_b.keys(), key=lambda x: -sum(e["mfe"] for e in by_b[x])/len(by_b[x])):
        lst = by_b[b]
        mfe = sum(e["mfe"] for e in lst)/len(lst)
        sl = 100*sum(1 for e in lst if e["sl_hit"])/len(lst)
        tp2 = 100*sum(1 for e in lst if e["mfe"]>=2.0)/len(lst)
        rrs = [e["rr"] for e in lst if e["rr"]<999]
        rr_avg = sum(rrs)/len(rrs) if rrs else 0
        print(f"{b:>10} | {len(lst):>5} | +{mfe:>5.2f}% | {sl:>4.1f}% | {tp2:>4.1f}% | {rr_avg:>7.2f}")

    # ─── 그룹 3: 2개 부스트 조합 (top 10) ───
    by_combo: dict[str, list] = defaultdict(list)
    for e in events:
        if len(e["boosts"]) >= 2:
            for i in range(len(e["boosts"])):
                for j in range(i+1, len(e["boosts"])):
                    key = f"{e['boosts'][i]}+{e['boosts'][j]}"
                    by_combo[key].append(e)
    if by_combo:
        print("\n【2부스트 조합 TOP 10 (RR 내림차순)】")
        print(f"{'조합':>20} | {'N':>4} | {'MFE':>6} | {'RR':>5}")
        print("-" * 50)
        combos = [(k, lst) for k, lst in by_combo.items() if len(lst) >= 5]
        for k, lst in sorted(combos,
                key=lambda x: -sum(e["mfe"] for e in x[1])/len(x[1]))[:10]:
            mfe = sum(e["mfe"] for e in lst)/len(lst)
            rrs = [e["rr"] for e in lst if e["rr"]<999]
            rr_avg = sum(rrs)/len(rrs) if rrs else 0
            print(f"{k:>20} | {len(lst):>4} | +{mfe:>4.2f}% | {rr_avg:>5.2f}")

    # JSON 저장
    out_path = CACHE_DIR / "confluence_matrix.json"
    summary = {
        "by_n_boosts": {n: {"N": len(by_n[n]),
                           "avg_mfe": round(sum(e["mfe"] for e in by_n[n])/len(by_n[n]), 2),
                           "avg_rr": round(sum(e["rr"] for e in by_n[n] if e["rr"]<999) / max(1, sum(1 for e in by_n[n] if e["rr"]<999)), 2)}
                       for n in by_n},
        "by_boost": {b: {"N": len(by_b[b]),
                        "avg_mfe": round(sum(e["mfe"] for e in by_b[b])/len(by_b[b]), 2),
                        "avg_rr": round(sum(e["rr"] for e in by_b[b] if e["rr"]<999) / max(1, sum(1 for e in by_b[b] if e["rr"]<999)), 2)}
                    for b in by_b},
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
