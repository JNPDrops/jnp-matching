"""Explicit, expiring ICEPAY own-order matching; at most five saves per activation.

Only the verified 36 receipt identities are accepted. Native Exact matching,
no write-offs, durable save claims, private UI/API evidence, read-only recovery.
"""
import asyncio
from operations import worker_write_fence as fence, task_drain
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from operations.icepay_apply import APPLY, EXPECTED_SHA, reconcile
from operations.icepay_booking import EXPIRES
from operations.icepay_import import BASE, DIVISION

PREFIX = 'icepay-match-20261001-03-'
PREPARE = PREFIX + 'prepare-v1'
VERIFY = PREFIX + 'verify-v1'
RECOVER = PREFIX + 'recover-v1'
GROUPS = {PREFIX + f'group-{n:02d}-v1' for n in range(1, 9)}
TASKS = GROUPS | {PREPARE, VERIFY, RECOVER}
BANK = 'd4b87c16-4c50-43c4-9c35-b9d74dba204d'
DEBTOR = '0492e907-6698-4281-98e5-c46e01ae9219'
JOB = 'ICEPAY-20261001-03'
LOG = logging.getLogger('uvicorn.error')
LEDGER_SELECT = 'ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,Account,AccountCode,GLAccountCode,JournalCode,YourRef,PaymentReference,FinancialYear,FinancialPeriod,Currency'
OPEN_SELECT = 'AccountId,AccountCode,AccountName,Amount,AmountInTransit,CurrencyCode,Description,EntryNumber,InvoiceNumber,JournalCode,YourRef'


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result != result.quantize(Decimal('.01')):
        raise ValueError('invalid_money')
    return result


def norm(value):
    return str(value or '').strip().strip('{}').lower()


def code(row, key):
    return str(row.get(key) or '').strip()


def now():
    return datetime.now(timezone.utc).isoformat()


class Reader:
    def __init__(self, app):
        if app.DIVISION != DIVISION or app.BASE_URL != BASE:
            raise ValueError('wrong_administration')
        self.app, self.calls = app, 0

    async def rows(self, resource, params):
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        if resource not in {'financialtransaction/TransactionLines', 'read/financial/ReceivablesList'}:
            raise ValueError('resource_not_allowed')
        url = f'{BASE}/api/v1/{DIVISION}/{resource}'
        seen, result = set(), []
        while url:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.netloc != 'start.exactonline.nl'
                or parsed.path != f'/api/v1/{DIVISION}/{resource}' or parsed.fragment
                or url in seen or self.calls >= 80):
                raise ValueError('read_origin_or_budget')
            seen.add(url)
            self.calls += 1
            token = await self.app._access_token()
            async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False, verify=TLS_CONTEXT) as client:
                from operations.worker_coordination import budgeted_http
                response = await budgeted_http(self.app, 'icepay', 'GET',
                    lambda: client.get(url, params=params, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}),
                    priority='routine', floor=200)
            if response.status_code != 200:
                raise ValueError('api_http_' + str(response.status_code))
            raw = response.json()
            data = raw.get('d')
            batch = data.get('results') if isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise ValueError('invalid_api_rows')
            result.extend(batch)
            if len(result) > 6000:
                raise ValueError('read_row_budget')
            url = raw.get('__next') or (data.get('__next') if isinstance(data, dict) else None)
            if url and not batch:
                raise ValueError('empty_api_page')
            params = None
            await asyncio.sleep(2)
        return result

    async def ledger(self):
        return await self.rows('financialtransaction/TransactionLines', {
            '$filter': "JournalCode eq '27' and FinancialYear eq 2026", '$select': LEDGER_SELECT})

    async def orders(self, references, history=False):
        references = sorted(set(references))
        if not references or any(not re.fullmatch(r'TD\d+', ref) for ref in references):
            raise ValueError('invalid_order_scope')
        result = []
        for offset in range(0, len(references), 12):
            query = '(' + ' or '.join("YourRef eq '" + ref + "'" for ref in references[offset:offset+12]) + ')'
            if history:
                query = "JournalCode eq '70' and " + query
            result.extend(await self.rows('financialtransaction/TransactionLines' if history else 'read/financial/ReceivablesList', {
                '$filter': query, '$select': LEDGER_SELECT if history else OPEN_SELECT}))
        return result


