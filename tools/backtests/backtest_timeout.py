"""크트키 봇 PRE_ALERT_TIMEOUT_BARS 비교 백테스트.

비교군: 4 vs 6 vs 8 vs 12 봉.
데이터: backtest_data/BTCUSDT_{15m,4h,1d}_1y.json (약 31일치 15m).
재사용: 봇 codebase 의 evaluate_symbol_15m 슬라이딩 호출.

측정:
  - 사전 알림 [1] 발사 카운트
  - 진입 신호 [2/3/4] 발사 카운트
  - 도달률 (진입 / 사전)
  - 진입 후 48봉 (12h) 가격 검증:
      · 최대 호상 변동 (MFE %)
      · 최대 역방향 변동 (MAE %)
      · SL 도달률 (ATR×1.5)
      · TP1 (크보나치 1.236) 도달률

본 스크립트는 학습/복기용. 결과로 봇 임계 자동 변경 X.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot   # noqa: E402

CACHE_DIR = ROOT / "backtest_data"
SYMBOL = "BTCUSDT"
INTERVAL_MS = 15 * 60 * 1000
VERIFY_BARS = 48   # 진입 후 48봉 = 12시간


# ─────────────────────────────────────────────────────────────
# 데이터 로드
# ─────────────────────────────────────────────────────────────
def load_klines(interval: str) -> list[dict]:
    cache = CACHE_DIR / f"{SYMBOL}_{interval}_1y.json"
    return json.loads(cache.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────
# 매 봉 alert 수집 — 봇 send_telegram 모킹
# ─────────────────────────────────────────────────────────────
class AlertCollector:
    def __init__(self) -> None:
        self.events: list[dict] = []   # [{ts, kind, level, ...}]
        self._current_bar_idx: int = 0
        self._current_close: float = 0.0
        self._current_atr: float = 0.0

    def set_context(self, bar_idx: int, close: float, atr: float) -> None:
        self._current_bar_idx = bar_idx
        self._current_close = close
        self._current_atr = atr

    def send(self, text: str) -> None:
        # 메시지 본문에서 단계와 방향 추출
        kind = "krtky" if "크트키" in text else ("rsi" if "RSI 단순" in text or "[ICT_SGBOT · RSI]" in text else "other")
        level = 0
        if "[1] 사전 알림" in text:
            level = 1
        elif "[2] 진입 신호" in text or "🟢" in text and "진입" in text:
            level = 2
        elif "[3]" in text:
            level = 3
        elif "[4]" in text or "강력 진입" in text:
            level = 4
        # 방향: LONG / SHORT
        if "LONG" in text or "롱" in text:
            direction = "long"
        elif "SHORT" in text or "숏" in text:
            direction = "short"
        else:
            direction = "?"
        self.events.append({
            "bar_idx": self._current_bar_idx,
            "kind": kind,
            "level": level,
            "direction": direction,
            "close": self._current_close,
            "atr": self._current_atr,
        })


# ─────────────────────────────────────────────────────────────
# 단일 timeout 값으로 백테스트 실행
# ─────────────────────────────────────────────────────────────
def run_one_timeout(klines_15m: list[dict], klines_4h: list[dict],
                    klines_1d: list[dict], timeout_bars: int) -> list[dict]:
    """봇 codebase 그대로 슬라이딩 윈도우 호출."""
    # 1) 봇 글로벌 상태 초기화
    bot.PRE_ALERT_TIMEOUT_BARS = timeout_bars
    bot.SYMBOLS = [SYMBOL]
    bot.STATE = defaultdict(bot.SymbolState)
    bot.STATE[SYMBOL] = bot.SymbolState()
    if hasattr(bot, "RSI_STATE"):
        try:
            from rsi_alert_core import RSISymbolState
            bot.RSI_STATE = defaultdict(RSISymbolState)
            bot.RSI_STATE[SYMBOL] = RSISymbolState()
        except Exception:
            pass
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_15M))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_4H))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_1D))
    bot.ISOLATED_SR_CACHE = defaultdict(list)

    # 2) send_telegram 모킹
    collector = AlertCollector()
    bot.send_telegram = collector.send

    # 3) 워밍업 — 첫 30봉으로 SERIES 채움 (RSI 워밍업)
    warmup_n = 50
    for k in klines_15m[:warmup_n]:
        bot.SERIES_15M[SYMBOL].append(k)
    for k in klines_4h[: warmup_n // 16 + 5]:
        bot.SERIES_4H[SYMBOL].append(k)
    for k in klines_1d[:10]:
        bot.SERIES_1D[SYMBOL].append(k)

    # 4) 슬라이딩 윈도우 — 매 봉 마감 시 evaluate_symbol_15m
    last_4h_ot = bot.SERIES_4H[SYMBOL][-1]["open_time"] if bot.SERIES_4H[SYMBOL] else 0
    last_1d_ot = bot.SERIES_1D[SYMBOL][-1]["open_time"] if bot.SERIES_1D[SYMBOL] else 0
    i4 = sum(1 for k in klines_4h if k["open_time"] <= last_4h_ot)
    i1d = sum(1 for k in klines_1d if k["open_time"] <= last_1d_ot)

    for i in range(warmup_n, len(klines_15m)):
        k15 = klines_15m[i]
        bot.SERIES_15M[SYMBOL].append(k15)

        # 4h / 1d 봉이 새로 마감됐으면 같이 push
        while i4 < len(klines_4h) and klines_4h[i4]["close_time"] <= k15["close_time"]:
            bot.SERIES_4H[SYMBOL].append(klines_4h[i4]); i4 += 1
        while i1d < len(klines_1d) and klines_1d[i1d]["close_time"] <= k15["close_time"]:
            bot.SERIES_1D[SYMBOL].append(klines_1d[i1d]); i1d += 1

        atr = bot.calc_atr(list(bot.SERIES_15M[SYMBOL]))
        collector.set_context(i, k15["close"], atr)

        try:
            bot.evaluate_symbol_15m(SYMBOL)
        except Exception as e:
            # 일부 봉에서 데이터 부족 등 무시
            pass

    return collector.events


# ─────────────────────────────────────────────────────────────
# 사후 가격 검증 — 진입 후 48봉 MFE / MAE / SL / TP
# ─────────────────────────────────────────────────────────────
def verify_event(ev: dict, klines: list[dict], verify_n: int = VERIFY_BARS) -> dict:
    """진입 봉 이후 N봉의 가격 흐름으로 결과 측정."""
    if ev["level"] < 2:
        return {}   # 사전 알림은 검증 대상 아님
    entry_idx = ev["bar_idx"] + 1   # 컨펌봉 다음 봉부터
    if entry_idx + verify_n >= len(klines):
        return {"truncated": True}
    entry_price = ev["close"]
    atr = ev["atr"]
    sl_distance = atr * 1.5
    direction = ev["direction"]
    if direction == "long":
        sl_price = entry_price - sl_distance
    elif direction == "short":
        sl_price = entry_price + sl_distance
    else:
        return {}

    sl_hit = False
    mfe = 0.0   # 우호 변동
    mae = 0.0   # 역방향 변동
    sl_bar = -1
    for j in range(verify_n):
        k = klines[entry_idx + j]
        if direction == "long":
            move_up = (k["high"] - entry_price) / entry_price * 100
            move_dn = (k["low"] - entry_price) / entry_price * 100
            mfe = max(mfe, move_up)
            mae = min(mae, move_dn)
            if k["low"] <= sl_price and not sl_hit:
                sl_hit = True
                sl_bar = j
        else:
            move_dn = (entry_price - k["low"]) / entry_price * 100
            move_up = (entry_price - k["high"]) / entry_price * 100
            mfe = max(mfe, move_dn)
            mae = min(mae, move_up)
            if k["high"] >= sl_price and not sl_hit:
                sl_hit = True
                sl_bar = j

    # TP1: 크보나치 1.236 = direction 으로 거리만큼 (단순화: 1% 도달)
    tp1_pct = 1.236 / 1.0   # 단순화: MFE 가 1.236% 이상이면 TP1 도달
    tp1_hit = mfe >= 1.236
    tp2_hit = mfe >= 2.0
    rr = mfe / abs(mae) if mae < 0 else float("inf")

    return {
        "sl_hit": sl_hit, "sl_bar": sl_bar,
        "mfe_pct": round(mfe, 3), "mae_pct": round(mae, 3),
        "tp1_hit": tp1_hit, "tp2_hit": tp2_hit,
        "rr": round(rr, 2) if rr != float("inf") else None,
    }


# ─────────────────────────────────────────────────────────────
# 결과 통계
# ─────────────────────────────────────────────────────────────
def summarize(events: list[dict], klines: list[dict]) -> dict:
    pre_long = sum(1 for e in events if e["kind"] == "krtky" and e["level"] == 1 and e["direction"] == "long")
    pre_short = sum(1 for e in events if e["kind"] == "krtky" and e["level"] == 1 and e["direction"] == "short")
    entry_events = [e for e in events if e["kind"] == "krtky" and e["level"] >= 2]
    n_entry = len(entry_events)
    n_pre = pre_long + pre_short

    verified = [verify_event(e, klines) for e in entry_events]
    verified = [v for v in verified if v and not v.get("truncated")]

    if verified:
        avg_mfe = sum(v["mfe_pct"] for v in verified) / len(verified)
        avg_mae = sum(v["mae_pct"] for v in verified) / len(verified)
        sl_hit_pct = 100 * sum(1 for v in verified if v["sl_hit"]) / len(verified)
        tp1_pct = 100 * sum(1 for v in verified if v["tp1_hit"]) / len(verified)
        tp2_pct = 100 * sum(1 for v in verified if v["tp2_hit"]) / len(verified)
        rr_vals = [v["rr"] for v in verified if v["rr"] is not None]
        avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
    else:
        avg_mfe = avg_mae = sl_hit_pct = tp1_pct = tp2_pct = avg_rr = 0.0

    return {
        "pre_long": pre_long, "pre_short": pre_short, "pre_total": n_pre,
        "entry_total": n_entry,
        "reach_pct": (100 * n_entry / n_pre) if n_pre else 0.0,
        "verified_n": len(verified),
        "avg_mfe": round(avg_mfe, 3),
        "avg_mae": round(avg_mae, 3),
        "sl_hit_pct": round(sl_hit_pct, 1),
        "tp1_pct": round(tp1_pct, 1),
        "tp2_pct": round(tp2_pct, 1),
        "avg_rr": round(avg_rr, 2),
    }


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
def main() -> int:
    print(f"=== BTCUSDT 15m timeout 비교 백테스트 ===")
    klines_15m = load_klines("15m")
    klines_4h = load_klines("4h")
    klines_1d = load_klines("1d")
    print(f"데이터: 15m={len(klines_15m)}봉 · 4h={len(klines_4h)}봉 · 1d={len(klines_1d)}봉")
    days = len(klines_15m) / 96
    print(f"기간: 약 {days:.1f}일\n")

    results = {}
    for timeout_bars in [4, 6, 8, 12]:
        print(f"--- timeout = {timeout_bars}봉 실행 중 ---")
        t0 = time.time()
        events = run_one_timeout(klines_15m, klines_4h, klines_1d, timeout_bars)
        elapsed = time.time() - t0
        stats = summarize(events, klines_15m)
        stats["elapsed_sec"] = round(elapsed, 1)
        results[timeout_bars] = stats
        print(f"  사전 {stats['pre_total']} (롱 {stats['pre_long']}/숏 {stats['pre_short']}) "
              f"→ 진입 {stats['entry_total']} · 도달률 {stats['reach_pct']:.1f}%")
        print(f"  검증 {stats['verified_n']}건 · MFE 평균 +{stats['avg_mfe']:.2f}% · "
              f"MAE 평균 {stats['avg_mae']:.2f}% · SL hit {stats['sl_hit_pct']:.1f}%")
        print(f"  TP1 도달 {stats['tp1_pct']:.1f}% · TP2 {stats['tp2_pct']:.1f}% · "
              f"평균 RR {stats['avg_rr']:.2f}")
        print(f"  실행 시간 {elapsed:.1f}s\n")

    # 결과 표 출력
    print("=" * 78)
    print(f"{'timeout':>8} | {'사전':>5} | {'진입':>5} | {'도달%':>6} | "
          f"{'MFE':>7} | {'MAE':>7} | {'SL%':>5} | {'TP1%':>5} | {'TP2%':>5} | {'RR':>5}")
    print("-" * 78)
    for tb, s in results.items():
        print(f"{tb:>8} | {s['pre_total']:>5} | {s['entry_total']:>5} | "
              f"{s['reach_pct']:>5.1f}% | "
              f"+{s['avg_mfe']:>5.2f}% | {s['avg_mae']:>6.2f}% | "
              f"{s['sl_hit_pct']:>4.1f}% | {s['tp1_pct']:>4.1f}% | {s['tp2_pct']:>4.1f}% | "
              f"{s['avg_rr']:>5.2f}")
    print("=" * 78)

    # JSON 보고서 저장
    report_path = ROOT / "backtest_data" / "timeout_experiment_results.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
