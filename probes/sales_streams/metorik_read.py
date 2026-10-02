"""Read-only Metorik sample; never writes to WooCommerce, Metorik or Exact."""
import argparse
from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import httpx
from preview import preview, money

BASE = 'https://app.metorik.com/api/v1/store'


class ReadError(Exception):
    pass


def get_json(client, path, params=None):
    if path not in ('', '/orders'):
        raise ReadError('Endpoint not allowed.')
    try:
        response = client.get(BASE + path, params=params)
    except httpx.HTTPError:
        raise ReadError('Network/TLS error; details suppressed.') from None
    if response.status_code != 200:
        raise ReadError('Metorik HTTP %d; response body suppressed.' % response.status_code)
    try:
        data = json.loads(response.text, parse_float=Decimal)
    except ValueError:
        raise ReadError('Metorik response is not JSON.') from None
    if not isinstance(data, dict):
        raise ReadError('Unexpected response shape.')
    return data


def store_info(client):
    raw = get_json(client, '')
    fields = ('name', 'timezone', 'currency', 'platform', 'earliest_date')
    if any(not isinstance(raw.get(k), str) or not raw[k] for k in fields):
        raise ReadError('Store metadata incomplete.')
    if raw['platform'].lower() != 'woocommerce':
        raise ReadError('The selected store is not WooCommerce.')
    return {k: raw[k] for k in fields}


def sample(client, limit=50, pause=time.sleep):
    if not 1 <= limit <= 500:
        raise ReadError('Sample limit must be between 1 and 500.')
    rows, seen = [], set()
    per_page = min(limit, 100)
    for page in range(1, (limit + per_page - 1) // per_page + 1):
        pause(1.1)  # Shared store limit: other integrations can still cause 429.
        body = get_json(client, '/orders', {'page': page, 'per_page': per_page,
                         'order_by': 'order_created_at', 'order_dir': 'desc'})
        data, pg = body.get('data'), body.get('pagination')
        if (not isinstance(data, list) or not isinstance(pg, dict)
                or pg.get('current_page') != page or pg.get('per_page') != per_page
                or type(pg.get('has_more_pages')) is not bool or len(data) > per_page):
            raise ReadError('Invalid pagination; no partial report emitted.')
        if not data and pg['has_more_pages']:
            raise ReadError('Empty intermediate page.')
        for order in data:
            if not isinstance(order, dict):
                raise ReadError('Invalid order.')
            oid = order.get('order_id')
            if type(oid) is not int or oid <= 0 or oid in seen:
                raise ReadError('Invalid/duplicate order identity; no partial report emitted.')
            seen.add(oid)
            # Project immediately; do not persist personal details from the API.
            fields = ('order_id', 'order_number', 'status', 'currency', 'total',
                      'total_refunds', 'payment_method', 'order_created_at', 'order_updated_at')
            rows.append({k: order.get(k) for k in fields})
        if len(rows) >= limit or not pg['has_more_pages']:
            return rows[:limit], bool(pg['has_more_pages'] or len(rows) > limit)
    raise ReadError('Incomplete pagination.')


def report(rows, mapping, store, more):
    normalized = []
    for row in rows:
        order = {'id': row['order_id'], 'number': row.get('order_number'),
                 'payment_method': row.get('payment_method'), 'status': row.get('status'),
                 'currency': row.get('currency'), 'total': row.get('total')}
        # A nonzero summary is only a review flag, never a fabricated refund record.
        normalized.append(order)
    result = preview(normalized, mapping)
    for output, raw in zip(result['orders'], rows):
        output['source_order_id'] = output.pop('order_id')
        output['issues'].extend(['METORIK_AMOUNT_SEMANTICS_UNVERIFIED',
                                 'SOURCE_ID_MAPPING_UNVERIFIED'])
        output['routing_status'] = 'REVIEW'
        try:
            refunds = money(raw.get('total_refunds'))
            output['refund_total_reported'] = str(refunds)
            if refunds != 0:
                output['issues'].append('REFUND_REQUIRES_CREDIT_REVIEW')
        except (ValueError, ArithmeticError):
            output['refund_total_reported'] = None
            output['issues'].append('REFUND_TOTAL_UNKNOWN')
        output['order_updated_at'] = raw.get('order_updated_at')
    result.update(mode='METORIK_READ_ONLY_SAMPLE', review_count=len(rows), store=store,
                  source='Metorik synchronized copy; freshness not independently verified',
                  more_orders_available=more, currencies=dict(Counter(r.get('currency') for r in rows)),
                  scope='Latest orders by creation time; bounded sample, not a full audit or snapshot.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--expect-store', help='Exact store name returned by --check')
    parser.add_argument('--sample-limit', type=int, default=50)
    parser.add_argument('--mapping', type=Path)
    a = parser.parse_args()
    key = os.environ.get('METORIK_API_KEY', '').strip()
    try:
        if not key:
            raise ReadError('METORIK_API_KEY is missing.')
        with httpx.Client(headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
                          follow_redirects=False, trust_env=False, timeout=30) as client:
            store = store_info(client)
            if a.check:
                output = {'connection': 'METORIK_STORE_OK', 'store': store,
                          'orders_read': False, 'financial_writes': 0}
            else:
                if a.expect_store != store['name'] or not a.mapping:
                    raise ReadError('Confirm --expect-store and provide --mapping before reading orders.')
                mapping = json.loads(a.mapping.read_text())
                if not isinstance(mapping, dict):
                    raise ReadError('Mapping must be an object.')
                rows, more = sample(client, a.sample_limit)
                output = report(rows, mapping, store, more)
        print(json.dumps(output, indent=2, default=str))
    except (ReadError, ValueError, OSError, ArithmeticError):
        # Never print raw upstream messages, headers, bodies or credentials.
        import sys
        error = sys.exc_info()[1]
        parser.exit(2, (str(error) if isinstance(error, ReadError) else 'Invalid configuration/data; details suppressed.') + '\n')


if __name__ == '__main__':
    main()
