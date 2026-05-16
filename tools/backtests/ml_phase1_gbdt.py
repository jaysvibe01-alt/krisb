"""Phase 1 — GBDT (LightGBM) 부스트 가중치 + 변수 중요도 학습.

목적:
  1) 어떤 변수·부스트가 Win/Loss 결정에 진짜 영향 큰지 객관 확인
  2) 봇 알림 격상 로직 (현재 부스트 카운트) 의 가중치 데이터로 학습
  3) Walk-forward CV 로 overfitting 검증

데이터: 1년 4종목 백테스트 366 진입 (M2 매수존 + RSI 30/70 + timeout 8)
모델: LGBMClassifier (Win/Loss 분류) + LGBMRegressor (RR 회귀)
"""
from __future__ import annotations

import json
import sys
import warnings
from collections import defaultdict, deque
from pathlib import Path
from statistics import median, mean

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (accuracy_score, roc_auc_score, classification_report,
                              mean_absolute_error, r2_score)

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import krtky_alert_bot as bot
from rsi_alert_core import RSISymbolState
from backtest_user_model import verify_user_model, Collector, load, SYMBOLS

BOOST_KEYWORDS = {
    "흡수 누적": "ABSORB", "다이버전스": "DIVER", "SR Flip": "SRFLIP",
    "크보나치": "KBONA", "일봉 4분할": "QUART", "고립 반전": "ISOSR",
    "과매도 컨플루언스": "HTF_OS", "과매수 컨플루언스": "HTF_OB",
    "신저가 갱신": "BREAK_LO", "신고가 갱신": "BREAK_HI", "꼬리 50": "WICK50",
}


def extract_boosts(text: str) -> set[str]:
    return {code for kw, code in BOOST_KEYWORDS.items() if kw in text}


def build_dataset() -> pd.DataFrame:
    """봇 백테스트 → DataFrame (X + y) 변환."""
    bot.PRE_ALERT_TIMEOUT_BARS = 8
    bot.SYMBOLS = list(SYMBOLS)
    bot.STATE = defaultdict(bot.SymbolState)
    bot.RSI_STATE = defaultdict(RSISymbolState)
    bot.SERIES_15M = defaultdict(lambda: deque(maxlen=200))
    bot.SERIES_4H = defaultdict(lambda: deque(maxlen=100))
    bot.SERIES_1D = defaultdict(lambda: deque(maxlen=50))
    bot.ISOLATED_SR_CACHE = defaultdict(list)
    c = Collector()
    bot.send_telegram = c.send

    klines_cache = {}
    for symbol in SYMBOLS:
        k15 = load(symbol, "15m"); k4h = load(symbol, "4h"); k1d = load(symbol, "1d")
        klines_cache[symbol] = k15
        bot.STATE[symbol] = bot.SymbolState()
        bot.RSI_STATE[symbol] = RSISymbolState()
        bot.SERIES_15M[symbol] = deque(maxlen=200)
        bot.SERIES_4H[symbol] = deque(maxlen=100)
        bot.SERIES_1D[symbol] = deque(maxlen=50)
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
            rsi_15m = bot.calc_rsi([x["close"] for x in bot.SERIES_15M[symbol]])
            rsi_4h = bot.calc_rsi([x["close"] for x in bot.SERIES_4H[symbol]]) if bot.SERIES_4H[symbol] else 50
            candle = bot.analyze_candle(kk, atr)
            vol_ok, vol_msg = bot.is_volume_significant(symbol, list(bot.SERIES_15M[symbol]))
            # vol_multiple 추출
            try:
                vol_mult = float(vol_msg.split("× SMA")[0].split("(")[-1].split("절대 ")[-1].split(", ")[-1].strip())
            except:
                vol_mult = 0
            c.set(
                symbol=symbol, bar_idx=i, close=kk["close"], atr=atr,
                bar_ts=kk["close_time"],
                rsi_15m=rsi_15m, rsi_4h=rsi_4h,
                body_to_range=candle.body_to_range,
                body_to_atr=candle.body_to_atr,
                vol_abs=kk["volume"],
                vol_mult=vol_mult,
                upper_wick_50=int(candle.upper_wick_50),
                lower_wick_50=int(candle.lower_wick_50),
            )
            try: bot.evaluate_symbol_15m(symbol)
            except: pass

    # 라벨링 — verify 후 MFE/MAE/RR
    rows = []
    for ev in c.events:
        v = verify_user_model(ev, klines_cache[ev["symbol"]], ev["symbol"], realistic=True)
        if not v or not v.get("entered"):
            continue
        boosts = extract_boosts(ev["text"])
        rows.append({
            "symbol": ev["symbol"],
            "direction": ev["direction"],
            "bar_ts": ev["bar_ts"],
            "level": ev["level"],
            # features
            "rsi_15m": ev["rsi_15m"],
            "rsi_4h": ev["rsi_4h"],
            "body_to_range": ev["body_to_range"],
            "body_to_atr": ev["body_to_atr"],
            "vol_mult": ev["vol_mult"],
            "vol_abs": ev["vol_abs"],
            "atr": ev["atr"],
            "lower_wick_50": ev["lower_wick_50"],
            "upper_wick_50": ev["upper_wick_50"],
            # boosts (binary)
            **{f"b_{b}": int(b in boosts) for b in BOOST_KEYWORDS.values()},
            "n_boosts": len(boosts),
            # labels
            "mfe": v["mfe"],
            "mae": v["mae"],
            "sl_hit": int(v["sl_hit"]),
            "rr": v["rr"] if v["rr"] != float("inf") and v["rr"] < 100 else 100,
            "tp1_hit": int(v["tp1_hit"]),
            "tp2_hit": int(v["tp2_hit"]),
            "win": int(v["mfe"] > abs(v["mae"])),   # MFE > |MAE| = win
        })
    return pd.DataFrame(rows)


