# Backtest Tools

These scripts were copied from the research workspace at `C:\전략분석\크트키_분석\krtky_bot`.

They are kept as validation and research utilities. Most scripts expect local candle/result cache files under a sibling `backtest_data` directory, so copy or regenerate data locally before running them. Secrets, logs, and raw large cache files are not committed.

Run from the repository root with `src` and this folder on `PYTHONPATH`, for example:

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\tools\backtests"
python .\tools\backtests\backtest_universe_expand.py
```
