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
    return legacy.decode_evidence(row[0]) if row else None


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


def verify_difference_change(before, after, order, amount, *, reverse=False):
    def total(rows):
        return sum((money(r['AmountDC']) for r in rows if str(r.get('GLAccountCode') or '').strip() == '9920'), Decimal(0))
    expected = money(amount) if reverse else -money(amount)
    if total(after) - total(before) != expected:
        fail('difference_general_ledger_delta_not_verified')
    if difference_total(after, order) != (Decimal(0) if reverse else -money(amount)):
        fail('difference_order_ledger_delta_not_verified')


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


async def entry_lines(receipt, *, full=False):
    query = "EntryID eq guid'" + receipt['entry_id'] + "'"
    if not full:
        query += " and (ID eq guid'" + receipt['bank_line_id'] + "' or ID eq guid'" + receipt['offset_id'] + "')"
    return await legacy.read_all('financialtransaction/TransactionLines', {
        '$filter': query, '$select': SELECT})


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
    if await frame.locator('#GLAccount_alt').input_value() != '1100' or await frame.locator('#Account_alt').input_value() != legacy.application().COLLECTIVE_DEBTOR_CODE:
        fail('match_account_or_offset_changed')
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
    if mode in {'undo', 'undo_orphans', 'match', 'correct'} and any(r['state'].endswith('_requested') for r in plan['receipts']):
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
        mode == 'recover' and r['state'] in {'undo_requested', 'match_requested', 'correct_requested'} or
        mode == 'undo' and r['state'] in {'pending', 'inspected'} and r['allocated_reference'] != r['source_order'] or
        mode == 'undo_orphans' and r['state'] in {'pending', 'inspected'} and r['allocated_reference'] != r['source_order'] and r['invoice'] is None or
        mode == 'correct' and r['state'] in {'pending', 'inspected'} and r['allocated_reference'] != r['source_order'] and not r['exception'] or
        mode == 'match' and (r['state'] == 'unmatched_verified' or r['state'] in {'pending', 'inspected'} and r['allocated_reference'] == r['source_order']) and not r['exception']
    )]
    if mode in {'match', 'correct'}:
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
            full_ledger = bool(receipt.get('difference_decision') or
                receipt['allocated_reference'] == (plan.get('approved_decision') or {}).get('order'))
            before = await entry_lines(receipt, full=full_ledger)
            verify_source(receipt, before)
            frame = await open_match(context, page, receipt)
            rows = await match_rows(frame)
            receipt['evidence'].append(dict(at=now(), phase='undo' if mode == 'undo_orphans' else mode, rows=rows, ledger=before))
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
                    if old_match['writeoff'] == '3':
                        verify_difference_change(previous['ledger'], before, old_match['cells'][4],
                            money(receipt['amount']) - euro(old_match['cells'][6]), reverse=True)
                    receipt['state'] = 'unmatched_verified'
                else:
                    invoice = receipt['invoice']
                    if len(checked) != 1 or checked[0]['cells'][4] != receipt['source_order'] or checked[0]['cells'][2] != str(invoice['EntryNumber']) or not checked[0]['matchId'] or credits or any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber']) for r in opened):
                        fail('match_outcome_unresolved_no_retry')
                    difference = money(receipt['amount']) - money(invoice['AmountDC'])
                    if difference and difference_total(before, receipt['source_order']) != -difference:
                        fail('source_difference_outcome_unresolved_no_retry')
                    if difference:
                        prior = [e for e in receipt['evidence'] if e['phase'] in {'match', 'correct'}][-1]
                        verify_difference_change(prior['ledger'], before, receipt['source_order'], difference)
                    if receipt['state'] == 'correct_requested':
                        previous = [e for e in receipt['evidence'] if e['phase'] == 'correct'][-1]
                        old_match = verify_wrong_selection(receipt, previous['rows'])
                        restored = [r for r in opened if r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == old_match['cells'][2]]
                        if len(restored) != 1 or money(restored[0]['Amount']) != euro(old_match['cells'][6]):
                            fail('previous_invoice_reopen_unresolved_no_retry')
                    receipt.update(state='matched_verified', workflow_status='decided', execution_status='verified')
                receipt['attempts'][-1]['outcome'] = 'verified_by_recovery_read'
            elif mode == 'inspect':
                receipt['state'] = 'inspected'
            elif mode in {'undo', 'undo_orphans'}:
                row = verify_wrong_selection(receipt, rows, plan.get('approved_decision'))
                if row['writeoff'] == '3' and difference_total(before, row['cells'][4]) != euro(row['cells'][6]) - money(receipt['amount']):
                    fail('existing_difference_ledger_not_unique')
                await toggle(frame, row)
                if any(r['checked'] for r in await match_rows(frame)) or euro(await frame.locator('#SelectedAmount').input_value()) != 0:
                    fail('undo_still_selected')
                await save_once(frame, receipt, plan, 'undo')
                reopened = await open_match(context, page, receipt)
                after = await match_rows(reopened)
                actual = await entry_lines(receipt, full=full_ledger)
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
                if row['writeoff'] == '3':
                    verify_difference_change(before, actual, row['cells'][4], money(receipt['amount']) - euro(row['cells'][6]), reverse=True)
                receipt['state'] = 'unmatched_verified'
                receipt['attempts'][-1]['outcome'] = 'verified'
                receipt['evidence'].append(dict(at=now(), phase='undo_readback', rows=after, ledger=actual))
            else:
                old_match = None
                if mode == 'correct':
                    # Replace one proven wrong selection with the own invoice
                    # in a single native save, without a broad match operation.
                    old_match = verify_wrong_selection(receipt, rows)
                    await toggle(frame, old_match)
                elif checked:
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
                    credit_valid = (not credits) if mode == 'correct' else len(credits) == 1 and money(credits[0]['Amount']) == -money(receipt['amount'])
                    if len(invoices) != 1 or money(invoices[0]['Amount']) != money(invoice['AmountDC']) or not credit_valid:
                        fail('strict_api_preconditions_changed')
                    if old_match and any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == old_match['cells'][2] for r in opened):
                        fail('previous_invoice_unexpectedly_open')
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
                    await save_once(frame, receipt, plan, 'correct' if mode == 'correct' else 'match')
                    reopened = await open_match(context, page, receipt)
                    after = await match_rows(reopened)
                    selected = [r for r in after if r['checked']]
                    actual = await entry_lines(receipt, full=full_ledger)
                    verify_source(receipt, actual)
                    if difference and difference_total(actual, receipt['source_order']) != -difference:
                        fail('source_difference_readback_not_verified')
                    if difference:
                        verify_difference_change(before, actual, receipt['source_order'], difference)
                    if len(selected) != 1 or selected[0]['cells'][4] != receipt['source_order'] or selected[0]['cells'][2] != str(invoice['EntryNumber']) or not selected[0]['matchId']:
                        fail('strict_match_not_verified')
                    opened = await legacy.receivables()
                    if any((r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber'])) or
                           (r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '')) for r in opened):
                        fail('strict_api_readback_not_verified')
                    if old_match:
                        restored = [r for r in opened if r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == old_match['cells'][2]]
                        if len(restored) != 1 or money(restored[0]['Amount']) != euro(old_match['cells'][6]):
                            fail('previous_invoice_reopen_not_verified')
                    receipt.update(state='matched_verified', workflow_status='decided', execution_status='verified')
                    receipt['attempts'][-1]['outcome'] = 'verified'
                    receipt['evidence'].append(dict(at=now(), phase='match_readback', rows=after, ledger=actual))
            legacy.artifact(PLAN, plan)
            if mode == 'correct' and len(queue) < limit:
                # The previous verified correction may release the next own
                # invoice in a chain. Never exceed the explicit small-group cap.
                for candidate in plan['receipts']:
                    if len(queue) >= limit:
                        break
                    if candidate in queue or candidate['state'] not in {'pending', 'inspected'} or candidate['exception'] or candidate['allocated_reference'] == candidate['source_order']:
                        continue
                    if any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(candidate['invoice']['EntryNumber'])
                           and r.get('YourRef') == candidate['source_order'] and money(r['Amount']) == money(candidate['invoice']['AmountDC']) for r in opened):
                        queue.append(candidate)
            # Navigating the parent closes the inspected modal before next item.
    legacy.update(phase='strict_' + mode + '_complete', strict_states={s:sum(r['state']==s for r in plan['receipts']) for s in {r['state'] for r in plan['receipts']}})