def train_classifier(df: pd.DataFrame, target: str = "win") -> dict:
    """LGBM Classifier — Win/Loss 또는 TP1 hit 예측."""
    feat_cols = [c for c in df.columns if c not in
                 ("symbol", "direction", "bar_ts", "level", "mfe", "mae", "rr",
                  "sl_hit", "tp1_hit", "tp2_hit", "win")]
    # one-hot
    df_x = pd.get_dummies(df[feat_cols + ["symbol", "direction"]],
                          columns=["symbol", "direction"], dtype=int)
    y = df[target]

    # 시계열 sort
    df_sorted = df.sort_values("bar_ts")
    sorted_idx = df_sorted.index
    df_x = df_x.loc[sorted_idx]
    y = y.loc[sorted_idx]

    # Walk-forward CV (5-fold time-series)
    tscv = TimeSeriesSplit(n_splits=5)
    aucs, accs = [], []
    for fold, (tr, te) in enumerate(tscv.split(df_x)):
        Xtr, Xte = df_x.iloc[tr], df_x.iloc[te]
        ytr, yte = y.iloc[tr], y.iloc[te]
        m = lgb.LGBMClassifier(n_estimators=200, max_depth=5,
                                num_leaves=15, learning_rate=0.05,
                                min_data_in_leaf=10, verbose=-1)
        m.fit(Xtr, ytr)
        pred = m.predict(Xte)
        proba = m.predict_proba(Xte)[:, 1]
        accs.append(accuracy_score(yte, pred))
        try: aucs.append(roc_auc_score(yte, proba))
        except: aucs.append(0.5)

    # 전체 학습 (feature importance)
    m_final = lgb.LGBMClassifier(n_estimators=200, max_depth=5,
                                  num_leaves=15, learning_rate=0.05,
                                  min_data_in_leaf=10, verbose=-1)
    m_final.fit(df_x, y)
    imp = pd.DataFrame({
        "feature": df_x.columns,
        "importance": m_final.feature_importances_,
    }).sort_values("importance", ascending=False)

    # baseline (단순 다수결)
    baseline_acc = max(y.mean(), 1 - y.mean())

    return {
        "target": target,
        "n_samples": len(df),
        "n_features": len(df_x.columns),
        "positive_rate": float(y.mean()),
        "baseline_acc": float(baseline_acc),
        "cv_acc_mean": float(np.mean(accs)),
        "cv_acc_std": float(np.std(accs)),
        "cv_auc_mean": float(np.mean(aucs)),
        "cv_auc_std": float(np.std(aucs)),
        "improvement_over_baseline": float(np.mean(accs) - baseline_acc),
        "top_features": imp.head(15).to_dict(orient="records"),
    }


def train_regressor(df: pd.DataFrame) -> dict:
    """LGBM Regressor — RR 회귀."""
    feat_cols = [c for c in df.columns if c not in
                 ("symbol", "direction", "bar_ts", "level", "mfe", "mae", "rr",
                  "sl_hit", "tp1_hit", "tp2_hit", "win")]
    df_x = pd.get_dummies(df[feat_cols + ["symbol", "direction"]],
                          columns=["symbol", "direction"], dtype=int)
    y = df["rr"].clip(0, 10)   # outlier cap

    df_sorted = df.sort_values("bar_ts")
    sorted_idx = df_sorted.index
    df_x = df_x.loc[sorted_idx]
    y = y.loc[sorted_idx]

    tscv = TimeSeriesSplit(n_splits=5)
    maes, r2s = [], []
    for tr, te in tscv.split(df_x):
        Xtr, Xte = df_x.iloc[tr], df_x.iloc[te]
        ytr, yte = y.iloc[tr], y.iloc[te]
        m = lgb.LGBMRegressor(n_estimators=200, max_depth=5,
                               num_leaves=15, learning_rate=0.05,
                               min_data_in_leaf=10, verbose=-1)
        m.fit(Xtr, ytr)
        pred = m.predict(Xte)
        maes.append(mean_absolute_error(yte, pred))
        r2s.append(r2_score(yte, pred))

    m_final = lgb.LGBMRegressor(n_estimators=200, max_depth=5,
                                 num_leaves=15, learning_rate=0.05,
                                 min_data_in_leaf=10, verbose=-1)
    m_final.fit(df_x, y)
    imp = pd.DataFrame({
        "feature": df_x.columns,
        "importance": m_final.feature_importances_,
    }).sort_values("importance", ascending=False)

    return {
        "target": "rr",
        "rr_mean": float(y.mean()),
        "rr_std": float(y.std()),
        "cv_mae_mean": float(np.mean(maes)),
        "cv_r2_mean": float(np.mean(r2s)),
        "top_features": imp.head(15).to_dict(orient="records"),
    }


