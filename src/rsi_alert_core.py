"""기존 rsi_alert_bot.py 의 평가 로직을 함수형으로 재구성한 모듈.

원본 파일:
    C:\\sum  you\\strategy_v4_faithful\\live_demo_v15\\rsi_alert_bot.py

원본은 자체 폴링 루프 + ccxt + pandas + V14Notifier 구성이지만, 본 모듈은
krtky_alert_bot 과 같은 Bitget WebSocket 콜백 / 같은 텔레그램 send 함수를
재사용한다. 따라서 두 알림기가 한 프로세스에서 같은 봉 스트림을 공유한다.

원본 4단계 알림 (롱 전용):
    [1] 사전 알림  : 15m RSI ≤ 30
    [2] 진입       : 다음 양봉 마감
    [3] ⭐ 진입    : 4H RSI ≤ 30 OR 5+ 연속 음봉
    [4] 🔥 강력    : 4H RSI ≤ 30 AND 5+ 연속 음봉
    Timeout        : 8봉 (2시간)

본 모듈은 시그널 전용. 자동매매에 직접 사용 금지.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────
# 원본과 동일한 임계값
# ─────────────────────────────────────────────────────────────
RSI_OVERSOLD = 30                # 원본 RSI ≤ 30
NEAR_OVERSOLD = 35
STRONG_RED_CONSEC_MIN = 5
PRE_ALERT_TIMEOUT_BARS = 8       # 8 × 15m = 2h
RSI_PERIOD = 14

KST = timezone(timedelta(hours=9))
log = logging.getLogger("krtky.rsi_alert")


# ─────────────────────────────────────────────────────────────
# Wilder RSI (krtky_alert_bot 과 동일 알고리즘, 입력은 list[float])
# ─────────────────────────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def count_consecutive_red(klines: list[dict], up_to_idx: int) -> int:
    """up_to_idx 까지 거꾸로 세는 연속 음봉 카운트."""
    count = 0
    for i in range(up_to_idx, -1, -1):
        k = klines[i]
        if k["close"] < k["open"]:
            count += 1
        else:
            break
    return count


# ─────────────────────────────────────────────────────────────
# 상태 머신 (PRE_ALERT 외에는 IDLE)
# ─────────────────────────────────────────────────────────────
@dataclass
class RSISymbolState:
    status: str = "IDLE"         # "IDLE" | "PRE_ALERT"
    trigger_bar_ts: Optional[int] = None
    trigger_rsi: float = 0.0
    bars_waited: int = 0
    last_processed_bar: Optional[int] = None


def to_kst(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%H:%M")


def _fmt_vol(symbol: str, vol: float) -> str:
    """short 단위로 거래량 표기. 원본 메시지 포맷 보존."""
    short = symbol.replace("USDT", "")
    return f"{vol:,.1f} {short}"


# ─────────────────────────────────────────────────────────────
# 메시지 빌더 — 원본 포맷 보존 + ICT_SGBOT 라벨 prefix
# ─────────────────────────────────────────────────────────────
# 모든 알림 메시지에 동봉되는 한 줄 면책 푸터 (ICT 봇 톤 통일)
DISCLAIMER_FOOTER = (
    "<i>※ 본 알림은 투자 권유·자문이 아닙니다. "
    "진입·청산·손절 판단은 본인 책임입니다.</i>"
)


def build_pre_alert(symbol: str, ts_ms: int, rsi: float, vol: float,
                    label: str, kind_emoji: str = "🪙",
                    last_close: Optional[float] = None) -> str:
    price_line = (
        f"⏰ {to_kst(ts_ms)} KST · 현재가 <b>{last_close:,.4f}</b>\n"
        if last_close is not None
        else f"⏰ {to_kst(ts_ms)} KST\n"
    )
    return (
        f"📊 [{label}] [1] 사전 알림 · <b>LONG</b> · "
        f"{kind_emoji} <b>{symbol}</b>\n"
        f"――――――――――\n"
        f"{price_line}\n"
        f"RSI 15m <b>{rsi:.1f}</b> (≤ 30 과매도)\n"
        f"거래량: {_fmt_vol(symbol, vol)}\n\n"
        f"🎯 다음 15분봉 양봉 마감 시 진입 신호 발송\n\n"
        f"{DISCLAIMER_FOOTER}"
    )


def build_entry(symbol: str, ts_ms: int, rsi: float, vol: float,
                rsi_4h: Optional[float], red_consec: int,
                is_strong: bool, is_partial: bool, label: str,
                kind_emoji: str = "🪙",
                last_close: Optional[float] = None) -> str:
    if is_strong:
        icon = "🔥"
        sub = f"강력 진입 신호 (4H 과매도 + {red_consec}연속 음봉)"
    elif is_partial:
        tag_bits = []
        if rsi_4h is not None and rsi_4h <= RSI_OVERSOLD:
            tag_bits.append("4H 과매도")
        if red_consec >= STRONG_RED_CONSEC_MIN:
            tag_bits.append(f"{red_consec}연속 음봉")
        icon = "⭐"
        sub = f"진입 신호 ({' + '.join(tag_bits)})"
    else:
        icon = "🟢"
        sub = "진입 신호"

    rsi_tag = " (과매도)" if rsi < NEAR_OVERSOLD else ""
    price_str = f" · 현재가 <b>{last_close:,.4f}</b>" if last_close is not None else ""

    lines = [
        f"{icon} [{label}] 롱 {sub} · {kind_emoji} <b>{symbol}</b>",
        "――――――――――",
        f"✅ 진입 캔들: {to_kst(ts_ms)} KST 양봉 마감{price_str}",
        f"RSI 15m: <b>{rsi:.1f}</b>{rsi_tag}",
    ]
    if rsi_4h is not None:
        tag_4h = " ⚠️과매도" if rsi_4h <= RSI_OVERSOLD else ""
        lines.append(f"RSI 4H: {rsi_4h:.1f}{tag_4h}")
    if red_consec > 0:
        lines.append(f"📉 연속 음봉: {red_consec}개")
    lines.append(f"거래량: {_fmt_vol(symbol, vol)}")
    lines.append("")
    lines.append(DISCLAIMER_FOOTER)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 평가 함수 — krtky_alert_bot 의 on_bar_closed 에서 호출
# ─────────────────────────────────────────────────────────────
def evaluate_rsi_15m(
    symbol: str,
    klines_15m: list[dict],
    klines_4h: list[dict],
    state: RSISymbolState,
    send: Callable[[str], None],
    label: str = "ICT_SGBOT · RSI",
    kind_emoji: str = "🪙",
) -> None:
    """원본 rsi_alert_bot.check_symbol 의 핵심 로직을 함수화.

    Parameters
    ----------
    klines_15m, klines_4h : 시리즈 (오름차순, 마지막은 닫힌 봉)
    state : 심볼별 RSI 상태 머신
    send  : 텔레그램 송신 함수 (krtky_alert_bot.send_telegram 등)
    label : 메시지 prefix (ICT_SGBOT · RSI / ICT_SGBOT · 크트키 등 구분용)
    """
    if len(klines_15m) < RSI_PERIOD + 5:
        return

    last = klines_15m[-1]
    if state.last_processed_bar == last["close_time"]:
        return
    state.last_processed_bar = last["close_time"]

    rsi = calc_rsi([k["close"] for k in klines_15m])
    is_green = last["close"] > last["open"]
    vol = last["volume"]

    log.info(
        "%s bar=%s close=%s %s RSI=%.1f status=%s",
        symbol,
        to_kst(last["close_time"]),
        last["close"],
        "G" if is_green else ("R" if last["close"] < last["open"] else "-"),
        rsi,
        state.status,
    )

    if state.status == "IDLE":
        if rsi <= RSI_OVERSOLD:
            state.status = "PRE_ALERT"
            state.trigger_bar_ts = last["close_time"]
            state.trigger_rsi = rsi
            state.bars_waited = 0
            send(build_pre_alert(symbol, last["close_time"], rsi, vol, label,
                                 kind_emoji=kind_emoji,
                                 last_close=last["close"]))
            log.info("%s PRE_ALERT (RSI=%.1f)", symbol, rsi)
        return

    # PRE_ALERT 상태
    state.bars_waited += 1

    if is_green:
        rsi_4h: Optional[float]
        if klines_4h and len(klines_4h) >= RSI_PERIOD + 1:
            rsi_4h = calc_rsi([k["close"] for k in klines_4h])
        else:
            rsi_4h = None

        # 양봉 직전까지의 연속 음봉 카운트 (klines_15m[-2] 부터 거꾸로)
        red_consec = count_consecutive_red(klines_15m, len(klines_15m) - 2)

        is_strong = (
            rsi_4h is not None
            and rsi_4h <= RSI_OVERSOLD
            and red_consec >= STRONG_RED_CONSEC_MIN
        )
        is_partial = (
            (rsi_4h is not None and rsi_4h <= RSI_OVERSOLD)
            or red_consec >= STRONG_RED_CONSEC_MIN
        )

        send(build_entry(
            symbol, last["close_time"], rsi, vol,
            rsi_4h=rsi_4h, red_consec=red_consec,
            is_strong=is_strong, is_partial=is_partial,
            label=label, kind_emoji=kind_emoji,
            last_close=last["close"],
        ))
        log.info("%s ENTRY (RSI=%.1f 4H=%s red=%d strong=%s)",
                 symbol, rsi, rsi_4h, red_consec, is_strong)
        state.status = "IDLE"
        return

    if state.bars_waited >= PRE_ALERT_TIMEOUT_BARS:
        log.info("%s PRE_ALERT timeout after %d bars", symbol, state.bars_waited)
        state.status = "IDLE"
