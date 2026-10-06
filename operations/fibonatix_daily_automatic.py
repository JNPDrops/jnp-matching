"""Scoped native Exact Automatically, owned by the existing Fibonatix worker."""
import asyncio
import base64
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode, urlsplit
from operations import nightly_batches as n, worker_write_fence as fence, task_drain
from operations.fibonatix_daily_source import require

BANK='63305e6f-827d-4884-9df1-6ebbb5858a76'
NOTES='Fibonatix TD'


def refunded_ids(allowed, source_rows):
    """A confirmed refund on the same internal order excludes its receipt.

    Exclusion does not post or match the refund. Ambiguous/partial refunds stay
    in review as well; amount equality alone never links different orders.
    """
    orders={int(r['Brand TRX ID']) for r in source_rows
            if r['Type']=='RF' and r['Status(approved/declined)']=='Approved'
            and r['Status Code']=='20000' and r['Currency']=='EUR'
            and str(r['Brand TRX ID']).isdigit() and Decimal(r['Amount'])>0}
    return {pid for pid,row in allowed.items() if int(row['woo_id']) in orders}


def refund_selection_start(eligible, excluded):
    first=min(date.fromisoformat(r['date']) for r in eligible)
    require(all(date.fromisoformat(r['date'])<first for r in excluded),'refund_requires_narrower_selection')
    return first


def amount_groups(receipts, reviews):
    blocked={Decimal(r['amount']) for r in reviews}
    eligible=[r for r in receipts if r not in reviews]
    require(not any(Decimal(r['amount']) in blocked for r in eligible),'shared_review_amount_requires_individual_scope')
    if not eligible:return []
    low=Decimal('.01');high=max(Decimal(r['amount']) for r in eligible);groups=[]
    for value in sorted(blocked|{high+Decimal('.01')}):
        upper=min(value-Decimal('.01'),high)
        rows=[r for r in eligible if low<=Decimal(r['amount'])<=upper]
        if rows:groups.append(((str(low),str(upper)),rows))
        low=value+Decimal('.01')
    require(sum(len(rows) for _,rows in groups)==len(eligible),'incomplete_amount_partition')
    return groups


def checked(snapshot, allowed, first, last, amount_bounds=None):
    from operations.fibonatix_import import statement_rows
    from operations.strict_order_matching import euro
    controls={c['id']:c for c in snapshot['controls'] if c.get('id')}
    require(controls.get('BankAccount',{}).get('value','').strip('{}').lower()==BANK,'wrong_bank')
    require(controls.get('Notes',{}).get('value')==NOTES,'missing_note_filter')
    require(controls.get('Status1',{}).get('checked') is True and controls.get('Status2',{}).get('checked') is False,'wrong_status_filter')
    require(controls.get('EntryDate_Selection',{}).get('value')=='1000','missing_date_range')
    for field,day in [('EntryDate_From',first),('EntryDate_To',last)]:
        require(datetime.strptime(controls[field]['value'].strip(),'%d-%m-%Y').date()==day,'wrong_date_range')
    if amount_bounds:
        for field,value in zip(('Amount_From','Amount_To'),amount_bounds):
            require(euro(controls[field]['value'])==Decimal(value),'wrong_amount_filter')
    result=[]
    for row in statement_rows(snapshot):
        cells=row['cells'];match=re.search(r'Fibonatix (TD[0-9]+) \| Woo ([0-9]+) \| Betaling ([A-Za-z0-9]{6,32})\b',row['note'])
        require(match is not None,'unrecognized_receipt')
        ref,woo,pid=match.groups();expected=allowed.get(pid)
        require(expected is not None,'receipt_not_authorized')
        booked=datetime.strptime(cells[1],'%d-%m-%Y').date()
        amount=euro(cells[4])-euro(cells[5])
        require(cells[3]=='EUR' and cells[10]=='EUR' and cells[7].startswith('1100 -') and cells[8].startswith('100100 -'),'wrong_receipt_account')
        require(first<=booked<=last and booked.isoformat()==expected['date'] and ref==expected['ref'] and int(woo)==expected['woo_id'] and amount>0 and amount==Decimal(expected['amount']),'receipt_evidence_mismatch')
        if amount_bounds:require(Decimal(amount_bounds[0])<=amount<=Decimal(amount_bounds[1]),'receipt_outside_amount_filter')
        result.append({'payment_id':pid,'ref':ref,'amount':str(amount),'date':booked.isoformat()})
    require(len({r['payment_id'] for r in result})==len(result),'duplicate_receipt')
    return result


