"""One bounded ICEPAY acquisition; original files stay in private Postgres.

Uses observed portal controls. No Exact writes, persisted cookies, public
execution endpoint, or checkout-API polling. Never log raw worker output.
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

from operations import icepay_browser as b
from operations.icepay_fetch_probe import environments, stop_child, validate_forms

JOB = 'icepay-transactions-20261001-03-v12'
SAVED_SOURCE = 'icepay-transactions-20261001-03-v11'
RESUME_FROM = 'icepay-transactions-20261001-03-v4'
ACTIVATION = 'ICEPAY_TRANSACTION_TASK_ID'
EXPIRES = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
RANGE = '01/10/2026 - 03/10/2026'
START, END = date(2026,10,1), date(2026,10,3)
MAX_BYTES = 2_000_000
LOG = logging.getLogger('uvicorn.error')
REASONS = {'none','configuration','claim_failed','already_attempted','browser_install',
    'runtime_error','login_failed','date_control_missing','calendar_missing',
    'calendar_date_ambiguous','date_not_verified','clear_control_missing',
    'table_not_verified','export_ambiguous','export_not_completed','download_failed',
    'artifact_too_large','invalid_worker_output','invalid_csv','invalid_amount',
    'invalid_payment_date','out_of_period','duplicate_payment','count_mismatch',
    'unexpected_origin','unexpected_currency','order_reference_conflict','save_failed',
    'unsupported_form','credentials_rejected','verification_required','wrong_account'}
NOTIFICATIONS = re.compile(r'^Notifications(?:,\s*\d+\s+unread notifications?)?$')


class AcquisitionStopped(Exception):
    def __init__(self, reason):
        super().__init__(reason if reason in REASONS else 'runtime_error')


def status(state, stage, reason='none', account_verified=False):
    assert state in {'started','blocked','failed','downloaded','skipped'}
    assert stage in {'configuration','claim','install','login','payments_filter',
                     'payments_export','refunds','validation','storage','complete'}
    return {'state':state,'stage':stage,'reason':reason if reason in REASONS else 'runtime_error',
            'account_verified':account_verified is True,'financial_writes':False}


async def open_account_page(page, label):
    await b.wait_verified_account(page)
    link = await b.unique_account_link(page,label)
    if link is None:
        raise AcquisitionStopped('table_not_verified')
    target = urljoin(page.url,await link.get_attribute('href'))
    await link.click()
    await page.wait_for_url(target,timeout=15000)
    await page.wait_for_load_state('domcontentloaded')
    await b.wait_verified_account(page)


async def date_field(page, field_id):
    locator = page.locator('input[id="'+field_id+'"]')
    await locator.wait_for(state='visible',timeout=15000)
    fields = await b.visible(locator)
    if len(fields) != 1:
        raise AcquisitionStopped('date_control_missing')
    return fields[0]


async def clear_date(page, field_id):
    field = await date_field(page,field_id)
    if not await field.input_value():
        return
    parent = field
    for _ in range(5):
        parent = parent.locator('..')
        buttons = await b.visible(parent.get_by_role('button',name='Clear',exact=True))
        inputs = await b.visible(parent.locator('input[id^="tableFiltersForm."]'))
        if len(buttons)==1 and len(inputs)==1:
            await buttons[0].click()
            for _ in range(20):
                if not await field.input_value():
                    return
                await asyncio.sleep(.1)
            raise AcquisitionStopped('date_not_verified')
    raise AcquisitionStopped('clear_control_missing')


async def select_october_day(calendar, number):
    tables = []
    for table in await b.visible(calendar.locator('table')):
        heading = await table.locator('thead').inner_text()
        if 'October 2026' in heading:
            tables.append(table)
    if len(tables)!=1:
        raise AcquisitionStopped('calendar_date_ambiguous')
    cells = []
    for cell in await b.visible(tables[0].get_by_role('cell',name=str(number),exact=True)):
        if await cell.evaluate("el => !el.classList.contains('off') && !el.classList.contains('disabled')"):
            cells.append(cell)
    if len(cells)!=1:
        raise AcquisitionStopped('calendar_date_ambiguous')
    await cells[0].click()


async def select_period(page, field_id):
    field = await date_field(page,field_id)
    await field.click()
    locator = page.locator('.daterangepicker:visible')
    await locator.wait_for(state='visible',timeout=15000)
    calendars = await b.visible(locator)
    if len(calendars)!=1:
        raise AcquisitionStopped('calendar_missing')
    calendar = calendars[0]
    await select_october_day(calendar,1)
    await select_october_day(calendar,3)
    apply = await b.visible(calendar.get_by_role('button',name='Apply',exact=True))
    if len(apply)!=1:
        raise AcquisitionStopped('calendar_missing')
    await apply[0].click()
    if await field.input_value()!=RANGE:
        raise AcquisitionStopped('date_not_verified')


async def apply_period(page, *, refunds=False):
    await b.click_unique_read_control(page,re.compile(r'^Filter(?:\s+\d+)?$'))
    if not refunds:
        await clear_date(page,'tableFiltersForm.OrderTime.OrderTime')
    field_id = ('tableFiltersForm.DateCreated.DateCreated' if refunds
                else 'tableFiltersForm.PaymentTime.PaymentTime')
    await select_period(page,field_id)
    if refunds:
        await page.locator('select[id="tableFiltersForm.Enabled.value"]').select_option(label='All')
    await b.click_unique_read_control(page,re.compile(r'^Apply filters$'))
    await page.wait_for_load_state('networkidle',timeout=20000)
    await b.wait_verified_account(page)
    # Read back the exact selection after the server-driven filter update.
    await b.click_unique_read_control(page,re.compile(r'^Filter(?:\s+\d+)?$'))
    if await (await date_field(page,field_id)).input_value()!=RANGE:
        raise AcquisitionStopped('date_not_verified')
    if not refunds and await (await date_field(page,'tableFiltersForm.OrderTime.OrderTime')).input_value():
        raise AcquisitionStopped('date_not_verified')
    # Reapply the same read filter to close its panel without changing scope.
    await b.click_unique_read_control(page,re.compile(r'^Apply filters$'))
    await page.wait_for_load_state('networkidle',timeout=20000)


async def table_snapshot(page):
    await b.guard_page(page)
    tables = await b.visible(page.locator('table'))
    body = await page.locator('body').inner_text()
    empty = bool(re.search(r'No (?:payments|refunds|results|records)(?: found)?',body,re.I))
    match = re.search(r'Showing\s+\d+\s+to\s+\d+\s+of\s+([\d,.]+)\s+results',body,re.I)
    total = int(re.sub(r'\D','',match[1])) if match else None
    if len(tables)!=1:
        if empty and not tables:
            return {'headers':[],'rows':[],'total':0,'next':False}
        raise AcquisitionStopped('table_not_verified')
    headers = [re.sub(r'\s+',' ',v).strip()[:160] for v in await tables[0].locator('thead th').all_inner_texts()]
    rows = []
    for row in await tables[0].locator('tbody tr').all():
        cells = [re.sub(r'\s+',' ',v).strip()[:500] for v in await row.locator('td').all_inner_texts()]
        if cells and not empty:
            rows.append(cells)
    next_buttons = await b.visible(page.get_by_role('button',name='Next',exact=True))
    next_enabled = len(next_buttons)==1 and await next_buttons[0].is_enabled()
    if total is None and not next_enabled:
        total = len(rows)
    if total is None or total>5000 or (total and not headers):
        raise AcquisitionStopped('table_not_verified')
    return {'headers':headers,'rows':rows,'total':total,'next':next_enabled}


async def table_evidence(page):
    """Private bounded visible row evidence; only headers/counts reach logs."""
    await b.guard_page(page)
    return await page.evaluate(r'''() => {
      const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
      const text=e=>(e.innerText||'').replace(/\s+/g,' ').trim().slice(0,500);
      return Array.from(document.querySelectorAll('table,[role="table"]')).filter(visible).slice(0,5).map(table=>({
        headers:Array.from(table.querySelectorAll('thead th,[role="columnheader"]')).map(text).slice(0,40),
        rows:Array.from(table.querySelectorAll('tbody tr,[role="row"]')).filter(visible).slice(0,100).map(row=>({
          id:(row.querySelector('input[type="checkbox"]')?.getAttribute('aria-label')||'').slice(0,100),
          cells:Array.from(row.querySelectorAll('td,[role="cell"]')).map(text).slice(0,40)
        }))
      }));
    }''')


async def payment_checkbox_ids(page):
    identifiers = []
    for checkbox in await b.visible(page.locator('input[type="checkbox"]')):
        label = await checkbox.get_attribute('aria-label') or ''
        match = re.fullmatch(r'Select/deselect item (\d+) for bulk actions\.',label)
        if match:
            identifiers.append(match[1])
    if len(identifiers)!=len(set(identifiers)):
        raise AcquisitionStopped('duplicate_payment')
    return identifiers


async def stable_payment_page(page, page_size=None):
    previous, stable = None, 0
    for _ in range(30):
        snapshot = await page.evaluate(r'''() => {
          const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
          const ids=Array.from(document.querySelectorAll('input[type="checkbox"]'))
            .filter(visible).map(e=>(e.getAttribute('aria-label')||'').match(/^Select\/deselect item (\d+) for bulk actions\.$/))
            .filter(Boolean).map(m=>m[1]);
          const next=Array.from(document.querySelectorAll('button,[role="button"]'))
            .filter(visible).filter(e=>(e.getAttribute('aria-label')||e.innerText||'').trim()==='Next');
          return {ids,next_count:next.length,next:next.length===1&&!next[0].disabled&&next[0].getAttribute('aria-disabled')!=='true'};
        }''')
        ids = snapshot.get('ids',[])
        if len(ids)!=len(set(ids)) or snapshot.get('next_count',0)>1:
            raise AcquisitionStopped('table_not_verified')
        if page_size and snapshot['next'] and len(ids)!=page_size:
            stable = 0
        elif snapshot==previous:
            stable += 1
            if stable>=2:
                return snapshot
        else:
            stable = 0
        previous = snapshot
        await asyncio.sleep(.5)
    raise AcquisitionStopped('table_not_verified')


async def payment_identifiers(page):
    """Count observed per-row PaymentIDs across pages, independent of footer text."""
    page_size = None
    for select in await b.visible(page.locator('select')):
        options = [s.strip() for s in await select.locator('option').all_inner_texts()]
        if options==['25','50','100']:
            await select.select_option(label='100')
            page_size = 100
            break
    combined = set()
    for _ in range(200):
        await b.wait_verified_account(page)
        snapshot = await stable_payment_page(page,page_size)
        current = snapshot['ids']
        if combined.intersection(current):
            raise AcquisitionStopped('duplicate_payment')
        combined.update(current)
        if len(combined)>5000:
            raise AcquisitionStopped('artifact_too_large')
        if not snapshot['next']:
            if not combined:
                body = await page.locator('body').inner_text()
                if not re.search(r'No (?:payments|results|records)(?: found)?',body,re.I):
                    raise AcquisitionStopped('table_not_verified')
            return sorted(combined)
        if not current:
            raise AcquisitionStopped('table_not_verified')
        await b.click_unique_read_control(page,re.compile(r'^Next$'))
        for _ in range(30):
            following = (await stable_payment_page(page,page_size))['ids']
            if following and following!=current:
                break
            await asyncio.sleep(.2)
        else:
            raise AcquisitionStopped('table_not_verified')
    raise AcquisitionStopped('artifact_too_large')


def failure_location(error):
    """Only public source function/line and enumerated error class, no message."""
    names = {'TimeoutError','Error','ValueError','TypeError','KeyError','IndexError',
             'AttributeError','NameError','RuntimeError'}
    result = {'kind':type(error).__name__ if type(error).__name__ in names else 'Other',
              'function':'worker','line':0}
    trace = error.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_filename==__file__ and re.fullmatch(r'[a-z_]{1,60}',trace.tb_frame.f_code.co_name):
            result.update(function=trace.tb_frame.f_code.co_name,line=trace.tb_lineno)
        trace = trace.tb_next
    return result


async def csv_links(page):
    found = {}
    # Download anchors may have an explicit button role in a notification.
    for link in await b.visible(page.locator('a[href]')):
        label = await link.inner_text()
        if not re.search(r'\bcsv\b',label,re.I):
            continue
        href = await link.get_attribute('href')
        url = urljoin(page.url,href or '')
        if href and b.trusted(url):
            found[url] = link
    return found


async def close_notifications(page):
    marker = page.get_by_role('button',name='Mark all as read',exact=True)
    if not await b.visible(marker):
        return False
    await b.click_unique_read_control(page,re.compile(r'^Close$'))
    await marker.wait_for(state='hidden',timeout=15000)
    return True


async def open_notifications(page):
    marker = page.get_by_role('button',name='Mark all as read',exact=True)
    if not await b.visible(marker):
        await b.click_unique_read_control(page,NOTIFICATIONS)
    await marker.wait_for(state='visible',timeout=15000)


async def export_notifications(page):
    """Only export notices; omit URLs, emails and any credential-related text."""
    return await page.evaluate(r'''() => {
      const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
      const result=[];
      for(const b of document.querySelectorAll('button[aria-label="Close notification"]')) {
        if(!visible(b)) continue;
        let chosen='';
        for(let p=b.parentElement,n=0;p&&n<8;p=p.parentElement,n++) {
          if(p.querySelectorAll('button[aria-label="Close notification"]').length!==1) break;
          let text=(p.innerText||'').replace(/\s+/g,' ').trim();
          if(!/\bexport\b/i.test(text)) continue;
          if(/password|wachtwoord|secret|token/i.test(text)) break;
          text=text.replace(/https?:\/\/\S+/gi,'[link]').replace(/\S+@\S+/g,'[email]');
          chosen=text.slice(0,1000);
        }
        if(chosen) result.push(chosen);
      } return Array.from(new Set(result)).slice(0,5);
    }''')


async def download_control_metadata(page):
    return await page.evaluate(r'''() => Array.from(document.querySelectorAll('a,button'))
      .filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length))
      .filter(e=>/\bcsv\b/i.test(e.innerText||'')).slice(0,10).map(e=>{
        const href=e.getAttribute('href')||'';
        let kind='none';
        if(href.startsWith('javascript:')||href==='#') kind='script';
        else if(href) {try {kind=new URL(href,location.href).origin===location.origin?'portal':'external';}catch{kind='other';}}
        return {tag:e.tagName.toLowerCase(),label:(e.innerText||'').replace(/\s+/g,' ').trim().slice(0,100),href_kind:kind};
      })''')


async def payment_export(page, downloads, artifacts):
    artifacts['export_state'] = 'not_started'
    await open_notifications(page)
    await page.wait_for_load_state('networkidle',timeout=20000)
    await asyncio.sleep(.6)
    before = set(await csv_links(page))
    await close_notifications(page)
    await page.get_by_role('button',name='Actions',exact=True).wait_for(state='visible',timeout=15000)
    await b.click_unique_read_control(page,re.compile(r'^Actions$'))
    await page.get_by_role('button',name='Export payments',exact=True).wait_for(state='visible',timeout=15000)
    await b.click_unique_read_control(page,re.compile(r'^Export payments$',re.I))
    payment_column = page.locator('input[id="mountedActionSchema0.columnMap.PaymentID.isEnabled"]')
    await payment_column.wait_for(state='visible',timeout=15000)
    required = ['PaymentID','Amount','Currency','PaymentTime','LastPaymentStatus',
                'merchant.MerchantID','CheckoutReference','CheckoutDescription']
    for column in required:
        checkbox = page.locator('input[id="mountedActionSchema0.columnMap.'+column+'.isEnabled"]')
        if not await checkbox.is_checked():
            raise AcquisitionStopped('export_ambiguous')
    artifacts['export_state'] = 'submit_attempted'
    await b.click_unique_read_control(page,re.compile(r'^Export$'))
    artifacts['export_state'] = 'submitted'
    await payment_column.wait_for(state='hidden',timeout=20000)
    deadline, last_open = time.monotonic()+110, 0
    while time.monotonic()<deadline:
        await b.guard_page(page)
        if not downloads.empty():
            return await read_download(await downloads.get())
        if time.monotonic()-last_open>10:
            await close_notifications(page)
            await open_notifications(page)
            last_open = time.monotonic()
            await asyncio.sleep(.6)
        links = await csv_links(page)
        new = set(links)-before
        if len(new)>1:
            raise AcquisitionStopped('export_ambiguous')
        if new:
            await links[next(iter(new))].click()
            try:
                download = await asyncio.wait_for(downloads.get(),timeout=25)
            except asyncio.TimeoutError:
                raise AcquisitionStopped('download_failed') from None
            return await read_download(download)
        await asyncio.sleep(2)
    raise AcquisitionStopped('export_not_completed')


def csv_has_expected_ids(content, expected_ids):
    """Identify an existing export before full amount/date validation in parent."""
    try:
        text = content.decode('utf-8-sig')
        dialect = csv.Sniffer().sniff(text[:16000],delimiters=',;\t')
        reader = csv.DictReader(io.StringIO(text),dialect=dialect)
        keys = {re.sub(r'[^a-z0-9]','',k.lower()):k for k in reader.fieldnames or []}
        if 'paymentid' not in keys:
            return False
        ids = [r[keys['paymentid']].strip() for r in reader]
        return len(ids)==len(expected_ids) and set(ids)==set(expected_ids)
    except Exception:
        return False


def scope_csv(content, expected_ids):
    """Retain an original export and derive only the UI-proven ID selection."""
    report = {'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),
              'parseable':False,'rows':0,'matched_ids':0,'missing_ids':len(expected_ids),
              'extra_rows':0,'payment_id_header':False}
    try:
        text = content.decode('utf-16' if content[:2] in (b'\xff\xfe',b'\xfe\xff') else 'utf-8-sig')
        first,_,remaining = text.partition('\n')
        if re.fullmatch(r'sep=[,;\t]\r?',first,re.I):
            delimiter,text = first[4],remaining
        else:
            delimiter = csv.Sniffer().sniff(text[:16000],delimiters=',;\t').delimiter
        reader = csv.DictReader(io.StringIO(text),delimiter=delimiter)
        fields = reader.fieldnames or []
        keys = {re.sub(r'[^a-z0-9]','',k.lower()):k for k in fields}
        rows = list(reader)
        report.update(parseable=True,rows=len(rows),payment_id_header='paymentid' in keys)
        if 'paymentid' not in keys or len(keys)!=len(fields):
            return None,report
        expected = set(expected_ids)
        selected = [row for row in rows if (row.get(keys['paymentid']) or '').strip() in expected]
        ids = [(row.get(keys['paymentid']) or '').strip() for row in selected]
        report.update(matched_ids=len(set(ids)),missing_ids=len(expected-set(ids)),extra_rows=len(rows)-len(selected))
        if (set(ids)!=expected or len(ids)!=len(expected_ids) or
            any(None in row or any(v is None for v in row.values()) for row in selected)):
            return None,report
        output = io.StringIO()
        writer = csv.DictWriter(output,fieldnames=fields,delimiter=';')
        writer.writeheader()
        writer.writerows(selected)
        return output.getvalue().encode('utf-8-sig'),report
    except Exception:
        return None,report


async def resume_payment_export(page, downloads, expected_ids, artifacts):
    """Download a matching existing notification; never create another export."""
    await open_notifications(page)
    await page.wait_for_load_state('networkidle',timeout=20000)
    await asyncio.sleep(1)
    artifacts['export_notices'] = await export_notifications(page)
    artifacts['download_controls'] = await download_control_metadata(page)
    links = await csv_links(page)
    if not links or len(links)>5:
        raise AcquisitionStopped('export_ambiguous')
    artifacts['candidate_csvs'] = []
    total_bytes = 0
    for link in links.values():
        await b.guard_page(page)
        await link.click()
        try:
            download = await asyncio.wait_for(downloads.get(),timeout=25)
        except asyncio.TimeoutError:
            raise AcquisitionStopped('download_failed') from None
        content = await read_download(download)
        total_bytes += len(content)
        if total_bytes>MAX_BYTES:
            raise AcquisitionStopped('artifact_too_large')
        artifacts['candidate_csvs'].append(base64.b64encode(content).decode())
        scoped,_ = scope_csv(content,expected_ids)
        if scoped is not None:
            artifacts['source_export_csv'] = base64.b64encode(content).decode()
            return scoped
    raise AcquisitionStopped('export_not_completed')


async def legacy_payment_export(page, downloads, expected_ids, artifacts):
    """Use the observed legacy export after ICEPAY reported all modern rows failed."""
    artifacts['export_state'] = 'not_started'
    await close_notifications(page)
    await b.click_unique_read_control(page,re.compile(r'^Actions$'))
    legacy = page.get_by_role('button',name='Export payments (legacy)',exact=True)
    await legacy.wait_for(state='visible',timeout=15000)
    artifacts['export_state'] = 'submit_attempted'
    await legacy.click()
    artifacts['export_state'] = 'legacy_opened'
    confirmation = page.get_by_role('button',name='Export',exact=True)
    await confirmation.wait_for(state='visible',timeout=15000)
    artifacts['export_state'] = 'submit_attempted'
    await b.click_unique_read_control(page,re.compile(r'^Export$'))
    artifacts['export_state'] = 'submitted'
    try:
        download = await asyncio.wait_for(downloads.get(),timeout=60)
    except asyncio.TimeoutError:
        # A legacy configuration dialog, if any, is retained as form metadata.
        raise AcquisitionStopped('export_not_completed') from None
    content = await read_download(download)
    artifacts['export_state'] = 'submitted'
    artifacts['source_export_csv'] = base64.b64encode(content).decode()
    artifacts['candidate_csvs'] = [artifacts['source_export_csv']]
    scoped,_ = scope_csv(content,expected_ids)
    if scoped is None:
        raise AcquisitionStopped('export_not_completed')
    return scoped


async def read_download(download):
    if await download.failure():
        raise AcquisitionStopped('download_failed')
    path = await download.path()
    if not path or Path(path).stat().st_size>MAX_BYTES:
        raise AcquisitionStopped('artifact_too_large')
    return Path(path).read_bytes()


async def read_refunds(page):
    await close_notifications(page)
    await open_account_page(page,'Refunds')
    await apply_period(page,refunds=True)
    combined, seen, headers, total = [], set(), None, None
    for _ in range(50):
        snapshot = await table_snapshot(page)
        if headers is None:
            headers,total = snapshot['headers'],snapshot['total']
        if snapshot['headers']!=headers or snapshot['total']!=total:
            raise AcquisitionStopped('table_not_verified')
        fingerprint = json.dumps(snapshot['rows'],sort_keys=True)
        if fingerprint in seen and snapshot['rows']:
            raise AcquisitionStopped('table_not_verified')
        seen.add(fingerprint)
        combined.extend(snapshot['rows'])
        if not snapshot['next']:
            if len(combined)!=total:
                raise AcquisitionStopped('count_mismatch')
            return {'headers':headers,'rows':combined,'total':total,'period':RANGE}
        await b.click_unique_read_control(page,re.compile(r'^Next$'))
        await page.wait_for_load_state('networkidle',timeout=20000)
    raise AcquisitionStopped('artifact_too_large')


def amount(value):
    text = value.strip().replace('\u00a0','').replace(' ','')
    if re.fullmatch(r'-?\d+(?:,\d{1,2})?',text):
        text = text.replace(',','.')
    elif not re.fullmatch(r'-?\d+(?:\.\d{1,2})?',text):
        raise AcquisitionStopped('invalid_amount')
    result = Decimal(text)
    if not result.is_finite() or abs(result)>Decimal('1000000'):
        raise AcquisitionStopped('invalid_amount')
    return result.quantize(Decimal('.01'))


def payment_date(text):
    # ICEPAY CSV's US timestamps were verified in the existing sample export.
    # Portal date controls use a different (day/month) format.
    for fmt in ('%m/%d/%Y %I:%M:%S %p','%m/%d/%Y %I:%M %p','%m/%d/%Y %H:%M:%S',
                '%Y-%m-%d %H:%M:%S','%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text.strip(),fmt).date()
        except ValueError:
            pass
    raise AcquisitionStopped('invalid_payment_date')


def parse_payments(content, expected_count, expected_ids=None):
    try:
        text = content.decode('utf-8-sig')
        dialect = csv.Sniffer().sniff(text[:16000],delimiters=',;\t')
        reader = csv.DictReader(io.StringIO(text),dialect=dialect)
        normalize = lambda key: re.sub(r'[^a-z0-9]','',key.lower())
        headers = {normalize(k):k for k in reader.fieldnames or []}
        required = {'paymentid','merchantid','paymenttime','lastpaymentstatus','amount','currency'}
        if not required<=headers.keys() or len(headers)!=len(reader.fieldnames or []):
            raise AcquisitionStopped('invalid_csv')
        if not {'description','checkoutreference','checkoutdescription'}.intersection(headers):
            raise AcquisitionStopped('invalid_csv')
        source = list(reader)
    except AcquisitionStopped:
        raise
    except Exception:
        raise AcquisitionStopped('invalid_csv') from None
    if len(source)!=expected_count or len(source)>5000:
        raise AcquisitionStopped('count_mismatch')
    seen, selected, statuses = set(), [], Counter()
    for raw in source:
        if None in raw or any(v is None for v in raw.values()):
            raise AcquisitionStopped('invalid_csv')
        row = {key:raw[value].strip() for key,value in headers.items()}
        key = row['merchantid'],row['paymentid']
        if not all(re.fullmatch(r'\d+',v) for v in key):
            raise AcquisitionStopped('invalid_csv')
        if key in seen:
            raise AcquisitionStopped('duplicate_payment')
        seen.add(key)
        paid = payment_date(row['paymenttime'])
        if not START<=paid<=END:
            raise AcquisitionStopped('out_of_period')
        if row['merchantid']!=b.MERCHANT:
            continue
        if row['currency']!='EUR':
            raise AcquisitionStopped('unexpected_currency')
        value = amount(row['amount'])
        order_values = set()
        for field in ('checkoutreference','checkoutdescription','description','reference'):
            order_values.update(re.findall(r'\bOrder\s*#\s*(\d{4,10})\b',row.get(field,''),re.I))
        if len(order_values)>1:
            raise AcquisitionStopped('order_reference_conflict')
        selected.append({'payment_id':row['paymentid'],'merchant':b.MERCHANT,
            'date':paid.isoformat(),'amount':str(value),'status':row['lastpaymentstatus'],
            'order':next(iter(order_values),None)})
        statuses[row['lastpaymentstatus']] += 1
    if expected_ids is not None and ({payment for _,payment in seen}!=set(expected_ids)
                                    or len(expected_ids)!=len(source)):
        raise AcquisitionStopped('count_mismatch')
    ok = [r for r in selected if r['status']=='OK']
    return selected, {'source_rows':len(source),'td_rows':len(selected),
        'td_ok_count':len(ok),'td_ok_total':str(sum((Decimal(r['amount']) for r in ok),Decimal('0.00'))),
        'other_merchant_rows':len(source)-len(selected),'non_ok_count':len(selected)-len(ok),
        'missing_order_count':sum(r['order'] is None for r in ok),
        'nonpositive_ok_count':sum(Decimal(r['amount'])<=0 for r in ok),
        'per_day':{d: {'count':sum(r['date']==d for r in ok),
            'total':str(sum((Decimal(r['amount']) for r in ok if r['date']==d),Decimal('0.00')))}
            for d in ('2026-10-01','2026-10-02','2026-10-03')}}


async def worker(resume):
    stage, verified, artifacts = 'configuration', False, {}
    result = status('failed',stage,'runtime_error')
    try:
        validate_proof(resume)
        credentials = b.Credentials.from_env(os.environ)
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            base,_ = environments(os.environ)
            browser = await playwright.chromium.launch(headless=True,env=base)
            try:
                context = await browser.new_context(accept_downloads=True,service_workers='block',
                                                    timezone_id='Europe/Amsterdam')
                await b.protect_requests(context)
                page = await context.new_page()
                page.set_default_timeout(15000)
                downloads = asyncio.Queue()
                page.on('download',downloads.put_nowait)
                stage = 'login'
                login = await b.authenticate(page,credentials)
                artifacts['login'] = login
                if not login['account_verified']:
                    raise AcquisitionStopped('login_failed')
                verified = True
                stage = 'payments_filter'
                await open_account_page(page,'Payments')
                await apply_period(page)
                identifiers = await payment_identifiers(page)
                if identifiers!=resume['ui_payment_ids']:
                    raise AcquisitionStopped('count_mismatch')
                artifacts['proof'] = {'period':RANGE,'ui_payment_count':len(identifiers),
                                      'ui_payment_ids':identifiers}
                artifacts['table_evidence'] = {'payments':await table_evidence(page)}
                stage = 'payments_export'
                export_error = None
                try:
                    content = await resume_payment_export(page,downloads,identifiers,artifacts)
                    artifacts['payments_csv'] = base64.b64encode(content).decode()
                except AcquisitionStopped as error:
                    export_error = error
                stage = 'refunds'
                artifacts['refunds'] = await read_refunds(page)
                if export_error:
                    stage = 'payments_export'
                    raise export_error
                result = status('downloaded','complete',account_verified=True)
            except Exception as error:
                if not isinstance(error,(AcquisitionStopped,b.Stopped)):
                    artifacts['failure'] = failure_location(error)
                if verified:
                    try:
                        artifacts.setdefault('table_evidence',{})[stage] = await table_evidence(page)
                        key = 'refunds_filters' if stage=='refunds' else 'payments_filters'
                        artifacts['form_metadata'] = {key:{'path':urlsplit(page.url).path,
                            'controls':await b.inspect_controls(page)}}
                    except Exception:
                        pass
                raise
            finally:
                await browser.close()
    except AcquisitionStopped as exc:
        result = status('blocked',stage,str(exc),verified)
    except b.Stopped as exc:
        result = status('blocked',stage,str(exc) if str(exc) in REASONS else 'login_failed',verified)
    except Exception:
        result = status('failed',stage,'runtime_error',verified)
    return {'status':result,'artifacts':artifacts}


def claim(database_url):
    import psycopg
    with psycopg.connect(database_url,connect_timeout=10) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_transaction_tasks (
          job TEXT PRIMARY KEY, attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          status JSONB NOT NULL, artifacts JSONB NOT NULL DEFAULT '{}'::jsonb,
          summary JSONB NOT NULL DEFAULT '{}'::jsonb)''')
        return conn.execute('''INSERT INTO icepay_transaction_tasks(job,status) VALUES(%s,%s::jsonb)
          ON CONFLICT DO NOTHING RETURNING job''',
          (JOB,json.dumps(status('started','claim')))).fetchone() is not None


def validate_proof(proof):
    if not isinstance(proof,dict) or proof.get('period')!=RANGE:
        raise AcquisitionStopped('configuration')
    ids = proof.get('ui_payment_ids')
    if (not isinstance(ids,list) or len(ids)>5000 or
        any(not isinstance(v,str) or not re.fullmatch(r'\d+',v) for v in ids) or
        ids!=sorted(set(ids)) or proof.get('ui_payment_count')!=len(ids)):
        raise AcquisitionStopped('configuration')
    return proof


def previous_proof(database_url):
    import psycopg
    with psycopg.connect(database_url,connect_timeout=10) as conn:
        row = conn.execute("SELECT status,artifacts->'proof' FROM icepay_transaction_tasks WHERE job=%s",
                           (RESUME_FROM,)).fetchone()
    if not row or row[0].get('account_verified') is not True or row[0].get('stage')!='payments_export':
        raise AcquisitionStopped('configuration')
    return validate_proof(row[1])


def saved_source(database_url):
    import psycopg
    with psycopg.connect(database_url,connect_timeout=10) as conn:
        row = conn.execute('SELECT status,artifacts FROM icepay_transaction_tasks WHERE job=%s',
                           (SAVED_SOURCE,)).fetchone()
    if not row or row[0].get('account_verified') is not True:
        raise AcquisitionStopped('configuration')
    result,artifacts = decode(json.dumps({'status':row[0],'artifacts':row[1]}).encode())
    validate_proof(artifacts.get('proof'))
    content = base64.b64decode(artifacts.get('source_export_csv',''),validate=True)
    if hashlib.sha256(content).hexdigest()!='d623e07ceba28cbe211053933f1bd91fb71d985a6c382441e5ec155d986973ef':
        raise AcquisitionStopped('configuration')
    return status('downloaded','complete',account_verified=True),artifacts


def csv_schema(content):
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')),delimiter=';')
    fields = reader.fieldnames or []
    rows = list(reader)
    keys = {re.sub(r'[^a-z0-9]','',k.lower()):k for k in fields}
    return {'headers':[re.sub(r'[^\w .()/\-]','',s)[:100] for s in fields],
        'rows':len(rows),'malformed_rows':sum(None in row or any(v is None for v in row.values()) for row in rows),
        'nonnumeric_ids':{key:sum(not re.fullmatch(r'\d+',str(row.get(keys[key]) or '').strip()) for row in rows)
                          for key in ('paymentid','merchantid') if key in keys}}


def save(database_url, result, artifacts, summary):
    import psycopg
    with psycopg.connect(database_url,connect_timeout=10) as conn:
        conn.execute('''UPDATE icepay_transaction_tasks SET status=%s::jsonb,
          artifacts=%s::jsonb,summary=%s::jsonb WHERE job=%s''',
          (json.dumps(result),json.dumps(artifacts),json.dumps(summary),JOB))


def decode(stdout):
    if len(stdout)>8_000_000:
        raise AcquisitionStopped('artifact_too_large')
    raw = json.loads(stdout)
    if set(raw)!={'status','artifacts'}:
        raise AcquisitionStopped('invalid_worker_output')
    result = status(**{k:v for k,v in raw['status'].items() if k!='financial_writes'})
    artifacts = raw['artifacts']
    if not isinstance(artifacts,dict) or set(artifacts)-{'login','proof','payments_csv','refunds','form_metadata','failure',
                                                      'candidate_csvs','source_export_csv','export_state','export_notices','download_controls','table_evidence'}:
        raise AcquisitionStopped('invalid_worker_output')
    if 'form_metadata' in artifacts:
        validate_forms(artifacts['form_metadata'])
    if 'table_evidence' in artifacts:
        evidence = artifacts['table_evidence']
        if (not isinstance(evidence,dict) or set(evidence)-{'payments','payments_export','refunds'}
            or len(json.dumps(evidence))>500000):
            raise AcquisitionStopped('invalid_worker_output')
        for tables in evidence.values():
            if not isinstance(tables,list) or len(tables)>5:
                raise AcquisitionStopped('invalid_worker_output')
            for table in tables:
                if (set(table)!={'headers','rows'} or not isinstance(table['headers'],list)
                    or len(table['headers'])>40 or any(not isinstance(v,str) or len(v)>500 for v in table['headers'])
                    or not isinstance(table['rows'],list) or len(table['rows'])>100):
                    raise AcquisitionStopped('invalid_worker_output')
                for row in table['rows']:
                    if (set(row)!={'id','cells'} or not isinstance(row['id'],str) or len(row['id'])>100
                        or not isinstance(row['cells'],list) or len(row['cells'])>40
                        or any(not isinstance(v,str) or len(v)>500 for v in row['cells'])):
                        raise AcquisitionStopped('invalid_worker_output')
    if artifacts.get('export_state','not_started') not in {'not_started','submit_attempted','submitted','legacy_opened'}:
        raise AcquisitionStopped('invalid_worker_output')
    if 'export_notices' in artifacts:
        notices = artifacts['export_notices']
        if not isinstance(notices,list) or len(notices)>5 or any(not isinstance(s,str) or len(s)>1000 for s in notices):
            raise AcquisitionStopped('invalid_worker_output')
    if 'download_controls' in artifacts:
        controls = artifacts['download_controls']
        if not isinstance(controls,list) or len(controls)>10 or any(
            set(c)!={'tag','label','href_kind'} or c['tag'] not in {'a','button'} or
            c['href_kind'] not in {'none','script','portal','external','other'} or
            not isinstance(c['label'],str) or len(c['label'])>100 for c in controls):
            raise AcquisitionStopped('invalid_worker_output')
    if 'failure' in artifacts:
        failure = artifacts['failure']
        if (set(failure)!={'kind','function','line'} or type(failure['line']) is not int or
            not re.fullmatch(r'[a-z_]{1,60}',failure['function']) or
            failure['kind'] not in {'TimeoutError','Error','ValueError','TypeError','KeyError','IndexError',
                                   'AttributeError','NameError','RuntimeError','Other'}):
            raise AcquisitionStopped('invalid_worker_output')
    return result,artifacts


async def run():
    if os.environ.get(ACTIVATION)!=JOB or datetime.now(timezone.utc)>=EXPIRES:
        return
    result, artifacts, summary = status('blocked','configuration','configuration'), {}, {}
    database_url = os.environ.get('DATABASE_URL')
    try:
        b.Credentials.from_env(os.environ)
        if not database_url:
            raise ValueError()
        resume = await asyncio.to_thread(previous_proof,database_url)
    except Exception:
        LOG.warning('ICEPAY_TRANSACTIONS %s',json.dumps({'job':JOB,**result}))
        return
    try:
        claimed = await asyncio.to_thread(claim,database_url)
    except Exception:
        LOG.warning('ICEPAY_TRANSACTIONS %s',json.dumps({'job':JOB,**status('failed','claim','claim_failed')}))
        return
    if not claimed:
        return
    child = None
    base, child_env = environments(os.environ)
    result = status('failed','install','browser_install')
    LOG.warning('ICEPAY_TRANSACTIONS %s',json.dumps({'job':JOB,**status('started','install')}))
    try:
        if SAVED_SOURCE:
            result,artifacts = await asyncio.to_thread(saved_source,database_url)
        else:
            child = await asyncio.create_subprocess_exec(sys.executable,'-m','playwright','install','chromium','--only-shell',
                env=base,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
            if await asyncio.wait_for(child.wait(),150)!=0:
                return
            child = None
            result = status('failed','login','runtime_error')
            child = await asyncio.create_subprocess_exec(sys.executable,'-m','operations.icepay_transactions','--worker',
                env=child_env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
            stdout,_ = await asyncio.wait_for(child.communicate(json.dumps(resume).encode()),timeout=330)
            if child.returncode!=0:
                raise AcquisitionStopped('invalid_worker_output')
            result,artifacts = decode(stdout)
        if 'export_state' in artifacts:
            summary['export_state'] = artifacts['export_state']
        if 'export_notices' in artifacts:
            summary['export_notices'] = artifacts['export_notices']
        if 'download_controls' in artifacts:
            summary['download_controls'] = artifacts['download_controls']
        if 'proof' in artifacts:
            summary['ui_payment_count'] = validate_proof(artifacts['proof'])['ui_payment_count']
        if 'candidate_csvs' in artifacts:
            candidates = artifacts['candidate_csvs']
            if not isinstance(candidates,list) or len(candidates)>5:
                raise AcquisitionStopped('invalid_worker_output')
            summary['candidate_exports'] = []
            for encoded in candidates:
                candidate = base64.b64decode(encoded,validate=True)
                if len(candidate)>MAX_BYTES:
                    raise AcquisitionStopped('artifact_too_large')
                _,report = scope_csv(candidate,resume['ui_payment_ids'])
                summary['candidate_exports'].append(report)
        if 'failure' in artifacts:
            summary['failure'] = artifacts['failure']
        if 'table_evidence' in artifacts:
            summary['tables'] = {name:[{'headers':table['headers'],'rows':len(table['rows']),
                'row_widths':sorted(set(len(row['cells']) for row in table['rows']))} for table in tables]
                for name,tables in artifacts['table_evidence'].items()}
        refunds = artifacts.get('refunds')
        if refunds is not None:
            if refunds.get('period')!=RANGE or len(refunds.get('rows',[]))!=refunds.get('total'):
                raise AcquisitionStopped('invalid_worker_output')
            summary['refund_rows'] = refunds['total']
            summary['refund_columns'] = [re.sub(r'[^\w .()/\-]','',s)[:100] for s in refunds['headers']]
        if 'payments_csv' in artifacts:
            content = base64.b64decode(artifacts['payments_csv'],validate=True)
            if len(content)>MAX_BYTES:
                raise AcquisitionStopped('artifact_too_large')
            summary['csv_schema'] = csv_schema(content)
            proof = artifacts.get('proof',{})
            if proof.get('period')!=RANGE or type(proof.get('ui_payment_count')) is not int:
                raise AcquisitionStopped('invalid_worker_output')
            ids = proof.get('ui_payment_ids')
            if not isinstance(ids,list) or any(not isinstance(v,str) or not re.fullmatch(r'\d+',v) for v in ids):
                raise AcquisitionStopped('invalid_worker_output')
            rows,payment_summary = parse_payments(content,proof['ui_payment_count'],ids)
            summary.update(payment_summary)
            artifacts['normalized_payments'] = rows
            summary['sha256'] = hashlib.sha256(content).hexdigest()
            if 'source_export_csv' in artifacts:
                summary['original_export_sha256'] = hashlib.sha256(base64.b64decode(
                    artifacts['source_export_csv'],validate=True)).hexdigest()
            refunds = artifacts.get('refunds')
            if refunds is not None:
                if refunds.get('period')!=RANGE or len(refunds.get('rows',[]))!=refunds.get('total'):
                    raise AcquisitionStopped('invalid_worker_output')
                summary['refund_rows'] = refunds['total']
                summary['refund_columns'] = [re.sub(r'[^\w .()/\-]','',s)[:100] for s in refunds['headers']]
    except asyncio.CancelledError:
        raise
    except AcquisitionStopped as exc:
        result = status('blocked','validation',str(exc),result['account_verified'])
    except Exception:
        result = status('failed','validation','runtime_error',result['account_verified'])
    finally:
        await stop_child(child)
        try:
            await asyncio.to_thread(save,database_url,result,artifacts,summary)
        except Exception:
            result = status('failed','storage','save_failed',result['account_verified'])
        LOG.warning('ICEPAY_TRANSACTIONS %s',json.dumps({'job':JOB,**result,'summary':summary},sort_keys=True))
        for name, form in artifacts.get('form_metadata',{}).items():
            for start in range(0,len(form['controls']),10):
                LOG.warning('ICEPAY_TRANSACTION_FORM %s',json.dumps({'job':JOB,'page':name,
                    'offset':start,'controls':form['controls'][start:start+10]},sort_keys=True))


if __name__=='__main__' and sys.argv[1:]==['--worker']:
    print(json.dumps(asyncio.run(worker(json.loads(sys.stdin.read(200000)))),sort_keys=True))