async def verify_final():
    plan = load(PLAN)
    if not plan:
        fail('prepare_required')
    legacy.update(phase='strict_final_readback')
    lines = await legacy.ledger()
    opened = await legacy.receivables()
    actual = {r['ID']:r for r in lines}
    previous_difference = {r['ID'] for r in (load('settlement_after') or {}).get('added_lines', [])}
    fields = ['EntryID', 'EntryNumber', 'LineNumber', 'Description', 'AmountDC', 'AccountCode', 'GLAccountCode', 'JournalCode']
    for before in plan['original_ledger']:
        if before['ID'] in previous_difference:
            continue
        after = actual.get(before['ID'])
        if not after or any(before.get(k) != after.get(k) for k in fields):
            fail('original_journal_line_changed')
    def totals(rows):
        result = {}
        for row in rows:
            gl = str(row.get('GLAccountCode') or '').strip()
            result[gl] = result.get(gl, Decimal(0)) + money(row['AmountDC'])
        return {k:str(v) for k,v in result.items()}
    if totals(lines) != totals(plan['original_ledger']):
        fail('journal_totals_changed')
    target_ids = {r['bank_line_id'] for r in plan['receipts']}
    for source in source_receipts(plan['original_ledger'], legacy.application().COLLECTIVE_DEBTOR_CODE):
        if source['bank_line_id'] not in target_ids and any(actual[source[k]].get('YourRef') != source['source_order'] for k in ['bank_line_id', 'offset_id']):
            fail('previously_correct_source_reference_changed')
    incomplete = []
    for receipt in plan['receipts']:
        verify_source(receipt, lines)
        credits = [r for r in opened if r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '')]
        if receipt['state'] == 'matched_verified':
            invoice = receipt['invoice']
            if credits or any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber']) for r in opened):
                fail('verified_match_became_open')
            if any(actual[receipt[k]].get('YourRef') != receipt['source_order'] for k in ['bank_line_id', 'offset_id']):
                fail('verified_match_reference_changed')
        elif receipt['exception'] and receipt['state'] in {'pending', 'inspected', 'unmatched_verified', 'exception_verified'}:
            if len(credits) != 1 or money(credits[0]['Amount']) != -money(receipt['amount']):
                fail('exception_receipt_not_fully_open')
            if any(actual[receipt[k]].get('YourRef') not in {None, '', receipt['source_order']} for k in ['bank_line_id', 'offset_id']):
                fail('exception_has_wrong_order_reference')
            receipt.update(state='exception_verified', workflow_status='open', execution_status='open_receipt_verified')
        else:
            incomplete.append(receipt['source_order'])
    plan['final_readback'] = dict(at=now(), ledger=lines, receivables=opened, totals=totals(lines), incomplete=incomplete)
    legacy.artifact(PLAN, plan)
    legacy.update(phase='strict_repair_verified' if not incomplete else 'strict_repair_incomplete',
        historical_matches_repaired=not incomplete,
        strict_states={s:sum(r['state']==s for r in plan['receipts']) for s in {r['state'] for r in plan['receipts']}},
        strict_unresolved_count=len(incomplete))