async def selection(page,allowed,first,last,amount_bounds=None):
    from operations.fibonatix_import import ui_snapshot
    url='https://start.exactonline.nl/docs/CflStatementsToBeCompleted.aspx?'+urlencode({'_Division_':n.DIVISION,'BankAccount':'{'+BANK+'}'})
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    require(urlsplit(page.url).hostname=='start.exactonline.nl' and urlsplit(page.url).path.endswith('/CflStatementsToBeCompleted.aspx'),'wrong_page')
    async with page.expect_navigation(wait_until='load',timeout=60000):await page.get_by_text('Reset',exact=True).click()
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    await page.locator('#Status1').check();await page.locator('#Status2').uncheck()
    await page.locator('#EntryDate_Selection').select_option('1000')
    await page.locator('#EntryDate_From').fill(first.strftime('%d-%m-%Y'))
    await page.locator('#EntryDate_To').fill(last.strftime('%d-%m-%Y'))
    await page.locator('#Notes').fill(NOTES)
    if amount_bounds:
        for field,value in zip(('#Amount_From','#Amount_To'),amount_bounds):
            await page.locator(field).fill(value.replace('.',','))
    for field in ('#GLAccountTypeCheckBoxList1','#GLAccountTypeCheckBoxList2','#GLAccountTypeCheckBoxList3'):await page.locator(field).check()
    async with page.expect_navigation(wait_until='load',timeout=60000):await page.locator('#Filter_btnApply').click()
    if await page.locator('#List_ps-select').count() and await page.locator('#List_ps-select').input_value()!='9999':
        async with page.expect_navigation(wait_until='load',timeout=60000):await page.locator('#List_ps-select').select_option('9999')
    snapshot=await ui_snapshot(page)
    return snapshot,checked(snapshot,allowed,first,last,amount_bounds)


def persist(app,key,state,data):
    with app._db_connect() as conn:
        conn.execute('UPDATE jnp_fibonatix_automatic_runs SET state=%s,data=%s::jsonb,updated_at=now() WHERE task_key=%s',(state,json.dumps(data),key))


@fence.owned_operation('fibonatix')
async def click(app,key,page,allowed,first,last,data):
    require(not task_drain.requested(),'worker_draining')
    await asyncio.to_thread(persist,app,key,'click_requested',data)
    async def native():
        async with page.expect_navigation(wait_until='load',timeout=90000):await page.locator('#btnAutomatic').click()
    await fence.browser_save(app,'fibonatix',native)
    after,remaining=await selection(page,allowed,first,last)
    require({r['payment_id'] for r in remaining}<={r['payment_id'] for r in data['before_receipts']},'unexpected_receipt_after_action')
    all_remaining=remaining+data.get('excluded_refunds',[])
    data.update(after=after,remaining=all_remaining,remaining_open=len(all_remaining),matched=len(data['before_receipts'])-len(remaining),automatic_attempted=True)
    await asyncio.to_thread(persist,app,key,'completed',data)
    return {'state':'completed','open_before':len(data['before_receipts'])+len(data.get('excluded_refunds',[])),'remaining_open':len(all_remaining),'matched':data['matched']}


@fence.owned_operation('fibonatix')
async def click_group(app,key,page,allowed,first,last,data,batch):
    require(not task_drain.requested(),'worker_draining')
    batch['state']='click_requested'
    await asyncio.to_thread(persist,app,key,'click_requested',data)
    async def native():
        async with page.expect_navigation(wait_until='load',timeout=90000):await page.locator('#btnAutomatic').click()
    await fence.browser_save(app,'fibonatix',native)
    after,remaining=await selection(page,allowed,first,last,batch['bounds'])
    require({r['payment_id'] for r in remaining}<={r['payment_id'] for r in batch['receipts']},'unexpected_receipt_after_action')
    batch.update(state='completed',after=after,remaining=remaining,matched=len(batch['receipts'])-len(remaining))
    await asyncio.to_thread(persist,app,key,'processing',data)


