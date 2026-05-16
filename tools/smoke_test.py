"""크트키 알림봇 스모크 테스트 (Bitget WS 통합판).

목적:
  1. Bitget REST 백필이 정상 응답하는가
  2. swing-pivot / 다이버전스 함수가 호출 가능한가
  3. Bitget WS 연결이 살아 봉 콜백을 한 번 이상 받는가
  4. Telegram 토큰 / Chat ID 없이도 dry-run 동작하는가
  5. on_bar_closed → evaluate_symbol_15m + RSI 평가기 통합 호출이 예외 없이 끝나는가

실행: `python _smoketest.py`
  - 약 25 초 안에 종료
  - 성공 시 exit 0, 한 단계라도 실패하면 exit 1
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

# 토큰/CHAT_ID 환경변수가 떠 있어도 스모크 테스트에서는 무시 (실제 송신 방지)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_IDS", None)
os.environ.setdefault("KRTKY_SKIP_ICT_CREDS", "1")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("smoke")


def step(label: str) -> None:
    log.info("─" * 30 + f" {label} " + "─" * 30)


def main() -> int:
    failures: list[str] = []

    # ─── 1) Import ─────────────────────────────────────────
    step("1. 모듈 임포트")
    try:
        import krtky_alert_bot as bot  # noqa: F401
        import bitget_ws  # noqa: F401
        import rsi_alert_core  # noqa: F401
        import krbonacci  # noqa: F401
        log.info("모듈 임포트 OK")
    except Exception as e:
        log.exception("모듈 임포트 실패: %s", e)
        return 1

    # ─── 2) Telegram dry-run 강제 ─────────────────────────
    step("2. Telegram dry-run 강제")
    # krtky_alert_bot 이 ICT .env 자동 로드해 TG_TOKEN 이 채워질 수 있으므로
    # 명시적으로 비우고 send 를 fake 로 교체
    bot.TG_TOKEN = ""
    bot.TG_CHAT_IDS = []
    sent: list[str] = []

    def fake_send(text: str) -> None:
        sent.append(text)
        head = text.split("\n", 1)[0][:80]
        log.info("[DRY-SEND] %s", head)

    bot.send_telegram = fake_send
    log.info("send_telegram → fake_send 로 모킹")

    # ─── 3) Bitget REST 백필 ─────────────────────────────────
    step("3. Bitget REST 백필 (BTCUSDT 15m·4h)")
    k15: list[dict] = []
    k4h: list[dict] = []
    try:
        k15 = bot.backfill_klines("BTCUSDT", "15m", limit=50)
        k4h = bot.backfill_klines("BTCUSDT", "4h", limit=20)
        assert len(k15) >= 30, f"15m 백필 길이 부족: {len(k15)}"
        assert len(k4h) >= 15, f"4h 백필 길이 부족: {len(k4h)}"
        last = k15[-1]
        for key in ("open_time", "open", "high", "low",
                    "close", "volume", "close_time"):
            assert key in last, f"키 누락: {key}"
        assert all(k15[i]["open_time"] < k15[i + 1]["open_time"]
                   for i in range(len(k15) - 1)), "15m 정렬 깨짐"
        log.info("REST 백필 OK · 15m=%d, 4h=%d, 마지막 close=%s",
                 len(k15), len(k4h), last["close"])
    except Exception as e:
        log.exception("REST 백필 실패: %s", e)
        failures.append(f"REST: {e}")

    # ─── 4) swing pivot / 다이버 함수 호출 ──────────────────
    step("4. swing pivot 함수 호출 (정적 픽스처)")
    try:
        # 9봉: 4 평탄 + 1 푹꺼짐 + 4 평탄 → swing low 1개
        toy = []
        for i in range(4):
            toy.append({"open_time": i, "open": 100.0, "high": 100.5,
                        "low": 99.7, "close": 100.0, "volume": 1000.0,
                        "close_time": i})
        toy.append({"open_time": 4, "open": 100.0, "high": 100.1,
                    "low": 95.0, "close": 99.0, "volume": 2000.0,
                    "close_time": 4})
        for i in range(5, 9):
            toy.append({"open_time": i, "open": 99.0, "high": 99.5,
                        "low": 98.7, "close": 99.0, "volume": 1000.0,
                        "close_time": i})
        sl = bot.find_swing_lows(toy, left=3, right=3)
        assert sl == [4], f"swing low 인덱스 어긋남: {sl}"
        # bullish_div 는 swing 2개 필요 → False
        assert bot.detect_bullish_divergence(toy) is False
        log.info("swing pivot OK (toy idx=[4])")
    except Exception as e:
        log.exception("swing 검증 실패: %s", e)
        failures.append(f"swing: {e}")

    # ─── 5) Bitget WS 실연결 (20초) ────────────────────────────
    step("5. Bitget WS 실연결 (20초)")
    received: list[tuple[str, str, int, float]] = []

    def on_bar(symbol: str, interval: str, kline: dict) -> None:
        received.append((symbol, interval, kline["open_time"], kline["close"]))
        if len(received) <= 3:
            log.info("[WS] %s %s close=%s", symbol, interval, kline["close"])

    try:
        stream = bot.BitgetCandleStream(
            symbols=["BTCUSDT"],
            intervals=["candle15m", "candle4H"],
            on_bar_closed=on_bar,
        )
        t = threading.Thread(target=stream.start, kwargs={"block": True}, daemon=True)
        t.start()
        time.sleep(20.0)
        stream.stop()
        t.join(timeout=5.0)
        if not received:
            failures.append("WS: 20초 동안 봉 콜백 0회")
            log.error("WS 봉 콜백 미수신")
        else:
            log.info("WS OK · 총 %d 봉 수신 (snapshot 포함)", len(received))
    except Exception as e:
        log.exception("WS 실연결 실패: %s", e)
        failures.append(f"WS: {e}")

    # ─── 6) on_bar_closed 통합 평가 (dry-run) ─────────────────
    step("6. on_bar_closed 통합 평가 (백필 시리즈 → 평가기 진입)")
    try:
        if k15 and k4h:
            bot.SERIES_15M["BTCUSDT"].clear()
            bot.SERIES_4H["BTCUSDT"].clear()
            for k in k15[:-1]:
                bot.SERIES_15M["BTCUSDT"].append(k)
            for k in k4h[:-1]:
                bot.SERIES_4H["BTCUSDT"].append(k)
            bot.on_bar_closed("BTCUSDT", "15m", k15[-1])
            log.info("on_bar_closed OK · 누적 dry-send %d건", len(sent))
        else:
            log.warning("백필 결과 없어 통합 평가 단계 건너뜀")
    except Exception as e:
        log.exception("on_bar_closed 실패: %s", e)
        failures.append(f"on_bar_closed: {e}")

    # ─── 결과 ─────────────────────────────────────────────
    step("결과")
    if failures:
        log.error("스모크 테스트 실패: %s", failures)
        return 1
    log.info("스모크 테스트 전부 통과 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
