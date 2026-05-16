"""
Bitget USDT-M Perpetual Futures 공개 WebSocket 클라이언트.

기존 krtky_alert_bot.py 가 Binance REST 폴링(15m + 4h)으로 동작하던 부분을
실시간 WebSocket 푸시로 갈음하기 위한 모듈이다. 알림봇과의 인터페이스를
맞추기 위해 콜백으로 넘기는 kline dict 키 형식은 krtky_alert_bot.backfill_klines
와 동일하다:

    {
        "open_time":  int,  # ms epoch, 봉 시작
        "open":       float,
        "high":       float,
        "low":        float,
        "close":      float,
        "volume":     float,  # base asset 단위 (Binance 와 동일하게 base volume)
        "close_time": int,    # ms epoch, 봉 종료 직전 (start + interval - 1)
    }

Bitget WS V2 요점 (Context7 / 공식 문서):
    - 엔드포인트: wss://ws.bitget.com/v2/ws/public
    - 구독:
        {"op": "subscribe",
         "args": [{"instType": "USDT-FUTURES",
                   "channel":  "candle15m",
                   "instId":   "BTCUSDT"}, ...]}
    - 푸시 data 배열 순서 (모두 문자열):
        [ts, open, high, low, close, baseVol, quoteVol, usdtVol]
      ts 는 봉 시작 시각 (ms epoch).
    - 봉 닫힘 플래그가 없다 → 같은 ts 가 계속 업데이트되다가, 새 ts 가
      들어오면 직전 ts 의 봉이 닫힌 것으로 간주한다. 봇은 마지막에 한 번
      더 close 가 박혀 올라온 직전 봉 스냅샷을 콜백에 전달한다.
    - 핑/퐁: 클라이언트가 30초마다 "ping" 텍스트 프레임 전송, 서버는
      "pong" 으로 응답. 다른 형태(JSON ping 등)는 지원하지 않는다.

본 모듈은 시그널 전용. 자동매매에 직접 사용 금지.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

import websocket  # websocket-client 패키지

# Windows cp949 콘솔에서도 UTF-8 출력 가능하도록 (기존 봇과 동일 처리)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────
BITGET_WS_PUBLIC_V2 = "wss://ws.bitget.com/v2/ws/public"

# Bitget USDT-M 영구계약 instType (V2)
INST_TYPE_USDT_FUTURES = "USDT-FUTURES"

# 채널명 → 봉 길이(ms). krtky 알림봇은 15m / 4H 두 가지만 쓴다.
# 새 interval 추가 시 여기에만 채워 두면 close_time 계산이 자동으로 따라간다.
INTERVAL_MS: dict[str, int] = {
    "candle1m":   60 * 1000,
    "candle3m":   3 * 60 * 1000,
    "candle5m":   5 * 60 * 1000,
    "candle15m":  15 * 60 * 1000,
    "candle30m":  30 * 60 * 1000,
    "candle1H":   60 * 60 * 1000,
    "candle4H":   4 * 60 * 60 * 1000,
    "candle6H":   6 * 60 * 60 * 1000,
    "candle12H":  12 * 60 * 60 * 1000,
    "candle1D":   24 * 60 * 60 * 1000,
}

# 재연결 backoff (초)
RECONNECT_BACKOFF_INITIAL = 1.0
RECONNECT_BACKOFF_MAX = 30.0

# 핑 주기
PING_INTERVAL_SEC = 30.0
# 서버 응답이 이 시간 동안 한 글자도 없으면 좀비 커넥션으로 보고 끊는다
RECV_STALE_TIMEOUT_SEC = 90.0


log = logging.getLogger("krtky.bitget_ws")


# ─────────────────────────────────────────────────────────────
# 타입 alias
# ─────────────────────────────────────────────────────────────
# 봉 완성 콜백: (symbol, interval_short, kline_dict) → None
#   interval_short 는 "15m", "4h" 처럼 사람이 읽기 쉬운 표기 (Binance interval
#   문자열과 호환되도록 소문자 h). 채널명 "candle15m" → "15m", "candle4H" → "4h".
OnBarClosed = Callable[[str, str, dict], None]


def _interval_short(channel: str) -> str:
    """Bitget 채널명을 Binance 식 interval 문자열로 변환.

    candle15m → 15m, candle4H → 4h, candle1D → 1d
    """
    suffix = channel.replace("candle", "")
    return suffix.lower()


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────
class BitgetCandleStream:
    """Bitget USDT-M 선물 캔들 스트림 (멀티 심볼 × 멀티 인터벌).

    Parameters
    ----------
    symbols : list[str]
        구독할 심볼들. 예: ["BTCUSDT", "ETHUSDT"]. 대문자 권장.
    intervals : list[str]
        Bitget 채널 표기 ("candle15m", "candle4H"). INTERVAL_MS 키와 일치해야 한다.
    on_bar_closed : OnBarClosed
        봉이 닫혔다고 판정될 때 호출되는 콜백.
        (symbol, "15m"/"4h"/..., kline_dict) 시그니처.

    Notes
    -----
    - 콜백은 WS 수신 스레드(websocket-client 내부 스레드)에서 호출된다.
      알림봇 측에서 무거운 작업을 하려면 큐로 떠넘기는 편이 안전하다.
    - close_time 은 (open_time + interval_ms - 1) 로 계산한다. Bitget 은
      별도 close_time 을 주지 않는다.
    """

    def __init__(
        self,
        symbols: list[str],
        intervals: list[str],
        on_bar_closed: OnBarClosed,
    ) -> None:
        unknown = [iv for iv in intervals if iv not in INTERVAL_MS]
        if unknown:
            raise ValueError(f"지원하지 않는 interval: {unknown}. "
                             f"INTERVAL_MS 에 추가하세요.")

        self.symbols: list[str] = [s.upper() for s in symbols]
        self.intervals: list[str] = list(intervals)
        self.on_bar_closed: OnBarClosed = on_bar_closed

        # 직전에 받은 봉의 (open_time, kline_dict). key = (symbol, channel)
        # 새 open_time 이 들어오면 이 dict 에 보관된 봉이 "닫혔다" 고 보고
        # 콜백을 발사한다.
        self._last_bar: dict[tuple[str, str], dict] = {}

        # snapshot 단계에서 이미 콜백을 쏜 봉의 open_time 집합.
        # 재연결 직후 snapshot 으로 과거 봉 여러 개가 한 번에 들어올 수 있는데
        # 그때 중복 콜백을 막기 위한 가드.
        self._closed_keys: dict[tuple[str, str], set[int]] = defaultdict(set)

        # WS / 스레드 제어
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_recv_ts: float = time.time()
        self._backoff: float = RECONNECT_BACKOFF_INITIAL

    # ─────────────────────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────────────────────
    def start(self, block: bool = True) -> None:
        """스트림 시작.

        block=True (기본) 면 현재 스레드에서 무한 재연결 루프를 돈다.
        block=False 면 별도 스레드에서 돌고 즉시 반환한다 (stop() 으로 종료).
        """
        self._stop_event.clear()
        if block:
            self._run_forever()
        else:
            self._ws_thread = threading.Thread(
                target=self._run_forever,
                name="BitgetCandleStream",
                daemon=True,
            )
            self._ws_thread.start()

    def stop(self) -> None:
        """스트림 정지 (재연결 루프 종료 + 소켓 close)."""
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                log.debug("ws.close() 예외 무시: %s", e)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5.0)

    def add_subscription(self, symbol: str) -> bool:
        """런타임 신규 종목 구독 추가 (모든 intervals).

        데일리 universe 갱신 시 호출. WS 가 살아 있으면 즉시 subscribe payload
        전송, 아니면 다음 재연결 _on_open 에서 자동 포함되도록 self.symbols 만
        갱신. Returns True 면 즉시 전송 성공.
        """
        sym = symbol.upper()
        if sym in self.symbols:
            return True
        self.symbols.append(sym)
        if self._ws is None:
            log.info("add_subscription %s: WS 미연결, 다음 재연결 시 포함", sym)
            return False
        args = [
            {"instType": INST_TYPE_USDT_FUTURES, "channel": ch, "instId": sym}
            for ch in self.intervals
        ]
        try:
            self._ws.send(json.dumps({"op": "subscribe", "args": args}))
            log.info("add_subscription %s: %d 채널 구독 추가", sym, len(args))
            return True
        except Exception as e:
            log.warning("add_subscription %s 송신 실패: %s", sym, e)
            return False

    def remove_subscription(self, symbol: str) -> bool:
        """런타임 종목 구독 해제. self.symbols 에서 제거 + unsubscribe payload."""
        sym = symbol.upper()
        if sym in self.symbols:
            self.symbols.remove(sym)
        if self._ws is None:
            return False
        args = [
            {"instType": INST_TYPE_USDT_FUTURES, "channel": ch, "instId": sym}
            for ch in self.intervals
        ]
        try:
            self._ws.send(json.dumps({"op": "unsubscribe", "args": args}))
            log.info("remove_subscription %s: %d 채널 구독 해제", sym, len(args))
            return True
        except Exception as e:
            log.warning("remove_subscription %s 송신 실패: %s", sym, e)
            return False

    # ─────────────────────────────────────────────────────────
    # 내부 — 재연결 루프
    # ─────────────────────────────────────────────────────────
    def _run_forever(self) -> None:
        log.info("Bitget WS 시작 · symbols=%s · intervals=%s",
                 self.symbols, self.intervals)
        while not self._stop_event.is_set():
            try:
                self._connect_once()
            except Exception as e:
                log.exception("WS 루프 예외: %s", e)

            if self._stop_event.is_set():
                break

            # 정상 종료든 예외든, 백오프 후 재시도
            sleep_s = self._backoff
            log.warning("WS 재연결 대기 %.1fs", sleep_s)
            self._stop_event.wait(sleep_s)
            self._backoff = min(self._backoff * 2.0, RECONNECT_BACKOFF_MAX)

        log.info("Bitget WS 종료")

    def _connect_once(self) -> None:
        """한 번의 WebSocket 세션. 끊기면 함수가 반환된다."""
        self._ws = websocket.WebSocketApp(
            BITGET_WS_PUBLIC_V2,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._last_recv_ts = time.time()
        # ping 스레드는 세션마다 새로 만든다 (이전 ws 참조가 죽으므로)
        self._ping_thread = threading.Thread(
            target=self._ping_loop,
            args=(self._ws,),
            name="BitgetCandleStream-ping",
            daemon=True,
        )
        self._ping_thread.start()

        # websocket-client 의 자체 ping 은 쓰지 않는다 (Bitget 은 텍스트 "ping" 요구)
        self._ws.run_forever(
            ping_interval=0,
            ping_timeout=None,
        )

    # ─────────────────────────────────────────────────────────
    # 내부 — 콜백
    # ─────────────────────────────────────────────────────────
    def _on_open(self, ws: websocket.WebSocket) -> None:
        log.info("WS 연결 성공 — 구독 전송")
        self._backoff = RECONNECT_BACKOFF_INITIAL  # 정상 연결됐으니 백오프 리셋
        args = []
        for sym in self.symbols:
            for ch in self.intervals:
                args.append({
                    "instType": INST_TYPE_USDT_FUTURES,
                    "channel":  ch,
                    "instId":   sym,
                })
        payload = {"op": "subscribe", "args": args}
        try:
            ws.send(json.dumps(payload))
            log.info("구독 요청 %d건 전송", len(args))
        except Exception as e:
            log.error("구독 전송 실패: %s", e)

    def _on_message(self, ws: websocket.WebSocket, raw: str) -> None:
        self._last_recv_ts = time.time()

        # Bitget 은 헬스체크 응답으로 평문 "pong" 을 그대로 보낸다
        if raw == "pong":
            log.debug("PONG 수신")
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("JSON 파싱 실패: %r", raw[:200])
            return

        # 구독 ack / 에러 처리
        event = msg.get("event")
        if event:
            if event == "error":
                log.error("WS 에러 응답: code=%s msg=%s arg=%s",
                          msg.get("code"), msg.get("msg"), msg.get("arg"))
            else:
                log.info("WS 이벤트: %s arg=%s", event, msg.get("arg"))
            return

        # 캔들 푸시 처리
        arg = msg.get("arg") or {}
        channel = arg.get("channel", "")
        if not channel.startswith("candle"):
            return

        inst_id = arg.get("instId")
        action = msg.get("action", "")
        data = msg.get("data") or []
        if not inst_id or not data:
            return

        interval_ms = INTERVAL_MS.get(channel)
        if interval_ms is None:
            log.debug("미등록 채널 무시: %s", channel)
            return

        self._handle_candle_push(inst_id, channel, interval_ms, action, data)

    def _on_error(self, ws: websocket.WebSocket, error: BaseException) -> None:
        log.error("WS 에러: %s", error)

    def _on_close(self, ws: websocket.WebSocket,
                  status_code: Optional[int],
                  msg: Optional[str]) -> None:
        log.warning("WS 종료 status=%s msg=%s", status_code, msg)

    # ─────────────────────────────────────────────────────────
    # 내부 — 캔들 핸들링
    # ─────────────────────────────────────────────────────────
    def _handle_candle_push(
        self,
        symbol: str,
        channel: str,
        interval_ms: int,
        action: str,
        data: list,
    ) -> None:
        """한 푸시 메시지의 data 배열을 순회하며 봉 닫힘을 판정."""
        key = (symbol, channel)
        # Bitget 은 snapshot 에 과거 봉 여러 개를 ts 오름차순으로 실어 준다
        # (시뮬레이션 결과 — 문서상 명시 없음). 안전하게 ts 로 정렬.
        try:
            rows = sorted(data, key=lambda r: int(r[0]))
        except (ValueError, TypeError, IndexError):
            log.warning("데이터 정렬 실패: %r", data[:1])
            return

        for row in rows:
            kline = self._row_to_kline(row, interval_ms)
            if kline is None:
                continue
            new_open = kline["open_time"]

            prev = self._last_bar.get(key)
            if prev is None:
                # 첫 봉 — 비교 대상 없음. 그냥 보관.
                self._last_bar[key] = kline
                continue

            prev_open = prev["open_time"]

            if new_open == prev_open:
                # 같은 봉의 업데이트 — 최신 OHLCV 로 덮어쓰기만 한다
                self._last_bar[key] = kline
                continue

            if new_open > prev_open:
                # 새 봉으로 넘어갔다 → 직전 봉이 닫힌 것
                self._emit_closed(symbol, channel, prev)
                self._last_bar[key] = kline
                continue

            # new_open < prev_open: snapshot 중 과거 봉. 닫힌 게 확실하므로 즉시 emit.
            if new_open not in self._closed_keys[key]:
                self._emit_closed(symbol, channel, kline)

        # 디버그 로그 (debug 레벨)
        if log.isEnabledFor(logging.DEBUG):
            last = self._last_bar.get(key)
            if last:
                log.debug("%s %s action=%s last_open=%s close=%.4f",
                          symbol, channel, action,
                          last["open_time"], last["close"])

    def _row_to_kline(self, row: list, interval_ms: int) -> Optional[dict]:
        """Bitget 푸시 row → 알림봇 호환 kline dict."""
        try:
            open_time = int(row[0])
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])  # noqa: E741 (PEP8 — low 의 약자, 가독성 우선)
            c = float(row[4])
            base_vol = float(row[5])
        except (ValueError, TypeError, IndexError) as e:
            log.warning("row 파싱 실패 %s: %r", e, row)
            return None
        return {
            "open_time":  open_time,
            "open":       o,
            "high":       h,
            "low":        l,
            "close":      c,
            "volume":     base_vol,
            "close_time": open_time + interval_ms - 1,
        }

    def _emit_closed(self, symbol: str, channel: str, kline: dict) -> None:
        """봉 닫힘 콜백 발사 + 중복 가드 갱신."""
        key = (symbol, channel)
        ot = kline["open_time"]
        if ot in self._closed_keys[key]:
            return  # 이미 발사한 봉
        self._closed_keys[key].add(ot)

        # 메모리 누수 방지 — 채널별로 최근 500봉만 기억
        if len(self._closed_keys[key]) > 500:
            # 가장 오래된 것부터 잘라낸다
            keep = sorted(self._closed_keys[key])[-500:]
            self._closed_keys[key] = set(keep)

        try:
            self.on_bar_closed(symbol, _interval_short(channel), kline)
        except Exception as e:
            # 사용자 콜백에서 터진 예외는 스트림을 죽이지 않는다
            log.exception("on_bar_closed 콜백 예외 (%s %s): %s",
                          symbol, channel, e)

    # ─────────────────────────────────────────────────────────
    # 내부 — 핑 루프
    # ─────────────────────────────────────────────────────────
    def _ping_loop(self, ws: websocket.WebSocketApp) -> None:
        """30초마다 "ping" 텍스트 송신. 동시에 stale 감지."""
        while not self._stop_event.is_set():
            # 30초를 한 번에 자지 않고 1초씩 나눠 자야 stop() 응답이 빠르다
            for _ in range(int(PING_INTERVAL_SEC)):
                if self._stop_event.is_set():
                    return
                time.sleep(1.0)

            # 좀비 커넥션 체크 (서버 응답이 너무 오래 없으면 강제 close)
            if time.time() - self._last_recv_ts > RECV_STALE_TIMEOUT_SEC:
                log.warning("WS stale (%.0fs 무응답) — 강제 종료 후 재연결",
                            time.time() - self._last_recv_ts)
                try:
                    ws.close()
                except Exception:
                    pass
                return

            try:
                ws.send("ping")
                log.debug("PING 전송")
            except Exception as e:
                log.warning("PING 송신 실패 (소켓 닫힘 추정): %s", e)
                return


# ─────────────────────────────────────────────────────────────
# 단독 실행 데모
# ─────────────────────────────────────────────────────────────
def _demo() -> None:
    """모듈 단독 실행 시 BTCUSDT 15m + 4h 봉을 콘솔에 찍는 데모."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    def on_bar(symbol: str, interval: str, kline: dict) -> None:
        ot = kline["open_time"]
        # ms → 사람이 읽을 수 있는 KST
        from datetime import datetime, timedelta, timezone
        kst = timezone(timedelta(hours=9))
        ot_str = datetime.fromtimestamp(ot / 1000, tz=kst).strftime("%m-%d %H:%M")
        print(f"[CLOSED] {symbol} {interval} {ot_str} KST "
              f"O={kline['open']} H={kline['high']} "
              f"L={kline['low']} C={kline['close']} V={kline['volume']:.2f}")

    stream = BitgetCandleStream(
        symbols=["BTCUSDT"],
        intervals=["candle15m", "candle4H"],
        on_bar_closed=on_bar,
    )
    try:
        stream.start(block=True)
    except KeyboardInterrupt:
        print("\nCtrl+C — 종료 중...")
        stream.stop()


if __name__ == "__main__":
    _demo()