async def run(app,job):
    require(job['action']=='daily_automatically' and set(job['params'])=={'date'},'invalid_automatic_job')
    day=date.fromisoformat(job['params']['date']);key=n.identity(day,'fibonatix')+':automatically'
    require(job['task_key']==n.identity(day,'fibonatix') and app.DIVISION==n.DIVISION,'wrong_scope')
    require(fence.current_owner() is not None and fence.current_owner().role=='fibonatix','assigned_worker_required')
    with app._db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_fibonatix_automatic_runs(task_key text PRIMARY KEY,state text NOT NULL,data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now())''')
        prior=conn.execute('SELECT state,data FROM jnp_fibonatix_automatic_runs WHERE task_key=%s',(key,)).fetchone()
        if prior:
            require(prior[0]=='completed','previous_automatic_requires_review')
            return {'state':'completed','already_completed':True}
        imported=conn.execute('SELECT state,data FROM jnp_fibonatix_daily_imports WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchone()
        require(imported and imported[0]=='verified','daily_import_unfinished')
        states=dict(conn.execute('SELECT stage,state FROM jnp_nightly_stages WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchall())
        require(all(states.get(s) in n.TERMINAL for s in n.STAGES[:6]),'prior_day_work_unfinished')
        allowed={r['payment_id']:r for r in imported[1]['candidates']}
        # The already verified catch-up batch is authorized, never reimported.
        past=conn.execute('SELECT data FROM fibonatix_import_artifacts WHERE job=%s AND name=%s',('FIBO-20261003-05-AMSTERDAM','apply')).fetchone()
        require(past and past[0]['summary']['state']=='import_verified','catchup_import_not_verified')
        for row in past[0]['artifacts']['manifest']:
            pid=row.get('payment_id') or row['trx']
            require(pid not in allowed,'overlapping_source_ids')
            allowed[pid]=row
        first=min(date.fromisoformat(r['date']) for r in allowed.values())
        require(first<=day,'invalid_date_scope')
        src=conn.execute('SELECT result FROM paragon_login_probes WHERE probe_id=%s',(n.identity(day,'fibonatix')+':source',)).fetchone()[0]
        raw=base64.b64decode(src['source_csv'],validate=True)
        require(hashlib.sha256(raw).hexdigest()==imported[1]['source_sha256'],'refund_source_changed')
        from operations.fibonatix_daily_source import read_source
        source_rows,_=read_source(raw,day,utc_ui_proof=src['utc_ui_proof'])
        excluded_ids=refunded_ids(allowed,source_rows)
        review_rows=conn.execute("SELECT details->>'reference',details->>'status' FROM jnp_suspense_review WHERE details->>'journal_code'='26'").fetchall()
        ready_refs={ref for ref,status in review_rows if status=='psp_match_candidate'}
        amount_review_refs={ref for ref,status in review_rows if status=='amount_review'}
        conn.execute("INSERT INTO jnp_fibonatix_automatic_runs(task_key,state,data) VALUES(%s,'inspecting','{}'::jsonb)",(key,))
    from operations.strict_order_matching import session
    async with session() as (context,page):
        before,receipts=await selection(page,allowed,first,day)
        data={'before':before,'before_receipts':receipts,'first':first.isoformat(),'last':day.isoformat(),'policy':'Exact Automatically only'}
        excluded=[r for r in receipts if r['payment_id'] in excluded_ids]
        if excluded:
            eligible=[r for r in receipts if r['payment_id'] not in excluded_ids]
            data.update(before_refund_exclusion=before,excluded_refunds=excluded)
            await asyncio.to_thread(persist,app,key,'inspecting',data)
            if not eligible:
                data.update(before_receipts=[],remaining=excluded,remaining_open=len(excluded),matched=0,automatic_attempted=False)
                await asyncio.to_thread(persist,app,key,'completed',data)
                return {'state':'completed','open_before':len(excluded),'remaining_open':len(excluded),'matched':0}
            first=refund_selection_start(eligible,excluded)
            before,receipts=await selection(page,allowed,first,day)
            require({r['payment_id'] for r in receipts}=={r['payment_id'] for r in eligible},'refund_exclusion_selection_changed')
            data.update(before=before,before_receipts=receipts,first=first.isoformat())
        if not receipts:
            data.update(remaining=[],remaining_open=0,matched=0,automatic_attempted=False)
            await asyncio.to_thread(persist,app,key,'completed',data)
            return {'state':'completed','open_before':0,'remaining_open':0,'matched':0}
        require(all(r['ref'] in ready_refs|amount_review_refs for r in receipts),'receipt_not_ready_for_native')
        reviews=[r for r in receipts if r['ref'] in amount_review_refs]
        if reviews:
            groups=amount_groups(receipts,reviews)
            data.update(excluded_amounts=reviews,batches=[],before_receipts=[r for r in receipts if r not in reviews])
            await asyncio.to_thread(persist,app,key,'inspecting',data)
            for bounds,expected in groups:
                before_group,selected=await selection(page,allowed,first,day,bounds)
                require({r['payment_id'] for r in selected}=={r['payment_id'] for r in expected},'amount_selection_changed')
                batch={'bounds':bounds,'before':before_group,'receipts':selected}
                data['batches'].append(batch)
                await click_group(app,key,page,allowed,first,day,data,batch)
            remaining=reviews+data.get('excluded_refunds',[])+[r for batch in data['batches'] for r in batch['remaining']]
            data.update(remaining=remaining,remaining_open=len(remaining),matched=sum(b['matched'] for b in data['batches']),automatic_attempted=bool(groups))
            await asyncio.to_thread(persist,app,key,'completed',data)
            return {'state':'completed','open_before':len(receipts)+len(data.get('excluded_refunds',[])),'remaining_open':len(remaining),'matched':data['matched']}
        return await click(app,key,page,allowed,first,day,data)