async def diagnose_closed(limit, *, pending=False):
    """Read prior invoice-related postings; do not undo an existing payment."""
    plan = load(PLAN)
    if not plan:
        fail('prepare_required')
    opened = await legacy.receivables()
    found = []
    for receipt in plan['receipts']:
        invoice = receipt.get('invoice')
        eligible = receipt['state'] in {'pending', 'inspected'} if pending else (
            receipt['state'] == 'unmatched_verified' or receipt['state'] in {'pending', 'inspected'}
            and receipt['allocated_reference'] == receipt['source_order'] and not receipt['exception'])
        if not invoice or not eligible:
            continue
        if pending and receipt.get('existing_invoice_history'):
            continue
        if any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber']) for r in opened):
            continue
        if len(found) >= limit:
            break
        legacy.update(phase='strict_existing_invoice_review', strict_current_order=receipt['source_order'])
        evidence = dict(at=now(), invoice=invoice, invoice_is_open=False, source_lines=await entry_lines(receipt), receivables=opened)
        evidence['related_lines'] = await legacy.read_all('financialtransaction/TransactionLines', {
            '$filter': "YourRef eq '" + receipt['source_order'] + "'", '$select': SELECT + ',Date'})
        try:
            evidence['cashflow'] = await legacy.read_all('cashflow/Receivables', {
                '$filter': 'EntryNumber eq ' + str(int(invoice['EntryNumber'])),
                '$select': 'Account,AccountCode,AmountDC,Currency,Description,EntryNumber,GLAccount,GLAccountCode,Status,TransactionID,TransactionEntryID'})
        except HTTPException as exc:
            evidence['cashflow_error'] = dict(status=exc.status_code, detail=str(exc.detail)[:1000])
        receipt['existing_invoice_history'] = evidence
        found.append(receipt['source_order'])
        legacy.artifact(PLAN, plan)
    if found and not pending:
        async with session() as (context, page):
            for receipt in plan['receipts']:
                if receipt['source_order'] not in found:
                    continue
                invoice = receipt['invoice']
                actual = await entry_lines(receipt)
                verify_source(receipt, actual)
                frame = await open_match(context, page, receipt)
                rows = await match_rows(frame)
                current = await legacy.receivables()
                selected = [r for r in rows if r['checked']]
                credits = [r for r in current if r.get('JournalCode') == '26' and receipt['trx'] in (r.get('Description') or '')]
                invoice_open = any(r.get('JournalCode') == '70' and str(r.get('InvoiceNumber')) == str(invoice['EntryNumber']) for r in current)
                receipt['evidence'].append(dict(at=now(), phase='existing_match_readback', rows=rows, ledger=actual))
                receipt['existing_invoice_history']['screen'] = rows
                if (len(selected) == 1 and selected[0]['cells'][4] == receipt['source_order']
                        and selected[0]['cells'][2] == str(invoice['EntryNumber']) and selected[0]['matchId']
                        and euro(selected[0]['amount']) == money(receipt['amount']) and not credits and not invoice_open):
                    receipt.update(state='matched_verified', workflow_status='decided', execution_status='existing_match_verified',
                        verified_without_new_save=True)
                    receipt['existing_invoice_history']['result'] = 'existing_own_match_verified_without_save'
                elif not selected and not invoice_open and len(credits) == 1 and money(credits[0]['Amount']) == -money(receipt['amount']):
                    receipt.update(exception='own_invoice_already_closed_requires_review', workflow_status='open',
                        suggested_action='inspect_existing_payment_or_credit')
                    receipt['existing_invoice_history']['result'] = 'open_receipt_closed_invoice_exception'
                else:
                    legacy.artifact(PLAN, plan)
                    fail('existing_match_state_requires_review')
                legacy.artifact(PLAN, plan)
    legacy.update(phase='strict_existing_invoice_review_complete', strict_closed_invoice_reviews=len(found),
        strict_states={s:sum(r['state']==s for r in plan['receipts']) for s in {r['state'] for r in plan['receipts']}})


