"""Expiring, authenticated controller for one approved Fibonatix batch.

No arbitrary URL, script, XML topic, administration, journal or import payload.
XML bytes are held privately in Postgres, never in Git or public health/logs.
Import attempts are durably claimed before the single HTTP write. Ambiguous
outcomes must be reconciled by reading Exact, never by repeating the upload.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit, urlencode
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
import httpx

JOB = 'FIBO-20260922-20261002'
SHA = '18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2'
DIVISION = 3977752
BANK = '63305e6f-827d-4884-9df1-6ebbb5858a76'
DEBTOR = 'ec2af99c-809c-40e3-9057-8a31962ae1cf'
EXPIRES = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
LOCK = 397775226
router = APIRouter(prefix='/ops/fibonatix-20261002')
TASKS = set()
ARTIFACTS = {'ledger_before', 'ledger_after', 'receivables_before', 'receivables_after', 'ui', 'xml_response', 'automatic_before', 'automatic_after'}


def application():
    from app import main
    if main.DIVISION != DIVISION or main.BASE_URL != 'https://start.exactonline.nl':
        raise HTTPException(409, 'wrong_exact_configuration')
    return main


def authorize(request):
    token = os.environ.get('EXACT_IMPORT_CONTROL_TOKEN', '')
    supplied = request.headers.get('authorization', '')
    if datetime.now(timezone.utc) >= EXPIRES or len(token) < 40 or not hmac.compare_digest(supplied, 'Bearer ' + token):
        raise HTTPException(404, 'Not found')


def database():
    return application()._db_connect()


def init_db():
    with database() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS fibonatix_import_jobs (
            job TEXT PRIMARY KEY, sha TEXT NOT NULL, xml BYTEA NOT NULL,
            data JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS fibonatix_import_attempts (
            job TEXT NOT NULL, action TEXT NOT NULL,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(job,action))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS fibonatix_import_artifacts (
            job TEXT NOT NULL, name TEXT NOT NULL, data JSONB NOT NULL,
            PRIMARY KEY(job,name))''')


def state():
    with database() as conn:
        row = conn.execute('SELECT data FROM fibonatix_import_jobs WHERE job=%s', (JOB,)).fetchone()
    if row is None:
        raise HTTPException(404, 'batch_not_prepared')
    return row[0]


def update(**patch):
    with database() as conn:
        conn.execute('UPDATE fibonatix_import_jobs SET data=data || %s::jsonb, updated_at=now() WHERE job=%s',
                     (json.dumps(patch), JOB))


def artifact(name, value):
    assert name in ARTIFACTS
    with database() as conn:
        conflict = 'DO NOTHING' if name.endswith('_before') else 'DO UPDATE SET data=EXCLUDED.data'
        conn.execute('''INSERT INTO fibonatix_import_artifacts(job,name,data) VALUES(%s,%s,%s::jsonb)
            ON CONFLICT(job,name) ''' + conflict, (JOB, name, json.dumps(value)))
        # Repair only the known empty baseline caused by the old unpadded-code
        # query, and only while there has never been a financial write attempt.
        if name == 'receivables_before' and value:
            conn.execute('''UPDATE fibonatix_import_artifacts SET data=%s::jsonb
                WHERE job=%s AND name='receivables_before' AND data='[]'::jsonb
                AND NOT EXISTS(SELECT 1 FROM fibonatix_import_attempts WHERE job=%s)''',
                (json.dumps(value),JOB,JOB))


def claim(action):
    with database() as conn:
        row = conn.execute('''INSERT INTO fibonatix_import_attempts(job,action) VALUES(%s,%s)
            ON CONFLICT DO NOTHING RETURNING action''', (JOB, action)).fetchone()
    if row is None:
        raise HTTPException(409, 'write_already_attempted_reconcile_only')


def payload():
    with database() as conn:
        row = conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s AND sha=%s', (JOB, SHA)).fetchone()
    if row is None:
        raise HTTPException(409, 'batch_not_prepared')
    return bytes(row[0])


def parse_batch(blob, *, digest=SHA, count=845, net=Decimal('85500.86')):
    if len(blob) > 1000000 or hashlib.sha256(blob).hexdigest() != digest:
        raise ValueError('unexpected_xml_hash')
    root = ET.fromstring(blob)
    if root.tag != 'eExact' or [e.tag for e in root] != ['GLTransactions']:
        raise ValueError('unexpected_xml_topic')
    rows = []
    entries = set()
    for entry in root.findall('./GLTransactions/GLTransaction'):
        number = int(entry.get('entry'))
        if number in entries or not 26260008 <= number <= 26260018:
            raise ValueError('unexpected_entry_number')
        entries.add(number)
        date = entry.findtext('Date')
        if not '2026-09-22' <= date <= '2026-10-02' or entry.find('Journal').get('code') != '26':
            raise ValueError('wrong_date_or_journal')
        for line in entry.findall('GLTransactionLine'):
            gl = line.find('GLAccount').get('code')
            account = line.find('Account')
            code = account.get('code') if account is not None else None
            if not ((gl == '1100' and code == '100100') or (gl in {'1350','2000'} and code is None)):
                raise ValueError('wrong_offset_account')
            if line.find('Amount/Currency').get('code') != 'EUR' or line.findtext('Date') != date:
                raise ValueError('wrong_currency_or_line_date')
            amount = Decimal(line.findtext('Amount/Value'))
            if not amount.is_finite() or amount == 0 or amount != amount.quantize(Decimal('.01')):
                raise ValueError('invalid_amount')
            trx = line.findtext('References/PaymentReference') or ''
            ref = line.findtext('References/YourRef') or ''
            desc, note = line.findtext('Description') or '', line.findtext('Note') or ''
            if not re.fullmatch('[A-Za-z0-9]{8}', trx) or trx not in desc or desc not in note or JOB not in note:
                raise ValueError('missing_visible_reference')
            if gl == '1100' and (not re.fullmatch(r'TD\d+', ref) or ref not in desc or not re.search(r'Woo \d+', desc)):
                raise ValueError('missing_order_reference')
            if line.find('FinYear').get('number') != '2026' or int(line.find('FinPeriod').get('number')) != int(date[5:7]):
                raise ValueError('wrong_period')
            rows.append(dict(trx=trx, ref=ref, date=date, entry=number, gl=gl, account=code,
                             amount=str(amount), description=desc, note=note))
    if len(rows) != count or len({r['trx'] for r in rows}) != count or sum((Decimal(r['amount']) for r in rows), Decimal(0)) != net:
        raise ValueError('unexpected_count_duplicates_or_net')
    return rows


def brief(rows):
    positive = [Decimal(r['amount']) for r in rows if Decimal(r['amount']) > 0]
    negative = [Decimal(r['amount']) for r in rows if Decimal(r['amount']) < 0]
    return dict(job=JOB, sha=SHA, transactions=len(rows), entries=sorted({r['entry'] for r in rows}),
                receipts=len(positive), receipt_total=str(sum(positive)), refunds=len(negative),
                refund_total=str(-sum(negative)), net=str(sum(positive) + sum(negative)),
                offset_counts={k:sum(r['gl']==k for r in rows) for k in ['1100','1350','2000']})


async def read_all(path, params):
    app = application()
    async def get(url=None):
        for attempt in range(4):
            try:
                result = await app._request_json('GET',url) if url else await app.exact_get(path,params)
                # Leave room for the existing agents in Exact's minute budget.
                await asyncio.sleep(1.5)
                return result
            except HTTPException as exc:
                if exc.status_code not in {429,502,503,504} or attempt == 3:
                    raise
                update(read_wait_status=exc.status_code)
                await asyncio.sleep(30)
    page = await get()
    rows, seen = [], set()
    for _ in range(200):
        rows.extend(app._extract_results(page))
        body = page.get('d', {}) if isinstance(page, dict) else {}
        nxt = body.get('__next') if isinstance(body, dict) else None
        if not nxt:
            return rows
        prefix = f'{app.API_V1}/{DIVISION}/'
        if not nxt.startswith(prefix) or nxt in seen:
            raise HTTPException(409, 'unsafe_or_repeated_pagination')
        seen.add(nxt)
        page = await get(nxt)
    raise HTTPException(409, 'incomplete_pagination')


async def ledger():
    return await read_all('financialtransaction/TransactionLines', {
        '$filter': "JournalCode eq '26' and FinancialYear eq 2026",
        '$select': 'ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AccountCode,GLAccountCode,JournalCode,YourRef,FinancialYear,FinancialPeriod',
        '$orderby': 'EntryNumber,LineNumber'})


async def receivables():
    return await read_all('read/financial/ReceivablesList', {
        '$filter': "AccountId eq guid'" + DEBTOR + "'",
        '$select':'AccountId,AccountCode,Amount,AmountInTransit,CurrencyCode,Description,EntryNumber,InvoiceDate,InvoiceNumber,JournalCode,YourRef'})


def compare(rows, actual):
    wanted = {r['trx']:r for r in rows}
    hits = {k:[] for k in wanted}
    occupied = set()
    for tx in actual:
        if int(tx.get('EntryNumber') or 0) in {r['entry'] for r in rows}:
            occupied.add(int(tx['EntryNumber']))
        for token in re.findall(r'\b[A-Za-z0-9]{8}\b', str(tx.get('Description') or '')):
            if token in wanted:
                hits[token].append(tx)
    verified, errors = [], []
    for trx, found in hits.items():
        if not found:
            continue
        row = wanted[trx]
        bank = [t for t in found if str(t.get('GLAccountCode') or '').strip() == '1316']
        offset = [t for t in found if str(t.get('GLAccountCode') or '').strip() == row['gl']]
        if len(found) != 2 or len(bank) != 1 or len(offset) != 1:
            errors.append({'trx':trx,'reason':'not_one_bank_and_one_offset','lines':len(found)}); continue
        expected = Decimal(row['amount'])
        valid = Decimal(str(bank[0]['AmountDC'])) == expected and Decimal(str(offset[0]['AmountDC'])) == -expected
        valid = valid and str(offset[0].get('AccountCode') or '').strip() == (row['account'] or '')
        for tx in found:
            valid = valid and int(tx['EntryNumber']) == row['entry'] and tx.get('Description') == row['description']
            valid = valid and str(tx.get('JournalCode') or '').strip() == '26'
            valid = valid and int(tx['FinancialYear']) == 2026 and int(tx['FinancialPeriod']) == int(row['date'][5:7])
            raw_date = str(tx.get('Date') or '')
            if raw_date.startswith('/Date('):
                raw_date = datetime.fromtimestamp(int(re.search(r'-?\d+', raw_date).group())/1000, timezone.utc).date().isoformat()
            valid = valid and raw_date[:10] == row['date']
            if row['gl'] == '1100':
                valid = valid and (tx.get('YourRef') or '') == row['ref']
        if valid:
            verified.append(trx)
        else:
            errors.append({'trx':trx,'reason':'field_mismatch','lines':found})
    return dict(matched_transaction_ids=[k for k,v in hits.items() if v], verified_ids=verified,
                occupied_entries=sorted(occupied), errors=errors,
                complete=len(verified)==len(rows) and not errors,
                safe_to_import=not any(hits.values()) and not occupied)


async def preflight():
    rows = parse_batch(payload())
    data = await ledger()
    check = compare(rows, data)
    artifact('ledger_before', data)
    artifact('receivables_before', await receivables())
    update(preflight=check, summary=brief(rows), phase='ready' if check['safe_to_import'] else 'import_verified' if check['complete'] else 'blocked_existing_rows')
    return check


async def reconcile():
    rows = parse_batch(payload())
    data = await ledger()
    result = compare(rows, data)
    artifact('ledger_after', data)
    artifact('receivables_after', await receivables())
    update(reconciliation=result, phase='import_verified' if result['complete'] else 'not_fully_verified')
    return result


async def import_xml():
    check = await preflight()
    if check['complete']:
        update(phase='import_verified', upload_skipped_existing=True)
        return
    if not check['safe_to_import']:
        raise HTTPException(409, 'existing_rows_or_occupied_entries')
    app = application()
    token = await app._access_token()
    claim('xml_upload')
    update(phase='import_requested', financial_write_attempted=True)
    # One write only. Do not use the generic JSON helper's 401 recursion.
    try:
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            response = await client.post(app.BASE_URL + '/docs/XMLUpload.aspx',
                params={'Topic':'GLTransactions','_Division_':str(DIVISION)}, content=payload(),
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'})
        try:
            result_xml=ET.fromstring(response.content)
            result_body=ET.tostring(result_xml,encoding='unicode')[:30000] if result_xml.tag in {'eExact','Messages','Message'} else 'unexpected_document'
        except ET.ParseError:
            result_body='non_xml_response'
        artifact('xml_response', {'status':response.status_code,'body':result_body})
        update(xml_http_status=response.status_code)
    except Exception:
        update(upload_outcome='unknown_reconcile_required')
    result = await reconcile()
    if not result['complete']:
        update(phase='import_requires_review_no_retry')


async def ui_snapshot(page):
    return await page.evaluate('''() => {
        const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
        const business=e=>/^(BankAccount|EntryDate|Notes|Amount|Offset|Status|GLAccountType|List)/.test(e.id||'');
        return {title:document.title,text:document.body.innerText.slice(0,500000),
          controls:Array.from(document.querySelectorAll('input,select,button,a')).filter(e=>visible(e)||(e.tagName==='INPUT'&&business(e))).slice(0,300).map(e=>({
            tag:e.tagName,id:e.id,name:e.name||'',type:e.type||'',text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,180),
            value:(e.tagName==='SELECT'||(e.tagName==='INPUT'&&business(e)))?e.value:undefined,
            checked:e.type==='checkbox'?e.checked:undefined,
            options:e.tagName==='SELECT'?Array.from(e.options).map(o=>({value:o.value,text:o.text})).slice(0,100):undefined})),
          rows:Array.from(document.querySelectorAll('tr')).filter(visible).slice(0,10000).map(e=>({id:e.id,text:e.innerText.slice(0,5000),content:e.textContent.slice(0,5000),
            cells:Array.from(e.children).filter(c=>['TD','TH'].includes(c.tagName)).map(c=>c.innerText),
            titles:Array.from(e.querySelectorAll('[title]')).map(x=>x.title)}))}; }''')


def verify_statements(snapshot, rows):
    controls={c['id']:c for c in snapshot['controls'] if c.get('id')}
    if controls.get('BankAccount',{}).get('value','').strip('{}').lower() != BANK:
        raise HTTPException(409,'wrong_statement_bank')
    if controls.get('Notes',{}).get('value') != JOB:
        raise HTTPException(409,'statement_batch_filter_missing')
    found=[]
    for source in rows:
        hits=[r for r in snapshot['rows'] if len(r['cells'])>=15 and source['trx'] in (r.get('content','')+' '.join(r.get('titles',[])))]
        if len(hits)!=1:
            raise HTTPException(409,'statement_reference_not_unique_'+source['trx'])
        hit=hits[0]; text=hit.get('content','')+' '.join(hit.get('titles',[]))
        if source['description'] not in text or JOB not in text or not hit['cells'][7].startswith(source['gl']+' -'):
            raise HTTPException(409,'statement_note_or_offset_mismatch_'+source['trx'])
        found.append(source['trx'])
    return {'verified_ids':found,'complete':len(found)==845}


async def browser_snapshot(automatic=False):
    if automatic and not state().get('reconciliation',{}).get('complete'):
        raise HTTPException(409,'ledger_not_verified')
    from operations.exact_browser import Credentials, authenticate, protect_requests, trusted, PASSWORD, USERNAME, visible
    from playwright.async_api import async_playwright
    directory = str(Path(tempfile.gettempdir()) / 'jnp-paragon-browser-1.63.0')
    child_env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LD_LIBRARY_PATH','VIRTUAL_ENV'}}
    child_env['PLAYWRIGHT_BROWSERS_PATH'] = directory
    installer = await asyncio.create_subprocess_exec(sys.executable,'-m','playwright','install','chromium','--only-shell',env=child_env,
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    if await asyncio.wait_for(installer.wait(),150) != 0:
        raise HTTPException(409,'browser_install_failed')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = directory
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True,env=child_env)
        page=None
        stage='authenticate'
        try:
            context = await browser.new_context(accept_downloads=False,service_workers='block')
            await protect_requests(context)
            page = await context.new_page()
            page.set_default_timeout(15000)
            login = await authenticate(page,Credentials.from_env(os.environ))
            update(login=login)
            if not login['administration_verified']:
                raise HTTPException(409,'exact_login_not_verified')
            stage='open_statements'; update(browser_stage=stage)
            url = 'https://start.exactonline.nl/docs/CflStatementsToBeCompleted.aspx?' + urlencode({'_Division_':DIVISION,'BankAccount':'{'+BANK+'}'})
            await page.goto(url,wait_until='domcontentloaded',timeout=45000)
            if not trusted(page.url) or 'CflStatementsToBeCompleted.aspx' not in page.url:
                raise HTTPException(409,'unexpected_statement_page')
            if await visible(page.locator(PASSWORD + ', ' + USERNAME)):
                raise HTTPException(409,'statement_session_expired')
            # Both statuses are deliberately included for a complete inspection.
            # These controls were observed on this account's live statement page.
            stage='set_filters'; update(browser_stage=stage)
            await page.locator('#Status1').check()
            await page.locator('#Status2').check()
            await page.locator('#EntryDate_Selection').select_option('1100')
            await page.locator('#Notes').fill(JOB)
            for field in ['#GLAccountTypeCheckBoxList1','#GLAccountTypeCheckBoxList2','#GLAccountTypeCheckBoxList3']:
                await page.locator(field).check()
            stage='apply_filter'; update(browser_stage=stage)
            async with page.expect_navigation(wait_until='load',timeout=60000):
                await page.locator('#Filter_btnApply').click()
            stage='page_size'; update(browser_stage=stage)
            if await page.locator('#List_ps-select').input_value() != '9999':
                async with page.expect_navigation(wait_until='load',timeout=60000):
                    await page.locator('#List_ps-select').select_option('9999')
            with suppress(Exception):
                await page.wait_for_load_state('networkidle',timeout=10000)
            stage='read_statements'; update(browser_stage=stage)
            snapshot=await ui_snapshot(page)
            artifact('ui',snapshot)
            update(phase='statement_inspected')
            if automatic:
                verified=verify_statements(snapshot,parse_batch(payload()))
                artifact('automatic_before',snapshot)
                update(statement_verification=verified)
                claim('automatic_click')
                update(phase='automatic_requested',automatic_attempted=True)
                stage='click_automatic'; update(browser_stage=stage)
                await page.locator('#btnAutomatic').click()
                with suppress(Exception):
                    await page.wait_for_load_state('networkidle',timeout=45000)
                snapshots=[]
                for current in context.pages:
                    if trusted(current.url):
                        snapshots.append({'path':urlsplit(current.url).path,'snapshot':await ui_snapshot(current)})
                artifact('automatic_after',snapshots)
                update(phase='automatic_clicked_inspect_result')
        except Exception as exc:
            update(browser_stage=stage)
            # A business-page snapshot is safe; authentication pages are never
            # captured. Keep diagnostics private and do not repeat UI actions.
            if page is not None and not page.is_closed() and urlsplit(page.url).path.endswith('CflStatementsToBeCompleted.aspx'):
                with suppress(Exception):
                    await page.wait_for_load_state('domcontentloaded',timeout=10000)
                    artifact('ui',await ui_snapshot(page))
            if stage not in {'authenticate'}:
                update(browser_error=(type(exc).__name__+': '+str(exc).split('Call log:')[0])[:500])
            raise
        finally:
            await browser.close()


async def run(action):
    conn = database()
    locked = False
    try:
        locked = conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]
        if not locked:
            raise HTTPException(409,'job_already_running')
        update(running=True, action=action, last_error=None)
        operations={'preflight':preflight,'import':import_xml,'reconcile':reconcile,'inspect':browser_snapshot,
                    'automatic':lambda:browser_snapshot(automatic=True)}
        await operations[action]()
    except asyncio.CancelledError:
        update(phase='interrupted_reconcile_before_write',last_error='worker_cancelled')
        raise
    except Exception as exc:
        error = str(exc.detail)[:1500] if isinstance(exc,HTTPException) else type(exc).__name__
        update(phase='stopped',last_error=error,last_error_status=exc.status_code if isinstance(exc,HTTPException) else None)
    finally:
        if locked:
            update(running=False)
            conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
        conn.close()


async def shutdown():
    for task in list(TASKS):
        task.cancel()
    for task in list(TASKS):
        with suppress(asyncio.CancelledError):
            await task


@router.post('/prepare')
async def prepare(request:Request):
    authorize(request)
    blob = await request.body()
    try:
        rows = parse_batch(blob)
    except (ValueError,ET.ParseError,AttributeError,TypeError) as exc:
        raise HTTPException(400,'invalid_fixed_batch') from None
    init_db()
    data = dict(phase='prepared',summary=brief(rows),running=False,financial_write_attempted=False)
    with database() as conn:
        conn.execute('INSERT INTO fibonatix_import_jobs(job,sha,xml,data) VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',
                     (JOB,SHA,blob,json.dumps(data)))
    return state()


@router.get('/status')
async def status(request:Request):
    authorize(request)
    return state()


@router.get('/artifact/{name}')
async def get_artifact(name:str,request:Request):
    authorize(request)
    if name not in ARTIFACTS:
        raise HTTPException(404,'Not found')
    with database() as conn:
        row=conn.execute('SELECT data FROM fibonatix_import_artifacts WHERE job=%s AND name=%s',(JOB,name)).fetchone()
    if row is None:
        raise HTTPException(404,'artifact_not_ready')
    return row[0]


@router.post('/{action}')
async def start(action:str,request:Request):
    authorize(request)
    if action not in {'preflight','import','reconcile','inspect','automatic'}:
        raise HTTPException(404,'Not found')
    state()
    if any(not task.done() for task in TASKS):
        raise HTTPException(409,'job_already_running')
    task=asyncio.create_task(run(action))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return {'accepted':True,'action':action,'job':JOB}
