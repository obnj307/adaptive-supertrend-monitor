#!/usr/bin/env python3
"""Bitget BTCUSDT perpetual, confirmed 5m AlgoAlpha Adaptive SuperTrend alerts."""
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

SYMBOL = 'BTCUSDT'
PRODUCT_TYPE = 'USDT-FUTURES'
INTERVAL_MS = 300_000
ATR_LENGTH = 10
TRAINING_LENGTH = 100
FACTOR = 3.0
STATE_FILE = Path('state/last_processed.json')
API = 'https://api.bitget.com/api/v2/mix/market/candles'
LOG = logging.getLogger('supertrend')


def get_json(url, headers=None, data=None):
    for attempt in range(4):
        try:
            request = Request(url, data=data, headers=headers or {'User-Agent': 'AdaptiveSuperTrendMonitor/1.0'})
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def fetch_candles():
    params = urlencode({'symbol': SYMBOL, 'productType': PRODUCT_TYPE, 'granularity': '5m', 'limit': '200'})
    result = get_json(API + '?' + params)
    if result.get('code') != '00000':
        raise RuntimeError('Bitget API returned error: ' + str(result.get('msg', result.get('code'))))
    rows = result.get('data') or []
    now_ms = int(time.time() * 1000)
    by_time = {}
    for row in rows:
        stamp = int(row[0])
        if stamp + INTERVAL_MS <= now_ms - 1500:  # no unfinished candles
            by_time[stamp] = {'time': stamp, 'high': float(row[2]), 'low': float(row[3]), 'close': float(row[4])}
    candles = [by_time[k] for k in sorted(by_time)]
    if len(candles) < TRAINING_LENGTH + ATR_LENGTH + 3:
        raise RuntimeError(f'Insufficient confirmed candles: {len(candles)}')
    if any(b['time'] - a['time'] != INTERVAL_MS for a, b in zip(candles, candles[1:])):
        raise RuntimeError('Missing 5-minute candle(s); refusing to compute signals')
    return candles


def atr_values(candles, length=ATR_LENGTH):
    result = [None] * len(candles)
    trs = []
    for i, candle in enumerate(candles):
        previous_close = candles[i-1]['close'] if i else None
        tr = candle['high'] - candle['low'] if previous_close is None else max(
            candle['high'] - candle['low'], abs(candle['high'] - previous_close), abs(candle['low'] - previous_close))
        trs.append(tr)
        if i == length - 1:
            result[i] = sum(trs[:length]) / length
        elif i >= length:
            result[i] = (result[i-1] * (length - 1) + tr) / length
    return result


def cluster_atr(window):
    """Pine-like 1D k-means: initial centroids from ATR min/max, strict nearest-distance assignment."""
    lo, hi = min(window), max(window)
    means = [lo + (hi-lo)*p for p in (0.75, 0.5, 0.25)]
    for _ in range(100):
        groups = [[], [], []]
        for value in window:
            distances = [abs(value - m) for m in means]
            minimum = min(distances)
            winners = [i for i, d in enumerate(distances) if d == minimum]
            if len(winners) == 1:  # matches Pine strict < comparisons
                groups[winners[0]].append(value)
        if any(not g for g in groups):
            raise ValueError('Empty K-means cluster; cannot reproduce Pine output safely')
        new_means = [sum(g)/len(g) for g in groups]
        if new_means == means:
            break
        means = new_means
    else:
        raise RuntimeError('K-means failed to converge')
    distances = [abs(window[-1] - m) for m in means]
    index = distances.index(min(distances))  # Pine array.indexof(min)
    return means[index], index


def compute(candles):
    atr = atr_values(candles)
    output = []
    prev_lower = prev_upper = prev_st = None
    prev_direction = None
    for i, candle in enumerate(candles):
        if i < TRAINING_LENGTH + ATR_LENGTH - 2:
            continue
        window = atr[i-TRAINING_LENGTH+1:i+1]
        if any(v is None for v in window):
            continue
        selected_atr, cluster = cluster_atr(window)
        middle = (candle['high'] + candle['low']) / 2
        lower = middle - FACTOR * selected_atr
        upper = middle + FACTOR * selected_atr
        if prev_lower is not None:
            previous_close = candles[i-1]['close']
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


def send_telegram(record):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
    closed = datetime.fromtimestamp((record['time'] + INTERVAL_MS)/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    emoji = '🟢' if record['signal'] == 'BUY' else '🔴'
    volatility = ('HIGH', 'MEDIUM', 'LOW')[record['cluster']]
    message = (f"{emoji} {record['signal']} – BTCUSDT.P (Bitget)\n"
               f"Adaptive SuperTrend · 5m\n"
               f"Kerzenschluss: {closed}\n"
               f"Schlusskurs: {record['close']:.2f} USDT\n"
               f"SuperTrend: {record['st']:.2f}\n"
               f"Volatilität: {volatility}\n"
               f"Nur Signal – keine Order")
    payload = urlencode({'chat_id': chat_id, 'text': message}).encode()
    response = get_json(f'https://api.telegram.org/bot{token}/sendMessage', data=payload,
                        headers={'Content-Type': 'application/x-www-form-urlencoded'})
    if not response.get('ok'):
        raise RuntimeError('Telegram rejected message: ' + str(response.get('description')))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    candles = fetch_candles()
    records = compute(candles)
    if len(records) < 2:
        raise RuntimeError('Insufficient computed records')
    latest = records[-1]
    LOG.info('Latest confirmed candle %s, close=%.2f, direction=%s, cluster=%s, signal=%s',
             latest['time'], latest['close'], latest['direction'], latest['cluster'], latest['signal'])
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    last = state.get('last_processed_open_ms')
    if last is None:
        LOG.info('First run: initializing state without sending historical signals')
    elif last > latest['time']:
        raise RuntimeError('Saved state is ahead of the latest API candle')
    elif last < records[0]['time']:
        raise RuntimeError('State too old for available history; manual intervention needed')
    else:
        pending = [r for r in records if r['time'] > last and r['signal']]
        for record in pending:
            send_telegram(record)
            LOG.info('Telegram delivered %s for %s', record['signal'], record['time'])
    # Write state only after all pending notifications succeed.
    STATE_FILE.write_text(json.dumps({'last_processed_open_ms': latest['time']}, indent=2) + '\n')
    LOG.info('State saved')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('Monitor failed')
        sys.exit(1)
