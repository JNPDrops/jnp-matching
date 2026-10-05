"""Tax recognition inside the existing service. Bank scans are strictly GET-only.

Existing BankEntryLines have no documented PUT operation. Do not turn a
classification into a fictitious completed booking or delete/reimport entries.
The separate tax_allocation module creates scoped allocation rules; the user
applies them in Exact through Automatically. No existing bank entry is rewritten.
"""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from operations import allocation_connection as allocation
from operations import bacs_debtor_transfer as transport
from operations import tax_reference as tax

router = APIRouter()
log = logging.getLogger('uvicorn.error')
LOCK_ID = 3977752867393
INTERVAL = 1800
SCAN_VERSION = 'tax-ledger-routing-v2'
RESERVE = 150
POLICY = json.loads(Path(__file__).with_name('tax_policy.json').read_text())
STATUS = {'state': 'starting', 'read_only': True, 'booking_writes': False,
          'existing_bank_update_supported': False, 'last_scan': None}
ACCOUNT_CACHE = []
READS = {'financial/GLAccounts', 'crm/Accounts', 'financialtransaction/BankEntryLines'}


class BudgetDeferred(transport.Stop):
    pass


class TaxAPI(transport.Exact):
    def __init__(self, app):
        super().__init__(app, role="tax", priority="bulk", floor=RESERVE)

    async def request(self, method, url, params=None, payload=None):
        parsed = urlparse(url)
        root = f'/api/v1/{transport.DIVISION}/'
        if (method != 'GET' or payload is not None or parsed.scheme != 'https'
                or parsed.netloc != 'start.exactonline.nl' or parsed.username
                or parsed.fragment or not parsed.path.startswith(root)
                or parsed.path[len(root):] not in READS):
            raise transport.Stop('Tax agent permits only scoped Exact reads')
        remaining = self.limits.get('remaining')
        if type(remaining) is int and remaining <= RESERVE:
            raise BudgetDeferred('Tax agent waiting for API budget')
        return await super().request(method, url, params=params)

    async def rows(self, resource, params=None):
        if resource not in READS:
            raise transport.Stop('Unsupported tax read resource')
        root = f'{transport.BASE}/api/v1/{transport.DIVISION}/{resource}'
        url, seen, result = root, set(), []
        for _ in range(200):
            if url in seen or urlparse(url).path != urlparse(root).path:
                raise transport.Stop('Invalid tax pagination')
            seen.add(url)
            payload = await self.request('GET', url, params=params)
            d = payload.get('d')
            batch = d.get('results') if isinstance(d, dict) else d
            if not isinstance(batch, list):
                raise transport.Stop('Invalid tax read response')
            result.extend({k: v for k, v in r.items() if k != '__metadata'} for r in batch)
            url = (d.get('__next') if isinstance(d, dict) else None) or payload.get('__next')
            if not url:
                return result
            params = None
        raise transport.Stop('Incomplete tax pagination')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_tax_control (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
        enabled BOOLEAN NOT NULL DEFAULT TRUE, cursor_at TIMESTAMPTZ,
        next_scan TIMESTAMPTZ, metadata JSONB, metadata_at TIMESTAMPTZ,
        summary JSONB NOT NULL DEFAULT '{}')''')
    conn.execute('INSERT INTO jnp_tax_control(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING')
    conn.execute('ALTER TABLE jnp_tax_control ADD COLUMN IF NOT EXISTS scan_version TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_tax_observations (
        bank_line_id UUID PRIMARY KEY, bank_entry_id UUID, bank_modified TEXT,
        bank_date TEXT, description TEXT, decision JSONB NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


async def metadata(api):
    accounts = await api.rows('financial/GLAccounts', {'$select':
        'ID,Code,Description,BalanceType,Type,IsBlocked,VATCode', '$orderby': 'Code'})
    log.info('tax_agent account_candidates %s', json.dumps(tax.account_candidates(accounts)))
    counterparties = await api.rows('crm/Accounts', {'$select': 'ID,Code,Name,IsSupplier,EndDate',
        '$filter': "Code eq '" + str(POLICY['tax_account_code']).rjust(18) + "'"})
    counterparties = [x for x in counterparties
                      if str(x.get('Code') or '').strip() == POLICY['tax_account_code']
                      and 'belastingdienst' in str(x.get('Name') or '').lower()
                      and x.get('IsSupplier') is True
                      and (not x.get('EndDate') or tax.bank_date(x['EndDate']) >= datetime.now(timezone.utc).date())]
    if len(counterparties) != 1:
        raise transport.Stop('Belastingdienst relation missing or ambiguous')
    return {'accounts': accounts, 'tax_account_id': transport.guid(counterparties[0]['ID'])}


def selection(account_id, cursor):
    terms = [f"Account eq guid'{transport.guid(account_id)}'"]
    for value in ('BELASTINGDIENST', 'Belastingdienst', 'belastingdienst', tax.RSIN[:8], tax.RSIN[2:8], *sorted(tax.TAX_IBANS)):
        terms.append(f"substringof('{value}',Description)")
    where = '(' + ' or '.join(terms) + ')'
    if cursor:
        since = (cursor - timedelta(minutes=5)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        where += f" and Modified ge datetime'{since}'"
    return {'$filter': where, '$orderby': 'Modified,ID', '$select':
            'ID,EntryID,EntryNumber,LineNumber,Date,Modified,Description,Account,AccountCode,AccountName,GLAccountCode,AmountDC'}


def candidate(bank):
    decision = tax.classify_bank_line(bank)
    if decision is None:
        return None
    return tax.add_account_proposal(decision, ACCOUNT_CACHE, POLICY['gl_accounts'])


async def cycle(app):
    global ACCOUNT_CACHE
    if app.DIVISION != transport.DIVISION or app.BASE_URL != transport.BASE or not app.DATABASE_URL:
        STATUS['state'] = 'configuration_error'
        return
    with app._db_connect() as conn:
        initialize(conn)
        if not conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]:
            return
        try:
            enabled, cursor, next_scan, saved, saved_at, summary, scan_version = conn.execute(
                'SELECT enabled,cursor_at,next_scan,metadata,metadata_at,summary,scan_version FROM jnp_tax_control').fetchone()
            if saved:
                ACCOUNT_CACHE = saved['accounts']
            if summary:
                STATUS.update(summary)
            if not enabled:
                STATUS['state'] = 'paused'
                return
            now = datetime.now(timezone.utc)
            if next_scan and now < next_scan and scan_version == SCAN_VERSION:
                return
            # Check shared observed routing quota before obtaining a fresh header.
            from operations.automatic_debtor_routing import STATUS as routing_status
            limits = routing_status.get('api_limits') or {}
            remaining, reset = limits.get('remaining'), limits.get('reset_ms')
            if type(remaining) is int and remaining <= RESERVE and type(reset) is int and reset / 1000 > now.timestamp():
                STATUS['state'] = 'waiting_for_api_budget'
                return
            api = TaxAPI(allocation.RoutingApp(app))
            if scan_version != SCAN_VERSION:
                # Preserve full-rescan intent even if the first attempt fails.
                conn.execute('UPDATE jnp_tax_control SET cursor_at=NULL')
            # Persist cadence before calls, including failed reads/restarts.
            conn.execute('UPDATE jnp_tax_control SET next_scan=%s,scan_version=%s',
                         (now + timedelta(seconds=INTERVAL), SCAN_VERSION))
            STATUS['read_stage'] = 'metadata'
            if not saved or not saved_at or now - saved_at >= timedelta(days=1):
                saved = await metadata(api)
                conn.execute('UPDATE jnp_tax_control SET metadata=%s::jsonb,metadata_at=%s', (json.dumps(saved), now))
                # Log account names/codes only, never bank data, amounts or references.
                candidates = tax.account_candidates(saved['accounts'])
                log.info('tax_agent account_candidates %s', json.dumps({k:
                    [{'code': a['code'], 'description': a['description']} for a in v]
                    for k, v in candidates.items()}))
            ACCOUNT_CACHE = saved['accounts']
            STATUS['read_stage'] = 'bank_lines'
            # Reclassify historical misallocations when recognition changes.
            banks = await api.rows('financialtransaction/BankEntryLines', selection(
                saved['tax_account_id'], cursor if scan_version == SCAN_VERSION else None))
            counts = Counter()
            observations = []
            history = Counter()
            templates = Counter()
            for bank in banks:
                decision = candidate(bank)
                if decision is None:
                    continue
                decision['existing_gl_account_code'] = str(bank.get('GLAccountCode') or '').strip()
                decision['existing_account_code'] = str(bank.get('AccountCode') or '').strip()
                decision['allocation_mismatch'] = bool(decision.get('gl_account_code')
                    and decision['existing_gl_account_code'] != decision['gl_account_code'])
                counts[decision['status']] += 1
                if decision.get('tax_bucket'):
                    history[(decision['tax_bucket'], decision['existing_gl_account_code'])] += 1
                    templates[(decision['tax_letter'], decision['subnumber'], decision['tax_year'],
                               decision['period_code'], decision['assessment_kind'], decision['direction'])] += 1
                observations.append((transport.guid(bank['ID']), transport.guid(bank['EntryID']),
                    str(bank.get('Modified') or ''), str(bank.get('Date') or ''),
                    str(bank.get('Description') or ''), json.dumps(decision)))
            with conn.transaction():
                for values in observations:
                    conn.execute('''INSERT INTO jnp_tax_observations
                        (bank_line_id,bank_entry_id,bank_modified,bank_date,description,decision)
                        VALUES(%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(bank_line_id) DO UPDATE SET
                        bank_entry_id=EXCLUDED.bank_entry_id,bank_modified=EXCLUDED.bank_modified,
                        bank_date=EXCLUDED.bank_date,description=EXCLUDED.description,
                        decision=EXCLUDED.decision,observed_at=NOW()''', values)
                summary = {'state': 'ready', 'last_scan': now.isoformat(), 'scanned_in_last_batch': len(banks),
                    'tax_lines_in_last_batch': len(observations), 'classifications': dict(counts),
                    'read_only': True, 'booking_writes': False, 'existing_bank_update_supported': False,
                    'account_mapping_complete': all(POLICY['gl_accounts'].values()),
                    'allocation_mismatches': conn.execute("SELECT COUNT(*) FROM jnp_tax_observations WHERE decision->>'allocation_mismatch' = 'true'").fetchone()[0]}
                conn.execute('UPDATE jnp_tax_control SET cursor_at=%s,summary=%s::jsonb', (now, json.dumps(summary)))
            STATUS.update(summary)
            log.info('tax_agent scan_complete %s', json.dumps({'read_only': True, 'counts': dict(counts),
                'historical_gl_counts': [{'tax': k[0], 'gl_code': k[1], 'count': v} for k, v in history.items()],
                'templates': [{'letter': k[0], 'subnumber': k[1], 'year': k[2], 'period': k[3],
                               'kind': k[4], 'direction': k[5], 'count': v} for k, v in templates.items()]}))
            from operations import tax_allocation
            try:
                await tax_allocation.sync(app, conn, POLICY, saved, api.limits)
                log.info('tax_agent allocation_rules %s', json.dumps(tax_allocation.STATUS))
            except Exception as exc:
                tax_allocation.STATUS['state'] = 'error'
                log.warning('tax_agent allocation_rules_error type=%s status=%s', type(exc).__name__,
                            exc.status_code if isinstance(exc, transport.ExactRequestError) else None)
        except BudgetDeferred:
            STATUS['state'] = 'waiting_for_api_budget'
        except transport.ExactRequestError as exc:
            STATUS.update(state='read_error', last_http_status=exc.status_code)
            log.warning('tax_agent read_error status=%s', exc.status_code)
        except transport.Stop as exc:
            reasons = {'Belastingdienst relation missing or ambiguous': 'relation_not_unique',
                       'Invalid tax pagination': 'pagination',
                       'Invalid tax read response': 'response_shape',
                       'Exact GET transport/auth failure; inspect audit before retrying': 'transport_or_auth'}
            reason = reasons.get(str(exc), 'other_guard')
            STATUS.update(state='read_error', read_error_reason=reason)
            log.warning('tax_agent read_error stage=%s reason=%s', STATUS.get('read_stage'), reason)
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))


async def serve(app):
    while True:
        try:
            await cycle(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATUS['state'] = 'read_error'
            log.warning('tax_agent read_error type=%s', type(exc).__name__)
        await asyncio.sleep(30)


@router.get('/api/tax/status')
async def status():
    from operations.tax_allocation import STATUS as RULE_STATUS
    return {**STATUS, 'allocation_rules': dict(RULE_STATUS)}


@router.get('/api/tax/report')
async def report(request: Request):
    # Reuse the existing operator session. No new public financial-data API.
    config = allocation.configuration()
    if not allocation.configured(config) or request.session.get('allocation_operator') != allocation.store_key(config):
        raise HTTPException(401, 'Operatoraanmelding via /allocation/login vereist')
    from app import main
    with main._db_connect() as conn:
        rows = conn.execute('''SELECT bank_line_id::text,bank_date,description,decision
            FROM jnp_tax_observations ORDER BY observed_at DESC,bank_line_id LIMIT 500''').fetchall()
        total = conn.execute('SELECT COUNT(*) FROM jnp_tax_observations').fetchone()[0]
    return {'read_only': True, 'booking_writes': False, 'total': total, 'shown': len(rows),
            'truncated': total > len(rows), 'account_candidates': tax.account_candidates(ACCOUNT_CACHE),
            'gl_mapping': POLICY['gl_accounts'], 'items': [
                {'bank_line_id': row[0], 'bank_date': row[1], 'description': row[2],
                 'tax': tax.add_account_proposal(row[3], ACCOUNT_CACHE, POLICY['gl_accounts'])}
                for row in rows]}