def main() -> int:
    print("=== Phase 1 — GBDT (LightGBM) 학습 ===\n")
    print("데이터셋 구축 중...")
    df = build_dataset()
    print(f"진입 데이터셋: {len(df)} rows × {len(df.columns)} cols")
    print(f"  Win rate: {df['win'].mean()*100:.1f}%  /  TP1 hit: {df['tp1_hit'].mean()*100:.1f}%")
    print(f"  RR mean: {df['rr'].clip(0,10).mean():.2f}  median: {df['rr'].clip(0,10).median():.2f}")
    print(f"  Symbol 분포: {dict(df['symbol'].value_counts())}\n")

    # 1) Win/Loss 분류
    print("=" * 70)
    print("【분류 1: Win (MFE > |MAE|) 예측】")
    print("=" * 70)
    win_res = train_classifier(df, "win")
    print(f"표본: {win_res['n_samples']} · positive rate: {win_res['positive_rate']*100:.1f}%")
    print(f"Baseline accuracy: {win_res['baseline_acc']*100:.1f}% (단순 다수결)")
    print(f"5-fold CV accuracy: {win_res['cv_acc_mean']*100:.1f}% ± {win_res['cv_acc_std']*100:.1f}%")
    print(f"5-fold CV AUC:      {win_res['cv_auc_mean']*100:.1f}% ± {win_res['cv_auc_std']*100:.1f}%")
    print(f"개선폭: {(win_res['cv_acc_mean']-win_res['baseline_acc'])*100:+.1f}%p")
    print("\nTop 15 Feature Importance (Win 예측):")
    print(f"  {'순위':>3} | {'Feature':>20} | {'중요도':>7}")
    for i, f in enumerate(win_res["top_features"][:15], 1):
        print(f"  {i:>3} | {f['feature']:>20} | {f['importance']:>7}")

    # 2) TP1 hit 분류
    print("\n" + "=" * 70)
    print("【분류 2: TP1 hit (MFE >= 1.236%) 예측】")
    print("=" * 70)
    tp_res = train_classifier(df, "tp1_hit")
    print(f"Positive rate: {tp_res['positive_rate']*100:.1f}%")
    print(f"Baseline accuracy: {tp_res['baseline_acc']*100:.1f}%")
    print(f"5-fold CV accuracy: {tp_res['cv_acc_mean']*100:.1f}% ± {tp_res['cv_acc_std']*100:.1f}%")
    print(f"5-fold CV AUC:      {tp_res['cv_auc_mean']*100:.1f}% ± {tp_res['cv_auc_std']*100:.1f}%")
    print(f"개선폭: {(tp_res['cv_acc_mean']-tp_res['baseline_acc'])*100:+.1f}%p")
    print("\nTop 10 Feature Importance (TP1 hit 예측):")
    for i, f in enumerate(tp_res["top_features"][:10], 1):
        print(f"  {i:>3} | {f['feature']:>20} | {f['importance']:>7}")

    # 3) RR 회귀
    print("\n" + "=" * 70)
    print("【회귀: RR 예측】")
    print("=" * 70)
    rr_res = train_regressor(df)
    print(f"RR mean: {rr_res['rr_mean']:.2f} ± {rr_res['rr_std']:.2f}")
    print(f"5-fold CV MAE: {rr_res['cv_mae_mean']:.3f}")
    print(f"5-fold CV R²:  {rr_res['cv_r2_mean']:.3f}  (1.0=완전, 0.0=평균만, 음수=평균보다 못함)")

    # 결과 저장
    out = {
        "n_samples": len(df), "n_features": len(df.columns),
        "win_classifier": win_res,
        "tp1_classifier": tp_res,
        "rr_regressor": rr_res,
    }
    out_path = ROOT / "backtest_data" / "ml_phase1_gbdt.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")

    # CSV 데이터셋 저장 (재사용)
    csv_path = ROOT / "backtest_data" / "ml_dataset.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n저장:")
    print(f"  {out_path}")
    print(f"  {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
