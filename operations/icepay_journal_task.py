"""One operator-requested ICEPAY bank journal on existing Render.

Inspection is read-only. Creation requires an exact, expiring activation ID and
a durable claim, and permits one fixed Journal POST only. No entries, matching,
public execution endpoint, global write switches or extra infrastructure.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from urllib.parse import urlsplit

TASK_ID = 'icepay-journal-inspect-20261004-v1'
EXPIRES = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
DIVISION = 3977752
BASE = 'https://start.exactonline.nl'
RESOURCES = {'financial/Journals', 'financial/GLAccounts', 'crm/BankAccounts'}
CREATE_ID = 'icepay-journal-create-20261004-v1'
CREATE_ENV = 'ICEPAY_JOURNAL_TASK_ID'
JOURNAL_ID = '07da1219-d8d8-4f72-b4f8-3d6735d6e65a'
LEDGER_ID = '9cb20757-80d9-4199-b2ff-055562855e5e'
UNALLOCATED_ID = 'e0006d7d-d564-4629-bd89-4fffae183273'
PAYLOAD = {
    'ID': JOURNAL_ID, 'Code': '27', 'Description': 'ICEPAY EUR', 'Type': 12,
    'Currency': 'EUR', 'GLAccount': LEDGER_ID,
    'PaymentInTransitAccount': UNALLOCATED_ID,
    'BankAccountIncludingMask': 'ICEEUR', 'BankAccountCountry': 'NL',
    'BankAccountDescription': 'ICEPAY EUR',
    'AllowVariableCurrency': False, 'AllowVariableExchangeRate': False,
    'AutoSave': False, 'IsBlocked': False,
}
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


def event(name, *, task_id=TASK_ID, **data):
    LOG.warning('ICEPAY_JOURNAL_TASK %s', json.dumps(
        {'task': task_id, 'event': name, **data}, sort_keys=True, default=str))


class Reader:
    def __init__(self, app):
        require(app.DIVISION == DIVISION and app.BASE_URL == BASE, 'wrong_administration')
        self.app, self.calls = app, 0

    async def rows(self, resource, params=None):
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
                from operations.worker_coordination import budgeted_http
                r = await budgeted_http(self.app, 'icepay', 'GET',
                    lambda: client.get(url, params=params, headers={'Authorization': 'Bearer '+token,
                                                    'Accept': 'application/json'}),
                    priority='routine', floor=200)
            require(r.status_code == 200, 'http_' + str(r.status_code))
            raw = r.json()
            data = raw.get('d')
            batch = data.get('results') if isinstance(data, dict) else data
            require(isinstance(batch, list), 'invalid_rows')
            rows.extend(batch)
            require(len(rows) <= 3000, 'row_budget')
            url = raw.get('__next') or (data.get('__next') if isinstance(data, dict) else None)
            params = None
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
            banks = await api.rows('crm/BankAccounts', {'$filter': ' or '.join(
                "ID eq guid'" + str(bank_id) + "'" for bank_id in sorted(bank_ids))})
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


def claim(app, task_id=TASK_ID):
    with app._db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_journal_tasks (
            task_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            result JSONB NOT NULL)''')
        return conn.execute('''INSERT INTO icepay_journal_tasks(task_id,result)
            VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING task_id''',
            (task_id, '{"status":"started","exact_writes":0}')).fetchone() is not None


def save(app, result, task_id=TASK_ID):
    with app._db_connect() as conn:
        conn.execute('UPDATE icepay_journal_tasks SET result=%s::jsonb WHERE task_id=%s',
                     (json.dumps(result), task_id))


def code(row):
    return str(row.get('Code') or '').strip()


def is_icepay(row):
    return 'icepay' in str(row.get('Description') or '').lower().replace(' ', '')


def validate_existing(row):
    require(all(row.get(k) == PAYLOAD[k] for k in
                ('Type', 'Currency', 'GLAccount', 'PaymentInTransitAccount'))
            and row.get('IsBlocked') is False and bool(row.get('BankAccountID')),
            'existing_icepay_configuration_differs')
    return {'status': 'already_exists', 'journal_code': code(row),
            'journal_id': row.get('ID'), 'ledger_code': '1317',
            'unallocated_code': '1360', 'currency': 'EUR', 'exact_writes': 0}


def preflight(journals, ledgers):
    # These identities and types were read from production on 2026-10-04.
    for expected_code, expected_id, expected_type in (
            ('1317', LEDGER_ID, 12), ('1360', UNALLOCATED_ID, 90)):
        matches = [r for r in ledgers if code(r) == expected_code]
        require(len(matches) == 1 and matches[0].get('ID') == expected_id
                and matches[0].get('Type') == expected_type
                and matches[0].get('BalanceType') == 'B'
                and matches[0].get('IsBlocked') is False,
                'ledger_changed_' + expected_code)
    require(is_icepay(next(r for r in ledgers if code(r) == '1317')),
            'icepay_ledger_description_changed')
    matches = [r for r in journals if is_icepay(r) or r.get('GLAccount') == LEDGER_ID]
    require(len(matches) <= 1, 'multiple_icepay_journals')
    if matches:
        require(is_icepay(matches[0]), 'icepay_ledger_already_used')
        return validate_existing(matches[0])
    require(not any(code(r) == '27' or r.get('ID') == JOURNAL_ID
                    or r.get('BankAccountIncludingMask') == 'ICEEUR' for r in journals),
            'journal_code_or_identifier_in_use')
    fibo = [r for r in journals if code(r) == '26']
    require(len(fibo) == 1 and fibo[0].get('Type') == 12
            and fibo[0].get('Currency') == 'EUR'
            and fibo[0].get('PaymentInTransitAccount') == UNALLOCATED_ID,
            'reference_journal_changed')
    return None


class Creator(Reader):
    async def create_journal(self, payload):
        # One fixed POST is the entire write surface. Never PUT/DELETE or retry.
        require(payload == PAYLOAD, 'unexpected_write_payload')
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        token = await self.app._access_token()
        url = f'{BASE}/api/v1/{DIVISION}/financial/Journals'
        async with httpx.AsyncClient(timeout=45, follow_redirects=False,
                                     trust_env=False, verify=TLS_CONTEXT) as client:
            from operations.worker_coordination import budgeted_http
            response = await budgeted_http(self.app, 'icepay', 'POST',
                lambda: client.post(url, json=payload,
                    headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json'}),
                priority='critical', floor=200)
        require(response.status_code in (200, 201), 'create_http_' + str(response.status_code))
        # Do not expose response bodies. Independent readback proves success.


async def create_once(app, api=None, persist=None):
    api = api or Creator(app)
    if persist is None:
        async def persist(value):
            await asyncio.to_thread(save, app, value, CREATE_ID)
    journals = await api.rows('financial/Journals')
    ledgers = await api.rows('financial/GLAccounts')
    existing = preflight(journals, ledgers)
    if existing:
        return existing
    banks = await api.rows('crm/BankAccounts', {'$filter': "BankAccount eq 'ICEEUR'"})
    require(not banks, 'bank_identifier_already_exists')
    # Recheck the code immediately before the sole POST.
    latest = await api.rows('financial/Journals')
    existing = preflight(latest, ledgers)
    if existing:
        return existing
    await persist({'status': 'write_started', 'journal_code': '27',
                   'journal_id': JOURNAL_ID, 'exact_writes': 'unknown'})
    await api.create_journal(dict(PAYLOAD))
    rows = await api.rows('financial/Journals')
    matches = [r for r in rows if r.get('ID') == JOURNAL_ID]
    require(len(matches) == 1, 'create_readback_missing')
    row = matches[0]
    require(all(row.get(k) == PAYLOAD[k] for k in PAYLOAD if k != 'BankAccountCountry')
            and str(row.get('BankAccountCountry') or '').strip() == 'NL'
            and bool(row.get('BankAccountID')), 'create_readback_mismatch')
    require(sum(is_icepay(r) for r in rows) == 1, 'duplicate_icepay_after_create')
    return {'status': 'created_verified', 'journal_code': '27', 'journal_id': JOURNAL_ID,
            'ledger_code': '1317', 'unallocated_code': '1360', 'currency': 'EUR',
            'bank_identifier': 'ICEEUR', 'exact_writes': 1}


async def run_create(app):
    if os.environ.get(CREATE_ENV) != CREATE_ID or datetime.now(timezone.utc) >= EXPIRES:
        return
    try:
        if not await asyncio.to_thread(claim, app, CREATE_ID):
            return
        event('started', task_id=CREATE_ID)
        result = await asyncio.wait_for(create_once(app), timeout=180)
        await asyncio.to_thread(save, app, result, CREATE_ID)
        event('complete', task_id=CREATE_ID, **result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Keep the durable write_started record on an uncertain outcome. No
        # reset/retry, and never claim zero writes after attempting the POST.
        event('stopped', task_id=CREATE_ID,
              reason=str(exc) if isinstance(exc, Stopped) else 'runtime_error',
              error_type=type(exc).__name__, exact_writes='inspect_durable_result')


async def run(app):
    if datetime.now(timezone.utc) >= EXPIRES:
        return
    if os.environ.get(CREATE_ENV) == CREATE_ID:
        await run_create(app)
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
