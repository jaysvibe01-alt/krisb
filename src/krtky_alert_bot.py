"""
크트키 알림봇 — Kris B PPT 충실 구현 · Bitget WebSocket 실시간판

"크트키 자리" 핵심 정의 (사용자 확정):
  롱: 거래량 많이 + RSI ≤ 30 + 15분 양봉 마감 컨펌 → 다음봉 밑꼬리 진입
  숏: 거래량 많이 + RSI ≥ 70 + 15분 음봉 마감 컨펌 → 다음봉 윗꼬리 진입

RSI 임계는 사전 알림 봉(직전 장대봉)에 적용한다. 컨펌봉은 양봉/음봉 마감
+ 장대 조건만 본다 — 그 시점엔 RSI 가 이미 반등/하락 중일 수 있다.

⚠️ 손절 운영 경고 (슬라이드 37·42):
PPT 본인 복기에서 "방향은 맞았는데 손절선에 털림" 케이스가 자주 나온다.
봇은 ATR×1.5 가이드만 동봉할 뿐, 실제 손절 운영은 사람 몫이다.
절대 자동매매에 직접 연결하지 말 것.

PPT 표지 4원칙 (슬라이드 1):
  1) 15분봉 기준
  2) 장대 + 거래량 많이
  3) 과매도 + 양봉 컨펌 후
  4) 다음 캔들 밑꼬리 진입

본문 추가 룰:
  - 슬라이드 14: 진입 보수성 3단계 → [1] 사전 알림 → [2] 양봉 컨펌
  - 슬라이드 15: 흡수 누적 N봉 ("짧아진 캔들들" 복수형)
  - 슬라이드 18: 꼬리 ≥ 50% → 컨펌 생략 후보 (부가 태그)
  - 슬라이드 20·22·26: RSI 다이버전스 컨플루언스
  - 슬라이드 25: 색상 코드 (노랑=다이버, 주황=봉갱신, 초록=양봉컨펌) ← 주황 보강
  - 슬라이드 36: 풀음봉/풀양봉 진입 거부 필터
  - 슬라이드 37·42: 손절선 운영 어려움 → ATR×1.5 가이드 동봉
  - 슬라이드 40·43·44: SR Flip — 장대봉 몸통/꼬리 가격대 기억 후 리테스트 감지

데이터 소스: Bitget USDT-M 영구계약 WebSocket (실시간) + REST backfill
알림 채널: Telegram (t.me/ICT_SGBOT — 사용자 ICT 봇과 동일 채팅)

본 봇은 시그널 전용. 자동매매에 직접 사용 금지.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from bitget_ws import BitgetCandleStream
from rsi_alert_core import RSISymbolState, evaluate_rsi_15m
from yfinance_source import fetch_yf_klines, is_yfinance_symbol
from krbonacci import (
    compute_take_profit_targets,
    detect_krbonacci_confluence,
    detect_quartile_confluence,
    hit_to_label,
    quartile_hit_to_label,
)

# Windows cp949 콘솔에서도 UTF-8 출력 가능하도록
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 설정 (환경변수로 오버라이드 가능)
# ─────────────────────────────────────────────────────────────
BITGET_REST_BASE = "https://api.bitget.com"

# Universe 구성 — 거래량 상위 N개 + 고정 EXTRA. main() 시작 시 compute_universe() 로 초기화.
# 환경변수 KRTKY_TOP_N 로 오버라이드 가능 (기본 30).
TOP_VOLUME_N = int(os.environ.get("KRTKY_TOP_N", "30"))
# 고정 종목 — 토큰화 주식·원자재. top20 에서 빠져도 항상 universe 에 포함.
# Bitget USDT-FUTURES 페어 표기 (티커 + USDT 합친 형태).
EXTRA_SYMBOLS = [
    "EWYUSDT",     # Korea ETF (토큰화)
    "NVDAUSDT",    # Nvidia
    "TSLAUSDT",    # Tesla
    "MSFTUSDT",    # Microsoft
    "INTCUSDT",    # Intel
    "CLUSDT",      # Crude Oil
    "XAUUSDT",     # Gold
]
# 기본 10종목 (2026-05-17 코덱스 권장 — 종목 확장 1순위로 채택)
# 백테스트 검증: 신규 6종 모두 EV 양수, avg R 0.24~0.39, Win 71~77%
#   DOGE 111거래 +0.390R Win 77% (★★ 우수)
#   LINK  99거래 +0.325R Win 74% (★★ 우수)
#   AVAX 116거래 +0.294R Win 74% (★ 채택)
#   ADA   99거래 +0.299R Win 73% (★ 채택)
#   SUI  114거래 +0.275R Win 74% (★ 채택)
#   BNB  137거래 +0.243R Win 71% (★ 채택)
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",       # 코어 4
    "DOGEUSDT", "LINKUSDT",                            # ★★ 10종
    "ADAUSDT", "AVAXUSDT", "SUIUSDT", "BNBUSDT",       # ★ 10종
    "TONUSDT", "HYPEUSDT",                             # 12종 (TON ★★)
    "BCHUSDT", "FILUSDT", "ARBUSDT",                   # 19종 ★★
    "LTCUSDT", "OPUSDT", "DOTUSDT", "ETCUSDT",         # 19종 ★
    # 29종 확장 (사용자 요청: ZEC, AAVE, ENA, PENGU + rate-limit 재시도)
    "AAVEUSDT", "ENAUSDT", "PENGUUSDT", "NEARUSDT",   # ★★ PREMIUM (Win 76-83%)
    "APTUSDT", "SEIUSDT", "INJUSDT", "TIAUSDT", "WIFUSDT",  # ★★ 우수~PREMIUM
    "ZECUSDT",                                          # ★ 채택
    # TRX 거부 (avg -0.080R Win 53% 손해)
    # PEPE 거부 (다운로드 실패 — Binance 표기 형식 확인 필요)
]
# 런타임 universe — main() 에서 compute_universe() 결과로 채워짐
SYMBOLS: list[str] = list(DEFAULT_SYMBOLS)

# 시리즈 캐시 — 봇이 RSI/ATR/다이버 계산에 쓰는 최근 봉 보관량
SERIES_MAX_15M = 200
SERIES_MAX_4H = 100
SERIES_MAX_1D = 50   # 일봉 50봉 — Kris 텔레그램 03:11 4분할 피보용

# 코인별 거래량 절대 임계 (PPT 슬라이드 21·30: "15분봉 2000개" 메모)
# base asset 단위. 4코인은 하드코딩 유지 (운영 검증된 값). EXTRA 7개 +
# top20 신규 종목은 워밍업 직후 24h 평균 × 0.5 로 자동 등록 (set_dynamic_volume_min).
VOLUME_ABSOLUTE_MIN: dict[str, float] = {
    "BTCUSDT": 1500.0,
    "ETHUSDT": 30_000.0,
    "SOLUSDT": 200_000.0,
    "XRPUSDT": 50_000_000.0,
}
DYNAMIC_VOLUME_MULTIPLE = 0.5   # 24h 평균 × 이 배수 = 신규 종목 임계
DYNAMIC_VOLUME_BACKFILL_BARS = 96   # 24h = 15m × 96봉

# 상대 거래량 (직전 N봉 평균 대비)
VOLUME_SMA_PERIOD = 20
VOLUME_SMA_MULTIPLE = 2.0

# 장대 캔들 필터 (PPT 원칙 ②)
BODY_TO_RANGE_MIN = 0.6   # 몸통이 전체의 60% 이상
BODY_TO_ATR_MIN = 1.0     # 몸통이 ATR(14) 이상

# 풀음봉 거부 (슬라이드 36)
NO_WICK_THRESHOLD = 0.1   # 꼬리 합계 ratio < 10% → 거부

# 흡수 캔들 (슬라이드 7·15)
ABSORB_VOLUME_RATIO = 1.2
ABSORB_BODY_RATIO = 0.6
ABSORB_STREAK_LENGTH = 3   # 슬라이드 15 "캔들들" 복수형

# RSI 임계 (사용자 확정 정의: 30/70 정석 — 사용자 발언 "다음봉 밑꼬리/윗꼬리 진입" 룰과
# 함께 정의된 표지 4원칙의 본래 값. 운영 중 32/68 으로 잠시 완화했지만 사용자가
# "크트키 자리 제대로 분석" 요구로 정의대로 복귀. 백테스트도 30/70 으로 재산출).
# 사전 알림 봉(장대 반대 캔들)에만 적용. 컨펌봉은 RSI 체크 안 함.
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# ────────────────────────────────────────────────────────────────────
# RSI soft 임계 — Kris 본인 슬라이드 사례 기반 (2026-05-17 추가)
# 출처: PPT 분석 (analysis_slides_17_31.md)
#   슬라이드 19: RSI 43.6 — 장대+거래량+과매도+양봉 컨펌 후 밑꼬리 롱 (4원칙 충족)
#   슬라이드 20: RSI 36.2 — 과매도+양봉 컨펌 후
#   슬라이드 26: RSI 47.8
#   슬라이드 27: RSI 40.2 — 과매도+양봉 전환 후 밑꼬리 롱
#
# Kris 본인 "과매도" = RSI 30 절대 임계가 아니라 "RSI 라인이 30 근처로
# 내려갔다가 반등 시작 + 시각적 다이버/꼬리"의 종합 판정.
#
# 봇 적용:
#   - hard 임계 (30/70): 기본 통과
#   - soft 임계 (40/60): 역추세 시그널(다이버/SR Flip/흡수/HTF) 동반 시만 통과
# ────────────────────────────────────────────────────────────────────
RSI_OVERSOLD_SOFT = 40   # 추세선 리테스트 OR 더블바텀 동반 시만 LONG 통과 (BTC만)
RSI_OVERBOUGHT_SOFT = 60  # 대칭 — SHORT
RSI_SOFT_GATE_ENABLED = True

# 옵션 D: 추세선 리테스트 + 더블바텀 신규 감지 (2026-05-17)
# 사람 눈 패턴을 자동 감지해 RSI soft 임계 활성화 조건으로 사용.
# 단순 다이버/SR/흡수만으론 가짜 자리도 통과 → 더 엄격한 차트 패턴 요구.
TRENDLINE_RETEST_LOOKBACK = 30   # 최근 30봉 EMA20 기울기 평가 윈도우
TRENDLINE_TOLERANCE_PCT = 0.005  # EMA20 ±0.5% 터치 (BTC 변동성 고려)
DOUBLE_BOTTOM_LOOKBACK = 25      # 직전 25봉 안에서 두 저점
DOUBLE_BOTTOM_TOLERANCE = 0.010  # 두 저점 ±1.0% 안 (BTC 변동성)

# 노란 다이버 라인 (PPT 슬라이드 6·12·20·22·26·35) — swing-pivot 기반.
# 슬라이드 20·22 의 1차 → 2차 진입이 ~30~100 봉 떨어진 사례를 잡으려면
# 윈도우가 충분히 커야 한다 (기존 lookback=5 로는 못 잡음).
DIVERGENCE_SWING_WINDOW = 50   # 최근 50봉 안에서 swing 2개 비교
SWING_PIVOT_LR = 3             # 좌우 3봉보다 낮으면 swing low 인정

# MTF 4시간봉 컨플루언스 ([4] 강력 단계) — 4H 도 30/70 정석
HTF_RSI_OVERSOLD = 30
HTF_RSI_OVERBOUGHT = 70

# 신고가/신저가 갱신(주황색·슬라이드 25) — 직전 N봉 기준
LEVEL_BREAK_LOOKBACK = 20

# SR Flip 휴리스틱 (슬라이드 40·43·44) — 장대봉의 몸통/꼬리 영역을 보관
SR_FLIP_TOLERANCE = 0.003   # 0.3% 이내 리테스트 시 SR Flip 인정
SR_FLIP_LEVELS_MAX = 6      # 심볼당 보관할 최근 장대봉 수

# 고립 반전 캔들 (출처: Kris 텔레그램 03:14-15) — PPT 외 신규 룰
# "연속된 음봉/양봉 중간의 양봉/음봉" — 추세 속 고립 반전 캔들의 high/low 가
# 향후 단기 저항·지지로 작용한다는 본인 발언. 8봉 중 75% 가 같은 방향이면
# 추세로 인정, 그 사이에 끼인 1봉이 반대 방향이면 고립 반전으로 기록.
ISOLATED_LOOKBACK = 8
ISOLATED_STREAK_RATIO = 0.75
ISOLATED_SR_TOLERANCE = 0.003   # 0.3% 매칭
ISOLATED_SR_TTL_SEC = 6 * 3600  # 6시간 만료
ISOLATED_SR_MAX_BARS = 50       # 또는 50봉 후 만료

# Timeout: [1] 사전 알림 후 [2] 컨펌 대기 (8봉 = 2시간).
# 백테스트(1년·4종목·1,705건)에서 8봉이 최적 — RR 9.43 / 도달률 27.6% / SL 66.9%.
# 비교: 4봉(현재 X) 도달률 18.9%·RR 5.21, 12봉 도달률 32%·RR 8.73 (도달률↑ but RR↓).
# 결과 파일: backtest_data/timeout_multi_results.json
PRE_ALERT_TIMEOUT_BARS = 8

# Segment 필터 (백테스트 1년 4종목 결과 기반).
# 출처: regime_analysis.json + user_model_backtest.json (M2 매수존 적용 후)
#
# ⚠️ Long/BTCUSDT SKIP 보류 — PPT 본인 슬라이드 3·7·9·11·14·15·17·20·22 모두
#    BTC 롱 진입 성공 사례. 백테스트 RR 0.47 은 데이터 기간(2024~2025) 의
#    특정 BTC regime 에서만 약한 결과일 가능성. BTC 별도 정밀 분석 필요.
#
# 현재 SKIP — 약한 손실 segment만 (RR < 0.7) — BTC 제외
#   Short/SOLUSDT : RR 0.68, Win 61.1%, SL 48.1% — 표본 54건
# PREMIUM — 강한 우위 segment (RR > 1.5, Win > 70%) — ★ 강조 알림
#   Long/SOLUSDT  : RR 1.89, Win 74.4%, TP1 76.9% — 표본 39건
#   Long/ETHUSDT  : RR 1.79, Win 72.0%, TP1 76.0% — 표본 50건
SKIP_SEGMENTS: set[tuple[str, str]] = {
    ("short", "SOLUSDT"),
}
# 2026-05-17: 29종 확장 — ★★ PREMIUM (Win ≥ 75%, avg R ≥ 0.35)
PREMIUM_SEGMENTS: set[tuple[str, str]] = {
    ("long", "SOLUSDT"),    # RR 1.89 Win 74% (기존)
    ("long", "ETHUSDT"),    # RR 1.79 Win 72% (기존)
    ("long", "DOGEUSDT"),   # avg 0.390R Win 77% N=111
    ("long", "LINKUSDT"),   # avg 0.325R Win 74% N=99
    ("long", "TONUSDT"),    # avg 0.358R Win 77% N=108
    ("long", "BCHUSDT"),    # avg 0.418R Win 77% N=98
    ("long", "FILUSDT"),    # avg 0.450R Win 79% N=91
    ("long", "ARBUSDT"),    # avg 0.431R Win 76% N=101
    # 29종 신규 PREMIUM (★★ Win 75%+)
    ("long", "WIFUSDT"),    # avg 0.650R Win 83.3% N=108 — 최강!
    ("long", "AAVEUSDT"),   # avg 0.550R Win 82.7% N=104
    ("long", "PENGUUSDT"),  # avg 0.542R Win 80.6% N=134
    ("long", "TIAUSDT"),    # avg 0.441R Win 76.1% N=113
    ("long", "INJUSDT"),    # avg 0.401R Win 78.2% N=110
    ("long", "NEARUSDT"),   # avg 0.365R Win 75.5% N=110
    ("long", "ENAUSDT"),    # avg 0.354R Win 76.8% N=95
}

# ────────────────────────────────────────────────────────────────────
# BTC 전용 정밀 게이트 (backtest_btc_rules.py R8 + R5 결과 기반)
# 출처: btc_rules_compare.json (2026-05)
#
# R1 baseline (BTC 모든 진입): RR 중앙 0.87, Win 57% — 손해 자리
# R8 (시간×방향 best 조합) : RR 중앙 1.53, Win 75%, N=32 ★ 채택
#   - Long  : KST 06-12 (Kris 본인이 슬라이드 9·14·17 잡은 시간대) — RR 1.37 Win 80% N=10
#   - Short : KST 06-18 (한국 오전~오후) — RR 1.73 Win 73% N=22
# R5 (다이버 + 일봉4분할) : RR 중앙 13.23, Win 86%, N=7 → PREMIUM 태그 (N 적어 게이트엔 부적합)
#
# 다른 종목 (ETH/SOL/XRP) 은 BTC 만큼 시간 의존성이 강하지 않아 게이트 적용 X
# ────────────────────────────────────────────────────────────────────
BTC_TIME_GATE_ENABLED = True
# 2026-05-17 게이트 완화 (사용자 지적: BTC 진입 너무 적음)
# 백테스트 검증: 시간대별 자연 분포 → 18-24 KST 가 36% 점유 (Long+Short 합쳐 최다)
# S2 시나리오 적용: N 45→81 (+80%), avg R +0.328→+0.316 (거의 유지), Win 75.6→72.8%
BTC_LONG_HOURS_KST = range(6, 18)   # 06:00 ≤ h < 18:00 KST (Kris 새벽 차단)
BTC_SHORT_HOURS_KST = range(0, 22)  # 00:00 ≤ h < 22:00 KST (한국 심야 22-24 만 차단)


def _kst_hour(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=TIMEZONE_KST).hour


def passes_btc_time_gate(symbol: str, direction: str, bar_ts_ms: int) -> bool:
    """BTC 전용 R8 시간×방향 게이트.

    BTC 가 아니면 항상 True (다른 종목은 영향 없음).
    BTC 면:
      - long  : KST 06-12 시간대만 통과
      - short : KST 06-18 시간대만 통과
    """
    if not BTC_TIME_GATE_ENABLED:
        return True
    if symbol != "BTCUSDT":
        return True
    h = _kst_hour(bar_ts_ms)
    if direction == "long":
        return h in BTC_LONG_HOURS_KST
    if direction == "short":
        return h in BTC_SHORT_HOURS_KST
    return True


def is_btc_diver_quart_premium(symbol: str, extras: list[str]) -> bool:
    """R5 다이버 + 일봉4분할 컨플루언스 동시 — BTC 전용 PREMIUM 태그."""
    if symbol != "BTCUSDT":
        return False
    has_diver = any("다이버전스" in x for x in extras)
    has_quart = any("4분할" in x or "Quartile" in x or "쿼타일" in x for x in extras)
    return has_diver and has_quart


# ────────────────────────────────────────────────────────────────────
# BTC 역추세 시그널 게이트 (backtest_btc_countertrend.py 결과 기반)
# 출처: btc_countertrend.json (2026-05)
#
# 발견:
#   추세 추종 (CT=0): N=86, RR 0.77, Win 56% — 손해 자리
#   역추세    (CT≥1): N=19, RR 1.57, Win 63% — RR 두 배
#
# 방향별:
#   Long  / CT=0: RR 0.44 Win 45.5% ← BTC Long 손해의 원흉! → 게이트 필수
#   Short / CT=0: RR 1.08 Win 66.7% ← 살아남음 → 게이트 없음
#
# 시그널 단독 효과:
#   DIVER   : RR 7.64 Win 75% N=8 ★★★ — 압도적 1위
#   HTF_OB  : RR 2.01 Win 67% N=6 ★★
#
# 정책:
#   - BTC Long  : 역추세 시그널(DIVER/ABSORB/ISOSR/HTF/WICK50) 1개 이상 필수
#   - BTC Short : 게이트 없음 (시간 게이트 R8 만으로 충분)
#   - DIVER 단독으로도 PREMIUM 태그 (RR 7.64)
# ────────────────────────────────────────────────────────────────────
BTC_CT_GATE_ENABLED = True
COUNTERTREND_KEYWORDS = ("다이버전스", "흡수 누적", "고립 반전",
                        "과매도 컨플루언스", "과매수 컨플루언스")


def _count_countertrend_signals(extras: list[str]) -> int:
    """역추세 시그널 (DIVER/ABSORB/ISOSR/HTF) 개수."""
    n = 0
    for kw in COUNTERTREND_KEYWORDS:
        if any(kw in x for x in extras):
            n += 1
    return n


def has_wick50(kline: dict, direction: str) -> bool:
    """진입 컨펌봉의 꼬리가 본체의 50% 이상인지 (Kris 슬라이드 41-44).

    long  : 밑꼬리 / 본체 ≥ 0.5
    short : 윗꼬리 / 본체 ≥ 0.5
    """
    o, h, l, c = kline["open"], kline["high"], kline["low"], kline["close"]
    body = abs(c - o)
    if body == 0:
        return False
    if direction == "long":
        return (min(o, c) - l) / body >= 0.5
    return (h - max(o, c)) / body >= 0.5


def passes_btc_ct_gate(symbol: str, direction: str, extras: list[str],
                       confirm_bar: dict) -> tuple[bool, int]:
    """BTC 역추세 시그널 게이트 — (통과여부, CT 시그널 개수).

    BTC 가 아니면 항상 통과 (다른 종목 영향 없음).
    BTC 면:
      - long  : CT 시그널 ≥1 (없으면 RR 0.44 참사)
      - short : 게이트 없음 (Short/CT=0 도 RR 1.08 살아남음)
    """
    ct = _count_countertrend_signals(extras)
    if has_wick50(confirm_bar, direction):
        ct += 1
    if not BTC_CT_GATE_ENABLED:
        return True, ct
    if symbol != "BTCUSDT":
        return True, ct
    if direction == "long":
        return ct >= 1, ct
    return True, ct  # Short 은 게이트 없음


def is_btc_diver_premium(symbol: str, extras: list[str]) -> bool:
    """BTC + DIVER 단독으로도 PREMIUM (RR 7.64 Win 75% N=8)."""
    if symbol != "BTCUSDT":
        return False
    return any("다이버전스" in x for x in extras)

# [MID 2 패치] [1] 사전 알림 재발사 가드 — "한 추세에 1회만".
# 18봉 연속 음봉 같은 추세에서 매 봉마다 [1] 알림이 반복 발사되는 걸 방지.
# 알림 발사 직후 armed 플래그 ON, RSI 가 임계 ± RSI_RECOVERY_BAND 회복 시 해제.
RSI_RECOVERY_BAND = 5

# 백필 한계 — REST 한 번에 가져올 최대 봉 수
BACKFILL_LIMIT = 200

TIMEZONE_KST = timezone(timedelta(hours=9))

# 텔레그램 (ICT_SGBOT = t.me/ICT_SGBOT)
# 기존 rsi_alert_bot.py + ICT-grid-analyst 와 같은 채팅으로 보낸다.
# 우선순위: 환경변수 > ICT .env > ICT bot/config.py 의 모듈 상수
def _load_ict_telegram_creds() -> tuple[Optional[str], Optional[str]]:
    """ICT-grid-analyst 의 토큰/chat_id 자동 로드.

    ICT .env 는 chat_id 만 갖고 토큰은 ICT/bot/config.py 의 하드코딩
    fallback 에 있음 (`TELEGRAM_BOT_TOKEN = os.environ.get(..., "<token>")`).
    따라서 .env 만 읽으면 토큰이 누락돼 dry-run 으로 빠짐. 두 소스를
    모두 시도해 채워준다.

    importlib 로 명시 path 에서 ICT config.py 를 로드하므로, 같은 이름의
    다른 config.py 모듈과 sys.path 충돌은 없다.
    """
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None

    # 1) ICT .env
    ict_env = Path(r"C:\sum  you\system_upgrade\ict-grid-analyst\bot\.env")
    if ict_env.exists():
        for line in ict_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "TELEGRAM_BOT_TOKEN" and v:
                bot_token = v
            elif k == "TELEGRAM_CHAT_ID" and v:
                chat_id = v

    # 2) ICT bot/config.py — 한 쪽이라도 비어 있으면 fallback
    if not bot_token or not chat_id:
        ict_cfg = Path(r"C:\sum  you\system_upgrade\ict-grid-analyst\bot\config.py")
        if ict_cfg.exists():
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "ict_config_for_krtky", str(ict_cfg)
                )
                if spec is not None and spec.loader is not None:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if not bot_token:
                        bot_token = getattr(mod, "TELEGRAM_BOT_TOKEN", None) or None
                    if not chat_id:
                        chat_id = getattr(mod, "TELEGRAM_CHAT_ID", None) or None
            except Exception as e:
                print(f"[krtky] ICT config.py 로드 실패 (무시): {e}",
                      file=sys.stderr)
    return bot_token, chat_id


# 우선순위 0 (가장 강함): _krtky_secrets.py 파일이 있으면 그걸 무조건 사용.
# env 가 어떤 이유로 (매니저 detached context 등) 전달 안 돼도 라우팅 안정.
# ICT 봇은 이 파일을 안 읽으므로 완전 격리.
_secret_token: Optional[str] = None
_secret_chats: list[str] = []
try:
    import _krtky_secrets  # type: ignore
    _secret_token = getattr(_krtky_secrets, "TELEGRAM_BOT_TOKEN", None) or None
    _secret_chats = list(getattr(_krtky_secrets, "TELEGRAM_CHAT_IDS", []) or [])
except ImportError:
    pass
except Exception as e:
    print(f"[krtky] _krtky_secrets 로드 실패 (무시): {e}", file=sys.stderr)

# 우선순위 1: 크트키 전용 환경변수 (ICT 봇과 라우팅 분리용)
_krtky_token = os.environ.get("KRTKY_TG_TOKEN", "").strip()
_krtky_chats = [c.strip() for c in os.environ.get("KRTKY_TG_CHAT_IDS", "").split(",") if c.strip()]

# 우선순위 2: 일반 TELEGRAM_* env 변수
_env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_env_chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]

# 우선순위 3: ICT .env / config.py
_ict_token, _ict_chat = _load_ict_telegram_creds()

# 최종 — _krtky_secrets > KRTKY_TG_* > TELEGRAM_* > ICT
TG_TOKEN = _secret_token or _krtky_token or _env_token or (_ict_token or "")
if _secret_chats:
    TG_CHAT_IDS = _secret_chats
elif _krtky_chats:
    TG_CHAT_IDS = _krtky_chats
elif _env_chats:
    TG_CHAT_IDS = _env_chats
elif _ict_chat:
    TG_CHAT_IDS = [_ict_chat]
else:
    TG_CHAT_IDS = []

# 메시지 prefix — 한 봇이 두 알림기를 운영하므로 출처 구분
KRTKY_LABEL = os.environ.get("KRTKY_BOT_LABEL", "ICT_SGBOT · 크트키")
RSI_LABEL = os.environ.get("RSI_BOT_LABEL", "ICT_SGBOT · RSI")

# 통합 RSI 흐름 토글 — 기존 별도 rsi_alert_bot.py 가 같은 채널로 단순 RSI
# 알림을 보내고 있다면, 크트키 봇 안의 RSI 평가기는 끄는 게 맞다 (중복 방지).
# KRTKY_INTEGRATE_RSI=0 으로 OFF. 기본은 ON.
INTEGRATE_RSI = os.environ.get("KRTKY_INTEGRATE_RSI", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)

LOG_PATH = os.environ.get("KRTKY_LOG_PATH", "krtky_alert_bot.log")


# ─────────────────────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("krtky")


# ─────────────────────────────────────────────────────────────
# Bitget REST — 워밍업 백필
# ─────────────────────────────────────────────────────────────
# Bitget granularity 표기: "15m", "1H", "4H", "1D" ...
# WS 채널은 candle15m / candle4H / candle1D 이지만 REST는 별도 표기
_BITGET_GRANULARITY = {
    "15m": "15m",
    "4h": "4H",
    "1d": "1D",
}
_INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "4h":  4 * 60 * 60 * 1000,
    "1d":  24 * 60 * 60 * 1000,
}


def backfill_klines(symbol: str, interval_short: str, limit: int = BACKFILL_LIMIT) -> list[dict]:
    """Bitget v2 REST candles 엔드포인트로 과거 봉 시리즈 가져오기.

    WS 가 실시간 푸시만 주므로, RSI/ATR/다이버전스 계산을 위한 과거 데이터를
    한 번 미리 채워 둔다. WS 가 연결되면 그 이후로는 콜백으로만 갱신한다.
    """
    granularity = _BITGET_GRANULARITY.get(interval_short)
    if granularity is None:
        raise ValueError(f"backfill 지원 안 함: {interval_short}")
    url = f"{BITGET_REST_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol":       symbol,
        "productType":  "USDT-FUTURES",
        "granularity":  granularity,
        "limit":        str(limit),
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != "00000":
        raise RuntimeError(f"Bitget REST 에러: {body}")
    rows = body.get("data") or []
    # Bitget REST data: [ts, open, high, low, close, baseVol, quoteVol, usdtVol]
    # 정렬은 응답에 따라 다를 수 있어 명시적으로 ts 오름차순 정렬한다
    rows = sorted(rows, key=lambda r: int(r[0]))
    out = []
    interval_ms = _INTERVAL_MS[interval_short]
    for row in rows:
        ot = int(row[0])
        out.append({
            "open_time":  ot,
            "open":       float(row[1]),
            "high":       float(row[2]),
            "low":        float(row[3]),
            "close":      float(row[4]),
            "volume":     float(row[5]),
            "close_time": ot + interval_ms - 1,
        })
    return out


def fetch_top_volume_symbols(top_n: int = TOP_VOLUME_N) -> list[str]:
    """Bitget v2 tickers API 로 24h USDT 거래대금 상위 N개 USDT-FUTURES 심볼.

    응답 필드 'usdtVolume' (24h 거래대금) 기준 내림차순 정렬.
    'USDT' 로 끝나는 심볼만 (다른 페어 제외).
    """
    url = f"{BITGET_REST_BASE}/api/v2/mix/market/tickers"
    r = requests.get(url, params={"productType": "USDT-FUTURES"}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != "00000":
        raise RuntimeError(f"Bitget tickers 에러: {body}")
    rows = body.get("data") or []
    ranked: list[tuple[str, float]] = []
    for row in rows:
        sym = str(row.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        try:
            vol_usdt = float(row.get("usdtVolume") or row.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        ranked.append((sym, vol_usdt))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in ranked[:top_n]]


def compute_universe(top_n: int = TOP_VOLUME_N) -> list[str]:
    """거래량 상위 top_n + EXTRA_SYMBOLS (고정) 합쳐 중복 제거.

    EXTRA 는 top_n 에서 빠져도 항상 포함. 순서: top 먼저 → EXTRA 보강.
    실패 시 DEFAULT_SYMBOLS + EXTRA_SYMBOLS 로 fallback.
    """
    try:
        top = fetch_top_volume_symbols(top_n)
    except Exception as e:
        log.error("compute_universe: top 거래량 fetch 실패 (%s) — DEFAULT fallback", e)
        top = list(DEFAULT_SYMBOLS)
    seen: set[str] = set()
    universe: list[str] = []
    for sym in top + EXTRA_SYMBOLS:
        if sym not in seen:
            universe.append(sym)
            seen.add(sym)
    return universe


def set_dynamic_volume_min(symbol: str, klines_15m: list[dict]) -> None:
    """워밍업 직후 24h 평균 거래량 × DYNAMIC_VOLUME_MULTIPLE 로 임계 등록.

    기존 하드코딩 4종목 (BTC/ETH/SOL/XRP) 은 덮어쓰지 않음 — 운영 검증된 값 유지.
    EXTRA 7개 + top20 신규 종목만 동적 계산.
    """
    if symbol in {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"}:
        return  # 하드코딩 유지
    if symbol in VOLUME_ABSOLUTE_MIN:
        return  # 이미 등록됨 (덮어쓰기 X)
    if not klines_15m or len(klines_15m) < 10:
        log.warning("%s 동적 거래량 임계 등록 실패: 봉 데이터 부족 (%d)",
                    symbol, len(klines_15m) if klines_15m else 0)
        return
    recent = klines_15m[-DYNAMIC_VOLUME_BACKFILL_BARS:]
    avg_vol = sum(k["volume"] for k in recent) / len(recent)
    threshold = avg_vol * DYNAMIC_VOLUME_MULTIPLE
    VOLUME_ABSOLUTE_MIN[symbol] = threshold
    log.info("%s 동적 거래량 임계 등록: 24h avg=%.2f × %.2f = %.2f",
             symbol, avg_vol, DYNAMIC_VOLUME_MULTIPLE, threshold)


# ─────────────────────────────────────────────────────────────
# 지표 계산
# ─────────────────────────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    """Wilder RSI."""
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


def calc_atr(klines: list[dict], period: int = 14) -> float:
    trs = []
    for i in range(1, len(klines)):
        prev_close = klines[i - 1]["close"]
        high = klines[i]["high"]
        low = klines[i]["low"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    if len(trs) < period:
        return sum(trs) / len(trs)
    return sum(trs[-period:]) / period


# ─────────────────────────────────────────────────────────────
# 캔들 분석
# ─────────────────────────────────────────────────────────────
@dataclass
class CandleAnalysis:
    is_bullish: bool
    is_bearish: bool
    body: float
    upper_wick: float
    lower_wick: float
    range_: float
    body_to_range: float
    body_to_atr: float
    is_long_body: bool          # PPT 원칙 ②: 장대
    is_full_no_wick: bool       # 슬라이드 36: 진입 거부
    upper_wick_50: bool         # 슬라이드 18: 컨펌 생략 후보 (숏)
    lower_wick_50: bool         # 슬라이드 18: 컨펌 생략 후보 (롱)


def analyze_candle(k: dict, atr: float) -> CandleAnalysis:
    o, h, l, c = k["open"], k["high"], k["low"], k["close"]
    body = abs(c - o)
    range_ = max(h - l, 1e-12)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_to_range = body / range_
    body_to_atr = body / max(atr, 1e-12)
    return CandleAnalysis(
        is_bullish=c > o,
        is_bearish=c < o,
        body=body,
        upper_wick=upper_wick,
        lower_wick=lower_wick,
        range_=range_,
        body_to_range=body_to_range,
        body_to_atr=body_to_atr,
        is_long_body=body_to_range >= BODY_TO_RANGE_MIN and body_to_atr >= BODY_TO_ATR_MIN,
        is_full_no_wick=((upper_wick + lower_wick) / range_) < NO_WICK_THRESHOLD,
        upper_wick_50=(upper_wick / range_) >= 0.5,
        lower_wick_50=(lower_wick / range_) >= 0.5,
    )


def is_volume_significant(symbol: str, klines: list[dict]) -> tuple[bool, str]:
    """PPT 슬라이드 21·30: 절대치 OR 직전 평균 대비 상대치."""
    if len(klines) < VOLUME_SMA_PERIOD + 1:
        return False, "데이터 부족"
    last_vol = klines[-1]["volume"]
    sma_volumes = [k["volume"] for k in klines[-(VOLUME_SMA_PERIOD + 1):-1]]
    vol_sma = sum(sma_volumes) / len(sma_volumes)

    abs_min = VOLUME_ABSOLUTE_MIN.get(symbol, 0.0)
    abs_ok = last_vol >= abs_min
    rel_ok = vol_sma > 0 and last_vol >= vol_sma * VOLUME_SMA_MULTIPLE
    multiple = (last_vol / vol_sma) if vol_sma > 0 else 0.0

    if abs_ok and rel_ok:
        return True, f"vol {last_vol:,.0f} (절대 ≥{abs_min:,.0f} ✓, {multiple:.1f}× SMA{VOLUME_SMA_PERIOD} ✓)"
    if abs_ok:
        return True, f"vol {last_vol:,.0f} (절대 ≥{abs_min:,.0f} ✓, {multiple:.1f}× SMA{VOLUME_SMA_PERIOD})"
    if rel_ok:
        return True, f"vol {last_vol:,.0f} ({multiple:.1f}× SMA{VOLUME_SMA_PERIOD} ✓)"
    return False, f"vol {last_vol:,.0f} (절대 {abs_min:,.0f} 미달, {multiple:.1f}× SMA)"


def _ema(values: list[float], period: int) -> list[float]:
    """단순 EMA — krbonacci.calc_ema 와 동일 알고리즘."""
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def detect_trendline_retest(klines: list[dict], direction: str = "long",
                            lookback: int = TRENDLINE_RETEST_LOOKBACK,
                            tolerance: float = TRENDLINE_TOLERANCE_PCT) -> bool:
    """추세선 리테스트 감지 — EMA20 기준 (2026-05-17).

    슬라이드 40 SR Flip 리테스트 + KRIS_SCALPING_RULEBOOK '롱 확인' 5단계:
      HTF 지지 도착 → 과매도/급락 반응 → 큰 양봉 돌파 → 눌림에서 몸통 지지 → 다음 캔들 회복

    여기선 단순화:
      Long  : EMA20 기울기 하향 + 마지막 봉 low 가 EMA20 ±0.3% 안에 터치 + close 가 EMA20 위
      Short : EMA20 기울기 상향 + 마지막 봉 high 가 EMA20 ±0.3% 안에 터치 + close 가 EMA20 아래
    """
    if len(klines) < lookback:
        return False
    closes = [k["close"] for k in klines[-lookback:]]
    ema = _ema(closes, period=20)
    if len(ema) < 5:
        return False
    last = klines[-1]
    ema_last = ema[-1]
    # 기울기 — 최근 5봉 EMA 변화
    slope = (ema[-1] - ema[-5]) / max(ema[-5], 1e-12)
    band = ema_last * tolerance
    if direction == "long":
        # 하락 추세선 리테스트 → 양봉 반등 (BTC 변동성 고려, 기울기 임계 완화)
        return (slope < -0.0003           # EMA 하향 (0.03% 이상)
                and last["low"] <= ema_last + band
                and last["high"] >= ema_last - band
                and last["close"] > ema_last)
    else:
        return (slope > 0.0003            # EMA 상향
                and last["high"] >= ema_last - band
                and last["low"] <= ema_last + band
                and last["close"] < ema_last)


def detect_double_bottom(klines: list[dict], direction: str = "long",
                         lookback: int = DOUBLE_BOTTOM_LOOKBACK,
                         tolerance: float = DOUBLE_BOTTOM_TOLERANCE) -> bool:
    """더블바텀(쌍바닥) / 더블탑(쌍천장) 감지 — 슬라이드 23 (2026-05-17).

    Long  : 직전 lookback 봉 안에 두 저점이 ±tolerance 안 + 그 사이 의미 있는 반등 봉
    Short : 직전 lookback 봉 안에 두 고점이 ±tolerance 안 + 그 사이 의미 있는 눌림 봉

    단순화: pivot 알고리즘 대신 swing low/high 두 개를 lookback 윈도우 안에서 찾고
    그 사이에 N 봉 이상 거리 + 가운데 반대 swing 이 있는지 본다.
    """
    if len(klines) < lookback:
        return False
    window = klines[-lookback:]
    if direction == "long":
        # 두 swing low 찾기 (좌우 2봉보다 낮은 봉)
        swings = []
        for i in range(2, len(window) - 2):
            lo = window[i]["low"]
            if (lo <= window[i-1]["low"] and lo <= window[i-2]["low"]
                    and lo <= window[i+1]["low"] and lo <= window[i+2]["low"]):
                swings.append((i, lo))
        if len(swings) < 2:
            return False
        # 마지막 두 swing low 비교
        i1, lo1 = swings[-2]
        i2, lo2 = swings[-1]
        if (i2 - i1) < 4:  # 너무 가까우면 노이즈
            return False
        rel_diff = abs(lo1 - lo2) / max(lo1, 1e-12)
        if rel_diff > tolerance:
            return False
        # 가운데 반등이 있어야 (mid_high > 두 저점 평균 × 1.005)
        mid_high = max(k["high"] for k in window[i1:i2+1])
        avg_lo = (lo1 + lo2) / 2
        return mid_high > avg_lo * 1.005
    else:
        swings = []
        for i in range(2, len(window) - 2):
            hi = window[i]["high"]
            if (hi >= window[i-1]["high"] and hi >= window[i-2]["high"]
                    and hi >= window[i+1]["high"] and hi >= window[i+2]["high"]):
                swings.append((i, hi))
        if len(swings) < 2:
            return False
        i1, hi1 = swings[-2]
        i2, hi2 = swings[-1]
        if (i2 - i1) < 4:
            return False
        rel_diff = abs(hi1 - hi2) / max(hi1, 1e-12)
        if rel_diff > tolerance:
            return False
        mid_low = min(k["low"] for k in window[i1:i2+1])
        avg_hi = (hi1 + hi2) / 2
        return mid_low < avg_hi * 0.995


def _is_full_bar_in_downtrend(klines: list[dict], last_kline: dict,
                              lookback: int = 6) -> bool:
    """풀봉 SKIP 정교화 (슬라이드 36 원문 의도) — 2026-05-17 추가.

    슬라이드 36 원문: "지지구간 2강으로 깨버림 / 풀음봉 = 매수할 생각 없음"
    → 풀봉 자체보다 "이미 무너진 매물대에서 추가 이탈 중인 풀봉"이 핵심.

    판정:
      ① 직전 lookback 봉 중 음봉이 과반 (3/6 이상) — 하락 추세 흐름
      ② 풀봉이 직전 N봉의 최저 close 보다 낮음 — 저점 갱신 중
      양쪽 모두 충족 시 True (= 진입 거부)

    그렇지 않으면 (예: 저점 반등 직후 풀양봉) False → 통과시킴.
    """
    if len(klines) < lookback + 1:
        return False
    tail = klines[-(lookback + 1):]  # last 포함
    # ① 음봉 과반
    bearish_count = sum(1 for k in tail[:-1] if k["close"] < k["open"])
    if bearish_count < lookback // 2 + 1:
        return False  # 음봉 과반 아님 → 추세 반전 자리 가능
    # ② 저점 갱신 중 (last close 가 직전 N봉 최저 close 이하)
    prev_min_close = min(k["close"] for k in tail[:-1])
    is_breaking_low = last_kline["close"] <= prev_min_close
    return is_breaking_low


def has_absorption_streak(klines: list[dict], n: int = ABSORB_STREAK_LENGTH) -> bool:
    """슬라이드 15 '짧아진 캔들들': 직전 N봉이 거래량↑ + 몸통↓.

    ⚠️ 상수 미사용 (백테스트 가설 P-13/P-14 / H-09 / H-10 검증 후 정정):
       선언된 ABSORB_VOLUME_RATIO=1.2 / ABSORB_BODY_RATIO=0.6 가 본문에 반영되지
       않음 (본문은 하드코딩 1.1 + 단순 < 비교 = effective 1.0). 두 가지 해석 중
       어느 쪽이 PPT 본인 의도와 일치하는지 백테스트로 결정 후 정정 예정:
         A) 본문 그대로 + 상수를 1.1 / 1.0 으로 정정 (운영 동작 유지)
         B) 본문을 상수 의도(1.2 / 0.6) 로 변경 (더 빡빡한 흡수 필터)
       자세한 내용: ../../크리스비_전략_정리/백테스트_가설/00_INDEX.md §결정 보류
    """
    if len(klines) < n + 1:
        return False
    tail = klines[-(n + 1):]
    for i in range(1, n + 1):
        prev_body = abs(tail[i - 1]["close"] - tail[i - 1]["open"])
        curr_body = abs(tail[i]["close"] - tail[i]["open"])
        if prev_body == 0:
            return False
        vol_up = tail[i]["volume"] > tail[i - 1]["volume"] * 1.1
        body_down = curr_body < prev_body
        if not (vol_up and body_down):
            return False
    return True


def find_swing_lows(klines: list[dict], left: int = SWING_PIVOT_LR,
                    right: int = SWING_PIVOT_LR) -> list[int]:
    """좌 left봉 + 우 right봉 모두 보다 낮은 swing low 인덱스 리스트.

    klines[i]["low"] 가
      - 좌측 left봉의 low 들과 비교해 <= (같은 값이 좌측에 있으면 그쪽이 swing)
      - 우측 right봉의 low 들과 비교해 strictly < (좌측 swing 우선)
    이면 swing low 로 인정.

    슬라이드 26·35 에서 본인이 RSI 패널에 노란 추세선으로 연결한 두 변곡점이
    이런 swing 정의에 해당한다.
    """
    out: list[int] = []
    n = len(klines)
    for i in range(left, n - right):
        lo = klines[i]["low"]
        if not all(klines[j]["low"] >= lo for j in range(i - left, i)):
            continue
        if not all(klines[j]["low"] > lo for j in range(i + 1, i + 1 + right)):
            continue
        out.append(i)
    return out


def find_swing_highs(klines: list[dict], left: int = SWING_PIVOT_LR,
                     right: int = SWING_PIVOT_LR) -> list[int]:
    """좌 left봉 + 우 right봉 모두 보다 높은 swing high 인덱스 리스트.

    find_swing_lows 의 거울 대칭. 슬라이드 26 (이중 고점 + RSI HL→LH)
    과 35 (0.86 → 0.892 두 swing high) 가 이 정의에 해당.
    """
    out: list[int] = []
    n = len(klines)
    for i in range(left, n - right):
        hi = klines[i]["high"]
        if not all(klines[j]["high"] <= hi for j in range(i - left, i)):
            continue
        if not all(klines[j]["high"] < hi for j in range(i + 1, i + 1 + right)):
            continue
        out.append(i)
    return out


def detect_bullish_divergence(klines: list[dict]) -> bool:
    """노란 다이버 라인 (PPT 슬라이드 12·20·22·26 상승 다이버).

    최근 DIVERGENCE_SWING_WINDOW(50) 봉 안에서 swing low 두 개를 비교:
      - 가격 Lower Low (price[swing2] < price[swing1])
      - RSI Higher Low (rsi[swing2] > rsi[swing1])
    이면 True. RSI 는 각 swing 발생 시점 기준으로
    calc_rsi(closes[:i+1]) 로 계산해 시점 누수가 없다.

    슬라이드 20·22 의 1차(101,300/100,800) → 2차(99,200) 진입은
    두 저점이 30~100 봉 떨어진 사례 — 기존 lookback=5 로는 못 잡았다.
    """
    if len(klines) < RSI_PERIOD + SWING_PIVOT_LR * 2 + 5:
        return False
    window = (klines[-DIVERGENCE_SWING_WINDOW:]
              if len(klines) > DIVERGENCE_SWING_WINDOW else klines)
    swings = find_swing_lows(window)
    if len(swings) < 2:
        return False
    i_prev_local, i_curr_local = swings[-2], swings[-1]
    price_prev = window[i_prev_local]["low"]
    price_curr = window[i_curr_local]["low"]
    if price_curr >= price_prev:
        return False
    # window 로컬 인덱스 → 전체 klines 절대 인덱스
    offset = len(klines) - len(window)
    closes_all = [k["close"] for k in klines]
    rsi_prev = calc_rsi(closes_all[:offset + i_prev_local + 1])
    rsi_curr = calc_rsi(closes_all[:offset + i_curr_local + 1])
    return rsi_curr > rsi_prev


def detect_bearish_divergence(klines: list[dict]) -> bool:
    """노란 다이버 라인 (PPT 슬라이드 26·35 하락 다이버) — 거울 대칭.

    최근 DIVERGENCE_SWING_WINDOW 봉 안에서 swing high 두 개:
      - 가격 Higher High
      - RSI  Lower  High
    """
    if len(klines) < RSI_PERIOD + SWING_PIVOT_LR * 2 + 5:
        return False
    window = (klines[-DIVERGENCE_SWING_WINDOW:]
              if len(klines) > DIVERGENCE_SWING_WINDOW else klines)
    swings = find_swing_highs(window)
    if len(swings) < 2:
        return False
    i_prev_local, i_curr_local = swings[-2], swings[-1]
    price_prev = window[i_prev_local]["high"]
    price_curr = window[i_curr_local]["high"]
    if price_curr <= price_prev:
        return False
    offset = len(klines) - len(window)
    closes_all = [k["close"] for k in klines]
    rsi_prev = calc_rsi(closes_all[:offset + i_prev_local + 1])
    rsi_curr = calc_rsi(closes_all[:offset + i_curr_local + 1])
    return rsi_curr < rsi_prev


def detect_level_break(klines: list[dict], lookback: int = LEVEL_BREAK_LOOKBACK) -> tuple[bool, bool]:
    """슬라이드 25 주황색(봉 갱신) — 직전 N봉 신고가/신저가 갱신.

    Returns: (신고가_갱신, 신저가_갱신)
    """
    if len(klines) < lookback + 1:
        return False, False
    last = klines[-1]
    prev_window = klines[-(lookback + 1):-1]
    prev_high = max(k["high"] for k in prev_window)
    prev_low = min(k["low"] for k in prev_window)
    return last["high"] > prev_high, last["low"] < prev_low


@dataclass
class IsolatedSR:
    """고립 반전 캔들에서 추출한 단기 SR 자리 (출처: Kris 텔레그램 03:14-15)."""
    price: float           # 저항 또는 지지 가격
    kind: str              # "resistance" (저항) | "support" (지지)
    created_ts_ms: int     # 등록 시각 (봉 close_time 기준)
    interval: str          # "15m" | "4h"
    bars_since: int = 0    # 누적 봉 수 (만료 카운터)


def detect_isolated_reversal(
    klines: list[dict],
    lookback: int = ISOLATED_LOOKBACK,
    streak_ratio: float = ISOLATED_STREAK_RATIO,
) -> Optional[IsolatedSR]:
    """직전 lookback 봉이 한 방향 추세, 마지막 봉만 반대 방향이면 고립 반전.

    출처: Kris 텔레그램 03:14-15 — "연속된 음봉/양봉 중간의 양봉/음봉" 이
    이후 단기 SR 자리로 작용한다는 본인 발언.

    하락 추세 (음봉 비율 ≥ streak_ratio) 속 고립 양봉 → 그 양봉 high 가
    단기 저항. 상승 추세 속 고립 음봉 → 그 음봉 low 가 단기 지지.
    """
    if len(klines) < lookback + 1:
        return None
    window = klines[-(lookback + 1):]
    context, target = window[:-1], window[-1]   # context = 직전 lookback 봉
    n = len(context)
    n_bear = sum(1 for k in context if k["close"] < k["open"])
    n_bull = sum(1 for k in context if k["close"] > k["open"])
    target_bull = target["close"] > target["open"]
    target_bear = target["close"] < target["open"]

    # 하락 추세 속 고립 양봉 → 저항
    if target_bull and n_bear / n >= streak_ratio:
        return IsolatedSR(
            price=target["high"],
            kind="resistance",
            created_ts_ms=target["close_time"],
            interval="",  # 호출자가 채움
        )
    # 상승 추세 속 고립 음봉 → 지지
    if target_bear and n_bull / n >= streak_ratio:
        return IsolatedSR(
            price=target["low"],
            kind="support",
            created_ts_ms=target["close_time"],
            interval="",
        )
    return None


def detect_sr_flip(klines: list[dict], levels: deque, tol: float = SR_FLIP_TOLERANCE) -> Optional[float]:
    """슬라이드 40·43·44 SR Flip 휴리스틱.

    levels 에는 직전 장대봉의 몸통 끝 가격이 기록되어 있다. 현재 봉의 wick 가
    이 가격 ±tol 범위를 터치(low ≤ lvl ≤ high)하고 컨펌됐다면 리테스트 인정.
    Returns: 매칭된 레벨(가격) 또는 None.
    """
    if not levels or not klines:
        return None
    last = klines[-1]
    lo, hi = last["low"], last["high"]
    for lvl in levels:
        band = lvl * tol
        if lo <= lvl + band and hi >= lvl - band:
            return lvl
    return None


# ─────────────────────────────────────────────────────────────
# 상태 머신 (슬라이드 14의 [1]→[2] 흐름) + 시리즈 캐시
# ─────────────────────────────────────────────────────────────
@dataclass
class SymbolState:
    pre_long_bar: Optional[int] = None     # close_time(ms) of pre-alert
    pre_short_bar: Optional[int] = None
    # [MID 2] 사전 알림 발사 후 RSI 회복 전까지 같은 방향 재발사 차단
    last_long_armed: bool = False
    last_short_armed: bool = False
    last_processed_bar: Optional[int] = None
    # SR Flip 후보 가격대 (슬라이드 40·43·44): 직전 장대봉의 몸통 끝
    sr_levels: deque = field(default_factory=lambda: deque(maxlen=SR_FLIP_LEVELS_MAX))


STATE: dict[str, SymbolState] = {s: SymbolState() for s in SYMBOLS}


def init_symbol_state(symbol: str) -> None:
    """universe 에 새로 추가된 종목의 STATE / RSI_STATE / 시리즈 캐시 초기화.

    데일리 갱신에서 신규 종목 진입 시 호출. 기존 종목은 no-op.
    """
    if symbol not in STATE:
        STATE[symbol] = SymbolState()
    if symbol not in RSI_STATE:
        RSI_STATE[symbol] = RSISymbolState()
    if symbol not in SERIES_15M:
        SERIES_15M[symbol] = deque(maxlen=SERIES_MAX_15M)
    if symbol not in SERIES_4H:
        SERIES_4H[symbol] = deque(maxlen=SERIES_MAX_4H)
    if symbol not in SERIES_1D:
        SERIES_1D[symbol] = deque(maxlen=SERIES_MAX_1D)

# 통합 RSI 알림기(원본 rsi_alert_bot.py 이식)용 별도 상태 머신
RSI_STATE: dict[str, RSISymbolState] = {s: RSISymbolState() for s in SYMBOLS}

# 시리즈 캐시 — 봉이 들어올 때마다 deque 에 push, RSI/ATR 등이 여기서 읽는다
SERIES_15M: dict[str, deque] = {s: deque(maxlen=SERIES_MAX_15M) for s in SYMBOLS}
SERIES_4H: dict[str, deque] = {s: deque(maxlen=SERIES_MAX_4H) for s in SYMBOLS}
SERIES_1D: dict[str, deque] = {s: deque(maxlen=SERIES_MAX_1D) for s in SYMBOLS}

# 고립 반전 SR 캐시 (출처: Kris 텔레그램 03:14-15) — 심볼별 리스트.
# 새 봉이 닫힐 때마다 bars_since 를 +1 하고, 6시간(또는 50봉) 지나면 만료.
ISOLATED_SR_CACHE: dict[str, list[IsolatedSR]] = defaultdict(list)


def _prune_isolated_sr(symbol: str, now_ms: int) -> None:
    """6시간 또는 50봉 초과한 SR 자리 삭제."""
    keep: list[IsolatedSR] = []
    for sr in ISOLATED_SR_CACHE[symbol]:
        age_sec = (now_ms - sr.created_ts_ms) / 1000.0
        if age_sec <= ISOLATED_SR_TTL_SEC and sr.bars_since <= ISOLATED_SR_MAX_BARS:
            keep.append(sr)
    ISOLATED_SR_CACHE[symbol] = keep


def _register_isolated_sr(symbol: str, sr: IsolatedSR, interval: str) -> None:
    """detect_isolated_reversal 결과를 캐시에 등록.

    같은 가격대(±0.3% 안) 중복 SR 은 갱신만 한다.
    """
    sr.interval = interval
    for existing in ISOLATED_SR_CACHE[symbol]:
        band = existing.price * ISOLATED_SR_TOLERANCE
        if abs(existing.price - sr.price) <= band and existing.kind == sr.kind:
            existing.created_ts_ms = sr.created_ts_ms
            existing.bars_since = 0
            existing.interval = interval
            return
    ISOLATED_SR_CACHE[symbol].append(sr)


def match_isolated_sr(
    symbol: str,
    last_kline: dict,
    direction: str,
) -> Optional[IsolatedSR]:
    """[1] 사전 알림 발사 봉의 wick 이 캐시된 SR 자리를 가로지르는지 검사.

    [HIGH 패치] 이전 버전은 close 가 ±0.3% 안인지로 검사했으나, 장대
    음봉/양봉의 close 는 SR 자리에 정확히 안 닿는 경우가 다수다.
    Kris 의 의도는 "가격이 그 자리를 닿았는가" 이므로, 봉의 low~high
    범위가 SR 가격을 가로지르는지로 판정한다.

    direction:
        "long"  → 매수 자리 후보. support(지지) 와 매칭이 의미 있음.
        "short" → 매도 자리 후보. resistance(저항) 와 매칭.
    """
    want = "support" if direction == "long" else "resistance"
    lo, hi = last_kline["low"], last_kline["high"]
    for sr in ISOLATED_SR_CACHE[symbol]:
        if sr.kind != want:
            continue
        band = sr.price * ISOLATED_SR_TOLERANCE
        if lo - band <= sr.price <= hi + band:
            return sr
    return None


def _tick_isolated_sr_bars(symbol: str) -> None:
    """봉이 한 개 더 닫힐 때마다 bars_since 카운터 증가."""
    for sr in ISOLATED_SR_CACHE[symbol]:
        sr.bars_since += 1

# 콜백 직렬화 — WS 콜백은 수신 스레드에서 호출되므로 평가 로직 동시 진입 방지
EVAL_LOCK = threading.Lock()


def to_kst(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=TIMEZONE_KST).strftime("%m-%d %H:%M")


# ─────────────────────────────────────────────────────────────
# 텔레그램
# ─────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_IDS:
        log.warning("Telegram 미설정 → dry-run\n%s", text)
        return
    for chat_id in TG_CHAT_IDS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            # 디버그: 모든 응답 로그 (200 OK 도 silent drop 진단용)
            if r.status_code == 200:
                log.info("Telegram OK · chat=%s · msg_id=%s",
                         chat_id, r.json().get("result", {}).get("message_id"))
            else:
                log.error("Telegram %s · chat=%s · %s",
                          r.status_code, chat_id, r.text[:300])
        except requests.RequestException as e:
            log.error("Telegram 전송 실패 · chat=%s · %s", chat_id, e)


# ─────────────────────────────────────────────────────────────
# 알림 메시지 빌더
# ─────────────────────────────────────────────────────────────
def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


# 모든 알림 메시지에 동봉되는 한 줄 면책 푸터 (ICT 봇 톤 통일)
DISCLAIMER_FOOTER = (
    "<i>※ 본 알림은 투자 권유·자문이 아닙니다. "
    "진입·청산·손절 판단은 본인 책임입니다.</i>"
)


# 자산군 이모지 — 한눈에 코인/주식/원자재 분간
# 모르는 종목은 yfinance 소스면 📈, 그 외 🪙 (기본 코인)
SYMBOL_KIND_EMOJI: dict[str, str] = {
    # 금속
    "XAUUSDT": "🥇", "XAUTUSDT": "🥇", "XAGUSDT": "🥈",
    # 원유
    "CLUSDT": "🛢️",
    # 토큰화 주식 (Bitget USDT-FUTURES + yfinance EXTRA)
    "EWYUSDT": "📈", "NVDAUSDT": "📈", "TSLAUSDT": "📈",
    "MSFTUSDT": "📈", "INTCUSDT": "📈", "ORCLUSDT": "📈",
    "CRCLUSDT": "📈", "SOXLUSDT": "📈", "CBRSUSDT": "📈",
    "SNDKUSDT": "📈", "BILLUSDT": "📈",
}


def symbol_kind(symbol: str) -> str:
    """종목 이모지 — 명시 매핑 → yfinance 폴링 종목 → 기본 코인."""
    if symbol in SYMBOL_KIND_EMOJI:
        return SYMBOL_KIND_EMOJI[symbol]
    try:
        if is_yfinance_symbol(symbol):
            return "📈"
    except Exception:
        pass
    return "🪙"


def build_pre_alert(direction: str, symbol: str, k: dict, c: CandleAnalysis,
                    rsi_15m: float, rsi_4h: float, vol_msg: str, atr: float,
                    break_high: bool, break_low: bool,
                    isolated_sr: Optional[IsolatedSR] = None) -> str:
    arrow = "↓" if direction == "long" else "↑"
    rsi_tag = "과매도" if direction == "long" else "과매수"
    body_color = "음봉" if c.is_bearish else "양봉"
    next_action = ("다음 양봉 컨펌 시 진입 신호 발송"
                   if direction == "long"
                   else "다음 음봉 컨펌 시 진입 신호 발송")

    boost = []
    if direction == "long" and c.lower_wick_50:
        boost.append("⚡ 아랫꼬리 50%↑ — 컨펌 생략 후보")
    if direction == "short" and c.upper_wick_50:
        boost.append("⚡ 윗꼬리 50%↑ — 컨펌 생략 후보")
    if direction == "long" and break_low:
        boost.append("🟧 직전 20봉 신저가 갱신")
    if direction == "short" and break_high:
        boost.append("🟧 직전 20봉 신고가 갱신")
    if isolated_sr is not None:
        kind_kr = "지지" if isolated_sr.kind == "support" else "저항"
        boost.append(
            f"📍 고립 반전 캔들 SR ({isolated_sr.interval} {kind_kr}) "
            f"@ {fmt_price(isolated_sr.price)}"
        )
    boost_str = ("\n" + "\n".join(boost)) if boost else ""

    # 손절폭 + 가격 둘 다 표시 (사용자가 가격으로 오해 X)
    entry_price = float(k["close"])
    sl_distance = atr * 1.5
    sl_price = entry_price + sl_distance if direction == "long" else entry_price - sl_distance
    # 사전 알림 단계의 sl_price 는 "사전 알림 봉 close 기준" — 진입 [2] 에서 컨펌봉 기준으로 다시 계산됨
    sl_dir = "↓" if direction == "long" else "↑"
    sl_line = f"🛑 손절폭 ±{fmt_price(sl_distance)} ({sl_dir} {fmt_price(entry_price - sl_distance if direction == 'long' else entry_price + sl_distance)}) · ATR×1.5"

    return (
        f"🔷 [{KRTKY_LABEL}] [1] 사전 알림 · <b>{direction.upper()}</b> · "
        f"{symbol_kind(symbol)} <b>{symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {to_kst(k['close_time'])} KST · 현재가 <b>{fmt_price(k['close'])}</b>\n\n"
        f"장대 {body_color} {arrow} (몸통 {c.body_to_range:.0%}, ATR {c.body_to_atr:.1f}×)\n"
        f"{vol_msg}\n"
        f"RSI 15m <b>{rsi_15m:.1f}</b> ({rsi_tag}) · 4H {rsi_4h:.1f}\n\n"
        f"🎯 {next_action}\n"
        f"{sl_line}{boost_str}\n"
        f"\n{DISCLAIMER_FOOTER}"
    )


def build_entry_signal(level: int, direction: str, symbol: str, k: dict,
                       c: CandleAnalysis, rsi_15m: float, rsi_4h: float,
                       extras: list[str], atr: float,
                       trigger_low: Optional[float],
                       trigger_high: Optional[float],
                       klines_15m: Optional[list[dict]] = None,
                       klines_1d: Optional[list[dict]] = None) -> str:
    """[2]/[3]/[4] 진입 신호 메시지.

    크트키 자리 (사용자 직접 명시):
      롱: 양봉 컨펌 → 다음봉 밑꼬리 진입 → trigger 는 컨펌봉 low~open 사이
      숏: 음봉 컨펌 → 다음봉 윗꼬리 진입 → trigger 는 컨펌봉 high~open 사이

    klines_15m / klines_1d 가 주어지면 크보나치 익절 후보 가격 + RR 동봉.
    """
    icon = {1: "🟢", 2: "⭐", 3: "🔥"}[level]
    label = {
        1: "[2] 진입 신호",
        2: "[3] ⭐ 진입 신호 (컨플루언스)",
        3: "[4] 🔥 강력 진입 신호 (MTF 컨플루언스)",
    }[level]
    if direction == "long":
        next_action = "🎯 <b>크트키 자리</b> — 다음 15m봉 밑꼬리에서 매수"
    else:
        next_action = "🎯 <b>크트키 자리</b> — 다음 15m봉 윗꼬리에서 매도"

    trig_line = ""
    # 2026-05-17 결함 #5 발견 후 — fill 모델 분석:
    # zone_low limit ≈ fill 률 85% + 좋은 가격 (백테스트 검증)
    # zone_mid limit ≈ fill 률 15% (자리 놓침)
    # zone_high limit = fill 률 100% but 비싸게 사서 EV ↓
    # → 사용자에게 zone_low 부근 limit 권장
    if trigger_low is not None and trigger_high is not None:
        if direction == "long":
            trig_line = (f"\n진입 트리거 (밑꼬리 매수존): "
                         f"{fmt_price(trigger_low)} ~ {fmt_price(trigger_high)}"
                         f"\n  ⭐ limit 권장: {fmt_price(trigger_low)} 부근 "
                         f"(좋은 가격 + fill 률 ~85%)")
        else:
            trig_line = (f"\n진입 트리거 (윗꼬리 매도존): "
                         f"{fmt_price(trigger_low)} ~ {fmt_price(trigger_high)}"
                         f"\n  ⭐ limit 권장: {fmt_price(trigger_high)} 부근 "
                         f"(좋은 가격 + fill 률 ~85%)")
    extras_str = "\n• " + "\n• ".join(extras) if extras else ""

    # 익절 후보 + RR — sl 은 entry ∓ ATR×1.5
    entry_price = float(k["close"])
    sl_distance = atr * 1.5
    sl_price = entry_price - sl_distance if direction == "long" else entry_price + sl_distance
    tp_block = ""
    if klines_15m and sl_distance > 0:
        targets = compute_take_profit_targets(
            direction=direction,
            entry_price=entry_price,
            klines_15m=klines_15m,
            klines_1d=klines_1d,
        )
        emoji_seq = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        tp_lines: list[str] = []
        for i, (tp_label, tp_price, _src) in enumerate(targets[:5]):
            if direction == "long":
                rr = (tp_price - entry_price) / sl_distance
            else:
                rr = (entry_price - tp_price) / sl_distance
            if rr < 0.5:
                continue
            tp_lines.append(
                f"  {emoji_seq[i]} {fmt_price(tp_price)} — {tp_label} (RR {rr:.1f})"
            )
        if tp_lines:
            tp_block = "\n✨ <b>익절 후보 (RR 비율)</b>\n" + "\n".join(tp_lines)

    # 손절폭 + 가격 둘 다 표시
    sl_line = (
        f"🛑 손절폭 ±{fmt_price(sl_distance)} "
        f"({'↓' if direction == 'long' else '↑'} {fmt_price(sl_price)}) · ATR×1.5"
    )

    return (
        f"{icon} [{KRTKY_LABEL}] {label} · <b>{direction.upper()}</b> · "
        f"{symbol_kind(symbol)} <b>{symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {to_kst(k['close_time'])} KST · 현재가 <b>{fmt_price(k['close'])}</b>\n\n"
        f"컨펌봉 (몸통 {c.body_to_range:.0%}, ATR {c.body_to_atr:.1f}×)\n"
        f"RSI 15m {rsi_15m:.1f} · 4H {rsi_4h:.1f}\n"
        f"{sl_line}{trig_line}{tp_block}\n\n"
        f"{next_action}{extras_str}\n"
        f"\n{DISCLAIMER_FOOTER}"
    )


# ─────────────────────────────────────────────────────────────
# 봉별 평가 — 15분봉 마감 시 호출
# ─────────────────────────────────────────────────────────────
def evaluate_symbol_15m(symbol: str) -> None:
    closed_15m = list(SERIES_15M[symbol])
    closed_4h = list(SERIES_4H[symbol])

    if len(closed_15m) < 30 or len(closed_4h) < RSI_PERIOD + 2:
        log.debug("%s 시리즈 워밍업 중 (15m=%d, 4h=%d)",
                  symbol, len(closed_15m), len(closed_4h))
        return

    last = closed_15m[-1]
    state = STATE[symbol]

    # 같은 봉 중복 처리 방지
    if state.last_processed_bar == last["close_time"]:
        return

    closed_1d = list(SERIES_1D[symbol])
    atr = calc_atr(closed_15m, period=14)
    rsi_15m = calc_rsi([k["close"] for k in closed_15m])
    rsi_4h = calc_rsi([k["close"] for k in closed_4h])
    candle = analyze_candle(last, atr)
    vol_ok, vol_msg = is_volume_significant(symbol, closed_15m)
    break_high, break_low = detect_level_break(closed_15m)
    sr_level = detect_sr_flip(closed_15m, state.sr_levels)

    # ─── 풀봉 SKIP — 옵션 A baseline 유지 + 옵션 D 예외 통과 (2026-05-17)
    # 슬라이드 36 원문: "꼬리 없는 풀음봉 = 매수할 생각 없음 / 지지구간 2강으로
    # 깨버림 / 숏 대응이 유효". 단순 무차별 SKIP 이 RR 1.73 baseline 을 만들어줌.
    # 단, 추세선 리테스트 + 더블바텀 둘 다 동반된 풀양봉은 예외 통과 (옵션 D).
    # 예: 2026-05-16 23:44 BTC 78,215 반등 양봉 (저점 77,801 에서 반등 + 추세선
    # 리테스트 + 더블바텀 패턴) 같은 자리 회수.
    if candle.is_full_no_wick:
        # 옵션 D 예외: 풀양봉만 적용 (Long 자리 회수), 풀음봉은 무차별 SKIP 유지
        trendline_hit = detect_trendline_retest(closed_15m, "long")
        double_bottom_hit = detect_double_bottom(closed_15m, "long")
        passes_d_long = (candle.is_bullish
                         and (trendline_hit or double_bottom_hit))
        if not passes_d_long:
            log.info("%s SKIP 풀봉 (슬라이드 36) %s",
                     symbol, to_kst(last["close_time"]))
            state.last_processed_bar = last["close_time"]
            _record_sr_level(state, last, candle)
            return
        # 옵션 D — 추세선 리테스트 + 더블바텀 둘 다 동반된 풀양봉
        log.info("%s 풀양봉 통과 (옵션D: 추세선+더블바텀) %s",
                 symbol, to_kst(last["close_time"]))

    # ═══ LONG ═══
    # RSI 게이트:
    #   - hard 임계 (≤30): 기본 통과 (모든 종목)
    #   - soft 임계 (≤40): BTC Long 만, 추세선 리테스트 + 더블바텀 둘 다 동반 시 통과
    #     슬라이드 19(43.6)/20(36.2)/27(40.2) Kris 본인 사례 회수 + 78K 자리 케이스
    has_ct_signal_long = (
        detect_bullish_divergence(closed_15m)
        or has_absorption_streak(closed_15m)
        or sr_level is not None
        or rsi_4h <= HTF_RSI_OVERSOLD
    )
    # 옵션 D: 추세선 리테스트 또는 더블바텀 (사람 눈 패턴 명확한 자리)
    has_eye_pattern_long = (detect_trendline_retest(closed_15m, "long")
                            or detect_double_bottom(closed_15m, "long"))
    rsi_long_ok = rsi_15m <= RSI_OVERSOLD
    if (not rsi_long_ok
            and RSI_SOFT_GATE_ENABLED
            and symbol == "BTCUSDT"
            and rsi_15m <= RSI_OVERSOLD_SOFT
            and has_eye_pattern_long   # ★ 추세선 + 더블바텀 둘 다 필수
            and has_ct_signal_long):    # ★ + CT 시그널까지 필수 (3중 컨플루언스)
        rsi_long_ok = True
    long_pre_ok = (candle.is_bearish
                   and candle.is_long_body
                   and vol_ok
                   and rsi_long_ok)

    if (long_pre_ok
            and state.pre_long_bar != last["close_time"]
            and not state.last_long_armed):
        state.pre_long_bar = last["close_time"]
        state.last_long_armed = True   # [MID 2] RSI 회복 전까지 재발사 차단
        iso_sr = match_isolated_sr(symbol, last, "long")
        msg = build_pre_alert("long", symbol, last, candle, rsi_15m, rsi_4h,
                              vol_msg, atr, break_high, break_low,
                              isolated_sr=iso_sr)
        log.info("[1] LONG pre-alert %s @ %s", symbol, to_kst(last["close_time"]))
        send_telegram(msg)

    if state.pre_long_bar and state.pre_long_bar != last["close_time"]:
        elapsed_bars = (last["close_time"] - state.pre_long_bar) // (15 * 60 * 1000)
        if candle.is_bullish and candle.is_long_body and 1 <= elapsed_bars <= PRE_ALERT_TIMEOUT_BARS:
            level = 1
            extras = []
            if has_absorption_streak(closed_15m):
                extras.append("흡수 누적 (슬라이드 15)")
                level = max(level, 2)
            if detect_bullish_divergence(closed_15m):
                extras.append("🟡 상승 RSI 다이버전스 (슬라이드 20·22)")
                level = max(level, 2)
            if sr_level is not None:
                extras.append(f"SR Flip 리테스트 @ {fmt_price(sr_level)} (슬라이드 40·43·44)")
                level = max(level, 2)
            krb_hit = detect_krbonacci_confluence(closed_15m, "long")
            if krb_hit is not None:
                extras.append(hit_to_label(krb_hit))
                level = max(level, 2)
            # 일봉 4분할 컨플루언스 (Kris 텔레그램 03:11)
            if closed_1d:
                q_hit = detect_quartile_confluence(closed_1d, last, "long")
                if q_hit is not None:
                    extras.append(quartile_hit_to_label(q_hit))
                    level = max(level, 2)
            if rsi_4h <= HTF_RSI_OVERSOLD:
                extras.append(f"4H RSI {rsi_4h:.1f} 과매도 컨플루언스")
                level = max(level, 3)
            # 옵션 D: 추세선 리테스트 / 더블바텀 (사람 눈 패턴)
            if detect_trendline_retest(closed_15m, "long"):
                extras.append("📉 하락 추세선 리테스트 (EMA20 터치)")
                level = max(level, 2)
            if detect_double_bottom(closed_15m, "long"):
                extras.append("⚓ 더블바텀 (슬라이드 23)")
                level = max(level, 2)
            # ★ Segment 필터 (백테스트 1년 4종목 기반)
            if ("long", symbol) in SKIP_SEGMENTS:
                log.info("%s LONG entry SKIP — segment 손실 자리 (Long/%s)",
                         symbol, symbol)
                state.pre_long_bar = None
            elif not passes_btc_time_gate(symbol, "long", last["close_time"]):
                # R8: BTC Long 은 KST 06-12 만 통과 (RR 1.37 Win 80%)
                log.info("%s LONG entry SKIP — BTC 시간 게이트 (KST %dh, Long 허용=06-18, S2 완화)",
                         symbol, _kst_hour(last["close_time"]))
                state.pre_long_bar = None
            elif (lambda gp: not gp[0])(passes_btc_ct_gate(symbol, "long", extras, last)):
                # BTC Long 역추세 시그널 게이트 — CT≥1 필수 (없으면 RR 0.44 참사)
                log.info("%s LONG entry SKIP — BTC 역추세 시그널 부족 (CT=0, 추세 추종 자리)",
                         symbol)
                state.pre_long_bar = None
            else:
                if has_wick50(last, "long"):
                    extras.append("🪝 밑꼬리 50%+ 컨펌 (슬라이드 41-44)")
                    level = max(level, 2)
                if ("long", symbol) in PREMIUM_SEGMENTS:
                    extras.append(f"🌟 PREMIUM segment (Long/{symbol}: 백테스트 RR 1.79~1.89, Win 72~74%)")
                    level = max(level, 3)
                if is_btc_diver_premium(symbol, extras):
                    extras.append("💎 BTC DIVER 단독 PREMIUM (백테스트 RR 7.64, Win 75%)")
                    level = max(level, 3)
                if is_btc_diver_quart_premium(symbol, extras):
                    extras.append("💎💎 BTC R5 다이버×4분할 컨플루언스 (백테스트 RR 13.23, Win 86%)")
                    level = max(level, 3)
                # 매수존 = [컨펌봉 low, 컨펌봉 close]  ★ M2 (백테스트 1년 4종목 1위)
                trigger_low = last["low"]
                trigger_high = last["close"]
                msg = build_entry_signal(level, "long", symbol, last, candle,
                                         rsi_15m, rsi_4h, extras, atr,
                                         trigger_low, trigger_high,
                                         klines_15m=closed_15m,
                                         klines_1d=closed_1d)
                log.info("[%d] LONG entry %s extras=%s", level + 1, symbol, extras)
                send_telegram(msg)
                state.pre_long_bar = None
        elif elapsed_bars > PRE_ALERT_TIMEOUT_BARS:
            log.info("%s LONG pre-alert timeout (%d bars)", symbol, elapsed_bars)
            state.pre_long_bar = None

    # ═══ SHORT (대칭) ═══ — BTC 만 soft 게이트, 추세선 + 더블탑 동반 시
    has_ct_signal_short = (
        detect_bearish_divergence(closed_15m)
        or has_absorption_streak(closed_15m)
        or sr_level is not None
        or rsi_4h >= HTF_RSI_OVERBOUGHT
    )
    # Short 는 AND 유지 — OR 로 풀면 RR 1.73 → 1.07 폭락 (백테스트 검증)
    has_eye_pattern_short = (detect_trendline_retest(closed_15m, "short")
                             and detect_double_bottom(closed_15m, "short"))
    rsi_short_ok = rsi_15m >= RSI_OVERBOUGHT
    if (not rsi_short_ok
            and RSI_SOFT_GATE_ENABLED
            and symbol == "BTCUSDT"
            and rsi_15m >= RSI_OVERBOUGHT_SOFT
            and has_eye_pattern_short
            and has_ct_signal_short):
        rsi_short_ok = True
    short_pre_ok = (candle.is_bullish
                    and candle.is_long_body
                    and vol_ok
                    and rsi_short_ok)

    if (short_pre_ok
            and state.pre_short_bar != last["close_time"]
            and not state.last_short_armed):
        state.pre_short_bar = last["close_time"]
        state.last_short_armed = True   # [MID 2]
        iso_sr = match_isolated_sr(symbol, last, "short")
        msg = build_pre_alert("short", symbol, last, candle, rsi_15m, rsi_4h,
                              vol_msg, atr, break_high, break_low,
                              isolated_sr=iso_sr)
        log.info("[1] SHORT pre-alert %s @ %s", symbol, to_kst(last["close_time"]))
        send_telegram(msg)

    if state.pre_short_bar and state.pre_short_bar != last["close_time"]:
        elapsed_bars = (last["close_time"] - state.pre_short_bar) // (15 * 60 * 1000)
        if candle.is_bearish and candle.is_long_body and 1 <= elapsed_bars <= PRE_ALERT_TIMEOUT_BARS:
            level = 1
            extras = []
            if has_absorption_streak(closed_15m):
                extras.append("흡수 누적 (슬라이드 15)")
                level = max(level, 2)
            if detect_bearish_divergence(closed_15m):
                extras.append("🟡 하락 RSI 다이버전스 (슬라이드 26)")
                level = max(level, 2)
            if sr_level is not None:
                extras.append(f"SR Flip 리테스트 @ {fmt_price(sr_level)} (슬라이드 40·43·44)")
                level = max(level, 2)
            krb_hit = detect_krbonacci_confluence(closed_15m, "short")
            if krb_hit is not None:
                extras.append(hit_to_label(krb_hit))
                level = max(level, 2)
            if closed_1d:
                q_hit = detect_quartile_confluence(closed_1d, last, "short")
                if q_hit is not None:
                    extras.append(quartile_hit_to_label(q_hit))
                    level = max(level, 2)
            if rsi_4h >= HTF_RSI_OVERBOUGHT:
                extras.append(f"4H RSI {rsi_4h:.1f} 과매수 컨플루언스")
                level = max(level, 3)
            # 옵션 D: 상승 추세선 리테스트 / 더블탑
            if detect_trendline_retest(closed_15m, "short"):
                extras.append("📈 상승 추세선 리테스트 (EMA20 터치)")
                level = max(level, 2)
            if detect_double_bottom(closed_15m, "short"):
                extras.append("⚓ 더블탑 (슬라이드 23)")
                level = max(level, 2)
            # ★ Segment 필터
            if ("short", symbol) in SKIP_SEGMENTS:
                log.info("%s SHORT entry SKIP — segment 손실 자리 (Short/%s)",
                         symbol, symbol)
                state.pre_short_bar = None
            elif not passes_btc_time_gate(symbol, "short", last["close_time"]):
                # R8: BTC Short 은 KST 06-18 만 통과 (RR 1.73 Win 73%)
                log.info("%s SHORT entry SKIP — BTC 시간 게이트 (KST %dh, Short 허용=00-22, S2 완화)",
                         symbol, _kst_hour(last["close_time"]))
                state.pre_short_bar = None
            else:
                # BTC Short 은 CT 게이트 없음 (Short/CT=0 도 RR 1.08 살아남음)
                # 단 CT 시그널 있으면 PREMIUM 강조
                if has_wick50(last, "short"):
                    extras.append("🪝 윗꼬리 50%+ 컨펌 (슬라이드 41-44)")
                    level = max(level, 2)
                if ("short", symbol) in PREMIUM_SEGMENTS:
                    extras.append(f"🌟 PREMIUM segment (Short/{symbol})")
                    level = max(level, 3)
                if is_btc_diver_premium(symbol, extras):
                    extras.append("💎 BTC DIVER 단독 PREMIUM (백테스트 RR 7.64, Win 75%)")
                    level = max(level, 3)
                if is_btc_diver_quart_premium(symbol, extras):
                    extras.append("💎💎 BTC R5 다이버×4분할 컨플루언스 (백테스트 RR 13.23, Win 86%)")
                    level = max(level, 3)
                # 매도존 = [컨펌봉 close, 컨펌봉 high]  ★ M2 (대칭)
                trigger_low = last["close"]
                trigger_high = last["high"]
                msg = build_entry_signal(level, "short", symbol, last, candle,
                                         rsi_15m, rsi_4h, extras, atr,
                                         trigger_low, trigger_high,
                                         klines_15m=closed_15m,
                                         klines_1d=closed_1d)
                log.info("[%d] SHORT entry %s extras=%s", level + 1, symbol, extras)
                send_telegram(msg)
                state.pre_short_bar = None
        elif elapsed_bars > PRE_ALERT_TIMEOUT_BARS:
            log.info("%s SHORT pre-alert timeout (%d bars)", symbol, elapsed_bars)
            state.pre_short_bar = None

    # 장대봉 가격대를 SR 후보로 기록 (다음 봉부터 리테스트 감지 가능)
    _record_sr_level(state, last, candle)

    # [MID 2] RSI 가 임계 ± RSI_RECOVERY_BAND 회복하면 재발사 가드 해제
    if rsi_15m > RSI_OVERSOLD + RSI_RECOVERY_BAND:
        state.last_long_armed = False
    if rsi_15m < RSI_OVERBOUGHT - RSI_RECOVERY_BAND:
        state.last_short_armed = False

    state.last_processed_bar = last["close_time"]


def _record_sr_level(state: SymbolState, k: dict, c: CandleAnalysis) -> None:
    """장대봉의 몸통 끝 가격을 SR Flip 후보로 기록 (슬라이드 40·43·44)."""
    if not c.is_long_body:
        return
    # 양봉 → 종가가 직전 저항을 뚫은 자리 → 향후 지지로 작용
    # 음봉 → 종가가 직전 지지를 뚫은 자리 → 향후 저항으로 작용
    state.sr_levels.append(k["close"])


# ─────────────────────────────────────────────────────────────
# WS 콜백 — 봉 마감 시 호출
# ─────────────────────────────────────────────────────────────
# 평가 stale 가드 — interval 별 봉 길이 (snapshot 과거봉 차단용)
_INTERVAL_MS_BY_LABEL = {"15m": 900_000, "4h": 14_400_000, "1d": 86_400_000}


def on_bar_closed(symbol: str, interval: str, kline: dict) -> None:
    """BitgetCandleStream 콜백.

    같은 봉을 두 평가기에 모두 흘려보낸다:
      1) 크트키 (PPT 4원칙 + 보조 룰)
      2) RSI 단순 알림 (원본 rsi_alert_bot.py 로직)
    한 봇에서 두 알림 흐름을 동시 운영 → t.me/ICT_SGBOT 채팅에 prefix 로 구분.

    [HIGH 핫픽스] WS snapshot 으로 들어온 과거 봉(어제 / 그제 봉)이 평가기에
    흘러 들어가 가짜 PRE_ALERT 를 발사하는 걸 차단:
    close_time 이 현재 시각 - 봉길이 × 1.5 보다 과거면 시리즈만 갱신,
    평가기 호출 SKIP. (실시간 봉이 들어오면 자연스럽게 평가 재개됨)
    """
    if symbol not in STATE:
        return

    # 과거봉 stale 판정 — 시리즈 갱신은 항상, 평가만 skip
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    bar_ms = _INTERVAL_MS_BY_LABEL.get(interval)
    is_stale = bar_ms is not None and (now_ms - kline["close_time"]) > bar_ms * 1.5

    with EVAL_LOCK:
        if interval == "15m":
            series = SERIES_15M[symbol]
        elif interval == "4h":
            series = SERIES_4H[symbol]
        elif interval == "1d":
            series = SERIES_1D[symbol]
        else:
            log.debug("미사용 interval 무시: %s", interval)
            return

        # 동일 open_time 이면 갱신, 새 봉이면 추가
        is_new_bar = False
        if series and series[-1]["open_time"] == kline["open_time"]:
            series[-1] = kline
        elif series and kline["open_time"] < series[-1]["open_time"]:
            # 과거 봉 — 시리즈 중간 삽입은 안 하고 무시 (이미 처리된 봉)
            return
        else:
            series.append(kline)
            is_new_bar = True

        # 새 봉이 들어왔으면 — 만료 SR 정리 + 고립 반전 등록 (15m/4h)
        if is_new_bar and interval in ("15m", "4h"):
            _tick_isolated_sr_bars(symbol)
            _prune_isolated_sr(symbol, kline["close_time"])
            iso = detect_isolated_reversal(list(series))
            if iso is not None:
                _register_isolated_sr(symbol, iso, interval)
                log.info("[isolated SR] %s %s %s @ %s (Kris 03:14-15)",
                         symbol, interval, iso.kind, fmt_price(iso.price))

        if interval == "15m":
            if is_stale:
                log.debug("%s 15m 평가 SKIP (snapshot stale, close_time=%d)",
                          symbol, kline["close_time"])
                return
            # 1) 크트키 (PPT 4원칙)
            try:
                evaluate_symbol_15m(symbol)
            except Exception as e:
                log.exception("%s 크트키 15m 평가 예외: %s", symbol, e)
            # 2) 단순 RSI 알림 (원본 rsi_alert_bot 이식) — INTEGRATE_RSI 토글
            if INTEGRATE_RSI:
                try:
                    evaluate_rsi_15m(
                        symbol=symbol,
                        klines_15m=list(SERIES_15M[symbol]),
                        klines_4h=list(SERIES_4H[symbol]),
                        state=RSI_STATE[symbol],
                        send=send_telegram,
                        label=RSI_LABEL,
                        kind_emoji=symbol_kind(symbol),
                    )
                except Exception as e:
                    log.exception("%s RSI 15m 평가 예외: %s", symbol, e)


# ─────────────────────────────────────────────────────────────
# 시작 시 백필
# ─────────────────────────────────────────────────────────────
def warmup_symbol(symbol: str) -> bool:
    """단일 종목 워밍업. universe 확장 + 데일리 갱신에서 재사용.

    EXTRA_SYMBOLS (주식·원자재 토큰화 페어) 는 yfinance 실제 시장 데이터로
    채우고, 그 외는 Bitget REST 사용. Returns True 면 성공.
    """
    init_symbol_state(symbol)
    use_yf = is_yfinance_symbol(symbol)
    try:
        if use_yf:
            klines_15m = fetch_yf_klines(symbol, "15m", limit=BACKFILL_LIMIT)
            klines_4h = fetch_yf_klines(symbol, "4h", limit=BACKFILL_LIMIT)
            klines_1d = fetch_yf_klines(symbol, "1d", limit=SERIES_MAX_1D)
        else:
            klines_15m = backfill_klines(symbol, "15m", limit=BACKFILL_LIMIT)
            klines_4h = backfill_klines(symbol, "4h", limit=BACKFILL_LIMIT)
            klines_1d = backfill_klines(symbol, "1d", limit=SERIES_MAX_1D)
    except Exception as e:
        log.error("%s 워밍업 실패 (source=%s): %s",
                  symbol, "yfinance" if use_yf else "bitget", e)
        return False
    # 마지막 봉은 미완성일 수 있으니 보수적으로 제외
    if klines_15m:
        klines_15m = klines_15m[:-1]
    if klines_4h:
        klines_4h = klines_4h[:-1]
    if klines_1d:
        klines_1d = klines_1d[:-1]
    SERIES_15M[symbol].extend(klines_15m[-SERIES_MAX_15M:])
    SERIES_4H[symbol].extend(klines_4h[-SERIES_MAX_4H:])
    SERIES_1D[symbol].extend(klines_1d[-SERIES_MAX_1D:])

    # 동적 거래량 임계 등록 (BTC/ETH/SOL/XRP 외 신규 종목)
    set_dynamic_volume_min(symbol, list(SERIES_15M[symbol]))

    # 워밍업 직후 고립 반전 SR 자리도 미리 채워 둔다 (15m + 4h)
    for series, interval in (
        (list(SERIES_15M[symbol]), "15m"),
        (list(SERIES_4H[symbol]), "4h"),
    ):
        for i in range(ISOLATED_LOOKBACK + 1, len(series) + 1):
            iso = detect_isolated_reversal(series[:i])
            if iso is not None:
                _register_isolated_sr(symbol, iso, interval)

    src = "📈 yfinance" if use_yf else "bitget"
    log.info("%s 워밍업 완료 [%s]: 15m=%d, 4h=%d, 1d=%d, SR=%d",
             symbol, src, len(SERIES_15M[symbol]), len(SERIES_4H[symbol]),
             len(SERIES_1D[symbol]), len(ISOLATED_SR_CACHE[symbol]))
    return True


# ─────────────────────────────────────────────────────────────
# yfinance 폴링 — EXTRA 종목 (주식·원자재) 실제 시장 데이터
# ─────────────────────────────────────────────────────────────
YFINANCE_POLL_INTERVAL_SEC = 15 * 60   # 15m 봉 한 번 도는 주기


def yfinance_poll_loop() -> None:
    """매 15분 정각 + 30초 직후 EXTRA 종목 15m 봉 fetch.

    fetch 결과의 마지막 봉을 on_bar_closed 콜백에 흘려 보내 시리즈 갱신 +
    평가 트리거. yfinance 는 15분 지연되므로 30초 마진 둠.
    시장 시간 외엔 새 봉이 없으면 갱신 없음 (자연스러운 침묵).
    """
    while True:
        # 다음 15분 경계까지 sleep + 30초 마진
        now = datetime.now(timezone.utc)
        # 다음 15분 정각 (UTC 분이 0/15/30/45)
        next_min = ((now.minute // 15) + 1) * 15
        if next_min >= 60:
            next_tick = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            next_tick = now.replace(minute=next_min, second=0, microsecond=0)
        next_tick = next_tick + timedelta(seconds=30)
        sleep_sec = max(5.0, (next_tick - now).total_seconds())
        log.debug("[yfinance_poll] 다음 폴링: %s UTC (sleep %.0fs)",
                  next_tick.strftime("%H:%M:%S"), sleep_sec)
        time.sleep(sleep_sec)

        for symbol in list(YF_SYMBOLS_ACTIVE):
            try:
                klines = fetch_yf_klines(symbol, "15m", limit=3)
                if not klines:
                    continue
                # 마지막 두 봉 모두 흘려 보냄 — 봉 닫힘 판정에 직전 봉이 필요
                for k in klines[-2:]:
                    on_bar_closed(symbol, "15m", k)
            except Exception as e:
                log.warning("[yfinance_poll] %s 실패: %s", symbol, e)


# 폴링 대상 (main 에서 채움 — universe ∩ YF_SYMBOL_MAP)
YF_SYMBOLS_ACTIVE: set[str] = set()


def warmup_series() -> None:
    """시작 시 Bitget REST 로 universe 전체 종목의 15m / 4h / 1d 시리즈 채움."""
    for symbol in SYMBOLS:
        warmup_symbol(symbol)


# ─────────────────────────────────────────────────────────────
# 데일리 universe 갱신 — 매일 KST 09:00
# ─────────────────────────────────────────────────────────────
def update_universe(stream: "BitgetCandleStream") -> tuple[list[str], list[str]]:
    """compute_universe() 결과를 적용. 추가/제거 종목 반환.

    EXTRA_SYMBOLS 는 항상 보호 (top20 에서 빠져도 제거하지 않음).
    추가된 종목: 워밍업 + WS 구독 추가.
    제거된 종목 (EXTRA 제외): SERIES 캐시 비움 + WS 구독 해제.
    """
    global SYMBOLS
    new_uni = compute_universe()
    old_set = set(SYMBOLS)
    new_set = set(new_uni)
    extra_set = set(EXTRA_SYMBOLS)

    added = sorted(new_set - old_set)
    # EXTRA 는 removed 에서 무조건 제외 (보호)
    removed = sorted((old_set - new_set) - extra_set)

    # 1) 추가 — 워밍업 + WS 구독 추가 (EXTRA 는 yfinance 폴링에 등록만)
    for symbol in added:
        if warmup_symbol(symbol):
            if is_yfinance_symbol(symbol):
                YF_SYMBOLS_ACTIVE.add(symbol)
                log.info("%s yfinance 폴링 등록", symbol)
            else:
                stream.add_subscription(symbol)

    # 2) 제거 — WS 구독 해제 / yfinance 폴링 해제
    for symbol in removed:
        if is_yfinance_symbol(symbol):
            YF_SYMBOLS_ACTIVE.discard(symbol)
        else:
            stream.remove_subscription(symbol)
        for cache in (SERIES_15M, SERIES_4H, SERIES_1D, STATE, RSI_STATE):
            cache.pop(symbol, None)
        ISOLATED_SR_CACHE.pop(symbol, None)
        VOLUME_ABSOLUTE_MIN.pop(symbol, None)
        log.info("%s 캐시 정리 완료 (universe 에서 제거)", symbol)

    SYMBOLS = list(new_uni)
    return added, removed


def daily_refresh_loop(stream: "BitgetCandleStream") -> None:
    """매일 KST 09:00 깨어나 update_universe 호출.

    데몬 스레드로 실행. 변경 분 있으면 Telegram 알림 발사.
    """
    while True:
        now = datetime.now(TIMEZONE_KST)
        next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        sleep_sec = (next_run - now).total_seconds()
        log.info("[daily_refresh] 다음 갱신 예정: %s KST (sleep %.0fs)",
                 next_run.strftime("%Y-%m-%d %H:%M"), sleep_sec)
        time.sleep(sleep_sec)

        try:
            added, removed = update_universe(stream)
            log.info("[daily_refresh] 적용 완료: +%d 종목, -%d 종목 (현재 %d 종목)",
                     len(added), len(removed), len(SYMBOLS))
            if added or removed:
                send_universe_change_alert(added, removed)
        except Exception as e:
            log.exception("[daily_refresh] 실패: %s", e)


def send_universe_change_alert(added: list[str], removed: list[str]) -> None:
    """universe 변경분 Telegram 알림."""
    now_kst = datetime.now(TIMEZONE_KST).strftime("%m-%d %H:%M")
    added_str = ", ".join(s.replace("USDT", "") for s in added) if added else "-"
    removed_str = ", ".join(s.replace("USDT", "") for s in removed) if removed else "-"
    msg = (
        f"<b>🔄 크트키 universe 데일리 갱신</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_kst} KST\n\n"
        f"➕ <b>추가</b> ({len(added)}): {added_str}\n"
        f"➖ <b>제거</b> ({len(removed)}): {removed_str}\n\n"
        f"📊 현재 감시 {len(SYMBOLS)}종목 (top {TOP_VOLUME_N} + EXTRA {len(EXTRA_SYMBOLS)})"
    )
    send_telegram(msg)


# ─────────────────────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────────────────────
def send_startup_ping() -> None:
    """봇 시작 시 ICT_SGBOT 채팅에 헬스 체크 메시지 한 줄 발송.

    원본 rsi_alert_bot.py 와 같은 패턴. 봇 켜자마자 채팅에 도착하면
    토큰/chat_id 라우팅이 살아있다는 것이 즉시 확인된다.
    Telegram 미설정 (dry-run) 이면 콘솔에만 찍힌다.
    """
    now_kst = datetime.now(TIMEZONE_KST).strftime("%m-%d %H:%M")
    rsi_section = (
        f"\n<b>알림 흐름 2 — [{RSI_LABEL}]</b>\n"
        f"• 15m RSI ≤ 30 → 다음 양봉 컨펌 → 진입 신호\n"
        f"• 4H RSI≤30 + 5+ 연속 음봉 → 🔥 강력"
        if INTEGRATE_RSI
        else f"\n<i>RSI 단순 흐름은 OFF (기존 rsi_alert_bot.py 와 중복 방지)</i>"
    )
    # 종목 풀 구성 요약
    extra_short = "/".join(s.replace("USDT", "") for s in EXTRA_SYMBOLS)
    pool_line = (
        f"👀 종목 풀: 거래량 상위 {TOP_VOLUME_N} + 고정 {len(EXTRA_SYMBOLS)} "
        f"({extra_short}) — 현재 {len(SYMBOLS)}종목"
    )
    msg = (
        f"<b>🚀 크트키 알림봇 시작</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📡 데이터: Bitget USDT-M Futures WS\n"
        f"{pool_line}\n"
        f"🔄 데일리 갱신: 매일 KST 09:00 — 상위 {TOP_VOLUME_N} 재선정 (고정 {len(EXTRA_SYMBOLS)} 유지)\n"
        f"⏰ 시작 시각: {now_kst} KST\n\n"
        f"<b>알림 흐름 — [{KRTKY_LABEL}]</b>\n"
        f"• 15m 장대 + 거래량 + RSI≤{RSI_OVERSOLD}/≥{RSI_OVERBOUGHT}\n"
        f"  → 양봉/음봉 컨펌 → 다음봉 꼬리 진입\n"
        f"• 컨플루언스: 다이버·SR Flip·크보나치·일봉 4분할·고립 SR\n"
        f"{rsi_section}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>고지: 본 봇은 투자 권유·자문이 아닙니다. PPT 매매 룰 시그널 "
        f"모니터링 용도이며, 진입·청산·손절을 포함한 모든 투자 판단과 "
        f"손익 책임은 본인에게 있습니다.</i>"
    )
    send_telegram(msg)


def main() -> None:
    global SYMBOLS
    log.info("=" * 60)
    log.info("크트키 알림봇 시작 — Kris B PPT 충실 구현 · Bitget WS")
    log.info("크트키 자리: 거래량+RSI≤%d+양봉컨펌→밑꼬리(롱) / +RSI≥%d+음봉컨펌→윗꼬리(숏)",
             RSI_OVERSOLD, RSI_OVERBOUGHT)
    # universe 동적 계산 (거래량 상위 20 + 고정 7)
    SYMBOLS = compute_universe()
    log.info("종목 (%d개): %s", len(SYMBOLS), SYMBOLS)
    log.info("EXTRA 고정 %d개: %s", len(EXTRA_SYMBOLS), EXTRA_SYMBOLS)
    log.info("Timeout %d봉 · ATR×1.5 손절 가이드 (수동 운영 전제)",
             PRE_ALERT_TIMEOUT_BARS)
    log.info("거래량 임계 (4코인 하드코딩): %s", VOLUME_ABSOLUTE_MIN)
    # 토큰 / chat_id 진단 — 어떤 소스에서 잡혔는지 + 끝 4자리만 마스킹 출력
    if TG_TOKEN:
        tok_tail = TG_TOKEN[-4:] if len(TG_TOKEN) >= 4 else "??"
        chat_str = ", ".join(TG_CHAT_IDS) if TG_CHAT_IDS else "(없음)"
        log.info("Telegram: ON · token=…%s · chat_id=%s", tok_tail, chat_str)
    else:
        log.warning("Telegram: OFF (dry-run) · 토큰 없음 — ICT .env / config.py 확인 필요")
    log.info("알림 흐름: 크트키=ON · 통합 RSI=%s (KRTKY_INTEGRATE_RSI=%s)",
             "ON" if INTEGRATE_RSI else "OFF",
             os.environ.get("KRTKY_INTEGRATE_RSI", "(미설정→ON)"))
    log.info("=" * 60)

    log.info("Bitget REST 워밍업 중...")
    warmup_series()
    log.info("워밍업 완료.")

    # ICT_SGBOT 채팅 연동 헬스 체크 — 봇 켜자마자 한 줄 도착
    try:
        send_startup_ping()
        log.info("시작 핑 발송 완료.")
    except Exception as e:
        log.warning("시작 핑 발송 실패 (무시): %s", e)

    # EXTRA(yfinance) 분리 — Bitget WS 에는 코인 종목만 구독
    bitget_symbols = [s for s in SYMBOLS if not is_yfinance_symbol(s)]
    YF_SYMBOLS_ACTIVE.clear()
    YF_SYMBOLS_ACTIVE.update(s for s in SYMBOLS if is_yfinance_symbol(s))
    log.info("Bitget WS 구독: %d종목 · yfinance 폴링: %d종목 (%s)",
             len(bitget_symbols), len(YF_SYMBOLS_ACTIVE),
             sorted(YF_SYMBOLS_ACTIVE))

    log.info("WebSocket 시작.")

    stream = BitgetCandleStream(
        symbols=bitget_symbols,
        intervals=["candle15m", "candle4H", "candle1D"],
        on_bar_closed=on_bar_closed,
    )

    # yfinance 폴링 스레드 (EXTRA 종목 매 15분 fetch)
    if YF_SYMBOLS_ACTIVE:
        yf_thread = threading.Thread(
            target=yfinance_poll_loop,
            name="yfinance_poll",
            daemon=True,
        )
        yf_thread.start()
        log.info("yfinance 폴링 스레드 시작 (매 15분 EXTRA 종목 fetch)")

    # 데일리 갱신 — 매일 KST 09:00 universe 재선정
    refresh_thread = threading.Thread(
        target=daily_refresh_loop,
        args=(stream,),
        name="daily_refresh",
        daemon=True,
    )
    refresh_thread.start()
    log.info("데일리 갱신 스레드 시작 (매일 KST 09:00)")

    try:
        stream.start(block=True)
    except KeyboardInterrupt:
        log.info("Ctrl+C — 종료 중...")
        stream.stop()


if __name__ == "__main__":
    main()
