#!/usr/bin/env python3
"""Bitget USDT perpetual M5 monitor: Adaptive SuperTrend + Range Filter."""
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
            request = Request(url, data=data, headers=headers or {'User-Agent': 'AdaptiveSuperTrendMonitor/2.0'})
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def bitget(path, params):
    result = get_json(API_ROOT + path + '?' + urlencode(params))
    if result.get('code') != '00000':
        raise RuntimeError(f'Bitget {path}: {result.get("msg", result.get("code"))}')
    return result.get('data') or []


def select_symbols():
    tickers = bitget('/tickers', {'productType': PRODUCT_TYPE})
    ranked = []
    available = set()
    for item in tickers:
        symbol = str(item.get('symbol', '')).upper()
        if not symbol.endswith('USDT'):
            continue
        available.add(symbol)
        # Bitget Futures ticker usdtVolume is the 24h turnover in USDT.
        # Do not substitute baseVolume (which is denominated in the base coin).
        raw = item.get('usdtVolume')
        if raw is None or raw == '':
            LOG.warning('Missing usdtVolume for %s; skipping dynamic ranking', symbol)
            continue
        try:
            turnover = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(turnover) and turnover >= MIN_TURNOVER_USDT:
            ranked.append((symbol, turnover))
    ranked.sort(key=lambda x: x[1], reverse=True)
    selected = [symbol for symbol, _ in ranked[:TOP_N]]
    for symbol in PINNED:
        if symbol in available and symbol not in selected:
            selected.append(symbol)
        elif symbol not in available:
            LOG.warning('Pinned future not listed: %s', symbol)
    if not selected:
        raise RuntimeError('No eligible USDT futures found; check ticker response')
    LOG.info('Monitoring %d markets (%d dynamic + pinned), first: %s', len(selected), min(TOP_N, len(ranked)), ', '.join(selected[:10]))
    return selected


def fetch_candles(symbol):
    rows = bitget('/candles', {'symbol': symbol, 'productType': PRODUCT_TYPE, 'granularity': '5m', 'limit': '200'})
    now_ms = int(time.time() * 1000)
    by_time = {}
    for row in rows:
        stamp = int(row[0])
        if stamp + INTERVAL_MS <= now_ms - 1500:
            by_time[stamp] = {'time': stamp, 'high': float(row[2]), 'low': float(row[3]), 'close': float(row[4])}
    candles = [by_time[k] for k in sorted(by_time)]
    if len(candles) < TRAINING_LENGTH + ATR_LENGTH + 3:
        raise RuntimeError(f'{symbol}: only {len(candles)} confirmed candles')
    if any(b['time'] - a['time'] != INTERVAL_MS for a, b in zip(candles, candles[1:])):
        raise RuntimeError(f'{symbol}: missing M5 candle(s)')
    return candles


def atr_values(candles):
    values = [None] * len(candles)
    trs = []
    for i, candle in enumerate(candles):
        prev = candles[i - 1]['close'] if i else None
        tr = candle['high'] - candle['low'] if prev is None else max(candle['high'] - candle['low'], abs(candle['high'] - prev), abs(candle['low'] - prev))
        trs.append(tr)
        if i == ATR_LENGTH - 1:
            values[i] = sum(trs[:ATR_LENGTH]) / ATR_LENGTH
        elif i >= ATR_LENGTH:
            values[i] = (values[i - 1] * (ATR_LENGTH - 1) + tr) / ATR_LENGTH
    return values


def cluster_atr(window):
    lo, hi = min(window), max(window)
    means = [lo + (hi - lo) * p for p in (0.75, 0.5, 0.25)]
    for _ in range(100):
        groups = [[], [], []]
        for value in window:
            distances = [abs(value - m) for m in means]
            nearest = min(distances)
            winners = [i for i, d in enumerate(distances) if d == nearest]
            if len(winners) == 1:
                groups[winners[0]].append(value)
        if any(not group for group in groups):
            raise ValueError('Empty K-means cluster')
        new_means = [sum(g) / len(g) for g in groups]
        if new_means == means:
            break
        means = new_means
    else:
        raise RuntimeError('K-means failed to converge')
    distances = [abs(window[-1] - m) for m in means]
    index = distances.index(min(distances))
    return means[index], index


