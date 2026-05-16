import json, sys
from collections import defaultdict, deque
from statistics import median, mean
from pathlib import Path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import krtky_alert_bot as bot
from backtest_realistic import (verify_realistic, verify_ideal, Collector,
                                  load, SYMBOLS, SLIPPAGE_BPS, FUNDING_PER_8H_PCT)

BOOST_KEYWORDS = {"흡수 누적":"ABSORB","다이버전스":"DIVER","SR Flip":"SRFLIP",
                  "크보나치":"KBONA","일봉 4분할":"QUART","고립 반전":"ISOSR",
                  "과매도 컨플루언스":"HTF_OS","과매수 컨플루언스":"HTF_OB",
                  "신저가 갱신":"BREAK_LO","신고가 갱신":"BREAK_HI","꼬리 50":"WICK50"}

def extract_boosts(text):
    return sorted([code for kw, code in BOOST_KEYWORDS.items() if kw in text])

# Run backtest
bot.PRE_ALERT_TIMEOUT_BARS = 8
bot.SYMBOLS = list(SYMBOLS)
bot.STATE = defaultdict(bot.SymbolState)
from rsi_alert_core import RSISymbolState
bot.RSI_STATE = defaultdict(RSISymbolState)
bot.SERIES_15M = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_15M))
bot.SERIES_4H = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_4H))
bot.SERIES_1D = defaultdict(lambda: deque(maxlen=bot.SERIES_MAX_1D))
bot.ISOLATED_SR_CACHE = defaultdict(list)
c = Collector()
bot.send_telegram = c.send

klines_cache = {}
for symbol in SYMBOLS:
    k15 = load(symbol, "15m"); k4h = load(symbol, "4h"); k1d = load(symbol, "1d")
    klines_cache[symbol] = k15
    bot.STATE[symbol] = bot.SymbolState()
    bot.RSI_STATE[symbol] = RSISymbolState()
    bot.SERIES_15M[symbol] = deque(maxlen=200); bot.SERIES_4H[symbol] = deque(maxlen=100); bot.SERIES_1D[symbol] = deque(maxlen=50)
    bot.ISOLATED_SR_CACHE[symbol] = []
    for k in k15[:50]: bot.SERIES_15M[symbol].append(k)
    for k in k4h[:8]: bot.SERIES_4H[symbol].append(k)
    for k in k1d[:10]: bot.SERIES_1D[symbol].append(k)
    last_4h_ot = bot.SERIES_4H[symbol][-1]["open_time"]
    last_1d_ot = bot.SERIES_1D[symbol][-1]["open_time"]
    i4 = sum(1 for k in k4h if k["open_time"] <= last_4h_ot)
    i1d = sum(1 for k in k1d if k["open_time"] <= last_1d_ot)
    for i in range(50, len(k15)):
        kk = k15[i]
        bot.SERIES_15M[symbol].append(kk)
        while i4 < len(k4h) and k4h[i4]["close_time"] <= kk["close_time"]:
            bot.SERIES_4H[symbol].append(k4h[i4]); i4 += 1
        while i1d < len(k1d) and k1d[i1d]["close_time"] <= kk["close_time"]:
            bot.SERIES_1D[symbol].append(k1d[i1d]); i1d += 1
        atr = bot.calc_atr(list(bot.SERIES_15M[symbol]))
        c.set(symbol=symbol, bar_idx=i, close=kk["close"], atr=atr)
        try: bot.evaluate_symbol_15m(symbol)
        except: pass

# 부스트 추출 + realistic verify
for ev in c.events:
    ev["boosts"] = extract_boosts(ev["text"])
    v = verify_realistic(ev, klines_cache[ev["symbol"]], ev["symbol"])
    if v: ev["realistic"] = v

events = [e for e in c.events if "realistic" in e]
print(f"총 진입: {len(events)}\n")

# 개별 부스트별 realistic median
by_boost = defaultdict(list)
for e in events:
    if not e["boosts"]:
        by_boost["(없음)"].append(e)
    for b in e["boosts"]:
        by_boost[b].append(e)

print("=" * 90)
print(f"{'부스트':>12} | {'N':>5} | {'MFE 평균':>9} | {'MFE 중앙':>9} | {'RR 평균':>8} | {'RR 중앙 ★':>10} | {'Win%':>6} | {'SL%':>6}")
print("-" * 90)
def rr_stats(lst):
    rrs = [e["realistic"]["rr"] for e in lst if e["realistic"]["rr"] != float("inf") and e["realistic"]["rr"] < 100]
    return rrs
def metrics(lst):
    mfes = [e["realistic"]["mfe"] for e in lst]
    maes = [e["realistic"]["mae"] for e in lst]
    rrs = rr_stats(lst)
    win = sum(1 for e in lst if e["realistic"]["mfe"] > abs(e["realistic"]["mae"]))
    sl = sum(1 for e in lst if e["realistic"]["sl_hit"])
    return {
        "n": len(lst),
        "mfe_mean": mean(mfes) if mfes else 0,
        "mfe_med": median(mfes) if mfes else 0,
        "rr_mean": mean(rrs) if rrs else 0,
        "rr_med": median(rrs) if rrs else 0,
        "win_pct": 100*win/len(lst),
        "sl_pct": 100*sl/len(lst),
    }

# 정렬: RR median 내림차순
boosts_sorted = sorted(by_boost.items(), key=lambda x: -metrics(x[1])["rr_med"])
for b, lst in boosts_sorted:
    if len(lst) < 5: continue
    m = metrics(lst)
    star = " ★" if m["rr_med"] > 1.5 else ""
    print(f"{b:>12} | {m['n']:>5} | +{m['mfe_mean']:>6.2f}% | +{m['mfe_med']:>6.2f}% | {m['rr_mean']:>7.2f} | {m['rr_med']:>9.2f}{star} | {m['win_pct']:>5.1f}% | {m['sl_pct']:>5.1f}%")
print("=" * 90)
