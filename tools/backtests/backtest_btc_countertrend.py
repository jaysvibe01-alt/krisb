"""BTC 역추세 자리 정밀 분석.

크트키 본질 = 역추세. PPT 4원칙은 결국 "추세 끝 잡는 자리".
시간 게이트(R8)만으론 추세 한복판 진입을 못 막는다.

진짜 역추세 시그널:
  DIVER   - RSI 다이버 (가격 LL/HH 갱신했는데 RSI 안 따라옴)
  ABSORB  - 흡수 누적 (거래량 큰데 가격 안 빠짐 = 매도/매수 흡수)
  ISOSR   - 고립 반전 SR (직전 캔들과 동떨어진 자리에서 반전)
  HTF_OS  - 4H RSI 과매도/매수 컨플루언스
  WICK50  - 꼬리 50% 이상 (Kris 슬라이드 41-44 강조)

자리 보조 (역추세 아님):
  KBONA   - 크보나치 비율 (자리만)
  SRFLIP  - SR Flip 리테스트 (추세 추종에 가까움)
  QUART   - 일봉 4분할 (자리만)

분석:
  - 역추세 시그널 0/1/2/3+ 개수별 결과
  - 시그널 종류별 단독 효과
  - 추세 추종 모드(KBONA/SRFLIP만, 역추세 시그널 0개) vs 역추세 모드
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from statistics import median, mean
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import verify_user_model, Collector, load

KST = timezone(timedelta(hours=9))

# 역추세 시그널만 (자리 보조는 제외)
COUNTERTREND_KEYWORDS = {
    "다이버전스": "DIVER",
    "흡수 누적": "ABSORB",
    "고립 반전": "ISOSR",
    "과매도 컨플루언스": "HTF_OS",
    "과매수 컨플루언스": "HTF_OB",
}
# 자리 보조 (참고용)
POSITIONAL_KEYWORDS = {
    "크보나치": "KBONA",
    "SR Flip": "SRFLIP",
    "일봉 4분할": "QUART",
}


def extract_countertrend(text):
    return frozenset(code for kw, code in COUNTERTREND_KEYWORDS.items() if kw in text)


def extract_positional(text):
    return frozenset(code for kw, code in POSITIONAL_KEYWORDS.items() if kw in text)


def has_wick50(kline, direction):
    """진입 컨펌봉의 꼬리가 본체의 50% 이상인지 (Kris 슬라이드 41-44).

    long  : 밑꼬리(low~min(open,close)) / 본체(|open-close|) ≥ 0.5
    short : 윗꼬리(max(open,close)~high) / 본체 ≥ 0.5
    """
    o, h, l, c = kline["open"], kline["high"], kline["low"], kline["close"]
    body = abs(c - o)
    if body == 0:
        return False
    if direction == "long":
        lower_wick = min(o, c) - l
        return lower_wick / body >= 0.5
    else:
        upper_wick = h - max(o, c)
        return upper_wick / body >= 0.5


def kst_hour(ts_ms):
    h = datetime.fromtimestamp(ts_ms / 1000, tz=KST).hour
    if h < 6:
        return "00-06"
    if h < 12:
        return "06-12"
    if h < 18:
        return "12-18"
    return "18-24"


def passes_r8(direction, hour_str):
    """R8 시간 게이트 (이미 봇에 적용)."""
    if direction == "long":
        return hour_str == "06-12"
    if direction == "short":
        return hour_str in ("06-12", "12-18")
    return True


def run_btc_collect():
    """봇을 그대로 돌려서 BTC 진입 수집 — 컨펌봉 OHLC 도 같이."""
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
    # 게이트 끄고 모두 수집 (분석에서 다시 게이트 적용)
    bot.BTC_TIME_GATE_ENABLED = False
    c = Collector()
    bot.send_telegram = c.send

    k15 = load("BTCUSDT", "15m")
    k4h = load("BTCUSDT", "4h")
    k1d = load("BTCUSDT", "1d")
    bot.STATE["BTCUSDT"] = bot.SymbolState()
    bot.RSI_STATE["BTCUSDT"] = RSISymbolState()
    bot.SERIES_15M["BTCUSDT"] = deque(maxlen=200)
    bot.SERIES_4H["BTCUSDT"] = deque(maxlen=100)
    bot.SERIES_1D["BTCUSDT"] = deque(maxlen=50)
    bot.ISOLATED_SR_CACHE["BTCUSDT"] = []
    for k in k15[:50]:
        bot.SERIES_15M["BTCUSDT"].append(k)
    for k in k4h[:8]:
        bot.SERIES_4H["BTCUSDT"].append(k)
    for k in k1d[:10]:
        bot.SERIES_1D["BTCUSDT"].append(k)
    last_4h_ot = bot.SERIES_4H["BTCUSDT"][-1]["open_time"]
    last_1d_ot = bot.SERIES_1D["BTCUSDT"][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h_ot)
    i1d = sum(1 for k in k1d if k["open_time"] <= last_1d_ot)
    for i in range(50, len(k15)):
        kk = k15[i]
        bot.SERIES_15M["BTCUSDT"].append(kk)
        while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
            bot.SERIES_4H["BTCUSDT"].append(k4h[i4])
            i4 += 1
        while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
            bot.SERIES_1D["BTCUSDT"].append(k1d[i1d])
            i1d += 1
        atr = bot.calc_atr(list(bot.SERIES_15M["BTCUSDT"]))
        c.set(symbol="BTCUSDT", bar_idx=i, close=kk["close"], atr=atr, bar_ts=kk["close_time"])
        try:
            bot.evaluate_symbol_15m("BTCUSDT")
        except Exception:
            pass

    # 게이트 다시 켜둠
    bot.BTC_TIME_GATE_ENABLED = True

    enriched = []
    for ev in c.events:
        v = verify_user_model(ev, k15, "BTCUSDT", realistic=True)
        if not v or not v.get("entered"):
            continue
        idx = ev["bar_idx"]
        if idx < 14:
            continue
        # 컨펌봉 OHLC
        confirm_bar = k15[idx]
        wick50 = has_wick50(confirm_bar, ev["direction"])
        ct = extract_countertrend(ev["text"])
        pos = extract_positional(ev["text"])
        hour = kst_hour(ev["bar_ts"])
        enriched.append({
            "direction": ev["direction"],
            "bar_ts": ev["bar_ts"],
            "hour_kst": hour,
            "countertrend": ct,
            "positional": pos,
            "wick50": wick50,
            "ct_count": len(ct) + (1 if wick50 else 0),
            "passes_r8": passes_r8(ev["direction"], hour),
            "realistic": v,
        })
    return enriched


def stats(lst):
    if not lst:
        return None
    rrs_raw = [e["realistic"]["rr"] for e in lst]
    rrs = [r for r in rrs_raw if r != float("inf") and r < 100]
    mfes = [e["realistic"]["mfe"] for e in lst]
    win = sum(1 for e in lst if e["realistic"]["mfe"] > abs(e["realistic"]["mae"]))
    sl = sum(1 for e in lst if e["realistic"]["sl_hit"])
    tp1 = sum(1 for e in lst if e["realistic"]["tp1_hit"])
    tp2 = sum(1 for e in lst if e["realistic"]["tp2_hit"])
    return {
        "n": len(lst),
        "rr_med": round(median(rrs), 2) if rrs else 0,
        "rr_mean": round(mean(rrs), 2) if rrs else 0,
        "mfe_med": round(median(mfes), 2),
        "win_pct": round(100 * win / len(lst), 1),
        "sl_pct": round(100 * sl / len(lst), 1),
        "tp1_pct": round(100 * tp1 / len(lst), 1),
        "tp2_pct": round(100 * tp2 / len(lst), 1),
    }


def print_table(title, groups, order_key=None):
    print("=" * 105)
    print(f"【 {title} 】")
    print(f"{'그룹':>32} | {'N':>4} | {'RR중앙':>6} | {'RR평균':>6} | {'MFE중앙':>7} | {'Win%':>5} | {'SL%':>5} | {'TP1%':>5} | {'TP2%':>5}")
    print("-" * 105)
    rows = [(g, lst, stats(lst)) for g, lst in groups.items() if stats(lst) and stats(lst)["n"] >= 3]
    if order_key == "name":
        rows.sort(key=lambda x: x[0])
    else:
        rows.sort(key=lambda x: -x[2]["rr_med"])
    for g, _lst, s in rows:
        star = " ★★" if s["rr_med"] > 1.5 else (" ★" if s["rr_med"] > 1.2 else (" ↓↓" if s["rr_med"] < 0.5 else (" ↓" if s["rr_med"] < 0.8 else "  ")))
        print(f"{g:>32} | {s['n']:>4} | {s['rr_med']:>5.2f}{star}| {s['rr_mean']:>6.2f} | +{s['mfe_med']:>5.2f}% | {s['win_pct']:>4.1f}% | {s['sl_pct']:>4.1f}% | {s['tp1_pct']:>4.1f}% | {s['tp2_pct']:>4.1f}%")
    print("=" * 105)
    print()


def main():
    print("=== BTC 역추세 자리 정밀 분석 ===\n")
    events = run_btc_collect()
    print(f"BTC 전체 진입 N: {len(events)}\n")

    # A) 역추세 시그널 개수별
    by_count = defaultdict(list)
    for e in events:
        by_count[f"{e['ct_count']}개"].append(e)
    print_table("역추세 시그널 개수별 (DIVER/ABSORB/ISOSR/HTF/WICK50)", by_count, order_key="name")

    # B) 시그널 종류별 단독 효과 (해당 시그널 있는 진입 vs 없는 진입)
    print("=" * 105)
    print("【 시그널 단독 효과 (그 시그널 있는 진입의 통계) 】")
    print(f"{'시그널':>32} | {'N':>4} | {'RR중앙':>6} | {'RR평균':>6} | {'MFE중앙':>7} | {'Win%':>5} | {'SL%':>5} | {'TP1%':>5}")
    print("-" * 105)
    signal_keys = ["DIVER", "ABSORB", "ISOSR", "HTF_OS", "HTF_OB"]
    for sig in signal_keys:
        sub = [e for e in events if sig in e["countertrend"]]
        if len(sub) < 3:
            continue
        s = stats(sub)
        star = " ★★" if s["rr_med"] > 1.5 else (" ★" if s["rr_med"] > 1.2 else (" ↓" if s["rr_med"] < 0.8 else "  "))
        print(f"{sig:>32} | {s['n']:>4} | {s['rr_med']:>5.2f}{star}| {s['rr_mean']:>6.2f} | +{s['mfe_med']:>5.2f}% | {s['win_pct']:>4.1f}% | {s['sl_pct']:>4.1f}% | {s['tp1_pct']:>4.1f}%")
    sub = [e for e in events if e["wick50"]]
    if len(sub) >= 3:
        s = stats(sub)
        star = " ★★" if s["rr_med"] > 1.5 else (" ★" if s["rr_med"] > 1.2 else (" ↓" if s["rr_med"] < 0.8 else "  "))
        print(f"{'WICK50':>32} | {s['n']:>4} | {s['rr_med']:>5.2f}{star}| {s['rr_mean']:>6.2f} | +{s['mfe_med']:>5.2f}% | {s['win_pct']:>4.1f}% | {s['sl_pct']:>4.1f}% | {s['tp1_pct']:>4.1f}%")
    print("=" * 105)
    print()

    # C) 역추세 모드 vs 추세 추종 모드
    countertrend_mode = [e for e in events if e["ct_count"] >= 1]
    trend_follow_mode = [e for e in events if e["ct_count"] == 0]
    print("=" * 105)
    print("【 역추세 모드 (시그널≥1) vs 추세 추종 모드 (시그널=0) 】")
    print("-" * 105)
    for name, lst in [("역추세 모드 (≥1)", countertrend_mode), ("추세 추종 모드 (=0)", trend_follow_mode)]:
        if not lst:
            continue
        s = stats(lst)
        star = " ★★" if s["rr_med"] > 1.5 else (" ★" if s["rr_med"] > 1.2 else (" ↓" if s["rr_med"] < 0.8 else "  "))
        print(f"{name:>32} | {s['n']:>4} | {s['rr_med']:>5.2f}{star}| {s['rr_mean']:>6.2f} | +{s['mfe_med']:>5.2f}% | {s['win_pct']:>4.1f}% | {s['sl_pct']:>4.1f}% | {s['tp1_pct']:>4.1f}%")
    print("=" * 105)
    print()

    # D) R8 게이트 + 역추세 시그널 조합
    by_combo = {}
    by_combo["R8 OFF + CT 0"] = [e for e in events if not e["passes_r8"] and e["ct_count"] == 0]
    by_combo["R8 OFF + CT ≥1"] = [e for e in events if not e["passes_r8"] and e["ct_count"] >= 1]
    by_combo["R8 ON  + CT 0"] = [e for e in events if e["passes_r8"] and e["ct_count"] == 0]
    by_combo["R8 ON  + CT ≥1"] = [e for e in events if e["passes_r8"] and e["ct_count"] >= 1]
    by_combo["R8 ON  + CT ≥2"] = [e for e in events if e["passes_r8"] and e["ct_count"] >= 2]
    print_table("R8 시간 게이트 × 역추세 시그널 조합", by_combo, order_key="name")

    # E) 방향 × 역추세 시그널 수
    by_dir_ct = defaultdict(list)
    for e in events:
        ct_bucket = "0" if e["ct_count"] == 0 else ("1" if e["ct_count"] == 1 else "≥2")
        by_dir_ct[f"{e['direction']} / CT={ct_bucket}"].append(e)
    print_table("방향 × 역추세 시그널 수", by_dir_ct, order_key="name")

    # F) 최강 시그널 페어 (2개 동시)
    by_pair = defaultdict(list)
    for e in events:
        sigs = sorted(e["countertrend"])
        if e["wick50"]:
            sigs.append("WICK50")
        if len(sigs) >= 2:
            for i, s1 in enumerate(sigs):
                for s2 in sigs[i+1:]:
                    by_pair[f"{s1}+{s2}"].append(e)
    print_table("시그널 페어 (2개 동시 — 중복 카운트)", by_pair)

    # 저장
    out = {
        "n_total": len(events),
        "by_ct_count": {k: stats(v) for k, v in by_count.items() if stats(v)},
        "by_single_signal": {sig: stats([e for e in events if sig in e["countertrend"]]) for sig in signal_keys},
        "wick50": stats([e for e in events if e["wick50"]]),
        "countertrend_mode": stats(countertrend_mode),
        "trend_follow_mode": stats(trend_follow_mode),
        "by_r8_ct_combo": {k: stats(v) for k, v in by_combo.items() if stats(v)},
        "by_dir_ct": {k: stats(v) for k, v in by_dir_ct.items() if stats(v)},
        "by_pair": {k: stats(v) for k, v in by_pair.items() if stats(v) and stats(v)["n"] >= 3},
    }
    out_path = ROOT / "backtest_data" / "btc_countertrend.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_path}")


if __name__ == "__main__":
    main()
