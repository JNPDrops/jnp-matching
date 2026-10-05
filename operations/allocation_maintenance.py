"""Daily allocation-rule maintenance and full 1360 review.

Only allocation rules may be written. Bank entries and matching remain in Exact.
Tax references are reusable: a Vpb instalment is not settlement evidence.
"""
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import time
import unicodedata
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from operations import allocation_connection as allocation, bacs_debtor_transfer as m
from operations import tax_reference as tax, woo_iban_rules as woo
from operations.tax_allocation import ROOT
from operations import bank_resolution as resolution
from operations.bank_review_dashboard import decorate
from operations import task_drain, maintenance_write_operations as writes
from operations.worker_write_fence import audit_metadata
from operations.worker_coordination import BudgetDeferred
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
log = logging.getLogger('uvicorn.error')
LOCK = 397775213600
VERSION = 'maintenance-v5'
STATUS = {'state': 'starting', 'bank_writes': False, 'automatically_executed': False}
FIELDS = ('Account', 'AccountBankAccount', 'GLAccount', 'Words', 'Costcenter', 'Costunit', 'VATCode')
BANK_FIELDS = 'ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,GLAccountCode,Modified'
READS = {'financialtransaction/BankEntryLines', 'financialtransaction/BankEntries', 'financialtransaction/TransactionLines',
         'cashflow/Receivables', 'cashflow/Payments', 'read/financial/ReceivablesList', 'read/financial/PayablesList',
         'crm/Accounts', 'financial/GLAccounts', 'financial/Journals', 'salesentry/SalesEntries'}
PSP = re.compile(r'icepay|paynetics|fibonat|stripe|paypal|plisio|ninja|suap|myco', re.I)
BOSCI = re.compile(r'(?<![a-z0-9_])bosci_[a-f0-9]{29}(?:[a-f0-9]{3})?(?![a-z0-9_])', re.I)


class MaintenanceDrained(Exception):
    pass


class MaintenanceAPI(woo.ExactAPI):
    def __init__(self, app, limits):
        super().__init__(app, role='maintenance', priority='routine', floor=200)
        self.limits = dict(limits)
        self.allowed_posts = {}
        self.allowed_deletes = set()
        self.post_count = 0

    async def request(self, method, url, params=None, payload=None):
        if task_drain.requested() and not audit_metadata().get('worker_operation_id'):
            raise MaintenanceDrained()
        p = urlparse(url)
        prefix = f'/api/v1/{m.DIVISION}/'
        collection = p.path == urlparse(ROOT).path
        entity = re.fullmatch(re.escape(urlparse(ROOT).path) + r"\(guid'([0-9a-f-]{36})'\)", p.path)
        allowed_read = collection or (p.path.startswith(prefix) and p.path[len(prefix):] in READS)
        allowed = method == 'GET' and payload is None and allowed_read
        if method == 'POST':
            allowed = url == ROOT and isinstance(payload, dict) and payload == self.allowed_posts.get(payload.get('Words'))
        elif method == 'DELETE':
            allowed = bool(entity and entity[1] in self.allowed_deletes and payload is None and not p.query)
        if not allowed or p.scheme != 'https' or p.netloc != 'start.exactonline.nl' or p.username or p.fragment:
            raise m.Stop('Maintenance resource/method rejected')
        if method == 'GET':
            STATUS['read_stage'] = p.path.split(str(m.DIVISION) + '/', 1)[-1]
        if self.limits.get('remaining', 1000) <= 200:
            raise m.Stop('Maintenance API reserve')
        await asyncio.sleep(max(0, 1.2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        token = await self.app._access_token()
        if method == 'POST':
            self.post_count += 1
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False, verify=m.TLS_CONTEXT) as client:
            from operations.worker_coordination import budgeted_http
            r = await budgeted_http(self.app, self.role, method,
                lambda: client.request(method, url, params=params, json=payload,
                    headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}),
                priority=self.priority, floor=self.floor)
        self.limits = {name: int(r.headers[h]) for name, h in (
            ('remaining', 'x-ratelimit-remaining'), ('reset_ms', 'x-ratelimit-reset')) if r.headers.get(h, '').isdigit()}
        expected = (200,) if method == 'GET' else ((200, 201, 204) if method == 'POST' else (200, 204))
        if r.status_code not in expected:
            raise m.ExactRequestError(method, r.status_code, self.limits)
        return r.json() if r.content else {}


def signature(rule):
    # Do not merge merely similar words or different analytical/VAT dimensions.
    return tuple(str(rule.get(k) or '') for k in FIELDS)


