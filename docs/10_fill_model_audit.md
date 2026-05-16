# Fill Model Audit - 2026-05-17

## Scope

This audit checks whether the next-15m-candle wick entry can be executed without knowing the next candle's low/high in advance.

Data source:
- Binance USDT-M futures candles
- 29-symbol configured universe
- Local 1y cache under `tools/backtests/backtest_data`

Script:
- `tools/backtests/backtest_fill_models.py`

## Models Compared

| Model | Meaning | Executable live? |
|---|---|---|
| `old_mid_any_touch` | Previous optimistic model: any zone touch enters at midpoint even if midpoint was not touched. | No |
| `fixed_zone_high` | One aggressive limit at the expensive edge of the zone. Long = zone high, short = zone low. | Yes |
| `fixed_mid` | One limit at zone midpoint. | Yes |
| `fixed_zone_low` | One conservative limit at the best edge. Long = zone low, short = zone high. | Yes |
| `ladder_3` | Equal limits at expensive edge, midpoint, best edge. Partial fills are scaled. | Yes |
| `best_wick_oracle` | Uses next-bar wick extreme as entry price after the fact. | No |

## 29-Symbol Result

| Model | Filled | Fill% | Sum R | Avg R | Win% | Compound 1% | MaxDD 1% | Compound 3.5% | MaxDD 3.5% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `old_mid_any_touch` | 2,911 | 99.6% | +688.67R | +0.237R | 69.4% | +85,800.5% | 17.8% | +594,008,954,395.9% | 50.6% |
| `fixed_zone_high` | 2,911 | 99.6% | -714.33R | -0.245R | 46.2% | -99.9% | 99.9% | -100.0% | 100.0% |
| `fixed_mid` | 838 | 28.7% | -186.00R | -0.222R | 48.8% | -85.1% | 85.8% | -99.9% | 99.9% |
| `fixed_zone_low` | 142 | 4.9% | -18.85R | -0.133R | 53.5% | -17.7% | 19.6% | -52.2% | 55.9% |
| `ladder_3` | 2,911 | 99.6% | -306.39R | -0.105R | 48.3% | -95.5% | 95.9% | -100.0% | 100.0% |
| `best_wick_oracle` | 2,911 | 99.6% | +335.48R | +0.115R | 64.0% | +2,405.2% | 31.2% | +2,438,712.0% | 76.3% |

## Judgment

The strategy does not currently survive executable fill modeling.

The profitable results depend on either:
- `old_mid_any_touch`, which gives midpoint fills without requiring midpoint touch; or
- `best_wick_oracle`, which chooses the next candle wick price after the candle is known.

Both are invalid as live-order assumptions.

The live-compatible models are negative:
- aggressive one-shot entry: -714.33R
- midpoint one-shot entry: -186.00R
- best-edge one-shot entry: -18.85R
- 3-level ladder: -306.39R

## Practical Implication

The current bot alert text saying `limit 권장: zone_low 부근` cannot be justified by the backtest unless the live execution system can prove it consistently captures prices near the next candle wick extreme without using future information.

Before paper or live execution, the entry model should be redefined as one of:
1. fixed limit at a known price before the next candle starts,
2. pre-posted ladder with explicit levels and partial-fill accounting,
3. market/stop entry on a rule known at the time, or
4. lower-timeframe execution replay using 1m/tick data.

## Reproduction

```powershell
$env:KRTKY_SKIP_ICT_CREDS='1'
$env:TELEGRAM_BOT_TOKEN=''
$env:TELEGRAM_CHAT_IDS=''
python -X utf8 -u .\tools\backtests\backtest_fill_models.py
```

The detailed JSON output is written to:

```text
tools/backtests/backtest_data/fill_model_comparison.json
```

Raw JSON is intentionally gitignored with the rest of the large backtest cache.
