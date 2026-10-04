"""Source-order matching with private evidence and durable, single-attempt saves.

The operator supplies no URLs, scripts, accounting identities or write-offs.
Configuration and evidence come from the existing private import dossier.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from operations import fibonatix_import as legacy

router = APIRouter()
PLAN = 'strict_order_plan'
legacy.ARTIFACTS.add(PLAN)
SELECT = 'ID,EntryID,EntryNumber,LineNumber,Description,AmountDC,AccountCode,GLAccountCode,JournalCode,YourRef'


def fail(reason):
    raise HTTPException(409, reason)


def now():
    return datetime.now(timezone.utc).isoformat()


def load(name):
    with legacy.database() as conn:
        row = conn.execute('SELECT data FROM fibonatix_import_artifacts WHERE job=%s AND name=%s',
                           (legacy.JOB, name)).fetchone()
    return row[0] if row else None


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result != result.quantize(Decimal('.01')):
        fail('invalid_money')
    return result


def euro(value):
    return money(value.strip().replace('.', '').replace(',', '.') or '0')


def normalized(value):
    return str(value or '').strip().strip('{}').lower()


def source_receipts(lines, debtor):
    receipts = []
    for line in lines:
        match = re.fullmatch(r'Order (TD\d+) \| Woo (\d+) \| Betaling ([A-Za-z0-9]{8})', line.get('Description') or '')
        if not match or money(line['AmountDC']) <= 0 or str(line.get('AccountCode') or '').strip() != debtor:
            continue
        offsets = [r for r in lines if r['EntryID'] == line['EntryID'] and r['Description'] == line['Description']
                   and money(r['AmountDC']) == -money(line['AmountDC']) and str(r.get('GLAccountCode') or '').strip() == '1100']
        if len(offsets) != 1 or str(offsets[0].get('AccountCode') or '').strip() != debtor:
            fail('source_offset_not_unique')
        receipts.append(dict(source_order=match[1], woo=match[2], trx=match[3], amount=str(money(line['AmountDC'])),
            bank_line_id=line['ID'], offset_id=offsets[0]['ID'], entry_id=line['EntryID'], entry_number=line['EntryNumber'],
            description=line['Description'], allocated_reference=line.get('YourRef')))
    if len({r['trx'] for r in receipts}) != len(receipts) or len({r['bank_line_id'] for r in receipts}) != len(receipts):
        fail('duplicate_source_receipt')
    return receipts


def own_invoice(receipt, invoices, debtor):
    matches = [r for r in invoices if r.get('YourRef') == receipt['source_order'] and money(r['AmountDC']) > 0
               and str(r.get('GLAccountCode') or '').strip() == '1100']
    if len(matches) != 1:
        return None, 'invoice_missing_or_ambiguous'
    invoice = matches[0]
    if str(invoice.get('AccountCode') or '').strip() != debtor:
        return None, 'invoice_other_debtor'
    if money(invoice['AmountDC']) != money(receipt['amount']):
        return None, 'amount_difference_requires_case_decision'
    return invoice, None


def approved_difference(order, invoice_number, receipt_amount, invoice_amount, decision):
    if not decision or decision.get('scope') != 'this_case_only' or not decision.get('actor') or not decision.get('approved_at'):
        return False
    return (decision.get('order') == order and str(decision.get('invoice')) == str(invoice_number)
            and money(receipt_amount) - money(invoice_amount) == money(decision.get('payment_difference', 0)) > 0)


def difference_total(lines, order):
    return sum((money(r['AmountDC']) for r in lines if str(r.get('GLAccountCode') or '').strip() == '9920'
                and r.get('YourRef') == order), Decimal('0'))


async def prepare():
    legacy.update(phase='strict_reading_ledger')
    existing = load(PLAN)
    if existing and any(r.get('attempts') for r in existing['receipts']):
        fail('existing_repair_plan_cannot_be_replaced')
    lines = await legacy.ledger()
    opened = await legacy.receivables()
    legacy.update(phase='strict_reading_invoice_history')
    debtor = legacy.application().COLLECTIVE_DEBTOR_CODE
    receipts = source_receipts(lines, debtor)
    targets = [r for r in receipts if r['allocated_reference'] != r['source_order'] or any(
        x.get('JournalCode') == '26' and r['trx'] in (x.get('Description') or '') for x in opened)]
    orders = sorted({r['source_order'] for r in targets} | {r['allocated_reference'] for r in targets if re.fullmatch(r'TD\d+', r['allocated_reference'] or '')})
    invoices = []
    for start in range(0, len(orders), 15):
        refs = ' or '.join("YourRef eq '" + ref + "'" for ref in orders[start:start + 15])
        invoices.extend(await legacy.read_all('financialtransaction/TransactionLines', {
            '$filter': "JournalCode eq '70' and (" + refs + ')', '$select': SELECT}))
    for receipt in targets:
        invoice, reason = own_invoice(receipt, invoices, debtor)
        if sum(r['source_order'] == receipt['source_order'] for r in receipts) != 1:
            invoice, reason = None, 'multiple_receipts_for_source_order'
        receipt.update(invoice=invoice, exception=reason, state='pending', attempts=[], evidence=[],
                       workflow_status='open', assigned_to=None, suggested_action='match_own_invoice' if invoice else 'inspect_invoice_history')
    plan = dict(created_at=now(), policy='same_source_order_required', receipts=targets, invoices=invoices,
                original_ledger=lines, original_receivables=opened, approved_decision=(load('settlement_before') or {}).get('decision'))
    legacy.artifact(PLAN, plan)
    legacy.update(phase='strict_plan_prepared', strict_target_count=len(targets), strict_exception_count=sum(bool(r['exception']) for r in targets))


async def entry_lines(receipt):
    return await legacy.read_all('financialtransaction/TransactionLines', {
        '$filter': "EntryID eq guid'" + receipt['entry_id'] + "'", '$select': SELECT})


def verify_source(receipt, lines):
    found = {r['ID']: r for r in lines}
    for key, amount in [('bank_line_id', money(receipt['amount'])), ('offset_id', -money(receipt['amount']))]:
        row = found.get(receipt[key])
        if not row or money(row['AmountDC']) != amount or row['Description'] != receipt['description']:
            fail('source_changed')


@asynccontextmanager
async def session():
    from operations.exact_browser import Credentials, authenticate, protect_requests
    from playwright.async_api import async_playwright
    directory = str(Path(tempfile.gettempdir()) / 'jnp-paragon-browser-1.63.0')
    child_env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LD_LIBRARY_PATH','VIRTUAL_ENV'}}
    child_env['PLAYWRIGHT_BROWSERS_PATH'] = directory
    installer = await asyncio.create_subprocess_exec(sys.executable, '-m', 'playwright', 'install', 'chromium', '--only-shell',
        env=child_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    if await asyncio.wait_for(installer.wait(), 150) != 0:
        fail('browser_install_failed')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = directory
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, env=child_env)
        try:
            context = await browser.new_context(accept_downloads=False, service_workers='block')
            await protect_requests(context)
            page = await context.new_page()
            page.set_default_timeout(20000)
            result = await authenticate(page, Credentials.from_env(os.environ))
            if not result.get('administration_verified'):
                fail('administration_not_verified')
            yield context, page
        finally:
            await browser.close()


async def open_match(context, page, receipt):
    url = legacy.application().BASE_URL + '/docs/CflStatementsToBeCompleted.aspx?' + urlencode({
        '_Division_': legacy.DIVISION, 'BankAccount': '{' + legacy.BANK + '}'})
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    await page.locator('#Status1').check()
    await page.locator('#Status2').check()
    await page.locator('#EntryDate_Selection').select_option('1100')
    await page.locator('#Notes').fill(receipt['trx'])
    for field in ['#GLAccountTypeCheckBoxList1', '#GLAccountTypeCheckBoxList2', '#GLAccountTypeCheckBoxList3']:
        await page.locator(field).check()
    async with page.expect_navigation(wait_until='load', timeout=60000):
        await page.locator('#Filter_btnApply').click()
    if normalized(await page.locator('#BankAccount').input_value()) != legacy.BANK:
        fail('wrong_bank')
    rows = legacy.statement_rows(await legacy.ui_snapshot(page))
    if len(rows) != 1 or receipt['description'] not in rows[0]['note']:
        fail('statement_not_unique')
    cells = rows[0]['cells']
    if euro(cells[4]) - euro(cells[5]) != money(receipt['amount']) or cells[3] != 'EUR' or cells[10] != 'EUR':
        fail('statement_amount_or_currency_changed')
    link = page.locator('xpath=//tr[count(td)=6 and contains(td[1],"' + receipt['trx'] + '")]/preceding-sibling::tr[1]//a[@id="LinkMatch"]')
    if await link.count() != 1:
        fail('match_link_not_unique')
    await link.click()
    frames = []
    for _ in range(20):
        frames = [f for p in context.pages for f in p.frames if urlsplit(f.url).path.endswith('/FinEntryMatch.aspx')]
        if len(frames) == 1:
            break
        await asyncio.sleep(.5)
    if len(frames) != 1:
        fail('match_frame_not_unique')
    frame = frames[0]
    await frame.locator('#btnSave').wait_for()
    query = parse_qs(urlsplit(frame.url).query)
    for key, value in {'_Division_':legacy.DIVISION, 'Account':legacy.DEBTOR, 'EntryID':receipt['entry_id'],
                       'TransactionID':receipt['offset_id'], 'MatchStatementLineID':receipt['bank_line_id'], 'CurrencyBAC':'EUR'}.items():
        if normalized(query.get(key, [''])[0]) != normalized(value):
            fail('match_frame_identity_changed')
    if euro(await frame.locator('#EntryAmount').input_value()) != money(receipt['amount']):
        fail('match_amount_changed')
    return frame


async def match_rows(frame):
    # Read every grid row directly; the legacy diagnostic control list is capped.
    return await frame.locator('tr[id^="List_row_"]').evaluate_all('''rows => rows.map(r => ({
      id:r.id,cells:Array.from(r.children).map(c=>c.innerText),
      checked:!!r.querySelector('input[type=checkbox]')?.checked,
      disabled:!!r.querySelector('input[type=checkbox]')?.disabled,
      amount:r.querySelector('input[type=text]')?.value,
      writeoff:r.querySelector('select')?.value,
      matchId:r.querySelector('input[id$="PaymentTermMatchID"]')?.value
    }))''')


async def toggle(frame, row):
    checkbox = frame.locator('#' + row['id']).locator('input[type="checkbox"]')
    if not await checkbox.is_enabled():
        fail('invoice_checkbox_disabled')
    await checkbox.evaluate('(el)=>{if(el.disabled)throw new Error("disabled");el.click()}')


def verify_wrong_selection(receipt, rows, decision=None):
    checked = [r for r in rows if r['checked']]
    if len(checked) != 1:
        fail('existing_match_not_single_invoice')
    row = checked[0]
    cells = row['cells']
    if len(cells) != 10 or not cells[5].startswith('70 -') or cells[4] != receipt['allocated_reference'] or cells[4] == receipt['source_order']:
        fail('existing_match_does_not_confirm_candidate')
    if euro(row['amount']) != money(receipt['amount']) or not row['matchId']:
        fail('existing_match_amount_or_identity_missing')
    if row['writeoff'] != '0' or euro(cells[6]) != money(receipt['amount']):
        if row['writeoff'] != '3' or not approved_difference(cells[4], cells[2], receipt['amount'], euro(cells[6]), decision):
            fail('existing_writeoff_requires_case_repair')
    return row


async def save_once(frame, receipt, plan, phase):
    claim = 'strict_' + phase + '_' + receipt['bank_line_id']
    legacy.claim(claim)
    receipt['attempts'].append(dict(action=phase, at=now(), outcome='unknown_readback_required'))
    receipt['state'] = phase + '_requested'
    legacy.artifact(PLAN, plan)
    await frame.locator('#btnSave').click()
    await asyncio.sleep(2)


async def process(mode, limit):
    plan = load(PLAN)
    if not plan:
        fail('prepare_required')
    if mode in {'undo', 'match'} and any(r['state'].endswith('_requested') for r in plan['receipts']):
        fail('unresolved_save_requires_read_only_recovery')
    # Attach only the existing private, explicitly approved difference decision.
    # No tolerance or request-provided write-off is accepted.
    for receipt in plan['receipts']:
        if receipt['exception'] == 'amount_difference_requires_case_decision':
            matches = [r for r in plan['invoices'] if r.get('YourRef') == receipt['source_order']
                and str(r.get('GLAccountCode') or '').strip() == '1100' and money(r['AmountDC']) > 0
                and str(r.get('AccountCode') or '').strip() == legacy.application().COLLECTIVE_DEBTOR_CODE]
            if len(matches) == 1 and approved_difference(receipt['source_order'], matches[0]['EntryNumber'],
                    receipt['amount'], matches[0]['AmountDC'], plan.get('approved_decision')):
                receipt.update(invoice=matches[0], exception=None, difference_decision=plan['approved_decision'])
    legacy.artifact(PLAN, plan)
    queue = [r for r in plan['receipts'] if (
        mode == 'inspect' and r['state'] == 'pending' or
        mode == 'recover' and r['state'] in {'undo_requested', 'match_requested'} or
        mode == 'undo' and r['state'] in {'pending', 'inspected'} and r['allocated_reference'] != r['source_order'] or
        mode == 'match' and (r['state'] == 'unmatched_verified' or r['state'] in {'pending', 'inspected'} and r['allocated_reference'] == r['source_order']) and not r['exception']
    )]
    if mode == 'match':
        opened = await legacy.receivables()
        # A chain may not yet be fully released. Process available own invoices
        # and leave the others eligible for a later pass after further undo.
        queue = [r for r in queue if any(x.get('JournalCode') == '70'
            and str(x.get('InvoiceNumber')) == str(r['invoice']['EntryNumber'])
            and x.get('YourRef') == r['source_order'] and money(x['Amount']) == money(r['invoice']['AmountDC']) for x in opened)]
    queue = queue[:limit]
    if not queue:
        legacy.update(phase='strict_' + mode + '_no_eligible_items')
        return
    async with session() as (context, page):
        for receipt in queue:
            legacy.update(phase='strict_' + mode, strict_current_order=receipt['source_order'])
            before = await entry_lines(receipt)
            verify_source(receipt, before)
            frame = await open_match(context, page, receipt)
            rows = await match_rows(frame)
            receipt['evidence'].append(dict(at=now(), phase=mode, rows=rows, ledger=before))
            legacy.artifact(PLAN, plan)
            checked = [r for r in rows if r['checked']]
            if mode == 'recover':
                opened = await legacy.receivables()
                credits = [r for r in opened if r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '')]
                if receipt['state'] == 'undo_requested':
                    previous = [e for e in receipt['evidence'] if e['phase'] == 'undo'][-1]
                    old_match = verify_wrong_selection(receipt, previous['rows'], plan.get('approved_decision'))
                    restored = [r for r in opened if r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == old_match['cells'][2]]
                    if checked or len(credits) != 1 or money(credits[0]['Amount']) != -money(receipt['amount']) or len(restored) != 1 or money(restored[0]['Amount']) != euro(old_match['cells'][6]):
                        fail('undo_outcome_unresolved_no_retry')
                    if old_match['writeoff'] == '3' and difference_total(before, old_match['cells'][4]) != 0:
                        fail('difference_reversal_unresolved_no_retry')
                    receipt['state'] = 'unmatched_verified'
                else:
                    invoice = receipt['invoice']
                    if len(checked) != 1 or checked[0]['cells'][4] != receipt['source_order'] or checked[0]['cells'][2] != str(invoice['EntryNumber']) or not checked[0]['matchId'] or credits or any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber']) for r in opened):
                        fail('match_outcome_unresolved_no_retry')
                    difference = money(receipt['amount']) - money(invoice['AmountDC'])
                    if difference and difference_total(before, receipt['source_order']) != -difference:
                        fail('source_difference_outcome_unresolved_no_retry')
                    receipt.update(state='matched_verified', workflow_status='decided', execution_status='verified')
                receipt['attempts'][-1]['outcome'] = 'verified_by_recovery_read'
            elif mode == 'inspect':
                receipt['state'] = 'inspected'
            elif mode == 'undo':
                row = verify_wrong_selection(receipt, rows, plan.get('approved_decision'))
                if row['writeoff'] == '3' and difference_total(before, row['cells'][4]) != euro(row['cells'][6]) - money(receipt['amount']):
                    fail('existing_difference_ledger_not_unique')
                await toggle(frame, row)
                if any(r['checked'] for r in await match_rows(frame)) or euro(await frame.locator('#SelectedAmount').input_value()) != 0:
                    fail('undo_still_selected')
                await save_once(frame, receipt, plan, 'undo')
                reopened = await open_match(context, page, receipt)
                after = await match_rows(reopened)
                actual = await entry_lines(receipt)
                verify_source(receipt, actual)
                if any(r['checked'] for r in after):
                    fail('undo_not_verified')
                opened = await legacy.receivables()
                credits = [r for r in opened if r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '')]
                restored = [r for r in opened if str(r.get('InvoiceNumber')) == row['cells'][2] and r.get('JournalCode') == '70']
                if len(credits) != 1 or money(credits[0]['Amount']) != -money(receipt['amount']) or len(restored) != 1 or money(restored[0]['Amount']) != euro(row['cells'][6]):
                    fail('undo_api_readback_not_verified')
                if row['writeoff'] == '3' and difference_total(actual, row['cells'][4]) != 0:
                    fail('difference_reversal_not_verified')
                receipt['state'] = 'unmatched_verified'
                receipt['attempts'][-1]['outcome'] = 'verified'
                receipt['evidence'].append(dict(at=now(), phase='undo_readback', rows=after, ledger=actual))
            else:
                if checked:
                    fail('receipt_already_has_match')
                invoice = receipt['invoice']
                hits = [r for r in rows if len(r['cells']) == 10 and r['cells'][4] == receipt['source_order']
                        and r['cells'][2] == str(invoice['EntryNumber']) and r['cells'][5].startswith('70 -')]
                if len(hits) != 1 or euro(hits[0]['cells'][6]) != money(invoice['AmountDC']):
                    receipt.update(exception='own_invoice_not_fully_open', suggested_action='inspect_existing_match', workflow_status='open')
                else:
                    row = hits[0]
                    opened = await legacy.receivables()
                    invoices = [r for r in opened if r.get('YourRef') == receipt['source_order'] and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber']) and r.get('JournalCode') == '70' and r.get('CurrencyCode') == 'EUR']
                    credits = [r for r in opened if r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '') and r.get('CurrencyCode') == 'EUR']
                    if len(invoices) != 1 or money(invoices[0]['Amount']) != money(invoice['AmountDC']) or len(credits) != 1 or money(credits[0]['Amount']) != -money(receipt['amount']):
                        fail('strict_api_preconditions_changed')
                    await toggle(frame, row)
                    difference = money(receipt['amount']) - money(invoice['AmountDC'])
                    selected_row = frame.locator('#' + row['id'])
                    if difference:
                        if not approved_difference(receipt['source_order'], invoice['EntryNumber'], receipt['amount'], invoice['AmountDC'], receipt.get('difference_decision')) or difference_total(before, receipt['source_order']) != 0:
                            fail('source_difference_not_approved_or_already_posted')
                        await selected_row.locator('input[type="text"]').fill(format(money(receipt['amount']), '.2f').replace('.', ','))
                        await selected_row.locator('input[type="text"]').press('Tab')
                    await selected_row.locator('select').select_option('3' if difference else '0')
                    selected = [r for r in await match_rows(frame) if r['checked']]
                    if len(selected) != 1 or selected[0]['cells'][4] != receipt['source_order'] or euro(selected[0]['amount']) != money(receipt['amount']):
                        fail('strict_selection_changed')
                    if euro(await frame.locator('#Balance').input_value()) != 0:
                        fail('strict_balance_not_zero')
                    await save_once(frame, receipt, plan, 'match')
                    reopened = await open_match(context, page, receipt)
                    after = await match_rows(reopened)
                    selected = [r for r in after if r['checked']]
                    actual = await entry_lines(receipt)
                    verify_source(receipt, actual)
                    if difference and difference_total(actual, receipt['source_order']) != -difference:
                        fail('source_difference_readback_not_verified')
                    if len(selected) != 1 or selected[0]['cells'][4] != receipt['source_order'] or selected[0]['cells'][2] != str(invoice['EntryNumber']) or not selected[0]['matchId']:
                        fail('strict_match_not_verified')
                    opened = await legacy.receivables()
                    if any((r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber'])) or
                           (r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '')) for r in opened):
                        fail('strict_api_readback_not_verified')
                    receipt.update(state='matched_verified', workflow_status='decided', execution_status='verified')
                    receipt['attempts'][-1]['outcome'] = 'verified'
                    receipt['evidence'].append(dict(at=now(), phase='match_readback', rows=after, ledger=actual))
            legacy.artifact(PLAN, plan)
            # Navigating the parent closes the inspected modal before next item.
    legacy.update(phase='strict_' + mode + '_complete', strict_states={s:sum(r['state']==s for r in plan['receipts']) for s in {r['state'] for r in plan['receipts']}})


async def run(mode, limit):
    conn = legacy.database()
    locked = False
    try:
        locked = conn.execute('SELECT pg_try_advisory_lock(%s)', (legacy.LOCK,)).fetchone()[0]
        if not locked:
            return
        legacy.update(running=True, action='strict_' + mode, last_error=None)
        await prepare() if mode == 'prepare' else await process(mode, limit)
    except asyncio.CancelledError:
        if locked:
            legacy.update(phase='strict_interrupted_readback_required', last_error='worker_cancelled')
        raise
    except Exception as exc:
        if locked:
            legacy.update(phase='strict_stopped', last_error=str(exc.detail) if isinstance(exc, HTTPException) else type(exc).__name__)
    finally:
        if locked:
            legacy.update(running=False)
            conn.execute('SELECT pg_advisory_unlock(%s)', (legacy.LOCK,))
        conn.close()


@router.post('/strict/{mode}')
async def start(mode: str, request: Request, limit: int = 1):
    legacy.authorize(request)
    if mode not in {'prepare', 'inspect', 'undo', 'match', 'recover'} or not 1 <= limit <= 20:
        fail('invalid_strict_operation')
    legacy.state()
    if any(not t.done() for t in legacy.TASKS):
        fail('job_already_running')
    task = asyncio.create_task(run(mode, limit))
    legacy.TASKS.add(task)
    task.add_done_callback(legacy.TASKS.discard)
    return dict(accepted=True, mode=mode, limit=limit, read_only=mode in {'prepare', 'inspect', 'recover'})