def duplicates(rules, preferred=()):
    groups = defaultdict(list)
    for r in rules:
        if r.get('Words') or r.get('AccountBankAccount'):
            groups[signature(r)].append(r)
    result = []
    for group in groups.values():
        group.sort(key=lambda r: (str(r['ID']) not in preferred, str(r['ID'])))
        result.extend((r, group[0]) for r in group[1:])
    return result


def order_reference(description):
    if tax.tax_hint({'Description': description}) or PSP.search(description) or resolution.REFUND.search(description):
        return None
    explicit = re.findall(r'(?<![A-Za-z0-9])(?:TD|order(?:\s*(?:number|nummer|no\.?))?\s*[:#-]?\s*)([0-9]{4,7})(?![0-9])', description, re.I)
    numbers = explicit or re.findall(r'(?<![A-Za-z0-9])([0-9]{5,6})(?![A-Za-z0-9])', BOSCI.sub('', description))
    return 'TD' + numbers[0] if len(set(numbers)) == 1 else None


def vat_refund(bank, metadata):
    """Explicit operator policy: unambiguous BTW refunds go to 1770.

    A description can establish the tax without a reversible payment reference.
    It never establishes settlement of the return or any interest component.
    """
    source = dict(bank)
    if str(bank.get('Account') or '').lower() == str(metadata['tax_account_id']).lower():
        source['AccountName'] = 'Belastingdienst'
    decision = tax.vat_refund_decision(source)
    if not decision:
        return None
    gl = [a for a in metadata['accounts'] if str(a.get('Code') or '').strip() == '1770'
          and a.get('BalanceType') == 'B' and a.get('IsBlocked') is False]
    if len(gl) != 1:
        return None
    return {'GLAccount': m.guid(gl[0]['ID']), 'Words': decision['allocation_words']}