def compute(candles):
    atr = atr_values(candles)
    output = []
    prev_lower = prev_upper = prev_st = prev_direction = None
    for i, candle in enumerate(candles):
        window = atr[i - TRAINING_LENGTH + 1:i + 1]
        if len(window) != TRAINING_LENGTH or any(v is None for v in window):
            continue
        selected_atr, cluster = cluster_atr(window)
        middle = (candle['high'] + candle['low']) / 2
        lower, upper = middle - FACTOR * selected_atr, middle + FACTOR * selected_atr
        if prev_lower is not None:
            previous_close = candles[i - 1]['close']
            lower = lower if lower > prev_lower or previous_close < prev_lower else prev_lower
            upper = upper if upper < prev_upper or previous_close > prev_upper else prev_upper
        if prev_st is None:
            direction = 1
        elif math.isclose(prev_st, prev_upper, rel_tol=0, abs_tol=1e-10):
            direction = -1 if candle['close'] > upper else 1
        else:
            direction = 1 if candle['close'] < lower else -1
        st = lower if direction == -1 else upper
        signal = 'BUY' if prev_direction == 1 and direction == -1 else 'SELL' if prev_direction == -1 and direction == 1 else None
        output.append({'time': candle['time'], 'close': candle['close'], 'st': st, 'direction': direction, 'cluster': cluster, 'signal': signal})
        prev_lower, prev_upper, prev_st, prev_direction = lower, upper, st, direction
    return output


def ema_step(previous, value, length):
    """Pine ta.ema recurrence; initialize from first non-na sample."""
    if previous is None:
        return value
    alpha = 2.0 / (length + 1.0)
    return alpha * value + (1.0 - alpha) * previous


