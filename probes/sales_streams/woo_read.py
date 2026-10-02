"""GET-only WooCommerce reader for execution in the existing Render service shell."""
import argparse
from datetime import datetime
import ipaddress
import json
import os
from urllib.parse import urlsplit
import httpx
from preview import preview
from pathlib import Path

FIELDS = 'id,number,payment_method,status,currency,total,refunds'


class ReadError(Exception):
    pass


def configuration(env):
    base = env.get('WOO_BASE_URL', '').rstrip('/')
    u = urlsplit(base)
    if (u.scheme != 'https' or not u.hostname or u.username or u.password
            or u.query or u.fragment or u.port not in (None, 443)):
        raise ReadError('WOO_BASE_URL must be the canonical HTTPS shop URL, without credentials or query.')
    host = u.hostname.lower()
    if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
        raise ReadError('A public shop hostname is required.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ReadError('Use the shop hostname, not an IP address.')
    key, secret = env.get('WOO_CONSUMER_KEY', ''), env.get('WOO_CONSUMER_SECRET', '')
    if not key.startswith('ck_') or not secret.startswith('cs_'):
        raise ReadError('WooCommerce credentials missing or invalid; values are never logged.')
    return base + '/wp-json/wc/v3/orders', key, secret


def get_orders(client, url, params):
    try:
        r = client.get(url, params=params)
    except httpx.HTTPError:
        raise ReadError('WooCommerce network/TLS error; details suppressed.') from None
    if r.status_code != 200:
        raise ReadError('WooCommerce returned HTTP %s; body suppressed. Redirects are not followed.' % r.status_code)
    try:
        data = r.json()
        total = int(r.headers['X-WP-Total'])
        pages = int(r.headers['X-WP-TotalPages'])
        if not isinstance(data, list) or any(not isinstance(o, dict) for o in data) or total < 0 or pages < 0:
            raise ValueError()
    except (ValueError, KeyError):
        raise ReadError('Invalid order response or missing pagination metadata.') from None
    return data, total, pages


def read_window(client, url, after, before, max_pages=20):
    try:
        start, end = datetime.fromisoformat(after), datetime.fromisoformat(before)
        if start >= end or max_pages < 1:
            raise ValueError()
    except ValueError:
        raise ReadError('Supply an ordered ISO-8601 date window and a positive page limit.') from None
    params = dict(after=after, before=before, per_page=100, order='asc',
                  orderby='id', status='any', _fields=FIELDS)
    rows = []
    first_counts = None
    for page in range(1, max_pages + 1):
        data, total, pages = get_orders(client, url, dict(params, page=page))
        if first_counts is None:
            first_counts = (total, pages)
        if first_counts != (total, pages) or pages > max_pages:
            raise ReadError('Order population changed or exceeds page limit; no partial report emitted.')
        rows.extend({k: o[k] for k in FIELDS.split(',') if k in o} for o in data)
        if page >= pages:
            break
    ids = [o.get('id') for o in rows]
    if any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
        raise ReadError('Invalid order identities.')
    if len(rows) != first_counts[0] or len(set(ids)) != len(ids):
        raise ReadError('Incomplete or duplicate order pagination; no report emitted.')
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--check', action='store_true', help='One GET; prints no order details.')
    p.add_argument('--after')
    p.add_argument('--before')
    p.add_argument('--mapping', type=Path)
    p.add_argument('--max-pages', type=int, default=20)
    a = p.parse_args()
    try:
        url, key, secret = configuration(os.environ)
        with httpx.Client(auth=httpx.BasicAuth(key, secret), timeout=30,
                          follow_redirects=False, trust_env=False) as client:
            if a.check:
                get_orders(client, url, {'per_page': 1, '_fields': 'id'})
                output = {'connection': 'GET_ORDERS_OK', 'financial_writes': 0,
                          'key_read_only_scope': 'CONFIRM_IN_WOOCOMMERCE',
                          'note': 'A successful GET does not prove that the key has no write permission.'}
            else:
                if not a.after or not a.before or not a.mapping:
                    raise ReadError('Use --check or provide --after, --before and --mapping.')
                mapping = json.loads(a.mapping.read_text())
                rows = read_window(client, url, a.after, a.before, a.max_pages)
                output = preview(rows, mapping)
                output['source'] = {'after': a.after, 'before': a.before,
                                    'date_basis': 'WooCommerce order creation filter; not bank booking date',
                                    'pagination': 'counts and unique IDs checked; not a transactional snapshot'}
        print(json.dumps(output, indent=2))
    except (ReadError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ReadError) else 'Invalid configuration or input; details suppressed.'
        p.exit(2, message + '\n')


if __name__ == '__main__':
    main()