async def refresh_missing(limit):
    """Recheck previously missing invoices without posting or matching."""
    plan = load(PLAN)
    if not plan:
        fail('prepare_required')
    queue = [r for r in plan['receipts'] if r['exception'] == 'invoice_missing_or_ambiguous'
             and r['state'] in {'pending', 'inspected', 'unmatched_verified', 'exception_verified'}
             and not r.get('invoice_refresh')][:limit]
    for receipt in queue:
        legacy.update(phase='strict_refresh_missing', strict_current_order=receipt['source_order'])
        rows = await legacy.read_all('financialtransaction/TransactionLines', {
            '$filter': "JournalCode eq '70' and YourRef eq '" + receipt['source_order'] + "'",
            '$select': SELECT})
        invoice, reason = own_invoice(receipt, rows, legacy.application().COLLECTIVE_DEBTOR_CODE)
        receipt['invoice_refresh'] = dict(at=now(), rows=rows, result=reason or 'own_invoice_found')
        receipt.update(invoice=invoice, exception=reason, workflow_status='open',
            suggested_action='match_own_invoice' if invoice else 'inspect_invoice_history')
        if invoice and receipt['state'] == 'exception_verified':
            receipt['state'] = 'unmatched_verified'
        known = {r['ID'] for r in plan['invoices']}
        plan['invoices'].extend(r for r in rows if r['ID'] not in known)
        legacy.artifact(PLAN, plan)
    legacy.update(phase='strict_refresh_missing_complete', strict_missing_invoices_refreshed=len(queue),
        strict_exception_count=sum(bool(r['exception']) for r in plan['receipts']))


async def run(mode, limit):
    conn = legacy.database()
    locked = False
    try:
        locked = conn.execute('SELECT pg_try_advisory_lock(%s)', (legacy.LOCK,)).fetchone()[0]
        if not locked:
            return
        legacy.update(running=True, action='strict_' + mode, last_error=None)
        if mode == 'prepare':
            await prepare()
        elif mode == 'verify':
            await verify_final()
        elif mode == 'refresh_missing':
            await refresh_missing(limit)
        elif mode in {'diagnose_closed', 'history_pending'}:
            await diagnose_closed(limit, pending=mode == 'history_pending')
        else:
            await process(mode, limit)
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
    if mode not in {'prepare', 'inspect', 'undo', 'undo_orphans', 'match', 'correct', 'recover', 'verify', 'diagnose_closed', 'history_pending', 'refresh_missing'} or not 1 <= limit <= 5:
        fail('invalid_strict_operation')
    legacy.state()
    if any(not t.done() for t in legacy.TASKS):
        fail('job_already_running')
    task = asyncio.create_task(run(mode, limit))
    legacy.TASKS.add(task)
    task.add_done_callback(legacy.TASKS.discard)
    return dict(accepted=True, mode=mode, limit=limit, read_only=mode in {'prepare', 'inspect', 'recover', 'verify', 'diagnose_closed', 'history_pending', 'refresh_missing'})
