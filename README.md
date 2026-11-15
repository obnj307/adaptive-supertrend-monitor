# Adaptive SuperTrend Telegram Monitor (BTCUSDT.P)

Python reproduction of the AlgoAlpha Machine Learning Adaptive SuperTrend (Pine v5), using Bitget **USDT-FUTURES BTCUSDT** 5-minute candles. Alerts only on confirmed candle trend flips. No trading permissions or orders.

## Install on GitHub (phone-friendly)

1. Create a **private** GitHub repository, e.g. `adaptive-supertrend-monitor`.
2. Upload `bot.py`, `README.md`, `state/last_processed.json`, and `.github/workflows/monitor.yml` preserving the folder structure. If GitHub's mobile upload hides dotfolders, create `.github/workflows/monitor.yml` with **Add file → Create new file** and type the full path.
3. In **Settings → Secrets and variables → Actions**, create repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never commit them into files.
4. In **Settings → Actions → General → Workflow permissions**, select **Read and write permissions** (if allowed by repository settings).
5. In **Actions → Adaptive SuperTrend Monitor → Run workflow**, run once to initialize the checkpoint. The first run intentionally sends no old signal.
6. The workflow then runs on cron at minute 1, 6, 11, ... UTC. Scheduled GitHub jobs may run late or occasionally be skipped. It catches up on confirmed candles in the available 200-candle window when it resumes.
7. Check Actions logs for `State saved`; check `state/last_processed.json` changes after runs. Only BUY/SELL flips generate Telegram messages.

## Important details

- Bitget public API: `/api/v2/mix/market/candles`, `symbol=BTCUSDT`, `productType=USDT-FUTURES`, `granularity=5m`. Bitget displays this perpetual as BTCUSDT.P in chart interfaces.
- Only candles whose start time + 5 minutes is at least 1.5 seconds behind the local system clock are used. The workflow's 1-minute offset helps avoid API publication delays.
- ATR uses Wilder's RMA with an SMA seed. K-Means re-clusters the trailing 100 ATR values on every bar; the selected cluster centroid feeds the SuperTrend.
- Pine-to-Python equivalence has **not yet been validated** against TradingView bar-by-bar. Rare tie/empty-cluster behavior and ATR initialization can differ. Do not use this as a verified trading strategy.
- If K-Means cannot be safely calculated, the job fails rather than silently producing misleading alerts.
- If Telegram fails, checkpoint is not advanced. In rare partial failures (e.g., multiple catch-up alerts), a previously sent message may be repeated on retry.
- No external dependencies beyond Python standard library.
- GitHub Actions schedule is not real-time; delays are possible. Default branch is required for scheduled workflows.
- Uses the original AlgoAlpha code as conceptual reference; the Python code is a new implementation. Original Pine Script © AlgoAlpha, Mozilla Public License 2.0.
