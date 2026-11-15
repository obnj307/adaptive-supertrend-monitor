# Adaptive SuperTrend Monitor V2 – Bitget USDT Futures

Scans the **top 50** Bitget USDT perpetual futures by 24-hour `usdtVolume` with **minimum 10 million USDT turnover**, plus **BTCUSDT, ETHUSDT, ADAUSDT, DOTUSDT** when listed, even if they fall outside the top 50. Monitors confirmed **5-minute candles**, computes ATR(10), 100-period three-cluster K-Means, factor-3 Adaptive SuperTrend, and sends Telegram messages **only on new BUY/SELL trend flips**.

## Upgrade from V1 (on your phone)

1. Replace your repository's root `bot.py` with the V2 `bot.py` from this ZIP. GitHub: open `bot.py` → pencil icon → replace all text → **Commit changes**.
2. Keep the existing `.github/workflows/monitor.yml`, `requirements.txt`, and `state/last_processed.json`. **Do not reset the existing state file**: V2 migrates the V1 BTC checkpoint automatically.
3. Your existing repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` remain unchanged.
4. GitHub → Actions → Adaptive SuperTrend Monitor → Run workflow. Open the `monitor` job → `Run monitor` or `Run bot` logs. You should see `Monitoring N markets`, then one log line per market.
5. The first run for newly added symbols **initializes** them without replaying old signals. New signals after that are sent to Telegram.

## Important limitations

- GitHub scheduled runs may be delayed; signals are not guaranteed to arrive immediately after candle close.
- Top-50 membership is recalculated on each run. Symbols newly added to the list are initialized without historical alerts.
- Only confirmed candles are used. This is a signal monitor, not an order executor.
- K-Means is recomputed from a rolling 100-ATR window; indicator agreement with TradingView must be checked on sample candles. API history length / initialization may cause differences.
- If a market fails, the workflow reports failure; successfully processed market checkpoints are retained. Retry may re-send an alert if Telegram accepted it but state was not saved.
- The `.P` suffix is for display; the API uses `BTCUSDT` and `productType=USDT-FUTURES`.
- The script needs only Python 3.11 standard library; no external packages.