def init_claim(app, task):
    with app._db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS icepay_order_matching (
            job TEXT PRIMARY KEY, data JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS icepay_matching_runs (
            task TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), summary JSONB NOT NULL DEFAULT '{}'::jsonb)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS icepay_matching_saves (
            bank_line_id TEXT PRIMARY KEY, task TEXT NOT NULL, attempted_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        return conn.execute('INSERT INTO icepay_matching_runs(task) VALUES(%s) ON CONFLICT DO NOTHING RETURNING task', (task,)).fetchone() is not None


def load_plan(app):
    with app._db_connect() as conn:
        row = conn.execute('SELECT data FROM icepay_order_matching WHERE job=%s', (JOB,)).fetchone()
    if not row:
        raise ValueError('prepare_required')
    return row[0]


def store_plan(app, plan):
    with app._db_connect() as conn:
        conn.execute('''INSERT INTO icepay_order_matching(job,data) VALUES(%s,%s::jsonb)
            ON CONFLICT(job) DO UPDATE SET data=EXCLUDED.data,updated_at=now()''', (JOB, json.dumps(plan)))


def claim_save(app, task, receipt, plan):
    # Claim and unknown-outcome evidence commit atomically before the browser save.
    with app._db_connect() as conn:
        claimed = conn.execute('''INSERT INTO icepay_matching_saves(bank_line_id,task) VALUES(%s,%s)
            ON CONFLICT DO NOTHING RETURNING bank_line_id''', (receipt['bank_line_id'], task)).fetchone()
        if not claimed:
            raise ValueError('prior_save_claim_recovery_only')
        receipt.update(state='match_requested', execution_status='unknown_readback_required')
        receipt['attempts'].append({'at': now(), 'task': task, 'outcome': 'unknown_readback_required',**fence.audit_metadata()})
        conn.execute('UPDATE icepay_order_matching SET data=%s::jsonb,updated_at=now() WHERE job=%s', (json.dumps(plan), JOB))


def source_manifest(app):
    with app._db_connect() as conn:
        row = conn.execute('SELECT artifacts,summary FROM icepay_receipt_import_runs WHERE task=%s', (APPLY,)).fetchone()
    if not row or row[1].get('state') != 'import_verified' or row[1].get('verified_receipts') != 36 or row[1].get('xml_sha256') != EXPECTED_SHA:
        raise ValueError('verified_import_required')
    manifest = row[0]['manifest']
    if len(manifest) != 36 or len({r['payment_id'] for r in manifest}) != 36 or sum(money(r['amount']) for r in manifest) != Decimal('3161.24'):
        raise ValueError('source_manifest_changed')
    return manifest


def source_receipts(manifest, ledger):
    if not reconcile(manifest, ledger)['complete']:
        raise ValueError('source_ledger_changed')
    result = []
    for item in manifest:
        rows = [r for r in ledger if r.get('Description') == item['description']]
        bank = [r for r in rows if code(r, 'GLAccountCode') == '1317']
        offset = [r for r in rows if code(r, 'GLAccountCode') == '1100']
        if len(bank) != 1 or len(offset) != 1 or bank[0]['EntryID'] != offset[0]['EntryID'] or any(r.get('Currency') != 'EUR' for r in rows):
            raise ValueError('source_identity_changed')
        result.append({**item, 'source_order': item['ref'], 'trx': item['payment_id'],
            'bank_line_id': bank[0]['ID'], 'offset_id': offset[0]['ID'], 'entry_id': bank[0]['EntryID'],
            'state': 'pending', 'invoice': None, 'exception': None, 'evidence': [], 'attempts': [],
            'division': DIVISION, 'psp': 'ICEPAY', 'journal': '27', 'debtor': '109419', 'currency': 'EUR',
            'workflow_status': 'open', 'execution_status': 'not_executed', 'assigned_to': None,
            'decision_actor': 'Jasper', 'decision_at': '2026-10-04T21:21:17Z',
            'decision': 'Match only own order and equal full EUR invoice; no write-off.'})
    return result


def invoice_rows(receipt, history):
    return [r for r in history if r.get('YourRef') == receipt['source_order'] and code(r, 'JournalCode') == '70'
        and code(r, 'GLAccountCode') == '1100' and money(r['AmountDC']) > 0]


def open_rows(receipt, opened):
    invoices = [r for r in opened if code(r, 'JournalCode') == '70' and r.get('YourRef') == receipt['source_order']
        and str(r.get('InvoiceNumber')) == str(receipt['invoice']['EntryNumber'])]
    credits = [r for r in opened if code(r, 'JournalCode') == '27' and receipt['payment_id'] in (r.get('Description') or '')]
    return invoices, credits


def validate_open(receipt, opened):
    invoices, credits = open_rows(receipt, opened)
    if len(invoices) != 1 or len(credits) != 1:
        return 'invoice_closed_or_receipt_not_fully_open'
    for row, amount in ((invoices[0], money(receipt['amount'])), (credits[0], -money(receipt['amount']))):
        if norm(row.get('AccountId')) != DEBTOR or code(row, 'AccountCode') != '109419' or row.get('CurrencyCode') != 'EUR':
            return 'open_item_identity_changed'
        if money(row['Amount']) != amount or money(row.get('AmountInTransit') or 0) != 0:
            return 'partial_amount_or_in_transit'
    if credits[0].get('YourRef') != receipt['source_order'] or credits[0].get('Description') != receipt['description']:
        return 'receipt_reference_changed'
    return None


def classify(receipt, history, opened, counts):
    if counts[receipt['source_order']] != 1:
        return 'multiple_source_receipts_for_order'
    candidates = invoice_rows(receipt, history)
    receipt['invoice_candidates'] = candidates
    if not candidates:
        return 'invoice_not_found_including_closed_and_other_debtors'
    if len(candidates) != 1:
        return 'multiple_invoices_for_order'
    invoice = candidates[0]
    receipt['invoice'] = invoice
    receipt['difference'] = str(money(receipt['amount']) - money(invoice['AmountDC']))
    if norm(invoice.get('Account')) != DEBTOR or code(invoice, 'AccountCode') != '109419':
        return 'invoice_on_other_debtor'
    if invoice.get('Currency') != 'EUR':
        return 'invoice_currency_mismatch'
    if money(invoice['AmountDC']) != money(receipt['amount']):
        return 'amount_difference_requires_case_decision'
    return validate_open(receipt, opened)


def set_exception(receipt, reason):
    receipt.update(exception=reason, state='exception', workflow_status='open', execution_status='not_executed',
        suggested_action='Inspect the own invoice and existing allocations; resolve this case explicitly without cross-order matching or write-off.')


def stats(plan):
    receipts = plan.get('receipts', [])
    return {'receipts': len(receipts), 'matched': sum(r['state'] == 'matched_verified' for r in receipts),
        'matched_total': str(sum((money(r['amount']) for r in receipts if r['state'] == 'matched_verified'), Decimal('0.00'))),
        'pending': sum(r['state'] == 'pending' for r in receipts),
        'unknown_saves': sum(r['state'] == 'match_requested' for r in receipts),
        'exceptions': [{'order': r['source_order'], 'payment_id': r['payment_id'], 'amount': r['amount'],
            'invoice': r['invoice']['EntryNumber'] if r.get('invoice') else None, 'reason': r['exception']} for r in receipts if r['exception']]}


async def open_match(context, page, receipt):
    from operations.strict_order_matching import euro
    from operations.fibonatix_import import statement_rows, ui_snapshot
    await page.goto(BASE + '/docs/CflStatementsToBeCompleted.aspx?' + urlencode({
        '_Division_': DIVISION, 'BankAccount': '{' + BANK + '}'}), wait_until='domcontentloaded', timeout=60000)
    await page.locator('#Status1').check()
    await page.locator('#Status2').check()
    await page.locator('#EntryDate_Selection').select_option('1100')
    await page.locator('#Notes').fill(receipt['payment_id'])
    for field in ('#GLAccountTypeCheckBoxList1', '#GLAccountTypeCheckBoxList2', '#GLAccountTypeCheckBoxList3'):
        await page.locator(field).check()
    async with page.expect_navigation(wait_until='load', timeout=60000):
        await page.locator('#Filter_btnApply').click()
    if norm(await page.locator('#BankAccount').input_value()) != BANK:
        raise ValueError('wrong_bank')
    snapshot = await ui_snapshot(page)
    rows = statement_rows(snapshot)
    if len(rows) != 1 or receipt['description'] not in rows[0]['note']:
        receipt['evidence'].append({'at': now(), 'phase': 'statement_not_unique', 'ui': snapshot})
        raise ValueError('statement_not_unique')
    cells = rows[0]['cells']
    if euro(cells[4]) - euro(cells[5]) != money(receipt['amount']) or cells[3] != 'EUR' or cells[10] != 'EUR':
        raise ValueError('statement_amount_or_currency_changed')
    link = page.locator('xpath=//tr[count(td)=6 and contains(td[1],"' + receipt['payment_id'] + '")]/preceding-sibling::tr[1]//a[@id="LinkMatch"]')
    if await link.count() != 1:
        raise ValueError('match_link_not_unique')
    await link.click()
    frames = []
    for _ in range(20):
        frames = [f for p in context.pages for f in p.frames if urlsplit(f.url).path.endswith('/FinEntryMatch.aspx')]
        if len(frames) == 1:
            break
        await asyncio.sleep(.5)
    if len(frames) != 1:
        raise ValueError('match_frame_not_unique')
    frame = frames[0]
    await frame.locator('#btnSave').wait_for()
    query = parse_qs(urlsplit(frame.url).query)
    for key, value in {'_Division_': DIVISION, 'Account': DEBTOR, 'EntryID': receipt['entry_id'],
        'TransactionID': receipt['offset_id'], 'MatchStatementLineID': receipt['bank_line_id'], 'CurrencyBAC': 'EUR'}.items():
        if norm(query.get(key, [''])[0]) != norm(value):
            raise ValueError('match_frame_identity_changed')
    if euro(await frame.locator('#EntryAmount').input_value()) != money(receipt['amount']):
        raise ValueError('match_amount_changed')
    if await frame.locator('#GLAccount_alt').input_value() != '1100' or await frame.locator('#Account_alt').input_value() != '109419':
        raise ValueError('match_account_changed')
    return frame


def selected_proof(receipt, rows, saved=False):
    from operations.strict_order_matching import euro
    selected = [r for r in rows if r['checked']]
    if len(selected) != 1:
        raise ValueError('selection_not_single_invoice')
    row = selected[0]
    cells = row['cells']
    if (len(cells) != 10 or cells[4] != receipt['source_order'] or cells[2] != str(receipt['invoice']['EntryNumber'])
        or not cells[5].startswith('70 -') or euro(row['amount']) != money(receipt['amount'])
        or row['writeoff'] != '0' or (saved and not row['matchId'])):
        raise ValueError('selection_identity_amount_or_writeoff_changed')


async def verify_saved(context, page, reader, receipt, plan):
    from operations.strict_order_matching import match_rows
    frame = await open_match(context, page, receipt)
    rows = await match_rows(frame)
    selected_proof(receipt, rows, saved=True)
    ledger = await reader.ledger()
    current = source_receipts(plan['manifest'], ledger)
    own = next(r for r in current if r['payment_id'] == receipt['payment_id'])
    if any(own[key] != receipt[key] for key in ('bank_line_id', 'offset_id', 'entry_id')):
        raise ValueError('source_ids_changed_after_save')
    opened = await reader.orders([receipt['source_order']])
    invoices, credits = open_rows(receipt, opened)
    receipt['evidence'].append({'at': now(), 'phase': 'readback', 'rows': rows, 'open_items': opened,
        'source_lines': [r for r in ledger if r['ID'] in {receipt['bank_line_id'], receipt['offset_id']}]})
    if invoices or credits:
        raise ValueError('saved_match_not_closed_in_api')
    receipt.update(state='matched_verified', workflow_status='decided', execution_status='verified')
    receipt['attempts'][-1]['outcome'] = 'verified'


async def run(task_id=None):
    task = task_id if task_id is not None else os.environ.get('ICEPAY_MATCH_TASK_ID')
    if task not in TASKS or datetime.now(timezone.utc) >= EXPIRES:
        return
    from app import main
    if main.DIVISION != DIVISION or main.BASE_URL != BASE:
        return
    if not await asyncio.to_thread(init_claim, main, task):
        return
    reader, plan = Reader(main), {}
    summary = {'task': task, 'state': 'starting', 'financial_saves_this_run': 0}
    async def persist():
        if plan:
            await asyncio.to_thread(store_plan, main, plan)
        summary.update(stats(plan), api_calls=reader.calls)
        def record():
            with main._db_connect() as conn:
                conn.execute('UPDATE icepay_matching_runs SET summary=%s::jsonb WHERE task=%s', (json.dumps(summary), task))
        await asyncio.to_thread(record)
        LOG.warning('ICEPAY_OWN_ORDER_MATCH %s', json.dumps(summary, sort_keys=True))
    try:
        manifest = await asyncio.to_thread(source_manifest, main)
        if task == PREPARE:
            ledger = await reader.ledger()
            receipts = source_receipts(manifest, ledger)
            history = await reader.orders([r['ref'] for r in manifest], history=True)
            opened = await reader.orders([r['ref'] for r in manifest])
            counts = Counter(r['source_order'] for r in receipts)
            for receipt in receipts:
                reason = classify(receipt, history, opened, counts)
                if reason:
                    set_exception(receipt, reason)
            plan = {'manifest': manifest, 'receipts': receipts, 'prepared_at': now(),
                'initial_ledger': ledger, 'invoice_history': history, 'initial_open_items': opened}
            summary['state'] = 'prepared'
            return
        plan = await asyncio.to_thread(load_plan, main)
        if plan['manifest'] != manifest:
            raise ValueError('plan_source_changed')
        ledger = await reader.ledger()
        current = source_receipts(manifest, ledger)
        if [(r['bank_line_id'], r['offset_id'], r['entry_id']) for r in current] != [(r['bank_line_id'], r['offset_id'], r['entry_id']) for r in plan['receipts']]:
            raise ValueError('plan_ledger_identity_changed')
        if task == VERIFY:
            opened = await reader.orders([r['ref'] for r in manifest])
            for receipt in plan['receipts']:
                if receipt['state'] == 'matched_verified':
                    invoices, credits = open_rows(receipt, opened)
                    if invoices or credits:
                        raise ValueError('previous_match_reopened')
            plan['final_verification'] = {'at': now(), 'ledger': ledger, 'open_items': opened}
            summary.update(state='complete' if all(r['state'] in {'matched_verified', 'exception'} for r in plan['receipts']) else 'incomplete',
                bank_net=str(sum((money(r['AmountDC']) for r in ledger if code(r, 'GLAccountCode') == '1317'), Decimal('0.00'))))
            return
        if task in GROUPS and any(r['state'] == 'match_requested' for r in plan['receipts']):
            raise ValueError('unresolved_save_requires_recovery')
        queue = [r for r in plan['receipts'] if r['state'] == ('match_requested' if task == RECOVER else 'pending')][:5]
        if not queue:
            summary['state'] = 'no_eligible_items'
            return
        from operations.strict_order_matching import session, match_rows, toggle, euro
        async with session() as (context, page):
            for receipt in queue:
                if task_drain.requested(): raise ValueError('worker_draining')
                summary.update(state='processing', current_order=receipt['source_order'])
                await persist()
                if task == RECOVER:
                    await verify_saved(context, page, reader, receipt, plan)
                    await persist()
                    continue
                history = await reader.orders([receipt['source_order']], history=True)
                opened = await reader.orders([receipt['source_order']])
                reason = classify(receipt, history, opened, Counter(r['source_order'] for r in plan['receipts']))
                if reason:
                    set_exception(receipt, reason)
                    await persist()
                    continue
                frame = await open_match(context, page, receipt)
                rows = await match_rows(frame)
                receipt['evidence'].append({'at': now(), 'phase': 'before', 'rows': rows, 'open_items': opened})
                if any(r['checked'] for r in rows):
                    set_exception(receipt, 'existing_match_requires_inspection')
                    await persist()
                    continue
                hits = [r for r in rows if len(r['cells']) == 10 and r['cells'][4] == receipt['source_order']
                    and r['cells'][2] == str(receipt['invoice']['EntryNumber']) and r['cells'][5].startswith('70 -')]
                if len(hits) != 1 or euro(hits[0]['cells'][6]) != money(receipt['amount']):
                    set_exception(receipt, 'own_invoice_not_fully_open_in_ui')
                    await persist()
                    continue
                await toggle(frame, hits[0])
                await frame.locator('#' + hits[0]['id']).locator('select').select_option('0')
                selected_proof(receipt, await match_rows(frame))
                if euro(await frame.locator('#Balance').input_value()) != 0 or euro(await frame.locator('#SelectedAmount').input_value()) != money(receipt['amount']):
                    raise ValueError('matching_balance_or_selected_amount_changed')
                # Re-read immediately before claiming: invoice must still be fully open.
                opened = await reader.orders([receipt['source_order']])
                if validate_open(receipt, opened):
                    raise ValueError('last_api_precondition_changed')
                await save_receipt(main,task,receipt,plan,summary,persist,frame,context,page,reader)
        summary['state'] = 'group_verified'
    except Exception as error:
        from operations.icepay_transactions import failure_location
        summary.update(state='blocked', failure=failure_location(error))
        reason = str(error) if isinstance(error, ValueError) else getattr(error, 'detail', '')
        if re.fullmatch(r'[a-z_0-9]{1,100}', reason):
            summary['reason'] = reason
    finally:
        await persist()



@fence.owned_operation('icepay')
async def save_receipt(main, task, receipt, plan, summary, persist, frame, context, page, reader):
    await asyncio.to_thread(claim_save, main, task, receipt, plan)
    summary['financial_saves_this_run'] += 1
    await persist()
    await fence.browser_save(main,'icepay',frame.locator('#btnSave').click)
    await asyncio.sleep(2)
    await verify_saved(context, page, reader, receipt, plan)
    await persist()
