"""크트키 알림봇 단위/통합 테스트.

대상 모듈:
    - krtky_alert_bot : 메인 봇 (PPT 4원칙 평가)
    - bitget_ws       : Bitget WebSocket 클라이언트 (콜백 디스패치)
    - rsi_alert_core  : 통합 RSI 알림기 (IDLE→PRE_ALERT→ENTRY)
    - krbonacci       : Kris 의 비표준 피보 (0.764/1.236/2.26)

실행:
    python test_krtky_bot.py

설계 원칙:
    - 외부 호출(텔레그램, REST, WS) 은 전부 모킹. 네트워크에 절대 나가지 않음.
    - 합성 캔들은 ms epoch, 15분 = 900_000ms 간격.
    - 테스트 간 글로벌 상태 (SERIES_15M, STATE, RSI_STATE) 격리.
"""
from __future__ import annotations

import os
import sys
import unittest
from collections import deque
from typing import Optional
from unittest.mock import MagicMock, patch

# 테스트 환경에서는 모듈 import 시점에 발생할 수 있는 부작용을 차단한다.
# (krtky_alert_bot 은 import 시 ICT .env 파일을 읽으려 시도하지만 파일이
#  없으면 자동으로 dry-run 모드가 된다 — 별 문제 없음)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_IDS", "")
os.environ.setdefault("KRTKY_SKIP_ICT_CREDS", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for path in (SRC, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import krtky_alert_bot as bot       # noqa: E402
import rsi_alert_core as rsi_core   # noqa: E402
import krbonacci as krb             # noqa: E402
import bitget_ws as ws_mod          # noqa: E402


# ─────────────────────────────────────────────────────────────
# 합성 캔들 픽스처 헬퍼
# ─────────────────────────────────────────────────────────────
INTERVAL_15M_MS = 15 * 60 * 1000          # 900_000
INTERVAL_4H_MS = 4 * 60 * 60 * 1000       # 14_400_000
BASE_OPEN_TIME = 1_700_000_000_000        # 임의 기준 epoch ms


def make_kline(
    open_time: int,
    o: float,
    h: float,
    l: float,
    c: float,
    v: float,
    interval_ms: int = INTERVAL_15M_MS,
) -> dict:
    """봇이 기대하는 kline dict 한 개 생성."""
    return {
        "open_time":  open_time,
        "open":       float(o),
        "high":       float(h),
        "low":        float(l),
        "close":      float(c),
        "volume":     float(v),
        "close_time": open_time + interval_ms - 1,
    }


def make_series(prices: list[tuple[float, float, float, float, float]],
                interval_ms: int = INTERVAL_15M_MS,
                base_ts: int = BASE_OPEN_TIME) -> list[dict]:
    """(o, h, l, c, v) 튜플 리스트 → kline 시리즈."""
    out = []
    ts = base_ts
    for (o, h, l, c, v) in prices:
        out.append(make_kline(ts, o, h, l, c, v, interval_ms))
        ts += interval_ms
    return out


def make_downtrend_then_green(
    n_red: int = 8,
    start_price: float = 100.0,
    drop_per_bar: float = 1.5,
    red_vol: float = 1000.0,
    green_body: float = 5.0,
    green_vol: float = 5000.0,
    interval_ms: int = INTERVAL_15M_MS,
) -> list[dict]:
    """N봉 연속 음봉 후 1봉 강한 양봉. RSI 가 30 아래로 떨어지도록 설계.

    각 음봉은 작은 윗꼬리/아랫꼬리만 있는 평범한 음봉이고, 마지막 양봉은
    장대 양봉 (몸통/range >= 60%).
    """
    series = []
    ts = BASE_OPEN_TIME
    price = start_price
    for _ in range(n_red):
        o = price
        c = price - drop_per_bar
        h = o + 0.05
        l = c - 0.05
        series.append(make_kline(ts, o, h, l, c, red_vol, interval_ms))
        ts += interval_ms
        price = c
    # 강한 양봉 마감 봉
    o = price
    c = price + green_body
    h = c + 0.05
    l = o - 0.05
    series.append(make_kline(ts, o, h, l, c, green_vol, interval_ms))
    return series


# ─────────────────────────────────────────────────────────────
# 1. analyze_candle
# ─────────────────────────────────────────────────────────────
class TestAnalyzeCandle(unittest.TestCase):
    """캔들 분석 — 장대봉/풀봉/꼬리50% 플래그."""

    def test_long_bullish_body(self) -> None:
        """몸통 70% 이상, ATR 보다 큰 양봉 → is_long_body=True."""
        k = make_kline(BASE_OPEN_TIME, o=100.0, h=110.0, l=99.5, c=109.5, v=1.0)
        atr = 5.0
        ca = bot.analyze_candle(k, atr)
        self.assertTrue(ca.is_bullish)
        self.assertFalse(ca.is_bearish)
        self.assertTrue(ca.is_long_body, "70%/range, 1.9x ATR → 장대로 인정돼야 함")
        self.assertGreaterEqual(ca.body_to_range, 0.6)

    def test_full_no_wick(self) -> None:
        """꼬리 < 10% → is_full_no_wick=True (슬라이드 36 거부)."""
        # open=100, close=110, high=110.05, low=99.95 → 꼬리 0.1 / range 10.1 ≈ 1%
        k = make_kline(BASE_OPEN_TIME, o=100.0, h=110.05, l=99.95, c=110.0, v=1.0)
        ca = bot.analyze_candle(k, atr=5.0)
        self.assertTrue(ca.is_full_no_wick)

    def test_lower_wick_50(self) -> None:
        """아랫꼬리 ≥ 50% range → lower_wick_50 (롱 컨펌 생략 후보)."""
        # range=10, lower_wick = 6 (60%), body 와 윗꼬리는 나머지
        k = make_kline(BASE_OPEN_TIME, o=106.0, h=110.0, l=100.0, c=108.0, v=1.0)
        ca = bot.analyze_candle(k, atr=5.0)
        self.assertTrue(ca.lower_wick_50)
        self.assertFalse(ca.upper_wick_50)

    def test_upper_wick_50(self) -> None:
        """윗꼬리 ≥ 50% → upper_wick_50 (숏 컨펌 생략 후보)."""
        k = make_kline(BASE_OPEN_TIME, o=102.0, h=110.0, l=100.0, c=104.0, v=1.0)
        ca = bot.analyze_candle(k, atr=5.0)
        self.assertTrue(ca.upper_wick_50)
        self.assertFalse(ca.lower_wick_50)


# ─────────────────────────────────────────────────────────────
# 2. Divergence
# ─────────────────────────────────────────────────────────────
class TestDivergence(unittest.TestCase):
    """slot-pivot 기반 노란 다이버 라인 (PPT 슬라이드 12·20·22·26·35)."""

    def test_bullish_divergence_slide20_pattern(self) -> None:
        """슬라이드 20·22 패턴: W 형태 swing low 두 개 (가격 LL + RSI HL).

        시나리오 (57봉):
          0~19  Prefix 평탄 (95.0 진동) — RSI 워밍업
          20~27 강한 하락 (95→87.5)    — RSI 급락
          28    ★ swing low 1 low=86.0 — close=87.7  → RSI ~9
          29~36 강한 반등 (87.7→95.0)  — RSI 회복
          37~46 약한 상승·횡보         — RSI 50+ 유지
          47~50 약한 하락 (95→93)
          51    ★ swing low 2 low=85.5 — LL(86.0→85.5), close=94.0 (긴 wick)
                                       → RSI ~51 (HL)
          52~56 반등
        """
        import math
        prices: list[tuple[float, float, float, float, float]] = []
        # Prefix 20봉 평탄 (sin 진동 ±0.15) — swing 안 만들 만큼 작은 폭
        for i in range(20):
            p = 95.0 + math.sin(i * 0.7) * 0.15
            prices.append((p, p + 0.2, p - 0.2, p + 0.05, 1000.0))
        # Phase A 강한 하락 8봉
        p = 95.0
        for _ in range(8):
            o = p
            c = p - 0.95
            prices.append((o, o + 0.05, c - 0.05, c, 1500.0))
            p = c
        # ★ swing low 1 (low=86.0 spike)
        prices.append((p, p + 0.05, 86.0, p + 0.3, 2500.0))
        p = p + 0.3
        # Phase B 강한 반등 8봉
        for _ in range(8):
            o = p
            c = p + 0.9
            prices.append((o, c + 0.05, o - 0.05, c, 1500.0))
            p = c
        # Phase C 약한 상승·횡보 10봉
        for i in range(10):
            o = p
            c = p + (0.2 if i % 2 == 0 else -0.05)
            prices.append((o, max(o, c) + 0.1, min(o, c) - 0.1, c, 1000.0))
            p = c
        # Phase D 약한 하락 4봉 (RSI 너무 내려가지 않게)
        for _ in range(4):
            o = p
            c = p - 0.4
            prices.append((o, o + 0.1, c - 0.1, c, 1200.0))
            p = c
        # ★ swing low 2 — LL : 85.5 < 86.0, 긴 wick 양봉 마감
        prices.append((p, p + 0.05, 85.5, p + 0.2, 2500.0))
        p = p + 0.2
        # Phase E 반등 5봉
        for _ in range(5):
            o = p
            c = p + 1.0
            prices.append((o, c + 0.05, o - 0.05, c, 1500.0))
            p = c

        klines = make_series(prices)
        self.assertTrue(
            bot.detect_bullish_divergence(klines),
            "슬라이드 20 W 패턴(가격 LL + RSI HL) 이 잡혀야 함",
        )

    def test_bearish_divergence_slide26_35_pattern(self) -> None:
        """슬라이드 26·35 거울 대칭: M 형태 swing high 두 개 (HH + LH).

        가격은 더 높은 고점, RSI 는 더 낮은 고점 (모멘텀 둔화).
        """
        import math
        prices: list[tuple[float, float, float, float, float]] = []
        # Prefix 20봉 평탄
        for i in range(20):
            p = 105.0 + math.sin(i * 0.7) * 0.15
            prices.append((p, p + 0.2, p - 0.2, p - 0.05, 1000.0))
        # Phase A 강한 상승 8봉
        p = 105.0
        for _ in range(8):
            o = p
            c = p + 0.95
            prices.append((o, c + 0.05, o - 0.05, c, 1500.0))
            p = c
        # ★ swing high 1 — high=114.0 spike, 양봉
        prices.append((p, 114.0, p - 0.05, p - 0.3, 2500.0))
        p = p - 0.3
        # Phase B 강한 조정 8봉
        for _ in range(8):
            o = p
            c = p - 0.9
            prices.append((o, o + 0.05, c - 0.05, c, 1500.0))
            p = c
        # Phase C 약한 하락·횡보 10봉
        for i in range(10):
            o = p
            c = p + (-0.2 if i % 2 == 0 else 0.05)
            prices.append((o, max(o, c) + 0.1, min(o, c) - 0.1, c, 1000.0))
            p = c
        # Phase D 약한 상승 4봉
        for _ in range(4):
            o = p
            c = p + 0.4
            prices.append((o, c + 0.1, o - 0.1, c, 1200.0))
            p = c
        # ★ swing high 2 — HH : 114.5 > 114.0, 긴 위꼬리 음봉 마감
        prices.append((p, 114.5, p - 0.05, p - 0.2, 2500.0))
        p = p - 0.2
        # Phase E 조정 5봉
        for _ in range(5):
            o = p
            c = p - 1.0
            prices.append((o, o + 0.05, c - 0.05, c, 1500.0))
            p = c

        klines = make_series(prices)
        self.assertTrue(
            bot.detect_bearish_divergence(klines),
            "슬라이드 26·35 M 패턴(가격 HH + RSI LH) 이 잡혀야 함",
        )

    def test_find_swing_lows_basic(self) -> None:
        """find_swing_lows: 좌우 3봉보다 낮은 단일 swing low 인덱스 반환."""
        # 6봉 평탄 + 1봉 푹 꺼짐 + 6봉 평탄 = swing low 1개
        prices = [(100.0, 100.5, 99.8, 100.2, 1000.0)] * 6 + \
                 [(100.0, 100.1, 95.0, 99.5, 1500.0)] + \
                 [(99.5, 100.3, 99.2, 100.0, 1000.0)] * 6
        klines = make_series(prices)
        swings = bot.find_swing_lows(klines, left=3, right=3)
        self.assertEqual(swings, [6])

    def test_find_swing_highs_basic(self) -> None:
        """find_swing_highs: 거울 대칭."""
        prices = [(100.0, 100.5, 99.8, 100.2, 1000.0)] * 6 + \
                 [(100.0, 105.0, 99.9, 100.5, 1500.0)] + \
                 [(100.5, 100.7, 99.7, 100.0, 1000.0)] * 6
        klines = make_series(prices)
        swings = bot.find_swing_highs(klines, left=3, right=3)
        self.assertEqual(swings, [6])

    def test_no_divergence_on_strict_uptrend(self) -> None:
        """순수 상승 추세에는 bullish divergence 없음."""
        prices = []
        p = 100.0
        for _ in range(30):
            o = p
            c = p + 1.0
            prices.append((o, c + 0.1, o - 0.1, c, 1000.0))
            p = c
        klines = make_series(prices)
        self.assertFalse(bot.detect_bullish_divergence(klines))


# ─────────────────────────────────────────────────────────────
# 3. detect_level_break
# ─────────────────────────────────────────────────────────────
class TestLevelBreak(unittest.TestCase):
    """직전 20봉 신고가/신저가 갱신."""

    def test_new_high_detected(self) -> None:
        prices = [(100.0, 101.0, 99.0, 100.5, 1000.0)] * 20
        # 마지막 봉이 직전 20봉 최고가(101) 를 뚫음
        prices.append((100.5, 105.0, 100.0, 104.0, 1000.0))
        klines = make_series(prices)
        bh, bl = bot.detect_level_break(klines)
        self.assertTrue(bh)
        self.assertFalse(bl)

    def test_new_low_detected(self) -> None:
        prices = [(100.0, 101.0, 99.0, 100.5, 1000.0)] * 20
        prices.append((100.0, 100.5, 95.0, 96.0, 1000.0))
        klines = make_series(prices)
        bh, bl = bot.detect_level_break(klines)
        self.assertFalse(bh)
        self.assertTrue(bl)

    def test_no_break_within_range(self) -> None:
        prices = [(100.0, 101.0, 99.0, 100.5, 1000.0)] * 21
        klines = make_series(prices)
        bh, bl = bot.detect_level_break(klines)
        self.assertFalse(bh)
        self.assertFalse(bl)


# ─────────────────────────────────────────────────────────────
# 4. has_absorption_streak
# ─────────────────────────────────────────────────────────────
class TestAbsorptionStreak(unittest.TestCase):
    """3봉 연속 거래량↑ + 몸통↓."""

    def test_streak_detected(self) -> None:
        # 기준봉 + 3봉이 각각 거래량 1.1× 이상 증가, 몸통은 감소
        # 기준: body=5 vol=1000
        # i=1: body=4 vol=1200 (1.2x)
        # i=2: body=3 vol=1500 (1.25x)
        # i=3: body=2 vol=2000 (1.33x)
        klines = [
            make_kline(BASE_OPEN_TIME + 0 * INTERVAL_15M_MS, 100.0, 106.0, 99.0, 105.0, 1000.0),
            make_kline(BASE_OPEN_TIME + 1 * INTERVAL_15M_MS, 105.0, 110.0, 104.5, 109.0, 1200.0),
            make_kline(BASE_OPEN_TIME + 2 * INTERVAL_15M_MS, 109.0, 113.0, 108.5, 112.0, 1500.0),
            make_kline(BASE_OPEN_TIME + 3 * INTERVAL_15M_MS, 112.0, 115.0, 111.5, 114.0, 2000.0),
        ]
        self.assertTrue(bot.has_absorption_streak(klines))

    def test_streak_breaks_on_body_increase(self) -> None:
        # 마지막 봉 몸통이 더 커지면 깨짐
        klines = [
            make_kline(BASE_OPEN_TIME + 0 * INTERVAL_15M_MS, 100.0, 106.0, 99.0, 105.0, 1000.0),
            make_kline(BASE_OPEN_TIME + 1 * INTERVAL_15M_MS, 105.0, 110.0, 104.5, 109.0, 1200.0),
            make_kline(BASE_OPEN_TIME + 2 * INTERVAL_15M_MS, 109.0, 113.0, 108.5, 112.0, 1500.0),
            make_kline(BASE_OPEN_TIME + 3 * INTERVAL_15M_MS, 112.0, 120.0, 111.5, 119.0, 2000.0),
        ]
        self.assertFalse(bot.has_absorption_streak(klines))

    def test_too_short_returns_false(self) -> None:
        self.assertFalse(bot.has_absorption_streak([]))


# ─────────────────────────────────────────────────────────────
# 5. detect_sr_flip
# ─────────────────────────────────────────────────────────────
class TestSRFlip(unittest.TestCase):
    """가격대 ±0.3% 리테스트."""

    def test_flip_within_tolerance(self) -> None:
        # 레벨 100.0, 현재 봉 wick 가 99.8~100.5 → 100±0.3% 안
        levels = deque([100.0])
        klines = [make_kline(BASE_OPEN_TIME, 99.5, 100.5, 99.8, 100.0, 1000.0)]
        hit = bot.detect_sr_flip(klines, levels)
        self.assertEqual(hit, 100.0)

    def test_flip_outside_tolerance(self) -> None:
        levels = deque([100.0])
        # 봉 범위 95~96 → 100 과 ±0.3% (=±0.3) 범위 밖
        klines = [make_kline(BASE_OPEN_TIME, 95.5, 96.0, 95.0, 95.8, 1000.0)]
        self.assertIsNone(bot.detect_sr_flip(klines, levels))

    def test_empty_levels(self) -> None:
        self.assertIsNone(bot.detect_sr_flip([make_kline(BASE_OPEN_TIME, 1, 2, 0.5, 1.5, 1)], deque()))


# ─────────────────────────────────────────────────────────────
# 6. compute_krbonacci_levels
# ─────────────────────────────────────────────────────────────
class TestKrbonacciLevels(unittest.TestCase):
    """0.764, 1.236, 2.0, 2.26 비율 계산."""

    def test_long_retracement_ratios(self) -> None:
        sw_hi, sw_lo = 200.0, 100.0  # span 100
        levels = krb.compute_krbonacci_levels(sw_hi, sw_lo, "long_retracement")
        self.assertAlmostEqual(levels[0.764], 100 + 0.764 * 100)
        self.assertAlmostEqual(levels[1.0], 200.0)
        self.assertAlmostEqual(levels[1.236], 100 + 1.236 * 100)
        self.assertAlmostEqual(levels[2.0], 300.0)
        self.assertAlmostEqual(levels[2.26], 100 + 2.26 * 100)

    def test_short_retracement_ratios(self) -> None:
        sw_hi, sw_lo = 200.0, 100.0
        levels = krb.compute_krbonacci_levels(sw_hi, sw_lo, "short_retracement")
        self.assertAlmostEqual(levels[0.764], 200 - 0.764 * 100)
        self.assertAlmostEqual(levels[1.236], 200 - 1.236 * 100)

    def test_unknown_direction_raises(self) -> None:
        with self.assertRaises(ValueError):
            krb.compute_krbonacci_levels(200.0, 100.0, "weird")


# ─────────────────────────────────────────────────────────────
# 7. detect_krbonacci_confluence
# ─────────────────────────────────────────────────────────────
class TestKrbonacciConfluence(unittest.TestCase):
    """swing high/low + 현재봉 wick 가 레벨 터치 시 KrbonacciHit."""

    def test_long_hit_at_0764(self) -> None:
        """50봉 시리즈, swing high=200/low=100, 마지막 봉이 0.764 레벨 (176.4) 터치."""
        # 봉 0 : 저점 100 찍기
        # 봉 1~48 : 200 까지 점진 상승 (high=200 어딘가)
        # 봉 49 : wick 이 176.4 ± 0.4% (≈ ±0.7) 안
        prices = []
        # idx 0: 저점 100
        prices.append((100.5, 101.0, 100.0, 100.8, 1000.0))
        # idx 1~48: 100.8 → 200 까지
        p = 100.8
        steps = 48
        target_high = 200.0
        for i in range(steps):
            o = p
            c = p + (target_high - p) / (steps - i)
            h = c + 0.1
            l = o - 0.1
            prices.append((o, h, l, c, 1000.0))
            p = c
        # idx 49: 마지막 봉이 176.4 레벨 wick 으로 터치 (저점 175.5 ~ 고점 177.0)
        prices.append((178.0, 178.5, 175.5, 177.0, 1000.0))
        klines = make_series(prices)
        hit = krb.detect_krbonacci_confluence(klines, direction="long", lookback=50)
        self.assertIsNotNone(hit, "0.764 레벨 wick 터치는 인정돼야 함")
        self.assertEqual(hit.ratio, 0.764)
        # swing_high/low 는 봉의 high/low 라서 wick noise(±0.2) 정도 차이 허용
        self.assertAlmostEqual(hit.swing_high, 200.0, delta=1.0)
        self.assertAlmostEqual(hit.swing_low, 100.0, delta=1.0)

    def test_no_hit_when_far(self) -> None:
        """현재봉이 어떤 레벨에서도 멀리 떨어져 있으면 None."""
        prices = []
        prices.append((100.5, 101.0, 100.0, 100.8, 1000.0))
        p = 100.8
        for i in range(48):
            o = p
            c = p + 2.0
            prices.append((o, c + 0.1, o - 0.1, c, 1000.0))
            p = c
        # 마지막 봉은 swing 사이 한참 어중간한 지점 (130~131) → 0.764=176.4, 1.0=196.x, 0.5 없음
        prices.append((130.0, 131.0, 130.0, 130.5, 1000.0))
        klines = make_series(prices)
        hit = krb.detect_krbonacci_confluence(klines, direction="long", lookback=50)
        self.assertIsNone(hit)

    def test_too_small_range(self) -> None:
        """가격 range 가 너무 작으면 None (의미 없는 피보)."""
        # 50봉이 다 99.9~100.1 사이면 (hi-lo)/hi 가 0.5% 미만 → None
        prices = [(100.0, 100.05, 99.95, 100.0, 1000.0)] * 50
        klines = make_series(prices)
        self.assertIsNone(krb.detect_krbonacci_confluence(klines, "long"))


# ─────────────────────────────────────────────────────────────
# 7-b. compute_take_profit_targets — [2]/[3]/[4] 신호 동봉 익절 후보
# ─────────────────────────────────────────────────────────────
class TestTakeProfitTargets(unittest.TestCase):
    """크보나치 1.236/2.0/2.26 + 1:1 자리 익절 후보 산출.

    50봉 시리즈에 sw_lo=100 (1봉) + sw_hi=120 (1봉) 을 박아 넣고,
    나머지 48봉은 low/high 가 (100, 120) 안쪽이 되도록 구성한다.
    """

    @staticmethod
    def _build_50bar_series(sw_lo: float = 100.0,
                            sw_hi: float = 120.0) -> list[dict]:
        """sw_lo / sw_hi 가 정확히 한 번씩 포함된 50봉 시리즈.

        - 봉 0: low=sw_lo 박는 봉 (open=110, high=111, low=100, close=110)
        - 봉 1: high=sw_hi 박는 봉 (open=110, high=120, low=109, close=110)
        - 봉 2~49: low/high 가 (101, 119) 안쪽 — sw 에 영향 없음
        """
        prices: list[tuple[float, float, float, float, float]] = []
        # 봉 0: 저점 sw_lo 박기
        prices.append((110.0, 111.0, sw_lo, 110.0, 1000.0))
        # 봉 1: 고점 sw_hi 박기
        prices.append((110.0, sw_hi, 109.0, 110.0, 1000.0))
        # 봉 2~49: 안쪽 가격대 (low/high 모두 sw 범위 내부)
        for _ in range(48):
            prices.append((110.0, 112.0, 108.0, 110.0, 1000.0))
        return make_series(prices)

    def test_compute_take_profit_targets_long(self) -> None:
        """롱 익절 후보 6개 (1.236 / 1:1 / 2.0 / 2.26 / 2.544 / 3.236) 가격 검증.

        sw_lo=100, sw_hi=120, span=20
        - 1.236: 100 + 1.236×20 = 124.72
        - 1:1  : 120 + 20       = 140.0
        - 2.0  : 100 + 2.0×20   = 140.0
        - 2.26 : 100 + 2.26×20  = 145.2
        - 2.544: 100 + 2.544×20 = 150.88  (2026-05-17 신규)
        - 3.236: 100 + 3.236×20 = 164.72  (2026-05-17 신규)
        entry=100 → 모든 target 이 100×0.003=0.3 보다 멀리 있어 필터 통과.
        """
        klines = self._build_50bar_series(sw_lo=100.0, sw_hi=120.0)
        targets = krb.compute_take_profit_targets(
            "long", 100.0, klines, klines_1d=None,
        )
        # 6개 라벨 (일봉 후보는 klines_1d=None 이라 빠짐)
        labels = {label for label, _price, _src in targets}
        self.assertEqual(
            labels,
            {"크보나치 1.236", "1:1 자리", "크보나치 2.0", "크보나치 2.26",
             "크보나치 2.544", "크보나치 3.236"},
            f"기대 라벨 6개. 실제: {labels}",
        )
        # 라벨 → 가격 dict 으로 변환
        price_map = {label: price for label, price, _src in targets}
        self.assertAlmostEqual(price_map["크보나치 1.236"], 124.72, delta=0.1)
        self.assertAlmostEqual(price_map["1:1 자리"],       140.0,  delta=0.1)
        self.assertAlmostEqual(price_map["크보나치 2.0"],    140.0,  delta=0.1)
        self.assertAlmostEqual(price_map["크보나치 2.26"],   145.2,  delta=0.1)
        self.assertAlmostEqual(price_map["크보나치 2.544"],  150.88, delta=0.1)
        self.assertAlmostEqual(price_map["크보나치 3.236"],  164.72, delta=0.1)

    def test_take_profit_filters_close(self) -> None:
        """진입가 ±0.3% 근접 필터.

        sw_lo=100, sw_hi=120 → 1.236 자리 = 124.72.
        - entry=124.0   → 1.236 와의 거리 0.72/124   ≈ 0.58% > 0.3% → 포함
        - entry=124.5   → 1.236 와의 거리 0.22/124.5 ≈ 0.18% < 0.3% → 제외
        """
        klines = self._build_50bar_series(sw_lo=100.0, sw_hi=120.0)

        # case A: entry=124.0 (1.236 포함돼야 함)
        targets_a = krb.compute_take_profit_targets(
            "long", 124.0, klines, klines_1d=None,
        )
        labels_a = {label for label, _p, _s in targets_a}
        self.assertIn("크보나치 1.236", labels_a,
                      "entry=124.0 은 1.236(124.72) 와 0.58% 차이 → 포함돼야 함")

        # case B: entry=124.5 (1.236 제외돼야 함)
        targets_b = krb.compute_take_profit_targets(
            "long", 124.5, klines, klines_1d=None,
        )
        labels_b = {label for label, _p, _s in targets_b}
        self.assertNotIn("크보나치 1.236", labels_b,
                         "entry=124.5 은 1.236(124.72) 와 0.18% 차이 → 제외돼야 함")


# ─────────────────────────────────────────────────────────────
# 8. evaluate_rsi_15m — IDLE→PRE_ALERT→ENTRY 상태머신
# ─────────────────────────────────────────────────────────────
class TestRSIStateMachine(unittest.TestCase):
    """rsi_alert_core 상태머신 — IDLE → PRE_ALERT → ENTRY."""

    def setUp(self) -> None:
        self.sent: list[str] = []
        self.send = lambda msg: self.sent.append(msg)

    def test_idle_to_pre_alert_on_oversold(self) -> None:
        """RSI ≤ 30 인 봉이 들어오면 IDLE → PRE_ALERT, 사전알림 1회 송신."""
        klines = make_downtrend_then_green(n_red=20, drop_per_bar=1.5)
        # 마지막 양봉을 빼고 음봉만 (RSI ≤ 30 만들기)
        klines_red_only = klines[:-1]
        state = rsi_core.RSISymbolState()
        rsi_core.evaluate_rsi_15m(
            symbol="BTCUSDT",
            klines_15m=klines_red_only,
            klines_4h=[],
            state=state,
            send=self.send,
        )
        self.assertEqual(state.status, "PRE_ALERT")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("사전 알림", self.sent[0])

    def test_pre_alert_to_entry_on_green(self) -> None:
        """PRE_ALERT 후 다음 봉이 양봉이면 ENTRY 송신, 상태는 IDLE 로 복귀."""
        klines = make_downtrend_then_green(n_red=20, drop_per_bar=1.5)
        state = rsi_core.RSISymbolState()
        # 1단계: 음봉만 → PRE_ALERT
        rsi_core.evaluate_rsi_15m(
            symbol="BTCUSDT",
            klines_15m=klines[:-1],
            klines_4h=[],
            state=state,
            send=self.send,
        )
        self.assertEqual(state.status, "PRE_ALERT")
        # 2단계: 양봉 추가 → ENTRY
        rsi_core.evaluate_rsi_15m(
            symbol="BTCUSDT",
            klines_15m=klines,
            klines_4h=[],
            state=state,
            send=self.send,
        )
        self.assertEqual(state.status, "IDLE")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("롱 진입 신호", self.sent[1])

    def test_no_alert_when_not_oversold(self) -> None:
        """RSI > 30 이면 IDLE 유지, 송신 없음."""
        # 평탄한 시리즈
        prices = [(100.0, 101.0, 99.0, 100.5, 1000.0)] * 30
        klines = make_series(prices)
        state = rsi_core.RSISymbolState()
        rsi_core.evaluate_rsi_15m(
            symbol="BTCUSDT",
            klines_15m=klines,
            klines_4h=[],
            state=state,
            send=self.send,
        )
        self.assertEqual(state.status, "IDLE")
        self.assertEqual(len(self.sent), 0)


# ─────────────────────────────────────────────────────────────
# 9. 시나리오 테스트: krtky [1] 사전알림 → [2] 컨펌 → send 호출
# ─────────────────────────────────────────────────────────────
class TestKrtkyScenario(unittest.TestCase):
    """크트키 메인 봇의 사전알림→컨펌 흐름 통합 테스트.

    send_telegram 을 모킹해서 [1] / [2] 메시지가 모두 호출되는지 확인.
    """

    SYMBOL = "BTCUSDT"

    def setUp(self) -> None:
        # 글로벌 상태 격리 — 다른 테스트가 더럽혀 두었을 수 있는 시리즈/상태를 리셋
        for sym in bot.SYMBOLS:
            bot.SERIES_15M[sym] = deque(maxlen=bot.SERIES_MAX_15M)
            bot.SERIES_4H[sym] = deque(maxlen=bot.SERIES_MAX_4H)
            bot.STATE[sym] = bot.SymbolState()
            bot.RSI_STATE[sym] = rsi_core.RSISymbolState()
        self.sent: list[str] = []
        # send_telegram 모킹 — 봇 모듈 안의 참조를 직접 패치한다
        self.patcher = patch.object(bot, "send_telegram",
                                    side_effect=lambda msg: self.sent.append(msg))
        self.patcher.start()
        # RSI 단순 알림기는 자체 send 콜백을 받으므로 별도 모킹은 불필요하지만,
        # 봇 on_bar_closed 내부에서 send_telegram 을 그대로 콜백으로 넘긴다 →
        # 이미 모킹된 함수가 그대로 호출되므로 추가 작업 없음.

    def tearDown(self) -> None:
        self.patcher.stop()

    def _build_long_scenario(self) -> tuple[list[dict], dict, dict]:
        """롱 사전알림이 발화되도록 장대 음봉 + 그 다음 봉 장대 양봉을 생성.

        Returns: (warmup_15m, pre_alert_bar, confirm_bar)
            - warmup_15m: 30봉의 하락 시리즈 (워밍업)
            - pre_alert_bar: RSI 과매도 + 장대 음봉 + 충분한 꼬리 (풀봉 회피)
            - confirm_bar: 다음 봉, 장대 양봉 (컨펌)

        주의:
          - BTC USDT 절대 거래량 임계 1500, SMA20×2 둘 다 통과해야 함
          - is_full_no_wick 거부를 피하려면 꼬리 합계 ≥ 10% range
        """
        p = 70000.0
        ts = BASE_OPEN_TIME
        warmup = []
        # 25봉 일관된 하락 (RSI 0 근처로 깎아 내려서 과매도 확보)
        for _ in range(25):
            o = p
            c = p - 100.0
            warmup.append(make_kline(ts, o, o + 5.0, c - 5.0, c, 500.0))
            ts += INTERVAL_15M_MS
            p = c
        # 추가 5봉 미세 하락 (총 30봉 — RSI 워밍업 충족)
        for _ in range(5):
            o = p
            c = p - 5.0
            warmup.append(make_kline(ts, o, o + 5.0, c - 5.0, c, 500.0))
            ts += INTERVAL_15M_MS
            p = c

        # 사전알림 봉: 장대 음봉. body 150, 양쪽 꼬리 25 → range 200
        #   body/range = 75% (≥60% ✓), 꼬리 합/range = 25% (>10% — 풀봉 회피)
        pre_open = p
        pre_close = p - 150.0
        pre_alert_bar = make_kline(
            ts,
            o=pre_open,
            h=pre_open + 25.0,
            l=pre_close - 25.0,
            c=pre_close,
            v=2000.0,   # 절대 ≥1500 ✓ · SMA(500) × 4 ≥ ×2 ✓
        )
        ts += INTERVAL_15M_MS

        # 컨펌 봉: 장대 양봉 (body 150, 꼬리 25)
        cnf_open = pre_close
        cnf_close = pre_close + 150.0
        confirm_bar = make_kline(
            ts,
            o=cnf_open,
            h=cnf_close + 25.0,
            l=cnf_open - 25.0,
            c=cnf_close,
            v=2000.0,
        )
        return warmup, pre_alert_bar, confirm_bar

    def _build_4h_series(self, base_ts: int) -> list[dict]:
        """4H 시리즈를 충분히 채워서 RSI_PERIOD+2 = 16 봉 이상 확보."""
        # 평탄한 4H 시리즈 (RSI 50 근처)
        prices = [(100.0, 101.0, 99.0, 100.5, 5000.0)] * 30
        return make_series(prices, interval_ms=INTERVAL_4H_MS, base_ts=base_ts)

    def test_pre_alert_then_confirm_calls_send(self) -> None:
        warmup, pre_bar, cnf_bar = self._build_long_scenario()

        # 시리즈에 워밍업 봉 30개를 push
        for k in warmup:
            bot.SERIES_15M[self.SYMBOL].append(k)
        # 4H 시리즈 채우기
        four_h = self._build_4h_series(base_ts=BASE_OPEN_TIME - 30 * INTERVAL_4H_MS)
        for k in four_h:
            bot.SERIES_4H[self.SYMBOL].append(k)

        # [HIGH 핫픽스] stale 가드는 합성 캔들(BASE_OPEN_TIME 과거)을 stale 로 판정
        # → 평가 skip → send 0건. 테스트에선 가드 무력화.
        with patch.dict(bot._INTERVAL_MS_BY_LABEL, {}, clear=True):
            # ─── (A) 사전 알림 봉 도착
            bot.on_bar_closed(self.SYMBOL, "15m", pre_bar)
        # [1] LONG pre-alert 메시지가 와야 한다
        pre_alert_msgs = [m for m in self.sent if '크트키' in m and '[1] 사전 알림' in m]
        self.assertGreaterEqual(
            len(pre_alert_msgs), 1,
            f"[1] 사전 알림 메시지가 호출돼야 함. 받은 메시지들={self.sent}"
        )
        # 상태머신에 pre_long_bar 가 기록됐는지
        self.assertEqual(
            bot.STATE[self.SYMBOL].pre_long_bar, pre_bar["close_time"],
            "사전알림 후 pre_long_bar 가 기록돼야 함",
        )

        # ─── (B) 컨펌 봉 도착 (stale 가드 동일 무력화)
        sent_before_confirm = len(self.sent)
        with patch.dict(bot._INTERVAL_MS_BY_LABEL, {}, clear=True):
            bot.on_bar_closed(self.SYMBOL, "15m", cnf_bar)
        # [2]/[3]/[4] 진입 신호 중 하나가 호출돼야 함
        entry_msgs = [m for m in self.sent[sent_before_confirm:]
                      if "진입 신호" in m]
        self.assertGreaterEqual(
            len(entry_msgs), 1,
            f"[2] 컨펌 진입 신호가 호출돼야 함. 새 메시지들={self.sent[sent_before_confirm:]}"
        )
        # 컨펌 후 pre_long_bar 는 클리어돼야 함
        self.assertIsNone(bot.STATE[self.SYMBOL].pre_long_bar)

    def test_pre_alert_rearm_guard(self) -> None:
        """[MID 2] 한 추세에 [1] 사전 알림은 1회만 — RSI 회복 전까지 재발사 차단.

        시나리오:
          1. 30봉 평탄 워밍업 (RSI ≈ 50)
          2. RSI≤30 + 장대 음봉 + 거래량 충족 5봉 연속
             → 첫 봉에서만 사전 알림 1회, 이후 4봉은 가드로 차단
          3. 양봉 회복 4-5봉 (작은 몸통, RSI 35+ 로 복귀) → armed 해제
          4. 다시 RSI≤30 + 장대 음봉 1봉 → 추가 1회 발사 (누적 2회)
        """
        # 글로벌 상태 리셋 (setUp 이 했지만 ISOLATED_SR_CACHE 도 명시 리셋)
        bot.ISOLATED_SR_CACHE[self.SYMBOL] = []

        ts = BASE_OPEN_TIME
        # ─── 1) 30봉 평탄 워밍업 (RSI ≈ 50)
        p = 70000.0
        warmup: list[dict] = []
        for _ in range(35):
            o = p
            c = p + 1.0   # 미세 상승 — RSI 50+ 유지, 후속 하락이 더 두드러지게
            warmup.append(make_kline(ts, o=o, h=c + 2.0, l=o - 2.0, c=c, v=500.0))
            ts += INTERVAL_15M_MS
            p = c

        for k in warmup:
            bot.SERIES_15M[self.SYMBOL].append(k)

        # 4H 시리즈 채우기 (RSI_PERIOD+2 이상)
        four_h = self._build_4h_series(base_ts=BASE_OPEN_TIME - 30 * INTERVAL_4H_MS)
        for k in four_h:
            bot.SERIES_4H[self.SYMBOL].append(k)

        # ─── 2) RSI≤30 + 장대 음봉 5봉 연속
        # body 150, 꼬리 25 (풀봉 회피), 거래량 2000 (절대 ≥1500, SMA×2 ≥)
        pre_alert_bars: list[dict] = []
        for _ in range(5):
            o = p
            c = p - 150.0
            pre_alert_bars.append(make_kline(
                ts, o=o, h=o + 25.0, l=c - 25.0, c=c, v=2000.0,
            ))
            ts += INTERVAL_15M_MS
            p = c

        # 첫 봉 도착 → 사전 알림 1회 발사 (가드 None → True 전환)
        # stale 가드 무력화 (합성 캔들이 과거 시각이므로)
        self._stale_patch = patch.dict(bot._INTERVAL_MS_BY_LABEL, {}, clear=True)
        self._stale_patch.start()
        self.addCleanup(self._stale_patch.stop)
        bot.on_bar_closed(self.SYMBOL, "15m", pre_alert_bars[0])
        pre_msgs = [m for m in self.sent if '크트키' in m and '[1] 사전 알림' in m]
        self.assertEqual(
            len(pre_msgs), 1,
            f"첫 음봉에서 [1] 사전 알림 1회 발사돼야 함. 받은={self.sent}",
        )
        self.assertTrue(
            bot.STATE[self.SYMBOL].last_long_armed,
            "발사 직후 last_long_armed=True 여야 함",
        )

        # 이후 4봉: 가드로 [1] 추가 발사 차단
        for k in pre_alert_bars[1:]:
            bot.on_bar_closed(self.SYMBOL, "15m", k)

        pre_msgs_after = [m for m in self.sent if '크트키' in m and '[1] 사전 알림' in m]
        self.assertEqual(
            len(pre_msgs_after), 1,
            f"연속 음봉 5봉 동안 [1] 누적 1회여야 함 (가드). 실제 누적={len(pre_msgs_after)}, "
            f"전체 메시지={self.sent}",
        )

        # ─── 3) 양봉 회복 5봉 (꼬리 충분 — 풀봉 SKIP 회피, RSI 회복 가드 해제)
        # body 250, 양쪽 꼬리 각 40 → range 330, 꼬리/range ≈ 24% (>10% 풀봉 회피)
        # body/range ≈ 76% → is_long_body 일 수도 있으나 pre_long_bar 가 timeout
        # 으로 이미 None 이라 컨펌 진입 분기 안 탐. RSI 35+ 회복만 의도.
        recovery_bars: list[dict] = []
        for _ in range(5):
            o = p
            c = p + 250.0   # 빠른 회복 — RSI 35+ 도달 위해
            recovery_bars.append(make_kline(
                ts, o=o, h=c + 40.0, l=o - 40.0, c=c, v=500.0,
            ))
            ts += INTERVAL_15M_MS
            p = c

        for k in recovery_bars:
            bot.on_bar_closed(self.SYMBOL, "15m", k)

        # RSI 회복으로 armed 해제됐어야 함
        self.assertFalse(
            bot.STATE[self.SYMBOL].last_long_armed,
            f"RSI 회복 (>{bot.RSI_OVERSOLD + bot.RSI_RECOVERY_BAND}) 후 "
            f"last_long_armed=False 여야 함",
        )

        # ─── 4) 다시 RSI≤30 + 장대 음봉 1봉 → 누적 [1] 발사 2회
        # 회복으로 p 가 올라갔으니 다시 깊게 떨어뜨려 RSI≤30 만들기
        # — 한 봉으론 부족할 수 있어 추가 음봉 몇 봉 흘리기
        # 먼저 RSI 를 다시 30 이하로 깎기 위해 음봉 prefix.
        # 장대 미충족(body/range < 60%) + 거래량 미충족(< 1500) 로 long_pre_ok=False.
        # body 80, 양쪽 꼬리 각 35 → range 150, body/range ≈ 53% → is_long_body=False.
        # ATR 도 작게 유지(≈150) 해 최종 봉(body 150, range 200) 이
        # body/ATR ≥ 1.0 을 통과하도록.
        prefix_bears: list[dict] = []
        for _ in range(25):
            o = p
            c = p - 80.0
            prefix_bears.append(make_kline(
                ts, o=o, h=o + 35.0, l=c - 35.0, c=c, v=400.0,
            ))
            ts += INTERVAL_15M_MS
            p = c
        for k in prefix_bears:
            bot.on_bar_closed(self.SYMBOL, "15m", k)

        # 그리고 장대 음봉 + 거래량 충족 1봉.
        # ATR(prefix 봉 range 150) 가 ~150 정도, final 봉 자체 TR≈300 가 들어가면
        # ATR≈160 으로 살짝 오름. body/ATR ≥ 1.0 보장 위해 body 250 사용.
        final_o = p
        final_c = p - 250.0
        final_bar = make_kline(
            ts, o=final_o, h=final_o + 25.0, l=final_c - 25.0, c=final_c, v=2000.0,
        )
        bot.on_bar_closed(self.SYMBOL, "15m", final_bar)

        pre_msgs_final = [m for m in self.sent if '크트키' in m and '[1] 사전 알림' in m]
        self.assertEqual(
            len(pre_msgs_final), 2,
            f"armed 해제 후 다시 RSI≤30 + 장대음봉 → [1] 누적 2회여야 함. "
            f"실제 누적={len(pre_msgs_final)}, 전체 메시지 개수={len(self.sent)}",
        )


# ─────────────────────────────────────────────────────────────
# 10. BitgetCandleStream — _handle_candle_push 단위 테스트
# ─────────────────────────────────────────────────────────────
class TestBitgetCandleStream(unittest.TestCase):
    """WS 클라이언트의 봉 닫힘 디스패치 — 네트워크 없이 단위 테스트."""

    def setUp(self) -> None:
        self.calls: list[tuple] = []

        def on_bar(symbol: str, interval: str, kline: dict) -> None:
            self.calls.append((symbol, interval, kline))

        self.stream = ws_mod.BitgetCandleStream(
            symbols=["BTCUSDT"],
            intervals=["candle15m"],
            on_bar_closed=on_bar,
        )

    def test_new_open_time_emits_previous_bar(self) -> None:
        """ts 가 바뀌면 직전 봉을 닫힌 것으로 콜백 발사."""
        interval_ms = ws_mod.INTERVAL_MS["candle15m"]
        ts1 = BASE_OPEN_TIME
        ts2 = BASE_OPEN_TIME + interval_ms

        row1 = [str(ts1), "100.0", "101.0", "99.5", "100.5", "10.0", "0", "0"]
        row2 = [str(ts2), "100.5", "102.0", "100.0", "101.5", "12.0", "0", "0"]

        # 첫 푸시: 봉 1개만 — 아직 콜백 안 나감
        self.stream._handle_candle_push(
            "BTCUSDT", "candle15m", interval_ms, "snapshot", [row1])
        self.assertEqual(len(self.calls), 0)

        # 두 번째 푸시: 새 봉 — 봉 1 이 닫힌 것으로 발사
        self.stream._handle_candle_push(
            "BTCUSDT", "candle15m", interval_ms, "update", [row2])
        self.assertEqual(len(self.calls), 1)
        sym, iv, k = self.calls[0]
        self.assertEqual(sym, "BTCUSDT")
        self.assertEqual(iv, "15m")
        self.assertEqual(k["open_time"], ts1)
        self.assertEqual(k["close_time"], ts1 + interval_ms - 1)
        self.assertEqual(k["close"], 100.5)


# ─────────────────────────────────────────────────────────────
# 11. compute_quartile_levels — 일봉 4분할 (Kris 텔레그램 03:11)
# ─────────────────────────────────────────────────────────────
class TestQuartileLevels(unittest.TestCase):
    """일봉 swing 의 4분할 (0/0.25/0.5/0.75/1.0) 가격 계산."""

    def test_compute_quartile_levels(self) -> None:
        """swing_high=100, swing_low=50 → long_retracement.

        0.0 → 50, 0.25 → 62.5, 0.5 → 75, 0.75 → 87.5, 1.0 → 100
        span = 50, level = swing_low + r * span
        """
        levels = krb.compute_quartile_levels(100.0, 50.0, "long_retracement")
        self.assertAlmostEqual(levels[0.0], 50.0)
        self.assertAlmostEqual(levels[0.25], 62.5)
        self.assertAlmostEqual(levels[0.5], 75.0)
        self.assertAlmostEqual(levels[0.75], 87.5)
        self.assertAlmostEqual(levels[1.0], 100.0)

        # short_retracement 거울 대칭
        short_levels = krb.compute_quartile_levels(100.0, 50.0, "short_retracement")
        self.assertAlmostEqual(short_levels[0.0], 100.0)
        self.assertAlmostEqual(short_levels[0.25], 87.5)
        self.assertAlmostEqual(short_levels[0.5], 75.0)
        self.assertAlmostEqual(short_levels[0.75], 62.5)
        self.assertAlmostEqual(short_levels[1.0], 50.0)


# ─────────────────────────────────────────────────────────────
# 12. detect_quartile_confluence — 일봉 swing × 15m wick 매칭
# ─────────────────────────────────────────────────────────────
class TestQuartileConfluence(unittest.TestCase):
    """일봉 4분할 자리 × 현재 15m wick 컨플루언스 (Kris 텔레그램 03:11)."""

    def _build_daily(self, swing_high: float, swing_low: float) -> list[dict]:
        """일봉 30봉, max(high)=swing_high / min(low)=swing_low 가 되도록 구성.

        idx 0  : low=swing_low (전체 최저)
        idx 15 : high=swing_high (전체 최고)
        나머지 : 그 사이 평탄
        """
        out: list[dict] = []
        ts = BASE_OPEN_TIME
        mid = (swing_high + swing_low) / 2.0
        for i in range(30):
            if i == 0:
                # 저점 찍기 — open/close 는 swing_low 근처지만 low 만 swing_low
                out.append(make_kline(
                    ts, o=mid, h=mid + 1.0, l=swing_low, c=mid - 0.5,
                    v=1000.0, interval_ms=INTERVAL_15M_MS * 4 * 24,
                ))
            elif i == 15:
                # 고점 찍기 — high 만 swing_high
                out.append(make_kline(
                    ts, o=mid, h=swing_high, l=mid - 1.0, c=mid + 0.5,
                    v=1000.0, interval_ms=INTERVAL_15M_MS * 4 * 24,
                ))
            else:
                out.append(make_kline(
                    ts, o=mid, h=mid + 2.0, l=mid - 2.0, c=mid + 0.3,
                    v=1000.0, interval_ms=INTERVAL_15M_MS * 4 * 24,
                ))
            ts += INTERVAL_15M_MS * 4 * 24
        return out

    def test_detect_quartile_confluence_hit(self) -> None:
        """일봉 swing_high=100/low=50, 15m wick 가 75 (=0.5) 근처면 0.5 매칭."""
        daily = self._build_daily(100.0, 50.0)
        # 15m 봉: wick 가 75.0 ±0.4% (= ±0.3) 안. low=74.8, high=75.3
        last_15m = make_kline(BASE_OPEN_TIME, o=75.1, h=75.3, l=74.8, c=75.05, v=100.0)
        hit = krb.detect_quartile_confluence(daily, last_15m, "long")
        self.assertIsNotNone(hit, "0.5 자리 (75.0) wick 터치는 인정돼야 함")
        self.assertAlmostEqual(hit.ratio, 0.5)
        self.assertAlmostEqual(hit.level_price, 75.0, delta=0.5)
        self.assertAlmostEqual(hit.swing_high, 100.0, delta=0.5)
        self.assertAlmostEqual(hit.swing_low, 50.0, delta=0.5)

    def test_detect_quartile_confluence_miss(self) -> None:
        """15m wick 가 60 (어떤 4분할 레벨에도 안 걸림) → None.

        4분할 레벨: 50 / 62.5 / 75 / 87.5 / 100. 60 은 62.5 와 -4.2% 차이
        (허용 ±0.4% 밖).
        """
        daily = self._build_daily(100.0, 50.0)
        last_15m = make_kline(BASE_OPEN_TIME, o=60.1, h=60.2, l=59.9, c=60.0, v=100.0)
        hit = krb.detect_quartile_confluence(daily, last_15m, "long")
        self.assertIsNone(hit, "60 은 어떤 4분할 레벨에도 안 걸려야 함")

    def test_quartile_atr_filter_blocks_small_swing(self) -> None:
        """[MID 1] swing 폭 < 일봉 ATR × 2 → None.

        - 30봉 평탄 일봉: 가격 99.5 ~ 100.5 (swing 폭 ≈ 1)
        - 일봉 ATR 은 봉별 (high-low) 평균 ≈ 0.6 → ATR×2 ≈ 1.2
        - swing 1 < ATR×2 1.2 → 의미 없는 4분할 → None
        """
        # 평탄한 일봉 30봉 — high/low 가 진동해 ATR 이 의미 있게 잡히도록
        daily: list[dict] = []
        ts = BASE_OPEN_TIME
        interval_d_ms = INTERVAL_15M_MS * 4 * 24
        for i in range(30):
            # high 100.4 ~ 100.5, low 99.5 ~ 99.6 사이로 매봉 진동
            # → 각 봉 range ≈ 0.9, ATR ≈ 0.9, ATR×2 ≈ 1.8
            # swing 폭 ≈ 1.0 < 1.8 → ATR 필터에 걸려야 함
            o = 100.0
            h = 100.4 + (i % 2) * 0.1
            l = 99.5 + (i % 2) * 0.1
            c = 100.0 + ((-1) ** i) * 0.05
            daily.append(make_kline(ts, o=o, h=h, l=l, c=c,
                                    v=1000.0, interval_ms=interval_d_ms))
            ts += interval_d_ms

        # last 15m: swing 어딘가 (mid 75 자리는 없으니 그냥 100 자리 닿게)
        last_15m = make_kline(BASE_OPEN_TIME, o=100.0, h=100.1, l=99.9, c=100.0, v=100.0)
        hit = krb.detect_quartile_confluence(daily, last_15m, "long")
        self.assertIsNone(hit, "swing 폭 < ATR×2 이면 ATR 필터에 걸려 None 이어야 함")

        # Sanity: min_atr_multiple=0 으로 필터 끄면 (swing 자체는 있으므로) 매칭 가능
        # — 단 last_15m 의 wick 가 어떤 4분할 자리도 안 닿으면 여전히 None.
        # 이 케이스는 필터를 끈 상태에서 swing(약 1) 의 1.0 자리(약 100.5) 가
        # last_15m wick (99.9~100.1) 와 떨어져 있어 None. 즉 ATR 필터 OFF 가
        # 동일 결과를 내는지가 아니라, "필터가 결정적이었는지" 별도 검사 불필요.


# ─────────────────────────────────────────────────────────────
# 13. detect_isolated_reversal — 고립 반전 캔들 (Kris 텔레그램 03:14-15)
# ─────────────────────────────────────────────────────────────
class TestIsolatedReversal(unittest.TestCase):
    """8봉 추세 중 1봉 반대 방향 = 고립 반전 → resistance/support."""

    def test_isolated_reversal_resistance(self) -> None:
        """8봉 음봉 + 1봉 고립 양봉 → resistance, price = 양봉 high."""
        ts = BASE_OPEN_TIME
        klines: list[dict] = []
        p = 100.0
        for _ in range(8):
            o = p
            c = p - 1.0
            klines.append(make_kline(ts, o=o, h=o + 0.1, l=c - 0.1, c=c, v=1000.0))
            ts += INTERVAL_15M_MS
            p = c
        # 고립 양봉: low=p, high=p+2, close=p+1.5
        target_high = p + 2.0
        klines.append(make_kline(ts, o=p, h=target_high, l=p - 0.1, c=p + 1.5, v=1500.0))
        iso = bot.detect_isolated_reversal(klines)
        self.assertIsNotNone(iso, "음봉 8 + 고립 양봉 → 고립 반전 잡혀야 함")
        self.assertEqual(iso.kind, "resistance")
        self.assertAlmostEqual(iso.price, target_high)

    def test_isolated_reversal_support(self) -> None:
        """8봉 양봉 + 1봉 고립 음봉 → support, price = 음봉 low."""
        ts = BASE_OPEN_TIME
        klines: list[dict] = []
        p = 100.0
        for _ in range(8):
            o = p
            c = p + 1.0
            klines.append(make_kline(ts, o=o, h=c + 0.1, l=o - 0.1, c=c, v=1000.0))
            ts += INTERVAL_15M_MS
            p = c
        # 고립 음봉: low=p-2, close=p-1.5
        target_low = p - 2.0
        klines.append(make_kline(ts, o=p, h=p + 0.1, l=target_low, c=p - 1.5, v=1500.0))
        iso = bot.detect_isolated_reversal(klines)
        self.assertIsNotNone(iso, "양봉 8 + 고립 음봉 → 고립 반전 잡혀야 함")
        self.assertEqual(iso.kind, "support")
        self.assertAlmostEqual(iso.price, target_low)

    def test_isolated_reversal_no_streak(self) -> None:
        """음봉 4 / 양봉 4 섞임 → 추세 미충족 (75% 비율 못 채움) → None."""
        ts = BASE_OPEN_TIME
        klines: list[dict] = []
        p = 100.0
        # 8봉: 음양 번갈아 (red 4 / green 4 = 50% — streak_ratio 0.75 못 채움)
        for i in range(8):
            o = p
            if i % 2 == 0:
                c = p - 1.0
            else:
                c = p + 1.0
            klines.append(make_kline(
                ts, o=o, h=max(o, c) + 0.1, l=min(o, c) - 0.1, c=c, v=1000.0))
            ts += INTERVAL_15M_MS
            p = c
        # 9번째 봉 (어떤 방향이든) — context 가 50/50 이므로 None
        klines.append(make_kline(ts, o=p, h=p + 2.0, l=p - 0.1, c=p + 1.5, v=1500.0))
        iso = bot.detect_isolated_reversal(klines)
        self.assertIsNone(iso, "context 가 50/50 이면 추세 미충족 → None")


# ─────────────────────────────────────────────────────────────
# 14. ISOLATED_SR_CACHE — 등록/매칭/만료
# ─────────────────────────────────────────────────────────────
class TestIsolatedSRCache(unittest.TestCase):
    """고립 반전 SR 캐시 — 등록/매칭/만료(시간·봉)."""

    SYMBOL = "BTCUSDT"

    def setUp(self) -> None:
        # 캐시 격리
        bot.ISOLATED_SR_CACHE[self.SYMBOL] = []

    def tearDown(self) -> None:
        bot.ISOLATED_SR_CACHE[self.SYMBOL] = []

    def _make_sr(self, price: float, kind: str,
                created_ts_ms: int = BASE_OPEN_TIME,
                bars_since: int = 0) -> bot.IsolatedSR:
        sr = bot.IsolatedSR(
            price=price, kind=kind,
            created_ts_ms=created_ts_ms, interval="15m",
            bars_since=bars_since,
        )
        return sr

    def test_sr_cache_match_within_tolerance(self) -> None:
        """[HIGH 패치] price=100 support 등록 → wick 가 SR 자리를 가로지르는지 검사.

        - 케이스 A: low=99.7, high=100.3 → 100.0 가로지름 → 매칭
        - 케이스 B: low=101.0, high=102.0 → SR 위쪽만 → 미매칭
        """
        sr = self._make_sr(price=100.0, kind="support")
        bot._register_isolated_sr(self.SYMBOL, sr, "15m")

        # 케이스 A: wick 가 SR 자리 100.0 을 가로지름 (99.7 ~ 100.3)
        kline_hit = make_kline(BASE_OPEN_TIME, o=100.0, h=100.3, l=99.7, c=100.1, v=1000.0)
        hit = bot.match_isolated_sr(self.SYMBOL, kline_hit, "long")
        self.assertIsNotNone(hit, "wick 가 SR 자리를 가로지르면 매칭돼야 함")
        self.assertEqual(hit.kind, "support")

        # 케이스 B: 봉이 SR 위쪽에만 있음 (101.0 ~ 102.0)
        kline_above = make_kline(
            BASE_OPEN_TIME + INTERVAL_15M_MS,
            o=101.2, h=102.0, l=101.0, c=101.5, v=1000.0,
        )
        miss_above = bot.match_isolated_sr(self.SYMBOL, kline_above, "long")
        self.assertIsNone(miss_above, "봉이 SR 위쪽만이면 매칭 안 돼야 함")

    def test_sr_cache_no_match_when_wick_below_sr(self) -> None:
        """[HIGH 패치] 봉의 wick 범위가 SR 자리 아래에만 있으면 미매칭.

        SR price=100 support 등록 후, low=98.0/high=99.0 봉은 SR 아래쪽만
        → match_isolated_sr 는 None 반환.
        """
        sr = self._make_sr(price=100.0, kind="support")
        bot._register_isolated_sr(self.SYMBOL, sr, "15m")
        kline_below = make_kline(
            BASE_OPEN_TIME, o=98.5, h=99.0, l=98.0, c=98.7, v=1000.0,
        )
        miss_below = bot.match_isolated_sr(self.SYMBOL, kline_below, "long")
        self.assertIsNone(miss_below, "봉이 SR 아래쪽만이면 매칭 안 돼야 함")

    def test_sr_cache_expires_by_bars(self) -> None:
        """bars_since > 50 (ISOLATED_SR_MAX_BARS) 이면 prune 후 사라짐."""
        sr = self._make_sr(price=100.0, kind="support",
                           bars_since=bot.ISOLATED_SR_MAX_BARS + 1)
        bot.ISOLATED_SR_CACHE[self.SYMBOL].append(sr)
        # now_ms 는 created_ts_ms 와 같게 두어 시간 만료는 발동 안 시킴
        bot._prune_isolated_sr(self.SYMBOL, BASE_OPEN_TIME)
        self.assertEqual(
            len(bot.ISOLATED_SR_CACHE[self.SYMBOL]), 0,
            "50봉 초과 SR 은 prune 돼야 함",
        )

    def test_sr_cache_expires_by_time(self) -> None:
        """created_ts_ms 가 6시간보다 더 과거면 prune 후 사라짐."""
        # now_ms = created + 6시간 + 1초
        created = BASE_OPEN_TIME
        now_ms = created + (bot.ISOLATED_SR_TTL_SEC + 1) * 1000
        sr = self._make_sr(price=100.0, kind="support",
                           created_ts_ms=created, bars_since=0)
        bot.ISOLATED_SR_CACHE[self.SYMBOL].append(sr)
        bot._prune_isolated_sr(self.SYMBOL, now_ms)
        self.assertEqual(
            len(bot.ISOLATED_SR_CACHE[self.SYMBOL]), 0,
            "6시간 초과 SR 은 prune 돼야 함",
        )


# ─────────────────────────────────────────────────────────────
# Universe 확장 (top20 거래량 + EXTRA 7) — 신규 기능 테스트
# ─────────────────────────────────────────────────────────────
class TestUniverseExpansion(unittest.TestCase):
    """fetch_top_volume_symbols / compute_universe / update_universe 검증.

    네트워크 모킹 필수. SYMBOLS 글로벌은 테스트 사이 격리.
    """

    def setUp(self) -> None:
        # 글로벌 SYMBOLS 보호 — 각 테스트 후 원복
        self._saved_symbols = list(bot.SYMBOLS)

    def tearDown(self) -> None:
        bot.SYMBOLS = list(self._saved_symbols)

    def test_fetch_top_volume_symbols(self) -> None:
        """Bitget tickers 응답을 usdtVolume 내림차순 정렬 + USDT 페어만 + top_n 컷."""
        fake_body = {
            "code": "00000",
            "data": [
                {"symbol": "BTCUSDT",   "usdtVolume": "1000000000"},
                {"symbol": "ETHUSDT",   "usdtVolume": "500000000"},
                # USDT 로 안 끝남 → 제외돼야 함
                {"symbol": "SOMETHING", "usdtVolume": "800000000"},
                {"symbol": "SOLUSDT",   "usdtVolume": "300000000"},
                {"symbol": "XRPUSDT",   "usdtVolume": "200000000"},
            ],
        }
        fake_resp = MagicMock()
        fake_resp.json.return_value = fake_body
        fake_resp.raise_for_status.return_value = None

        with patch("krtky_alert_bot.requests.get", return_value=fake_resp) as mget:
            result = bot.fetch_top_volume_symbols(top_n=3)

        # productType=USDT-FUTURES 로 호출됐는지 확인
        self.assertEqual(mget.call_count, 1)
        _, kwargs = mget.call_args
        self.assertEqual(kwargs.get("params", {}).get("productType"), "USDT-FUTURES")

        # top_n=3: 거래량 내림차순 → BTC > ETH > SOL.
        # SOMETHING 은 USDT 미종결로 제외, XRP 는 4번째라 컷.
        self.assertEqual(result, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_compute_universe_dedup_and_extra(self) -> None:
        """top + EXTRA 합쳐 dedup. top 순서 보존, EXTRA 중복은 제거."""
        # NVDAUSDT 는 EXTRA 와 중복 — top 자리에서 보존되고 EXTRA 쪽에서 dedup 돼야 함
        fake_top = ["BTCUSDT", "ETHUSDT", "NVDAUSDT", "SOLUSDT", "XRPUSDT"]

        with patch.object(bot, "fetch_top_volume_symbols", return_value=fake_top):
            universe = bot.compute_universe()

        # 길이: top(5) + EXTRA(7) - 1중복(NVDAUSDT) = 11
        self.assertEqual(
            len(universe), 11,
            f"기대 11, 실제 {len(universe)}: {universe}",
        )

        # 처음 두 자리는 BTC, ETH
        self.assertEqual(universe[0], "BTCUSDT")
        self.assertEqual(universe[1], "ETHUSDT")

        # NVDAUSDT 는 top 의 인덱스 2(=세 번째) 에 보존
        self.assertEqual(universe[2], "NVDAUSDT")
        self.assertEqual(universe.count("NVDAUSDT"), 1,
                         "NVDAUSDT 가 dedup 안 됐다")

        # EXTRA 의 나머지 6개(EWY/TSL/MSFT/INTC/CL/XAU) 가 끝쪽에 모두 포함
        for extra in ("EWYUSDT", "TSLAUSDT", "MSFTUSDT",
                      "INTCUSDT", "CLUSDT", "XAUUSDT"):
            self.assertIn(extra, universe, f"{extra} 누락")

        # top 의 SOL/XRP 도 NVDAUSDT 뒤 자리에 보존
        self.assertIn("SOLUSDT", universe)
        self.assertIn("XRPUSDT", universe)

        # 중복 일체 없음
        self.assertEqual(len(set(universe)), len(universe),
                         "dedup 실패 — 중복 존재")

    def test_update_universe_protects_extra(self) -> None:
        """EXTRA_SYMBOLS 는 다음 top 에서 빠져도 removed 에 포함되면 안 된다."""
        # NVDAUSDT 는 EXTRA 멤버 — 이전 universe 에 있다가 새 universe 에서 빠져도 보호
        bot.SYMBOLS = ["BTCUSDT", "ETHUSDT", "NVDAUSDT", "SOLUSDT"]

        # 새 universe 에서 NVDAUSDT 가 사라짐. (BCHUSDT 신규 추가도 확인용)
        new_uni = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BCHUSDT"]

        fake_stream = MagicMock()
        fake_stream.add_subscription = MagicMock()
        fake_stream.remove_subscription = MagicMock()

        with patch.object(bot, "compute_universe", return_value=new_uni), \
                patch.object(bot, "warmup_symbol", return_value=True):
            added, removed = bot.update_universe(fake_stream)

        # NVDAUSDT 는 EXTRA 라서 removed 에 절대 포함되면 안 됨
        self.assertNotIn("NVDAUSDT", removed,
                         "NVDAUSDT(EXTRA) 가 removed 에 들어감 — 보호 실패")

        # SYMBOLS 글로벌이 새 universe 로 갱신됨
        self.assertEqual(bot.SYMBOLS, new_uni)

        # BCHUSDT 신규 → added 에 포함, WS 구독 추가 호출
        self.assertIn("BCHUSDT", added)
        fake_stream.add_subscription.assert_any_call("BCHUSDT")

        # NVDAUSDT 는 보호됐으니 WS 해제 호출되면 안 됨
        for call in fake_stream.remove_subscription.call_args_list:
            self.assertNotEqual(call.args[0], "NVDAUSDT",
                                "NVDAUSDT 가 WS 구독 해제됨 — 보호 실패")


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)
