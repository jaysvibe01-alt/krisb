"""BTCUSDT 4h 실연동 검증 — 고립 반전 캔들 SR 자리 (Kris 텔레그램 03:14-15).

bot.backfill_klines("BTCUSDT", "4h", limit=30) 으로 최근 30봉을 가져와
슬라이딩 윈도우로 detect_isolated_reversal 결과를 수집한다.

사용자 제시 사례: "93,500 부근 양봉이 resistance" — 최근 30봉 (≈ 5일)
안에 들어 있다면 잡혀야 한다. BTC 가 80k 근처라면 기간 외일 수 있다.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import krtky_alert_bot as bot   # noqa: E402


def main() -> None:
    print("=" * 70)
    print("BTCUSDT 4h 고립 반전 캔들 SR 실연동 검증")
    print("=" * 70)

    try:
        klines = bot.backfill_klines("BTCUSDT", "4h", limit=30)
    except Exception as e:
        print(f"[ERROR] backfill 실패: {e}")
        return

    print(f"가져온 봉 수: {len(klines)}")
    if not klines:
        print("[ERROR] 빈 시리즈")
        return

    first_ts = klines[0]["open_time"]
    last_ts = klines[-1]["open_time"]
    print(f"기간: {bot.to_kst(first_ts)} KST ~ {bot.to_kst(last_ts)} KST")
    price_min = min(k["low"] for k in klines)
    price_max = max(k["high"] for k in klines)
    print(f"가격 범위: {bot.fmt_price(price_min)} ~ {bot.fmt_price(price_max)}")

    # 슬라이딩 윈도우로 isolated reversal 수집
    found: list[tuple[int, bot.IsolatedSR, dict]] = []
    for i in range(bot.ISOLATED_LOOKBACK + 1, len(klines) + 1):
        iso = bot.detect_isolated_reversal(klines[:i])
        if iso is not None:
            target_bar = klines[i - 1]
            # 중복 제거: 같은 봉 close_time 가 이미 found 에 있으면 skip
            if any(f[1].created_ts_ms == iso.created_ts_ms for f in found):
                continue
            found.append((i - 1, iso, target_bar))

    print(f"\n검출된 고립 반전 SR 자리 수: {len(found)}")
    print("-" * 70)
    if not found:
        print("[INFO] 30봉 안에서 고립 반전 캔들 패턴이 한 건도 안 잡혔다.")
    else:
        for idx, (bar_idx, iso, bar) in enumerate(found[:5], start=1):
            kind_kr = "저항" if iso.kind == "resistance" else "지지"
            color = "양봉" if bar["close"] > bar["open"] else "음봉"
            print(f"[{idx}] {kind_kr} @ {bot.fmt_price(iso.price)}")
            print(f"    봉 시각 (close): {bot.to_kst(bar['close_time'])} KST")
            print(f"    캔들: {color} O={bot.fmt_price(bar['open'])} "
                  f"H={bot.fmt_price(bar['high'])} "
                  f"L={bot.fmt_price(bar['low'])} "
                  f"C={bot.fmt_price(bar['close'])}")
            # 컨텍스트: 직전 8봉 음양 분포
            context = klines[max(0, bar_idx - bot.ISOLATED_LOOKBACK):bar_idx]
            n_bear = sum(1 for k in context if k["close"] < k["open"])
            n_bull = sum(1 for k in context if k["close"] > k["open"])
            print(f"    직전 8봉: 음봉 {n_bear} / 양봉 {n_bull}")
            print()

    # 93,500 케이스 점검
    print("-" * 70)
    print("[93,500 부근 케이스 점검]")
    target_price = 93500.0
    band = target_price * 0.005   # ±0.5% 범위 (대략 검색)
    near_bars = [
        (i, k) for i, k in enumerate(klines)
        if (target_price - band) <= k["high"] and k["low"] <= (target_price + band)
    ]
    if not near_bars:
        print(f"[기간 외] 최근 30봉 (가격범위 {bot.fmt_price(price_min)} ~ "
              f"{bot.fmt_price(price_max)}) 안에 93,500 ±0.5% 봉 없음.")
        if found:
            print("→ 대신 위 sanity 결과로 detect_isolated_reversal 동작 확인.")
        else:
            print("[진단] lookback=8 / streak_ratio=0.75 로는 현재 시리즈에서")
            print("       추세 비율 75% 를 못 채우는 듯. 필요 시 lookback↓ 또는")
            print("       streak_ratio↓ 검토.")
    else:
        print(f"93,500 ±0.5% 안의 봉 {len(near_bars)} 개 발견:")
        for i, k in near_bars[:3]:
            print(f"  bar#{i} {bot.to_kst(k['close_time'])} KST "
                  f"H={bot.fmt_price(k['high'])} L={bot.fmt_price(k['low'])}")
        # 그 봉이 found 에 잡혔는지
        hit_idxs = {bi for bi, _, _ in found}
        captured = [i for i, _ in near_bars if i in hit_idxs]
        if captured:
            print(f"→ 그 중 isolated reversal 로 잡힌 봉 인덱스: {captured}")
        else:
            print("[진단] 93,500 부근 봉이 시리즈에 있지만 isolated reversal 로는")
            print("       안 잡힘. 직전 8봉 추세 비율이 75% 를 못 채우거나,")
            print("       그 봉이 반대 방향이 아닐 가능성.")


if __name__ == "__main__":
    main()