def compute_range_filter(candles):
    """Port of Range Filter Buy and Sell 5min, close/per=100/mult=3.

    Uses the available 200-candle Bitget window. EMA(199) warm-up may
    differ from TradingView, which usually has much longer history.
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
            # ta.ema(abs(src-src[1])) is na on the first bar.
            filtered = close
            result.append({'time': candle['time'], 'close': close,
                           'filter': filtered, 'range': None, 'signal': None})
            prev_filter = filtered
            prev_close = close
            continue

        avg_change = ema_step(avg_change, abs(close - prev_close), RANGE_PERIOD)
        smoothed_range = ema_step(smoothed_range, avg_change, RANGE_PERIOD * 2 - 1)
        rng = smoothed_range * RANGE_MULTIPLIER

        # Pine rngfilt(): move only when price crosses the previous filter +/- range.
        if close > prev_filter:
            filtered = max(prev_filter, close - rng)
        else:
            filtered = min(prev_filter, close + rng)

        if filtered > prev_filter:
            upward += 1
            downward = 0
        elif filtered < prev_filter:
            downward += 1
            upward = 0

        # The original Pine OR conditions reduce to these two expressions.
        long_cond = close > filtered and upward > 0
        short_cond = close < filtered and downward > 0
        condition = 1 if long_cond else -1 if short_cond else previous_condition
        signal = ('BUY' if long_cond and previous_condition == -1 else
                  'SELL' if short_cond and previous_condition == 1 else None)
        result.append({'time': candle['time'], 'close': close,
                       'filter': filtered, 'range': rng, 'signal': signal})
        previous_condition = condition
        prev_filter = filtered
        prev_close = close
    return result


def send_telegram(symbol, record, indicator):
    token, chat_id = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
    closed = datetime.fromtimestamp((record['time'] + INTERVAL_MS) / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    emoji = '🟢' if record['signal'] == 'BUY' else '🔴'
    message = (f'{emoji} {record["signal"]} – {symbol}.P (Bitget)\n'
               f'{indicator} · M5\n'
               f'Kerzenschluss: {closed}\n'
               f'Schlusskurs: {record["close"]:.8g} USDT\n')
    if indicator == 'Adaptive SuperTrend':
        volatility = ('HIGH', 'MEDIUM', 'LOW')[record['cluster']]
        message += (f'SuperTrend: {record["st"]:.8g}\n'
                    f'Volatilität: {volatility}\n')
    else:
        message += f'Range Filter: {record["filter"]:.8g}\n'
    message += 'Nur Signal – keine Order'
    response = get_json(f'https://api.telegram.org/bot{token}/sendMessage',
                        headers={'Content-Type': 'application/x-www-form-urlencoded'},
                        data=urlencode({'chat_id': chat_id, 'text': message}).encode())
    if not response.get('ok'):
        raise RuntimeError('Telegram rejected message: ' + str(response.get('description')))


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
    temp.replace(STATE_FILE)


def process_indicator(symbol, records, checkpoints, indicator, state):
    if len(records) < 2:
        raise RuntimeError(f'{indicator}: insufficient computed records')
    latest = records[-1]
    previous = checkpoints.get(symbol)
    LOG.info('%s %s candle=%s signal=%s', symbol, indicator,
             latest['time'], latest['signal'])
    if previous is None:
        LOG.info('%s %s first observation; no historical alerts', symbol, indicator)
    else:
        previous = int(previous)
        if previous > latest['time']:
            raise RuntimeError(f'{indicator}: checkpoint newer than exchange candle')
        if previous < records[0]['time']:
            raise RuntimeError(f'{indicator}: checkpoint older than available history')
        for record in records:
            if record['time'] > previous and record['signal']:
                send_telegram(symbol, record, indicator)
                LOG.info('%s %s Telegram %s sent for %s', symbol, indicator,
                         record['signal'], record['time'])
    checkpoints[symbol] = latest['time']
    save_state(state)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if not isinstance(state, dict):
        raise RuntimeError('Invalid state JSON')

    # Migrate V1/V2 state without losing SuperTrend checkpoints.
    if 'last_processed_open_ms' in state:
        old_symbols = {'BTCUSDT': int(state['last_processed_open_ms'])}
        state = {'supertrend': old_symbols, 'range_filter': {}}
    elif 'symbols' in state:
        state = {'supertrend': state['symbols'], 'range_filter': {}}
    st_checkpoints = state.setdefault('supertrend', {})
    rf_checkpoints = state.setdefault('range_filter', {})
    if not isinstance(st_checkpoints, dict) or not isinstance(rf_checkpoints, dict):
        raise RuntimeError('Invalid indicator checkpoints')

    symbols = select_symbols()
    errors = []
    for symbol in symbols:
        try:
            candles = fetch_candles(symbol)
        except Exception as exc:
            LOG.exception('Failed candles %s: %s', symbol, exc)
            errors.append(f'{symbol}/candles')
            continue

        # Independent error handling and state per indicator.
        for indicator, calculator, checkpoints in (
            ('Adaptive SuperTrend', compute, st_checkpoints),
            ('Range Filter', compute_range_filter, rf_checkpoints),
        ):
            try:
                records = calculator(candles)
                process_indicator(symbol, records, checkpoints, indicator, state)
            except Exception as exc:
                LOG.exception('Failed %s %s: %s', symbol, indicator, exc)
                errors.append(f'{symbol}/{indicator}')
    LOG.info('Processed %d markets, two indicators; failures=%d', len(symbols), len(errors))
    if errors:
        raise RuntimeError('Markets failed (check logs): ' + ', '.join(errors))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        LOG.exception('Monitor failed')
        sys.exit(1)