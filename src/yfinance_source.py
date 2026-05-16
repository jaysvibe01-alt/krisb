"""yfinance 기반 EXTRA 종목(주식·원자재) 시세 데이터 소스.

비트겟 토큰화 페어의 거래량/가격은 실제 NASDAQ·NYSE·KRX·NYMEX 와 어긋난다
(예: NVDA 토큰화 24h 거래량 ≈ $3K vs NASDAQ NVDA 수억 달러). 따라서
EXTRA_SYMBOLS 7개 (EWY/NVDA/TSLA/MSFT/INTC/CL/XAU) 만 본 모듈로 처리해
실제 시장 데이터로 RSI / 거래량 평가한다.

본 모듈은 시그널 전용. 자동매매에 직접 사용 금지.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import yfinance as yf


log = logging.getLogger("krtky.yfinance")

# Bitget USDT-FUTURES 페어 → yfinance 티커
YF_SYMBOL_MAP: dict[str, str] = {
    "EWYUSDT":  "EWY",      # iShares MSCI Korea ETF (NYSE Arca)
    "NVDAUSDT": "NVDA",     # NASDAQ
    "TSLAUSDT": "TSLA",     # NASDAQ
    "MSFTUSDT": "MSFT",     # NASDAQ
    "INTCUSDT": "INTC",     # NASDAQ
    "CLUSDT":   "CL=F",     # NYMEX Crude Oil 선물
    "XAUUSDT":  "GC=F",     # COMEX Gold 선물
}

# yfinance interval 매핑 — 4h 는 미지원이라 1h 후 합성
YF_INTERVAL_MAP: dict[str, str] = {
    "15m": "15m",
    "4h":  "1h",
    "1d":  "1d",
}
YF_PERIOD_MAP: dict[str, str] = {
    "15m": "5d",     # 5일 × 96봉 ≈ 480봉
    "4h":  "60d",    # 1h × 24 × 60 = 1440봉, 4개씩 묶으면 360봉
    "1d":  "100d",
}
INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "4h":  4 * 60 * 60 * 1000,
    "1d":  24 * 60 * 60 * 1000,
}


def is_yfinance_symbol(symbol: str) -> bool:
    """주어진 Bitget 심볼이 yfinance 매핑에 등록돼 있나."""
    return symbol in YF_SYMBOL_MAP


def fetch_yf_klines(symbol: str, interval_short: str, limit: int = 200) -> list[dict]:
    """yfinance 로 EXTRA 종목 봉 시리즈 가져오기.

    Bitget kline 형식과 호환되는 dict 리스트 반환:
      {open_time, open, high, low, close, volume, close_time} — 모두 ms epoch.
    Volume 은 yfinance 의 거래량 (실제 시장 share / contracts).

    interval_short == "4h" 면 1h 봉 4개씩 묶어 합성 (UTC 0/4/8/12/16/20시 정렬).
    """
    yf_sym = YF_SYMBOL_MAP.get(symbol)
    if yf_sym is None:
        raise ValueError(f"yfinance 매핑 없음: {symbol}")
    if interval_short not in YF_INTERVAL_MAP:
        raise ValueError(f"지원 안 함: {interval_short}")

    yf_iv = YF_INTERVAL_MAP[interval_short]
    period = YF_PERIOD_MAP[interval_short]
    try:
        df = yf.Ticker(yf_sym).history(period=period, interval=yf_iv, auto_adjust=False)
    except Exception as e:
        log.warning("yfinance %s %s 호출 실패: %s", yf_sym, yf_iv, e)
        return []
    if df.empty:
        log.warning("yfinance %s %s: 빈 응답 (시장 시간 외 또는 데이터 없음)",
                    yf_sym, yf_iv)
        return []

    if interval_short == "4h":
        df = _resample_to_4h(df)

    interval_ms = INTERVAL_MS[interval_short]
    rows: list[dict] = []
    for ts, row in df.iterrows():
        # tz-aware → UTC ms epoch
        if hasattr(ts, "to_pydatetime"):
            dt = ts.to_pydatetime()
        else:
            dt = ts
        open_time = int(dt.timestamp() * 1000)
        if pd.isna(row.get("Close")):
            continue
        try:
            o = float(row["Open"])
            h = float(row["High"])
            lo = float(row["Low"])
            c = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        vol = row.get("Volume", 0.0)
        vol = float(vol) if not pd.isna(vol) else 0.0
        rows.append({
            "open_time":  open_time,
            "open":       o,
            "high":       h,
            "low":        lo,
            "close":      c,
            "volume":     vol,
            "close_time": open_time + interval_ms - 1,
        })
    return rows[-limit:] if limit else rows


def _resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """1h 봉을 UTC 4시간 단위로 묶기.

    OHLC 표준 집계 — Open=first, High=max, Low=min, Close=last, Volume=sum.
    NaN 봉(시장 외 또는 결측) 은 drop.
    """
    if df_1h.empty:
        return df_1h
    df = df_1h.copy()
    agg = df.resample("4H").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Close"])
    return agg
