"""Nasdaq metadata cache for the existing US eligibility consumer.

No selection thresholds are applied here. Only directory-confirmed common/ADR
shares with complete public metadata can populate the cache; unknowns stay absent.
"""
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import tempfile

import httpx
from prism_core.us_stock_universe import DIRECTORY_URLS, parse_directories

SCREENER = 'https://api.nasdaq.com/api/screener/stocks'


def build_cache(nasdaq_text, other_text, downloads, *, captured_at):
    directory = parse_directories(nasdaq_text, other_text)
    categories = {r['Symbol'].replace('.', '-'): r['Market Category']
                  for r in csv.DictReader(io.StringIO('\n'.join(nasdaq_text.splitlines()[:-1])), delimiter='|')}
    indexed = {}
    for exchange, payload in downloads.items():
        rows = (payload.get('data') or {}).get('rows')
        if not isinstance(rows, list) or not rows:
            raise ValueError('Nasdaq download has no rows')
        for row in rows:
            symbol = row.get('symbol')
            if not isinstance(symbol, str):
                raise ValueError('Nasdaq symbol missing')
            symbol = symbol.replace('.', '-')
            if symbol in indexed:
                raise ValueError('Ambiguous Nasdaq symbol')
            indexed[symbol] = (exchange, row)
    cache = {}
    for record in directory.records:
        pair = indexed.get(record.symbol)
        if not record.name_verified or pair is None or pair[0] != record.exchange:
            continue
        row = pair[1]
        if not all(isinstance(row.get(k), str) and row[k].strip() for k in ('name', 'sector', 'industry')):
            continue
        # These exchange downloads quote US-listed securities in dollars. Do
        # not interpret another currency or an absent price as USD evidence.
        if not str(row.get('lastsale', '')).startswith('$'):
            continue
        try:
            cap = float(str(row['marketCap']).replace(',', ''))
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(cap) or cap <= 0:
            continue
        exchange = ({'Q':'NMS', 'G':'NGM', 'S':'NCM'}.get(categories.get(record.symbol))
                    if record.exchange == 'NASDAQ' else {'NYSE':'NYQ', 'AMEX':'ASE'}.get(record.exchange))
        if not exchange:
            continue
        cache[record.symbol] = {
            'quoteType':'EQUITY', 'exchange':exchange, 'currency':'USD',
            'longName':row['name'], 'shortName':row['name'], 'sector':row['sector'],
            'industry':row['industry'], 'marketCap':cap,
            'fetched_at':captured_at, 'metadata_source':'NASDAQ',
            # The public download may omit its publication timestamp. Do not
            # replace it with capture time or claim a real-time company profile.
            'source_asof':(downloads[record.exchange].get('data') or {}).get('asOf'),
        }
    if not cache:
        raise ValueError('No verified common-share metadata available')
    return cache


def refresh(path):
    with httpx.Client(timeout=25, follow_redirects=False,
                      headers={'User-Agent':'Mozilla/5.0', 'Accept':'application/json,text/plain'}) as client:
        texts = []
        for url in DIRECTORY_URLS:
            response = client.get(url)
            response.raise_for_status()
            texts.append(response.text)
        downloads = {}
        for exchange in ('NASDAQ', 'NYSE', 'AMEX'):
            response = client.get(SCREENER, params={'download':'true', 'exchange':exchange.lower()})
            response.raise_for_status()
            downloads[exchange] = response.json()
    captured = datetime.now(timezone.utc).isoformat()
    cache = build_cache(*texts, downloads, captured_at=captured)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(cache, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'source':'NASDAQ', 'verified_records':len(cache), 'captured_at':captured}
