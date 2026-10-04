"""Bounded, operator-requested ICEPAY journal preparation on existing Render.

This first phase only reads master data. No public endpoint, browser credentials,
financial entries, global write switches, or extra infrastructure are involved.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
from urllib.parse import urlsplit

TASK_ID = 'icepay-journal-inspect-20261004-v1'
EXPIRES = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
DIVISION = 3977752
BASE = 'https://start.exactonline.nl'
RESOURCES = {'financial/Journals', 'financial/GLAccounts', 'cashflow/BankAccounts'}
LOG = logging.getLogger('uvicorn.error')


class Stopped(Exception):
    pass


def require(ok, reason):
    if not ok:
        raise Stopped(reason)


def allowed_url(url, resource):
    p = urlsplit(url)
    return (resource in RESOURCES and p.scheme == 'https'
            and p.netloc == 'start.exactonline.nl' and not p.fragment
            and p.path == f'/api/v1/{DIVISION}/{resource}')


def event(name, **data):
    LOG.warning('ICEPAY_JOURNAL_TASK %s', json.dumps(
        {'task': TASK_ID, 'event': name, **data}, sort_keys=True, default=str))


class Reader:
    def __init__(self, app):
        require(app.DIVISION == DIVISION and app.BASE_URL == BASE, 'wrong_administration')
        self.app, self.calls = app, 0

    async def rows(self, resource):
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        url = f'{BASE}/api/v1/{DIVISION}/{resource}'
        rows, seen = [], set()
        while url:
            require(allowed_url(url, resource) and url not in seen, 'unsafe_pagination')
            require(self.calls < 40, 'request_budget')
            seen.add(url)
            self.calls += 1
            token = await self.app._access_token()
            async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                         trust_env=False, verify=TLS_CONTEXT) as client:
                r = await client.get(url, headers={'Authorization': 'Bearer '+token,
                                                    'Accept': 'application/json'})
            require(r.status_code == 200, 'http_' + str(r.status_code))
            raw = r.json()
            data = raw.get('d')
            batch = data.get('results') if isinstance(data, dict) else data
            require(isinstance(batch, list), 'invalid_rows')
            rows.extend(batch)
            require(len(rows) <= 3000, 'row_budget')
            url = raw.get('__next') or (data.get('__next') if isinstance(data, dict) else None)
            require(not url or bool(batch), 'empty_intermediate_page')
            await asyncio.sleep(1.2)
        return rows


def project(row, keys):
    return {key: row.get(key) for key in keys}


def target(row):
    text = str(row.get('Description') or '').lower().replace(' ', '')
    return 'icepay' in text or 'fibo' in text


async def inspect(app):
    api = Reader(app)
    journals = await api.rows('financial/Journals')
    # Read complete code inventories before selecting any free code.
    event('journal_codes', codes=[str(r.get('Code') or '').strip() for r in journals])
    selected = [r for r in journals if target(r)]
    for row in selected:
        event('journal', row={k: v for k, v in row.items()
                             if k not in {'__metadata', 'Creator', 'Modifier',
                                          'CreatorFullName', 'ModifierFullName'}})
    ledgers = await api.rows('financial/GLAccounts')
    event('ledger_codes', codes=[str(r.get('Code') or '').strip() for r in ledgers])
    ids = {r.get('GLAccount') for r in selected}
    for row in ledgers:
        code = str(row.get('Code') or '').strip()
        if target(row) or row.get('ID') in ids or code in {'1316', '1360'}:
            event('ledger', row=project(row, ('ID', 'Code', 'Description', 'Type',
                  'BalanceType', 'BalanceSide', 'IsBlocked', 'Matching', 'VATCode')))
    bank_ids = {r.get('BankAccountID') for r in selected} - {None}
    if bank_ids:
        try:
            banks = await api.rows('cashflow/BankAccounts')
            for row in banks:
                if row.get('ID') in bank_ids or target(row):
                    event('bank', row={k: v for k, v in row.items()
                                      if k not in {'__metadata', 'Creator', 'Modifier',
                                                   'CreatorFullName', 'ModifierFullName'}})
        except Stopped as exc:
            event('bank_read_unavailable', reason=str(exc))
    return {'status': 'inspected', 'api_calls': api.calls, 'exact_writes': 0,
            'icepay_journal_count': sum('icepay' in str(r.get('Description') or '').lower().replace(' ', '')
                                        for r in journals)}


def claim(app):
    with app._db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_journal_tasks (
            task_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            result JSONB NOT NULL)''')
        return conn.execute('''INSERT INTO icepay_journal_tasks(task_id,result)
            VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING task_id''',
            (TASK_ID, '{"status":"started","exact_writes":0}')).fetchone() is not None


def save(app, result):
    with app._db_connect() as conn:
        conn.execute('UPDATE icepay_journal_tasks SET result=%s::jsonb WHERE task_id=%s',
                     (json.dumps(result), TASK_ID))


async def run(app):
    if datetime.now(timezone.utc) >= EXPIRES:
        return
    try:
        if not await asyncio.to_thread(claim, app):
            return
        event('started', exact_writes=0)
        result = await asyncio.wait_for(inspect(app), timeout=180)
        await asyncio.to_thread(save, app, result)
        event('complete', **result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        event('stopped', reason=str(exc) if isinstance(exc, Stopped) else 'runtime_error',
              error_type=type(exc).__name__, exact_writes=0)
