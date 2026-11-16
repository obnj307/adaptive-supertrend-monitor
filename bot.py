
#!/usr/bin/env python3
"""Bitget USDT perpetual M5 monitor: Adaptive SuperTrend + Range Filter.

V3.1 fixes:
- Robust K-means clustering with deterministic tie handling.
- Controlled recovery from checkpoints older than available history.
- Explicit warnings for unrecoverable monitoring gaps.
- Independent indicator errors and checkpoint handling.
Python 3.11, standard library only.
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = 'https://api.bitget.com/api/v2/mix/market'
PRODUCT_TYPE = 'USDT-FUTURES'
INTERVAL_MS = 300_000

ATR_LENGTH = 10
TRAINING_LENGTH = 100
FACTOR = 3.0

TOP_N = 50
MIN_TURNOVER_USDT = 10_000_000
PINNED = ('BTCUSDT', 'ETHUSDT', 'ADAUSDT', 'DOTUSDT')

RANGE_PERIOD = 100
RANGE_MULTIPLIER = 3.0

STATE_FILE = Path('state/last_processed.json')
LOG = logging.getLogger('supertrend')


def get_json(url, headers=None, data=None):
    for attempt in range(4):
        try:
            request = Request(
                url,
                data=data,
                headers=headers or {
                    'User-Agent': 'AdaptiveSuperTrendMonitor/3.1'
                }
            )
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def bitget(path, params):
    result = get_json(
        API_ROOT + path + '?' + urlencode(params)
    )
    if result.get('code') != '00000':
        raise RuntimeError(
            f'Bitget {path}: '
            f'{result.get("msg", result.get("code"))}'
        )
    return result.get('data') or []


def select_symbols():
    tickers = bitget(
        '/tickers',
        {'productType': PRODUCT_TYPE}
    )

    ranked = []
    available = set()

    for item in tickers:
        symbol = str(item.get('symbol', '')).upper()

        if not symbol.endswith('USDT'):
            continue

        available.add(symbol)

        raw = item.get('usdtVolume')
        if raw is None or raw == '':
            LOG.warning(
                'Missing usdtVolume for %s; skipping ranking',
                symbol
            )
            continue

        try:
            turnover = float(raw)
        except (TypeError, ValueError):
            continue

        if (
            math.isfinite(turnover)
            and turnover >= MIN_TURNOVER_USDT
        ):
            ranked.append((symbol, turnover))

    ranked.sort(key=lambda x: x[1], reverse=True)

    selected = [
        symbol for symbol, _ in ranked[:TOP_N]
    ]

    for symbol in PINNED:
        if symbol in available and symbol not in selected:
            selected.append(symbol)
        elif symbol not in available:
            LOG.warning(
                'Pinned future not listed: %s',
                symbol
            )

    if not selected:
        raise RuntimeError(
            'No eligible USDT futures found'
        )

    LOG.info(
        'Monitoring %d markets, first: %s',
        len(selected),
        ', '.join(selected[:10])
    )

    return selected


def fetch_candles(symbol):
    rows = bitget(
        '/candles',
        {
            'symbol': symbol,
            'productType': PRODUCT_TYPE,
            'granularity': '5m',
            'limit': '200'
        }
    )

    now_ms = int(time.time() * 1000)
    by_time = {}

    for row in rows:
        stamp = int(row[0])

        if stamp + INTERVAL_MS <= now_ms - 1500:
            by_time[stamp] = {
                'time': stamp,
                'high': float(row[2]),
                'low': float(row[3]),
                'close': float(row[4])
            }

    candles = [
        by_time[k] for k in sorted(by_time)
    ]

    if len(candles) < TRAINING_LENGTH + ATR_LENGTH + 3:
        raise RuntimeError(
            f'{symbol}: only {len(candles)} confirmed candles'
        )

    if any(
        b['time'] - a['time'] != INTERVAL_MS
        for a, b in zip(candles, candles[1:])
    ):
        raise RuntimeError(
            f'{symbol}: missing M5 candle(s)'
        )

    return candles


def atr_values(candles):
    values = [None] * len(candles)
    trs = []

    for i, candle in enumerate(candles):
        prev = (
            candles[i - 1]['close']
            if i else None
        )

        if prev is None:
            tr = candle['high'] - candle['low']
        else:
            tr = max(
                candle['high'] - candle['low'],
                abs(candle['high'] - prev),
                abs(candle['low'] - prev)
            )

        trs.append(tr)

        if i == ATR_LENGTH - 1:
            values[i] = (
                sum(trs[:ATR_LENGTH]) / ATR_LENGTH
            )
        elif i >= ATR_LENGTH:
            values[i] = (
                values[i - 1] * (ATR_LENGTH - 1) + tr
            ) / ATR_LENGTH

    return values


def cluster_atr(window):
    """Robust 3-cluster K-means for ATR values.

    Every sample is assigned to exactly one cluster.
    Empty clusters retain their previous centers.
    """

    if not window or not all(
        math.isfinite(v) for v in window
    ):
        raise ValueError(
            'Invalid ATR training window'
        )

    lo = min(window)
    hi = max(window)

    means = [
        lo + (hi - lo) * p
        for p in (0.75, 0.5, 0.25)
    ]

    for _ in range(100):
        groups = [[], [], []]

        for value in window:
            index = min(
                range(3),
                key=lambda i: abs(value - means[i])
            )
            groups[index].append(value)

        new_means = [
            sum(group) / len(group)
            if group else means[i]
            for i, group in enumerate(groups)
        ]

        if all(
            math.isclose(
                a, b,
                rel_tol=1e-12,
                abs_tol=1e-12
            )
            for a, b in zip(new_means, means)
        ):
            means = new_means
            break

        means = new_means
    else:
        LOG.warning(
            'K-means reached iteration limit'
        )

    index = min(
        range(3),
        key=lambda i: abs(window[-1] - means[i])
    )

    return means[index], index


def compute(candles):
    atr = atr_values(candles)
    output = []

    prev_lower = None
    prev_upper = None
    prev_st = None
    prev_direction = None

    for i, candle in enumerate(candles):
        window = atr[
            i - TRAINING_LENGTH + 1:i + 1
        ]

        if (
            len(window) != TRAINING_LENGTH
            or any(v is None for v in window)
        ):
            continue

        selected_atr, cluster = cluster_atr(window)

        middle = (
            candle['high'] + candle['low']
        ) / 2

        lower = middle - FACTOR * selected_atr
        upper = middle + FACTOR * selected_atr

        if prev_lower is not None:
            previous_close = candles[i - 1]['close']

            lower = (
                lower
                if lower > prev_lower
                or previous_close < prev_lower
                else prev_lower
            )

            upper = (
                upper
                if upper < prev_upper
                or previous_close > prev_upper
                else prev_upper
            )

        if prev_st is None:
            direction = 1

        elif math.isclose(
            prev_st,
            prev_upper,
            rel_tol=0,
            abs_tol=1e-10
        ):
            direction = (
                -1 if candle['close'] > upper else 1
            )

        else:
            direction = (
                1 if candle['close'] < lower else -1
            )

        st = (
            lower if direction == -1 else upper
        )

        signal = (
            'BUY'
            if prev_direction == 1 and direction == -1
            else 'SELL'
            if prev_direction == -1 and direction == 1
            else None
        )

        output.append({
            'time': candle['time'],
            'close': candle['close'],
            'st': st,
            'direction': direction,
            'cluster': cluster,
            'signal': signal
        })

        prev_lower = lower
        prev_upper = upper
        prev_st = st
        prev_direction = direction

    return output


def ema_step(previous, value, length):
    """Pine ta.ema recurrence."""
    if previous is None:
        return value

    alpha = 2.0 / (length + 1.0)

    return (
        alpha * value
        + (1.0 - alpha) * previous
    )


def compute_range_filter(candles):
    """Range Filter Buy and Sell M5.

    Period 100, multiplier 3.
    Uses available 200-candle history.
    """

    result = []
    prev_close = None
    avg_change = None
    smoothed_range = None
    prev_filter = None

    upward = 0
    downward = 0
    previous_condition = 0

    for candle in candles:
        close = candle['close']

        if prev_close is None:
            filtered = close

            result.append({
                'time': candle['time'],
                'close': close,
                'filter': filtered,
                'range': None,
                'signal': None
            })

            prev_filter = filtered
            prev_close = close
            continue

        avg_change = ema_step(
            avg_change,
            abs(close - prev_close),
            RANGE_PERIOD
        )

        smoothed_range = ema_step(
            smoothed_range,
            avg_change,
            RANGE_PERIOD * 2 - 1
        )

        rng = (
            smoothed_range * RANGE_MULTIPLIER
        )

        if close > prev_filter:
            filtered = max(
                prev_filter,
                close - rng
            )
        else:
            filtered = min(
                prev_filter,
                close + rng
            )

        if filtered > prev_filter:
            upward += 1
            downward = 0

        elif filtered < prev_filter:
            downward += 1
            upward = 0

        long_cond = (
            close > filtered and upward > 0
        )

        short_cond = (
            close < filtered and downward > 0
        )

        condition = (
            1 if long_cond
            else -1 if short_cond
            else previous_condition
        )

        signal = (
            'BUY'
            if long_cond and previous_condition == -1
            else 'SELL'
            if short_cond and previous_condition == 1
            else None
        )

        result.append({
            'time': candle['time'],
            'close': close,
            'filter': filtered,
            'range': rng,
            'signal': signal
        })

        previous_condition = condition
        prev_filter = filtered
        prev_close = close

    return result


def send_telegram(symbol, record, indicator):
    token = os.environ.get(
        'TELEGRAM_BOT_TOKEN'
    )
    chat_id = os.environ.get(
        'TELEGRAM_CHAT_ID'
    )

    if not token or not chat_id:
        raise RuntimeError(
            'Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID'
        )

    closed = datetime.fromtimestamp(
        (record['time'] + INTERVAL_MS) / 1000,
        tz=timezone.utc
    ).strftime('%Y-%m-%d %H:%M UTC')

    emoji = (
        '🟢' if record['signal'] == 'BUY'
        else '🔴'
    )

    message = (
        f'{emoji} {record["signal"]} – {symbol}.P (Bitget)\n'
        f'{indicator} · M5\n'
        f'Kerzenschluss: {closed}\n'
        f'Schlusskurs: {record["close"]:.8g} USDT\n'
    )

    if indicator == 'Adaptive SuperTrend':
        volatility = (
            'HIGH', 'MEDIUM', 'LOW'
        )[record['cluster']]

        message += (
            f'SuperTrend: {record["st"]:.8g}\n'
            f'Volatilität: {volatility}\n'
        )
    else:
        message += (
            f'Range Filter: {record["filter"]:.8g}\n'
        )

    message += 'Nur Signal – keine Order'

    response = get_json(
        f'https://api.telegram.org/bot{token}/sendMessage',
        headers={
            'Content-Type':
            'application/x-www-form-urlencoded'
        },
        data=urlencode({
            'chat_id': chat_id,
            'text': message
        }).encode()
    )

    if not response.get('ok'):
        raise RuntimeError(
            'Telegram rejected message: '
            + str(response.get('description'))
        )


def save_state(state):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = STATE_FILE.with_suffix('.tmp')

    temp.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True
        ) + '\n'
    )

    temp.replace(STATE_FILE)


def process_indicator(
    symbol,
    records,
    checkpoints,
    indicator,
    state
):
    """Process new signals and recover safely from old checkpoints."""

    if len(records) < 2:
        raise RuntimeError(
            f'{indicator}: insufficient computed records'
        )

    latest = records[-1]
    previous = checkpoints.get(symbol)

    LOG.info(
        '%s %s candle=%s signal=%s',
        symbol,
        indicator,
        latest['time'],
        latest['signal']
    )

    if previous is None:
        LOG.info(
            '%s %s first observation; '
            'no historical alerts',
            symbol,
            indicator
        )

    else:
        previous = int(previous)

        if previous > latest['time']:
            raise RuntimeError(
                f'{indicator}: checkpoint newer '
                'than exchange candle'
            )

        if previous < records[0]['time']:
            gap_minutes = (
                records[0]['time'] - previous
            ) / 60000

            LOG.warning(
                '%s %s HISTORY GAP: '
                'checkpoint=%s '
                'oldest_available=%s '
                'gap_minutes=%.0f. '
                'Historical signals may be missing. '
                'Resynchronizing without sending '
                'stale alerts.',
                symbol,
                indicator,
                previous,
                records[0]['time'],
                gap_minutes
            )

            checkpoints[symbol] = latest['time']
            save_state(state)
            return

        for record in records:
            if (
                record['time'] > previous
                and record['signal']
            ):
                send_telegram(
                    symbol,
                    record,
                    indicator
                )

                LOG.info(
                    '%s %s Telegram %s sent for %s',
                    symbol,
                    indicator,
                    record['signal'],
                    record['time']
                )

    checkpoints[symbol] = latest['time']
    save_state(state)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            '%(asctime)s %(levelname)s %(message)s'
        )
    )

    if STATE_FILE.exists():
        state = json.loads(
            STATE_FILE.read_text()
        )
    else:
        state = {}

    if not isinstance(state, dict):
        raise RuntimeError(
            'Invalid state JSON'
        )

    # Migrate previous state formats.
    if 'last_processed_open_ms' in state:
        old_symbols = {
            'BTCUSDT': int(
                state['last_processed_open_ms']
            )
        }

        state = {
            'supertrend': old_symbols,
            'range_filter': {}
        }

    elif 'symbols' in state:
        state = {
            'supertrend': state['symbols'],
            'range_filter': {}
        }

    st_checkpoints = state.setdefault(
        'supertrend', {}
    )

    rf_checkpoints = state.setdefault(
        'range_filter', {}
    )

    if (
        not isinstance(st_checkpoints, dict)
        or not isinstance(rf_checkpoints, dict)
    ):
        raise RuntimeError(
            'Invalid indicator checkpoints'
        )

    symbols = select_symbols()
    errors = []

    for symbol in symbols:
        try:
            candles = fetch_candles(symbol)

        except Exception as exc:
            LOG.exception(
                'Failed candles %s: %s',
                symbol,
                exc
            )
            errors.append(
                f'{symbol}/candles'
            )
            continue

        indicators = (
            (
                'Adaptive SuperTrend',
                compute,
                st_checkpoints
            ),
            (
                'Range Filter',
                compute_range_filter,
                rf_checkpoints
            )
        )

        for indicator, calculator, checkpoints in indicators:
            try:
                records = calculator(candles)

                process_indicator(
                    symbol,
                    records,
                    checkpoints,
                    indicator,
                    state
                )

            except Exception as exc:
                LOG.exception(
                    'Failed %s %s: %s',
                    symbol,
                    indicator,
                    exc
                )

                errors.append(
                    f'{symbol}/{indicator}'
                )

    LOG.info(
        'Processed %d markets, two indicators; failures=%d',
        len(symbols),
        len(errors)
    )

    if errors:
        raise RuntimeError(
            'Markets failed (check logs): '
            + ', '.join(errors)
        )


if __name__ == '__main__':
    try:
        main()
    except Exception:
        LOG.exception('Monitor failed')
        sys.exit(1)