def normal_name(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    text = re.sub(r'\b(?:b\.?\s*v\.?|ltd|limited|inc|gmbh)\b', ' ', text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def supplier_proposal(bank, suppliers, history_counts):
    desc = str(bank.get('Description') or '').strip()
    if tax.tax_hint(bank) or m.amount(bank['AmountDC']) >= 0 or resolution.is_psp(bank) or resolution.REFUND.search(desc):
        return None
    text = ' ' + normal_name(desc) + ' '
    matches = []
    for supplier in suppliers:
        name = normal_name(str(supplier.get('Name') or ''))
        if len(name) >= 5 and ' ' + name + ' ' in text and supplier.get('IsSupplier') is True and not supplier.get('EndDate'):
            matches.append(supplier)
    if len(matches) != 1 or history_counts[str(matches[0]['ID']).lower()] < 2:
        return None
    if not 6 <= len(desc) <= 120:
        return None
    # Use the complete observed description, not a broad brand/PSP fragment.
    return {'Account': m.guid(matches[0]['ID']), 'Words': desc}


def classify(bank, rules, receivables):
    desc = str(bank.get('Description') or '').strip()
    result = {'bank_line_id': m.guid(bank['ID']), 'bank_entry_id': m.guid(bank['EntryID']),
              'bank_date': str(bank.get('Date')), 'description': desc, 'amount': str(bank['AmountDC']),
              'status': 'review', 'reason': 'Geen unieke onderbouwing', 'bank_write': False}
    from operations.tax_agent import candidate
    decision = candidate(bank)
    if decision:
        result.update(status='tax_review' if decision['status'] != 'TAX_IDENTIFIED' else 'tax_rule_ready',
                      reason=decision['reason'], tax=decision)
        return result
    if resolution.is_psp(bank):
        result.update(status='psp_deferred', reason='PSP-bankdagboek en kruispostrekening worden later ingericht')
        return result
    if m.amount(bank['AmountDC']) < 0:
        if resolution.REFUND.search(desc):
            result.update(status='refund_review', reason='Terugbetaling apart beoordelen'); return result
        found = [r for r in rules if str(r.get('Words') or '').strip()
                 and str(r['Words']).lower() in desc.lower() and not r.get('AccountBankAccount')]
        targets = {signature(r) for r in found}
        if len(targets) == 1:
            result.update(status='existing_rule_ready', reason='Bestaande tekstregel beschikbaar voor Automatically', rule_id=found[0]['ID'])
        else:
            result.update(status='supplier_review', reason='Leverancier/factuur of kostenonderbouwing nodig')
        return result
    reference = order_reference(desc)
    result['reference'] = reference
    if not reference:
        return result
    matches = [r for r in receivables if str(r.get('YourRef') or '').strip().upper() == reference]
    if len(matches) != 1:
        result.update(reason='Geen unieke openstaande factuur', status='invoice_review'); return result
    rec = matches[0]
    result['account_code'] = str(rec.get('AccountCode') or '').strip()
    if rec.get('CurrencyCode') != 'EUR' or m.amount(rec['Amount']) != m.amount(bank['AmountDC']) or m.amount(bank['AmountDC']) <= 0:
        result.update(reason='Bedrag of valuta wijkt af; ook kleine verschillen blijven open', status='amount_review'); return result
    if result['account_code'] != '109372':
        result.update(reason='Factuur staat niet op de banktransferdebiteur', status='account_review'); return result
    # Webshop invoices arrive after shipment. Validate order creation instead in
    # resolution.order_evidence; an earlier payment is not an invoice-date error.
    result.update(status='order_evidence_needed', reason='Unieke factuur en exact bedrag; controleer betaalmethode', receivable=rec)
    return result


def proposal(item, order, reference_counts):
    if item['status'] != 'order_evidence_needed' or reference_counts[item['reference']] != 1:
        return None
    rec = item['receivable']
    if not order or order.get('order_number') != '#' + item['reference'][2:] or order.get('payment_method') != 'bacs' or order.get('currency') != 'EUR':
        return None
    if order.get('status') not in ('completed', 'processing', 'on-hold', 'pending') or m.amount(order.get('total_refunds')) != 0 or m.amount(order.get('total')) != m.amount(item['amount']):
        return None
    code = BOSCI.findall(item['description'])
    words = code[0][:35].lower() if len(code) == 1 else item['description']
    if not words or len(words) > 120 or (not code and not re.search(r'[A-Za-z]{3}', words)):
        return None
    return {'Account': m.guid(rec['AccountId']), 'Words': words}


def can_retire_payment(body, bank_rows, open_refs, now):
    if 'bank_reference' not in body or now.date() - tax.bank_date(body['payment_date']) < timedelta(days=90):
        return False
    words = woo.camt_words(body['bank_reference'])
    number = str(body['order_number']).lstrip('#')
    reference = number if number.upper().startswith('TD') else 'TD' + number
    if reference.upper() in open_refs or len(bank_rows) != 1:
        return False
    b = bank_rows[0]
    return (words in str(b.get('Description') or '').lower() and str(b.get('GLAccountCode') or '').strip() == '1100'
            and str(b.get('AccountCode') or '').strip() == '109372'
            and m.amount(b.get('AmountDC')) == m.amount(body['amount'])
            and m.amount(b.get('AmountFC')) == m.amount(body['amount']))


def initialize(conn):
    writes.initialize(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_allocation_maintenance (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton), enabled BOOLEAN NOT NULL DEFAULT TRUE,
        next_scan TIMESTAMPTZ, next_cleanup TIMESTAMPTZ, cache JSONB, cache_at TIMESTAMPTZ,
        version TEXT, summary JSONB NOT NULL DEFAULT '{}')''')
    conn.execute('INSERT INTO jnp_allocation_maintenance(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_suspense_review (
        bank_line_id UUID PRIMARY KEY, details JSONB NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_bank_identity_review (
        cashflow_id UUID PRIMARY KEY, details JSONB NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    initialize_cleanup(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_suspense_rules (
        words TEXT PRIMARY KEY, payload JSONB NOT NULL, bank_line_id UUID NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', rule_id UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    conn.execute('ALTER TABLE jnp_woo_iban_events ADD COLUMN IF NOT EXISTS rule_retired_at TIMESTAMPTZ')


def initialize_cleanup(conn):
    # Tax and maintenance can initialize in different processes.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('jnp:rule-cleanup-schema',0))")
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_rule_cleanup_audit (
            rule_id UUID PRIMARY KEY, payload JSONB NOT NULL, keeper_id UUID, reason TEXT NOT NULL,
            state TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


async def delete_rule(conn, api, rule, reason, keeper=None):
    rule_id = m.guid(rule['ID'])
    # Session lock covers both the remote read/write and the durable result.
    # A try-lock never blocks the event loop behind another role's HTTP request.
    lock_key = 'jnp:allocation-rule:' + rule_id
    if not conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (lock_key,)).fetchone()[0]:
        return 'busy_keep'
    try:
        return await _delete_rule(conn, api, rule, reason, keeper)
    finally:
        conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (lock_key,))


async def _delete_rule(conn, api, rule, reason, keeper=None):
    rule_id = m.guid(rule['ID'])
    previous = conn.execute('SELECT state FROM jnp_rule_cleanup_audit WHERE rule_id=%s',
                            (rule_id,)).fetchone()
    # Fresh read immediately before deletion. Never delete an operator-edited rule.
    fresh = await api.rules()
    found = [r for r in fresh if r['ID'] == rule_id]
    if not found:
        state = 'deleted'
    elif previous and previous[0] in ('deleting', 'uncertain', 'deleted'):
        # Preserve an uncertain intent even when the operator changed the rule.
        # Continued presence is never authorization to send DELETE again.
        return 'uncertain'
    elif len(found) != 1 or signature(found[0]) != signature(rule):
        state = 'changed_keep'
    elif keeper and not any(r['ID'] == keeper and signature(r) == signature(rule) for r in fresh):
        state = 'keeper_missing_keep'
    else:
        state = 'deleting'
    conn.execute('''INSERT INTO jnp_rule_cleanup_audit(rule_id,payload,keeper_id,reason,state)
        VALUES(%s,%s::jsonb,%s,%s,%s) ON CONFLICT(rule_id) DO NOTHING''',
        (rule_id, json.dumps({k: rule.get(k) for k in FIELDS}), keeper, reason, state))
    if state == 'deleting':
        conn.execute("UPDATE jnp_rule_cleanup_audit SET state='deleting',updated_at=NOW() WHERE rule_id=%s", (rule_id,))
        api.allowed_deletes.add(rule_id)
        try:
            await api.request('DELETE', ROOT + "(guid'" + rule_id + "')")
            state = 'deleted' if not any(r['ID'] == rule_id for r in await api.rules()) else 'uncertain'
        except Exception:
            state = 'uncertain'
        finally:
            api.allowed_deletes.discard(rule_id)
    conn.execute('UPDATE jnp_rule_cleanup_audit SET state=%s,updated_at=NOW() WHERE rule_id=%s', (state, rule_id))
    if state == 'deleted' and keeper:
        conn.execute('UPDATE jnp_woo_iban_events SET rule_id=%s WHERE rule_id=%s', (keeper, rule_id))
        conn.execute('UPDATE jnp_tax_rules SET rule_id=%s WHERE rule_id=%s', (keeper, rule_id))
        conn.execute('UPDATE jnp_suspense_rules SET rule_id=%s WHERE rule_id=%s', (keeper, rule_id))
    return state


async def create_rule(conn, api, item, payload):
    words = payload['Words']
    conn.execute('''INSERT INTO jnp_suspense_rules(words,payload,bank_line_id) VALUES(%s,%s::jsonb,%s)
        ON CONFLICT(words) DO NOTHING''', (words, json.dumps(payload), item['bank_line_id']))
    previous, stored = conn.execute('SELECT state,payload FROM jnp_suspense_rules WHERE words=%s', (words,)).fetchone()
    if previous == 'retired' and stored == payload:
        conn.execute("UPDATE jnp_suspense_rules SET bank_line_id=%s,created_at=NOW(),state='pending' WHERE words=%s", (item['bank_line_id'], words))
    rules = await api.rules()
    found = [r for r in rules if str(r.get('Words') or '').strip() == words]
    exact = [r for r in found if signature(r) == signature(payload)]
    state, rule_id = 'conflict', None
    if stored != payload or len(found) != len(exact) or len(found) > 1:
        pass
    elif len(exact) == 1:
        state, rule_id = 'confirmed', exact[0]['ID']
    elif previous in ('creating', 'uncertain', 'confirmed', 'conflict'):
        state = 'uncertain'
    else:
        # Verify that the exact source line is still on 1360 before preparing its rule.
        fresh = await api.rows('financialtransaction/BankEntryLines', params={'$filter': "ID eq guid'" + item['bank_line_id'] + "'", '$select': BANK_FIELDS})
        if len(fresh) != 1 or fresh[0]['GLAccountCode'] != '1360' or str(fresh[0]['Description'] or '').strip() != item['description'] or m.amount(fresh[0]['AmountDC']) != m.amount(item['amount']):
            state = 'source_changed'
        else:
            if item.get('reference'):
                current = await api.rows('read/financial/ReceivablesList', params={
                    '$filter': "YourRef eq '" + item['reference'] + "'", '$select': 'AccountId,AccountCode,Amount,CurrencyCode,YourRef'})
                headers = await api.rows('financialtransaction/BankEntries', params={
                    '$filter': "EntryID eq guid'" + item['bank_entry_id'] + "'", '$select': 'EntryID,Currency'})
                if (len(current) != 1 or current[0]['CurrencyCode'] != 'EUR'
                        or current[0]['AccountId'] != payload['Account']
                        or m.amount(current[0]['Amount']) != m.amount(item['amount'])
                        or len(headers) != 1 or headers[0]['Currency'] != 'EUR'):
                    conn.execute("UPDATE jnp_suspense_rules SET state='source_changed' WHERE words=%s", (words,))
                    return 'source_changed'
            return await writes.create_confirmed_rule(conn, api, payload)
    conn.execute('UPDATE jnp_suspense_rules SET state=%s,rule_id=%s WHERE words=%s', (state, rule_id, words))
    return state


async def cleanup(conn, api, recs, now, banks):
    preferred = {str(r[0]) for r in conn.execute('SELECT rule_id FROM jnp_tax_rules WHERE rule_id IS NOT NULL').fetchall()}
    preferred.update(str(r[0]) for r in conn.execute('SELECT rule_id FROM jnp_woo_iban_events WHERE rule_id IS NOT NULL').fetchall())
    cleaned = Counter()
    for duplicate, keeper in duplicates(await api.rules(), preferred)[:20]:
        if task_drain.requested(): return cleaned
        cleaned[await writes.delete_confirmed_rule(conn, api, duplicate, 'identical_duplicate', keeper['ID'])] += 1
    open_refs = {str(r.get('YourRef') or '').strip().upper() for r in recs}
    old_jobs = conn.execute("SELECT event_id,body,rule_id FROM jnp_woo_iban_events WHERE state='done' AND rule_id IS NOT NULL AND rule_retired_at IS NULL").fetchall()
    for event_id, body, rule_id in old_jobs:
        if task_drain.requested(): return cleaned
        if 'bank_reference' not in body or now.date() - tax.bank_date(body['payment_date']) < timedelta(days=90):
            continue
        words = woo.camt_words(body['bank_reference'])
        matching = await api.rows('financialtransaction/BankEntryLines', params={
            '$filter': "substringof('" + words + "',Description)", '$select': BANK_FIELDS})
        if not can_retire_payment(body, matching, open_refs, now):
            continue
        candidates = [r for r in await api.rules() if str(r['ID']) == str(rule_id) and r.get('Words') == words
                      and not any(r.get(k) for k in ('GLAccount','AccountBankAccount','Costcenter','Costunit','VATCode'))
                      and r.get('Account') == matching[0]['Account']]
        if len(candidates) == 1:
            checkpoint = lambda: conn.execute("UPDATE jnp_woo_iban_events SET rule_retired_at=NOW(),reason='Toegewezen betaling; factuur niet meer open; regel na 90 dagen gearchiveerd' WHERE event_id=%s", (event_id,))
            state = await writes.delete_confirmed_rule(conn, api, candidates[0], 'bosci_allocated_invoice_closed_90_days', checkpoint=checkpoint)
            cleaned[state] += 1
    for words, payload, bank_id, rule_id, created_at in conn.execute(
            "SELECT words,payload,bank_line_id,rule_id,created_at FROM jnp_suspense_rules WHERE state='confirmed' AND rule_id IS NOT NULL").fetchall():
        if task_drain.requested(): return cleaned
        if payload.get('GLAccount') or now - created_at < timedelta(days=90):
            continue  # Tax rules remain available for refunds and instalments.
        if any(words.lower() in str(b.get('Description') or '').lower() for b in banks):
            continue
        lines = await api.rows('financialtransaction/BankEntryLines', params={
            '$filter': "ID eq guid'" + m.guid(bank_id) + "'", '$select': BANK_FIELDS})
        if len(lines) != 1 or lines[0].get('GLAccountCode') == '1360' or lines[0].get('Account') != payload['Account']:
            continue
        reference = order_reference(str(lines[0].get('Description') or ''))
        if reference and reference in open_refs:
            continue
        found = [r for r in await api.rules() if str(r['ID']) == str(rule_id) and signature(r) == signature(payload)]
        if len(found) == 1:
            checkpoint = lambda: conn.execute("UPDATE jnp_suspense_rules SET state='retired' WHERE words=%s", (words,))
            state = await writes.delete_confirmed_rule(conn, api, found[0], 'transaction_assigned_90_days', checkpoint=checkpoint)
            cleaned[state] += 1
    return cleaned


async def run(app, conn, api, now):
    suspense = await api.rows('financialtransaction/BankEntryLines', params={
        '$filter': "GLAccountCode eq '1360' or GLAccountCode eq '2000'", '$select': BANK_FIELDS, '$orderby': 'ID'})
    recs = await api.rows('read/financial/ReceivablesList', params={'$select': resolution.OPEN_FIELDS})
    payables = await api.rows('read/financial/PayablesList', params={'$select': resolution.OPEN_FIELDS})
    journals = {str(j['Code']).strip(): j for j in await api.rows('financial/Journals', params={
        '$select': 'Code,Description,Type,BankAccountDescription'})}
    log.info('bank_resolution sources %s', json.dumps({
        'journals': {k: j.get('Type') for k, j in journals.items()},
        'receivables_by_journal': dict(Counter(resolution.code(r.get('JournalCode')) for r in recs)),
        'payables_by_journal': dict(Counter(resolution.code(r.get('JournalCode')) for r in payables))}))
    headers = await resolution.load_headers(api, [b['EntryID'] for b in suspense])
    suspense = [resolution.enrich_bank(b, headers, journals) for b in suspense]
    assigned, unresolved = await resolution.load_open_bank_items(api, recs, payables, journals, headers, BANK_FIELDS)
    banks = list({b['ID']: b for b in suspense + assigned}.values())
    rules = await api.rules()
    metadata = conn.execute('SELECT metadata FROM jnp_tax_control').fetchone()[0]
    if not metadata:
        raise m.Stop('Tax metadata not ready')
    peers = resolution.duplicate_candidates(banks)
    items = [resolution.review(b, classify(b, rules, recs), recs, payables, journals, peers) for b in banks]
    cache, cache_at = conn.execute('SELECT cache,cache_at FROM jnp_allocation_maintenance').fetchone()
    if not cache or not cache_at or now - cache_at >= timedelta(days=1):
        suppliers = await api.rows('crm/Accounts', params={'$filter': 'IsSupplier eq true', '$select': 'ID,Code,Name,IsSupplier,EndDate'})
        history = await api.rows('financialtransaction/BankEntryLines', params={
            '$filter': "AmountDC lt 0 and Account ne null and GLAccountCode ne '1360'",
            '$select': 'ID,Account', '$orderby': 'ID'})
        cache = {'suppliers': suppliers, 'history_counts': dict(Counter(str(b.get('Account') or '').lower() for b in history))}
        conn.execute('UPDATE jnp_allocation_maintenance SET cache=%s::jsonb,cache_at=%s', (json.dumps(cache), now))
    suppliers, history_counts = cache['suppliers'], Counter(cache['history_counts'])
    refund_proposals = {}
    for bank, item in zip(banks, items):
        if item['gl_account'] != '1360':
            continue
        payload = vat_refund(bank, metadata)
        if payload:
            refund_proposals[item['bank_line_id']] = payload
            item.update(status='vat_refund_rule_proposed', reason='Belastingdienst en btw-teruggaaf expliciet herkend; toewijzing naar 1770')
        elif item['status'] == 'supplier_review':
            payload = supplier_proposal(bank, suppliers, history_counts)
            if payload:
                refund_proposals[item['bank_line_id']] = payload
                item.update(status='supplier_rule_proposed', reason='Unieke leveranciersnaam en minstens twee geboekte betalingen op die relatie')
    references = sorted({x['reference'] for x in items if x.get('reference') and x['status'] in
        ('order_evidence_needed', 'psp_order_evidence_needed', 'invoice_missing', 'amount_review', 'account_review')})
    orders = {}
    order_lookup_failed = False
    from operations.metorik_bacs_evidence import lookup_orders
    for start in range(0, len(references), 100):
        try:
            orders.update((await lookup_orders(references[start:start + 100]))['orders'])
        except (m.Stop, httpx.HTTPError):
            # A unavailable source must not hide the bank exceptions themselves.
            order_lookup_failed = True
            break
    items = [resolution.order_evidence(x, orders.get('#' + str(x.get('reference') or '')[2:])) for x in items]
    items = await resolution.inspect_missing_invoices(api, items)
    items = resolution.block_shared_invoice_candidates(items)
    reference_counts = Counter(x.get('reference') for x in items)
    created = Counter()
    for item in items:
        if task_drain.requested():
            raise MaintenanceDrained()
        payload = refund_proposals.get(item['bank_line_id'])
        if item['gl_account'] == '1360' and item.get('order_check') == 'verified':
            payload = payload or proposal(item, orders.get('#' + str(item.get('reference') or '')[2:]), reference_counts)
        if payload and api.post_count < 25:
            state = await create_rule(conn, api, item, payload)
            created[state] += 1
            item.update(status='new_rule_' + state, payload=payload)
            if state == 'confirmed':
                item.update(next_action='Beoordeel de bevestigde regel en de eigen order/factuur vóór afhandeling',
                    retry_when='na ondersteunde afhandeling en nieuwe uitlezing', reason='Toewijzingsregel bevestigd; bankregel nog niet afgehandeld')
        item.update(decorate(item, now.isoformat()))
    # Publish a full snapshot together: interrupted scans leave the last full
    # worklist available, including the unresolved identities and instructions.
    with conn.transaction():
        for item in items:
            conn.execute('''INSERT INTO jnp_suspense_review(bank_line_id,details) VALUES(%s,%s::jsonb)
                ON CONFLICT(bank_line_id) DO UPDATE SET details=EXCLUDED.details,observed_at=NOW()''',
                (item['bank_line_id'], json.dumps(item)))
        conn.execute('DELETE FROM jnp_suspense_review WHERE NOT (bank_line_id = ANY(%s::uuid[]))', ([b['ID'] for b in banks],))
        for item in unresolved:
            item.update(decorate(item, now.isoformat()))
            conn.execute('''INSERT INTO jnp_bank_identity_review(cashflow_id,details) VALUES(%s,%s::jsonb)
                ON CONFLICT(cashflow_id) DO UPDATE SET details=EXCLUDED.details,observed_at=NOW()''',
                (item['cashflow_id'], json.dumps(item)))
        conn.execute('DELETE FROM jnp_bank_identity_review WHERE NOT (cashflow_id = ANY(%s::uuid[]))', ([x['cashflow_id'] for x in unresolved],))
    next_cleanup = conn.execute('SELECT next_cleanup FROM jnp_allocation_maintenance').fetchone()[0]
    cleaned = Counter()
    if not task_drain.requested() and (not next_cleanup or now >= next_cleanup):
        conn.execute('UPDATE jnp_allocation_maintenance SET next_cleanup=%s', (now + timedelta(days=1),))
        cleaned = await cleanup(conn, api, recs, now, banks)
    counts = Counter(x['status'] for x in items)
    summary = {'state': 'ready', 'read_stage': 'complete', 'last_scan': now.isoformat(), 'interval_hours': 1, 'cleanup_interval_hours': 24,
               'bank_lines_1360': sum(b.get('GLAccountCode') == '1360' for b in suspense),
               'bank_lines_2000': sum(b.get('GLAccountCode') == '2000' for b in suspense),
               'assigned_open_bank_lines': len(assigned), 'includes_psp_journals': True,
               'match_candidates': sum(x['status'].endswith('_match_candidate') for x in items),
               'dashboard_items': len(items) + len(unresolved), 'order_lookup_incomplete': order_lookup_failed,
               'reviewed_bank_lines': len(banks), 'identity_review_count': len(unresolved),
               'review_version': resolution.VERSION, 'classifications': dict(counts), 'rules_checked': len(rules),
               'rule_writes': dict(created), 'cleanup': dict(cleaned), 'bank_writes': False,
               'automatically_executed': False, 'refresh_required': any(x['status'] in ('new_rule_confirmed','existing_rule_ready','tax_rule_ready') for x in items), 'vpb_instalments_preserved': True, 'tax_rules_auto_expiry': False}
    # Private operational logs contain concrete follow-up evidence; public status
    # remains aggregate-only. Never claim that a match candidate was executed.
    log.info('allocation_maintenance review %s', json.dumps({'summary': summary}))
    fields = ('bank_line_id','entry_number','description','amount','status','reference','account_code',
        'journal','next_action','reason','invoice_reference','invoice_entry','invoice_open_amount',
        'remaining_bank_amount','difference','order_total','order_status','payment_method','possible_duplicate_ids')
    for start in range(0, len(items), 20):
        log.info('bank_resolution details %s', json.dumps({'scan': now.isoformat(), 'offset': start,
            'items': [{k: x.get(k) for k in fields if x.get(k) is not None} for x in items[start:start + 20]]}))
    if unresolved:
        log.info('bank_resolution identity_review %s', json.dumps(unresolved))
    return summary


async def cycle(app):
    if task_drain.requested(): return
    if app.DIVISION != m.DIVISION or app.BASE_URL != m.BASE or not app.DATABASE_URL:
        STATUS['state'] = 'configuration_error'; return
    with app._db_connect() as conn:
        initialize(conn)
        from operations import tax_agent
        if not conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]: return
        locks = []
        try:
            enabled, next_scan, version, summary = conn.execute('SELECT enabled,next_scan,version,summary FROM jnp_allocation_maintenance').fetchone()
            STATUS.update(summary or {})
            if not enabled:
                STATUS['state'] = 'paused'; return
            if conn.execute('SELECT enabled FROM jnp_tax_control').fetchone()[0] is not True:
                STATUS['state'] = 'paused'; return
            now = datetime.now(timezone.utc)
            if next_scan and now < next_scan and version == VERSION: return
            # Every HTTP call reserves its quota in the shared database.
            limits = {}
            for key in (woo.LOCK, tax_agent.LOCK_ID):
                if not conn.execute('SELECT pg_try_advisory_lock(%s)', (key,)).fetchone()[0]: return
                locks.append(key)
            conn.execute('UPDATE jnp_allocation_maintenance SET next_scan=%s,version=%s', (now + timedelta(hours=1), VERSION))
            api = MaintenanceAPI(allocation.RoutingApp(app), limits)
            STATUS['state'] = 'scanning'
            summary = await run(app, conn, api, now)
            conn.execute('UPDATE jnp_allocation_maintenance SET summary=%s::jsonb', (json.dumps(summary),))
            STATUS.update(summary)
        except MaintenanceDrained:
            STATUS['state'] = 'draining'
        except BudgetDeferred:
            STATUS['state'] = 'waiting_for_budget'
        except asyncio.CancelledError:
            STATUS['state'] = 'interrupted'
            raise
        except Exception:
            STATUS['state'] = 'error'
            raise
        finally:
            try:
                conn.execute('UPDATE jnp_allocation_maintenance SET summary=%s::jsonb', (json.dumps(STATUS),))
            finally:
                for key in reversed(locks + [LOCK]):
                    conn.execute('SELECT pg_advisory_unlock(%s)', (key,))


async def serve(app):
    while not task_drain.requested():
        try:
            await cycle(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATUS['state'] = 'error'
            log.warning('allocation_maintenance error type=%s status=%s stage=%s', type(exc).__name__, exc.status_code if isinstance(exc, m.ExactRequestError) else None, STATUS.get('read_stage'))
        await task_drain.wait(60)


@router.get('/api/allocation-maintenance/status')
async def status():
    from app import main
    from operations.worker_coordination import status_snapshot
    try:
        with main._db_connect() as conn:
            conn.execute('SET default_transaction_read_only=on')
            row = conn.execute('SELECT enabled,summary FROM jnp_allocation_maintenance').fetchone()
            roles = status_snapshot(conn, main.DIVISION)['roles']
        return {**row[1], 'enabled': row[0], 'available': True,
                'worker': next((r for r in roles if r['role'] == 'maintenance'), None)}
    except Exception:
        return {'available': False, 'state': 'unavailable'}


class PrivateJSONResponse(JSONResponse):
    def __init__(self, *args, **kwargs):
        kwargs['headers'] = {**kwargs.get('headers', {}), 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}
        super().__init__(*args, **kwargs)


@router.get('/api/allocation-maintenance/report', response_class=PrivateJSONResponse)
async def report(request: Request):
    config = allocation.configuration()
    if not allocation.configured(config) or request.session.get('allocation_operator') != allocation.store_key(config):
        raise HTTPException(401, 'Operatoraanmelding via /allocation/login vereist')
    from app import main
    with main._db_connect() as conn:
        conn.execute('SET default_transaction_read_only=on')
        saved = conn.execute('SELECT summary FROM jnp_allocation_maintenance').fetchone()
        rows = conn.execute('SELECT details FROM jnp_suspense_review ORDER BY observed_at DESC,bank_line_id').fetchall()
        identities = conn.execute('SELECT details FROM jnp_bank_identity_review ORDER BY observed_at DESC,cashflow_id').fetchall()
    from operations.bank_review_dashboard import report_data
    return report_data(saved[0] if saved else {}, [r[0] for r in rows], [r[0] for r in identities])


@router.get('/allocation/review')
async def review_page(request: Request):
    try:
        data = await report(request)  # reuse the existing operator session
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return HTMLResponse('<!doctype html><html lang="nl"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Betalingen afhandelen · aanmelden</title><body style="font:17px system-ui;max-width:700px;margin:60px auto;padding:20px">'
            '<h1>Betalingen afhandelen</h1><p>Meld je aan om de betaalgegevens en afhandelinstructies te bekijken.</p>'
            '<p><a href="/allocation/login">Aanmelden via Exact Online</a></p></body></html>', status_code=401,
            headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'})
    from app import main
    return main.templates.TemplateResponse(request=request, name='bank_review.html', context=data,
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
