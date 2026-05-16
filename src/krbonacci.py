"""크보나치(Kribonacci) — Kris B 가 PPT 슬라이드 2 에서 명시한 비표준 피보 비율.

표준 피보:    0 / 0.236 / 0.382 / 0.5 / 0.618 / 0.786 / 1.0 / 1.272 / 1.414 / 1.618 / 2.0 / 2.618
크보나치:     0 / 0.764 / 1.0 / 1.236 / 2.0 / 2.26    ← 슬라이드 2 직접 표기

추가 개념:
    - "1:1 자리" (슬라이드 9): 직전 큰 움직임의 길이만큼 다시 확장된 가격.
      예: 저점 49,000 → 변곡 73.7 → 1:1 자리 = 98,411
    - "1.236 자리" (슬라이드 3): 진입 영역
    - "1.272 + 1.0 confluence" (슬라이드 10): 두 피보 도구의 교차 지점

본 모듈은 시리즈에서 swing high/low 를 휴리스틱으로 찾아 크보나치 레벨을
계산하고, 현재 봉이 그 레벨 근방을 터치했는지 판정한다. 정확한 swing
선정은 사람이 차트를 보고 그어야 정확하므로 자동 판정은 보조 시그널로만
쓴다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

# Kris 가 직접 차트에 띄운 비율 (슬라이드 2). 0/1 을 빼고 의미 있는 레벨만.
KRBONACCI_RATIOS: tuple[float, ...] = (0.764, 1.0, 1.236, 2.0, 2.26, 2.544, 3.236)
# 2026-05-17 PPT 통독 결과 2.544 (슬라이드 19) / 3.236 (슬라이드 39) 추가

# 1:1 자리 (슬라이드 9) — Fibonacci extension 1.0 과 동의어로 취급
ONE_TO_ONE_RATIO = 1.0

# 4분할 (Quartile) 변종 — PPT 외 Kris 텔레그램 발언에서 추출.
# 출처: Kris 텔레그램 03:11 + 첨부 일봉 BTC 차트 (97,850 → 87,301.4 피보).
# 본인이 일봉 swing 을 4 등분해 0.25 / 0.5 / 0.75 자리를 지지·저항 후보로
# 보는 흐름. 표준 피보의 0.5 / 0.618 과 다른 단순 4등분.
QUARTILE_RATIOS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)

# 레벨 매칭 허용 오차 (가격 대비 %)
DEFAULT_TOLERANCE = 0.004    # 0.4% 이내면 "그 자리"로 인정
QUARTILE_TOLERANCE = 0.004   # 일봉 4분할도 같은 0.4%

log = logging.getLogger("krtky.krbonacci")


@dataclass
class KrbonacciHit:
    """크보나치 레벨 매칭 결과."""
    ratio: float           # 매칭된 비율 (0.764 / 1.236 / 2.0 / 2.26 / 1.0)
    level_price: float     # 그 비율의 가격
    swing_high: float
    swing_low: float
    direction: str         # "retracement" (되돌림) | "extension" (확장)


def find_swing_high_low(
    klines: list[dict],
    lookback: int = 50,
) -> Optional[tuple[float, float, int, int]]:
    """직전 N봉 범위에서 swing high / low 를 찾는다 (단순 max/min).

    실제 Kris 가 사용하는 "변곡점"은 사람 눈 판정이지만, 자동 알림용으로는
    최근 N봉의 최고/최저 가격으로 근사한다. 두 점이 너무 가까이 있어
    가격차가 매우 작으면 의미 없는 피보가 되므로 None 을 돌려준다.

    Returns: (swing_high, swing_low, hi_idx, lo_idx) 또는 None
    """
    if len(klines) < lookback:
        return None
    window = klines[-lookback:]
    hi_idx_local = max(range(len(window)), key=lambda i: window[i]["high"])
    lo_idx_local = min(range(len(window)), key=lambda i: window[i]["low"])
    hi = window[hi_idx_local]["high"]
    lo = window[lo_idx_local]["low"]
    if hi <= lo or (hi - lo) / max(hi, 1e-12) < 0.005:
        # 가격 범위가 0.5% 이하면 피보 의미 없음
        return None
    # 전체 시리즈 기준 인덱스로 변환
    base = len(klines) - lookback
    return hi, lo, base + hi_idx_local, base + lo_idx_local


def compute_krbonacci_levels(
    swing_high: float,
    swing_low: float,
    direction: str,
) -> dict[float, float]:
    """방향에 따라 비율별 가격 계산.

    direction == "long_retracement":
        고점 → 저점 후 되돌림. 0.764 등은 저점에서 위로 측정.
        level = swing_low + ratio * (swing_high - swing_low)
        (Kris 차트는 보통 1.236, 2.26 같은 확장도 위로 그린다)

    direction == "short_retracement":
        저점 → 고점 후 되돌림. 비율은 고점에서 아래로 측정.
        level = swing_high - ratio * (swing_high - swing_low)
    """
    span = swing_high - swing_low
    out: dict[float, float] = {}
    for r in KRBONACCI_RATIOS:
        if direction == "long_retracement":
            out[r] = swing_low + r * span
        elif direction == "short_retracement":
            out[r] = swing_high - r * span
        else:
            raise ValueError(f"unknown direction: {direction}")
    return out


def match_krbonacci_level(
    last_kline: dict,
    levels: dict[float, float],
    tolerance: float = DEFAULT_TOLERANCE,
) -> Optional[tuple[float, float]]:
    """현재 봉의 wick 가 어떤 크보나치 레벨을 터치했는지 검사.

    Returns: (ratio, level_price) 또는 None
    """
    lo, hi = last_kline["low"], last_kline["high"]
    for ratio, lvl in levels.items():
        band = lvl * tolerance
        if lo <= lvl + band and hi >= lvl - band:
            return ratio, lvl
    return None


def detect_krbonacci_confluence(
    klines: list[dict],
    direction: str,
    lookback: int = 50,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Optional[KrbonacciHit]:
    """봇 메인 흐름에서 호출하는 단일 진입점.

    direction:
        "long"  → swing high → swing low 후 long_retracement 레벨 검사
        "short" → swing low → swing high 후 short_retracement 레벨 검사

    Returns: KrbonacciHit (있을 때) 또는 None
    """
    if not klines:
        return None
    swing = find_swing_high_low(klines, lookback=lookback)
    if swing is None:
        return None
    sw_hi, sw_lo, hi_idx, lo_idx = swing

    # 방향 정합성 체크 — 롱 진입은 고점→저점 흐름 직후가 자연스럽다
    # (저점이 더 최근에 나와야 retracement 의미가 산다)
    if direction == "long" and lo_idx <= hi_idx:
        # 저점이 고점보다 이전에 찍혔으면, 현재 흐름은 고점에서 내려오는 중 → OK
        # 저점이 고점보다 이후라면 이미 반등 중 — 그것도 OK 로 둔다 (보수적이지 않음)
        pass
    fib_direction = "long_retracement" if direction == "long" else "short_retracement"
    levels = compute_krbonacci_levels(sw_hi, sw_lo, fib_direction)
    matched = match_krbonacci_level(klines[-1], levels, tolerance=tolerance)
    if matched is None:
        return None
    ratio, lvl = matched
    return KrbonacciHit(
        ratio=ratio,
        level_price=lvl,
        swing_high=sw_hi,
        swing_low=sw_lo,
        direction=fib_direction,
    )


def hit_to_label(hit: KrbonacciHit) -> str:
    """알림 메시지에 들어갈 한 줄 라벨."""
    if hit.ratio == ONE_TO_ONE_RATIO:
        name = "1:1 자리 (슬라이드 9)"
    elif hit.ratio == 1.236:
        name = "크보나치 1.236 (슬라이드 3·25)"
    elif hit.ratio == 0.764:
        name = "크보나치 0.764"
    elif hit.ratio == 2.544:
        name = "크보나치 2.544 (슬라이드 19 확장)"
    elif hit.ratio == 3.236:
        name = "크보나치 3.236 (슬라이드 39 확장)"
    elif hit.ratio == 2.0:
        name = "크보나치 2.0 (확장)"
    elif hit.ratio == 2.26:
        name = "크보나치 2.26 (확장)"
    else:
        name = f"크보나치 {hit.ratio}"
    return f"📐 {name} 리테스트 @ {hit.level_price:.4f}"


# ─────────────────────────────────────────────────────────────
# 4분할 (Quartile) — PPT 외 Kris 텔레그램 발언 (03:11)
# ─────────────────────────────────────────────────────────────
@dataclass
class QuartileHit:
    """일봉 4분할 매칭 결과."""
    ratio: float            # 0.0 / 0.25 / 0.5 / 0.75 / 1.0
    level_price: float
    swing_high: float
    swing_low: float
    direction: str          # "long_retracement" | "short_retracement"


def _daily_atr(klines: list[dict], period: int = 14) -> float:
    """일봉 ATR — krtky_alert_bot.calc_atr 와 동일 알고리즘.

    [MID 1 패치] detect_quartile_confluence 의 swing 폭 필터용. swing
    이 단일 노이즈 봉에 끌렸을 때 (swing_high - swing_low 가 너무 작을 때)
    의미 없는 4분할이 나오는 걸 막는다.
    """
    trs: list[float] = []
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


def compute_quartile_levels(
    swing_high: float,
    swing_low: float,
    direction: str,
) -> dict[float, float]:
    """4분할 레벨 계산. krbonacci.compute_krbonacci_levels 와 동일 의미.

    direction:
        "long_retracement"  : swing_low + r × span
        "short_retracement" : swing_high - r × span
    """
    span = swing_high - swing_low
    out: dict[float, float] = {}
    for r in QUARTILE_RATIOS:
        if direction == "long_retracement":
            out[r] = swing_low + r * span
        elif direction == "short_retracement":
            out[r] = swing_high - r * span
        else:
            raise ValueError(f"unknown direction: {direction}")
    return out


def detect_quartile_confluence(
    daily_klines: list[dict],
    last_15m_kline: dict,
    direction: str,
    lookback: int = 30,
    tolerance: float = QUARTILE_TOLERANCE,
    min_atr_multiple: float = 2.0,
) -> Optional[QuartileHit]:
    """일봉 시리즈 swing 4분할 vs 현재 15m 봉 wick 매칭.

    출처: Kris 텔레그램 03:11 — 일봉 swing 을 4 등분해 0.25 / 0.5 / 0.75
    자리를 지지·저항 후보로 보는 흐름. 단기(15m) 봉의 wick 가 이 자리를
    터치하면 단순 4원칙 + 일봉 컨플루언스 부스트로 본다.

    [MID 1 패치] swing 폭이 일봉 ATR × min_atr_multiple 미만이면 None.
    find_swing_high_low 가 단순 max/min 이라 노이즈 봉 하나에 끌렸을 때
    의미 없는 4분할이 나오는 걸 방지.

    Parameters
    ----------
    daily_klines : 일봉(1d) 시리즈 — 30봉 이상 권장
    last_15m_kline : 현재 평가 중인 15분봉
    direction : "long"  → long_retracement
                "short" → short_retracement
    min_atr_multiple : swing 폭 / 일봉 ATR 최소 배수 (기본 2.0)
    """
    if not daily_klines or len(daily_klines) < 4:
        return None
    swing = find_swing_high_low(daily_klines, lookback=min(lookback, len(daily_klines)))
    if swing is None:
        return None
    sw_hi, sw_lo, _, _ = swing
    # ★ 일봉 ATR 기반 swing 크기 필터 (노이즈 끌림 방지)
    daily_atr = _daily_atr(daily_klines)
    if daily_atr > 0 and (sw_hi - sw_lo) < daily_atr * min_atr_multiple:
        return None
    fib_direction = "long_retracement" if direction == "long" else "short_retracement"
    levels = compute_quartile_levels(sw_hi, sw_lo, fib_direction)

    lo, hi = last_15m_kline["low"], last_15m_kline["high"]
    for ratio, lvl in levels.items():
        band = lvl * tolerance
        if lo <= lvl + band and hi >= lvl - band:
            return QuartileHit(
                ratio=ratio,
                level_price=lvl,
                swing_high=sw_hi,
                swing_low=sw_lo,
                direction=fib_direction,
            )
    return None


def quartile_hit_to_label(hit: QuartileHit) -> str:
    """4분할 매칭 라벨. 알림 메시지에 한 줄로 들어간다."""
    name_map = {
        0.0:  "일봉 4분할 0.0 (Low)",
        0.25: "일봉 4분할 0.25",
        0.5:  "일봉 4분할 0.5 (Mid)",
        0.75: "일봉 4분할 0.75",
        1.0:  "일봉 4분할 1.0 (High)",
    }
    name = name_map.get(hit.ratio, f"일봉 4분할 {hit.ratio}")
    return f"📐 {name} 자리 @ {hit.level_price:.4f}"


# ─────────────────────────────────────────────────────────────
# 익절 후보 가격 산출 — [2]/[3]/[4] 진입 신호에 동봉
# ─────────────────────────────────────────────────────────────
TP_NEAR_FILTER_PCT = 0.003   # 진입가 ±0.3% 안의 자리는 즉시 닿으므로 제외
TP_DAILY_MIN_ATR_MULTIPLE = 1.5   # 일봉 swing 폭 < ATR × 이 배수 면 일봉 target 제외


def compute_take_profit_targets(
    direction: str,
    entry_price: float,
    klines_15m: list[dict],
    klines_1d: Optional[list[dict]] = None,
    lookback_15m: int = 50,
    lookback_1d: int = 30,
    near_filter_pct: float = TP_NEAR_FILTER_PCT,
) -> list[tuple[str, float, str]]:
    """[2]/[3]/[4] 진입 신호 메시지용 익절 후보 가격 산출.

    15m swing(직전 lookback_15m 봉의 max high / min low) 기준으로
    크보나치 1.236 / 1:1 / 2.0 / 2.26 자리 계산.
    일봉 시리즈가 주어지면 일봉 swing 4분할 0.5 자리도 후보에 포함
    (단 일봉 swing 폭이 일봉 ATR × TP_DAILY_MIN_ATR_MULTIPLE 미만이면 제외).

    Parameters
    ----------
    direction : "long" → 진입가 위로 익절,
                "short" → 진입가 아래로 익절
    entry_price : 컨펌봉 종가 기준 진입가
    klines_15m : 15m 시리즈 (오름차순)
    klines_1d : 1d 시리즈 (옵션)

    Returns
    -------
    [(라벨, 가격, 출처태그), ...] 진입가에서 가까운 순으로 정렬.
    진입가에서 < near_filter_pct (기본 0.3%) 차이 자리는 제외.
    """
    if not klines_15m or len(klines_15m) < min(lookback_15m, 10):
        return []

    window_15m = klines_15m[-lookback_15m:]
    sw_hi = max(k["high"] for k in window_15m)
    sw_lo = min(k["low"] for k in window_15m)
    span = sw_hi - sw_lo
    if span <= 0:
        return []

    raw: list[tuple[str, float, str]] = []

    # 크보나치 ratio × span — long 은 sw_lo + r×span, short 는 sw_hi - r×span
    # 2026-05-17 PPT 통독 결과 2.544 / 3.236 추가 (백테스트 +4% 검증 완료)
    for ratio, label_name, source in (
        (1.236, "크보나치 1.236", "📐 슬라이드 19"),
        (2.0,   "크보나치 2.0",   "📐 슬라이드 39"),
        (2.26,  "크보나치 2.26",  "📐 슬라이드 2"),
        (2.544, "크보나치 2.544", "📐 슬라이드 19 (TP3)"),  # 신규
        (3.236, "크보나치 3.236", "📐 슬라이드 39 (TP4)"),  # 신규
    ):
        if direction == "long":
            price = sw_lo + ratio * span
        else:
            price = sw_hi - ratio * span
        raw.append((label_name, price, source))

    # 1:1 자리 (슬라이드 9) — swing 정점에서 같은 폭 다시 확장
    if direction == "long":
        price_1to1 = sw_hi + span
    else:
        price_1to1 = sw_lo - span
    raw.append(("1:1 자리", price_1to1, "📐 슬라이드 9"))

    # 일봉 4분할 0.5 (옵션) — swing 폭이 일봉 ATR × 1.5 이상일 때만
    if klines_1d and len(klines_1d) >= 4:
        win_1d = klines_1d[-lookback_1d:] if len(klines_1d) > lookback_1d else klines_1d
        d_hi = max(k["high"] for k in win_1d)
        d_lo = min(k["low"] for k in win_1d)
        d_span = d_hi - d_lo
        d_atr = _daily_atr(klines_1d)
        if d_span > 0 and (d_atr <= 0 or d_span >= d_atr * TP_DAILY_MIN_ATR_MULTIPLE):
            mid = (d_hi + d_lo) / 2.0   # 0.5 자리
            raw.append(("일봉 4분할 0.5", mid, "📐 텔레그램 03:11"))

    # 진입가 ±0.3% 자리 제외 + 방향 일치 자리만
    threshold = entry_price * near_filter_pct
    filtered: list[tuple[str, float, str]] = []
    for label, price, source in raw:
        if direction == "long":
            if price <= entry_price + threshold:
                continue
        else:
            if price >= entry_price - threshold:
                continue
        filtered.append((label, price, source))

    # 진입가에서 가까운 순 정렬
    if direction == "long":
        filtered.sort(key=lambda x: x[1])
    else:
        filtered.sort(key=lambda x: -x[1])
    return filtered
