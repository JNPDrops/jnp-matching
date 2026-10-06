"""Native Automatically for one proven BACS receipt, owned by maintenance.

Explicitly queued after the day gates; no automatic discovery or replay. The
existing continuous maintenance loop owns execution and retains its frequency.
"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode, urlsplit
from operations import nightly_batches as n, worker_write_fence as fence, task_drain
from operations.fibonatix_daily import exact_date
from operations.fibonatix_daily_source import require

BANK = '19ce0ede-0e7b-4f36-90f0-717555953819'
LOCK = 397775213600


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_bank_automatic_runs (
        division integer NOT NULL, processing_date date NOT NULL,
        state text NOT NULL, data jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(division,processing_date))''')


def validate_target(target):
    require(target['status']=='bacs_match_candidate' and target['journal_code']=='20'
            and target['account_code']=='109372' and target['currency']=='EUR'
            and target['payment_method']=='bacs' and target['order_status']=='completed'
            and target['reference']==target['invoice_reference']
            and Decimal(target['amount'])>0 and target.get('bank_line_id')
            and target.get('bank_entry_id') and target.get('invoice_entry')
            and target.get('description'), 'unproven_bacs_target')


def checked(snapshot, target):
    from operations.fibonatix_import import statement_rows
    from operations.strict_order_matching import euro
    controls={x['id']:x for x in snapshot['controls'] if x.get('id')}
    day=exact_date(target['bank_date'])
    require(controls.get('BankAccount',{}).get('value','').strip('{}').lower()==BANK,'wrong_bank')
    require(controls.get('Notes',{}).get('value')==target['description'],'wrong_notes')
    require(controls.get('Status1',{}).get('checked') is True and controls.get('Status2',{}).get('checked') is False,'wrong_status')
    require(controls.get('EntryDate_Selection',{}).get('value')=='1000','missing_date_scope')
    for field in ('EntryDate_From','EntryDate_To'):
        require(datetime.strptime(controls[field]['value'].strip(),'%d-%m-%Y').date().isoformat()==day,'wrong_date')
    rows=statement_rows(snapshot)
    require(len(rows)<=1,'ambiguous_bank_selection')
    for row in rows:
        cells=row['cells']
        require(row['note']==target['description'] and cells[1]==datetime.fromisoformat(day).strftime('%d-%m-%Y')
                and cells[3]=='EUR' and cells[10]=='EUR' and cells[7].startswith('1100 -')
                and cells[8].startswith('109372 -')
                and euro(cells[4])-euro(cells[5])==Decimal(target['amount']), 'bank_identity_changed')
    return rows


async def selection(page,target):
    from operations.fibonatix_import import ui_snapshot
    url='https://start.exactonline.nl/docs/CflStatementsToBeCompleted.aspx?'+urlencode({'_Division_':n.DIVISION,'BankAccount':'{'+BANK+'}'})
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    require(urlsplit(page.url).hostname=='start.exactonline.nl' and urlsplit(page.url).path.endswith('/CflStatementsToBeCompleted.aspx'),'wrong_page')
    async with page.expect_navigation(wait_until='load',timeout=60000):await page.get_by_text('Reset',exact=True).click()
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    await page.locator('#Status1').check();await page.locator('#Status2').uncheck()
    await page.locator('#EntryDate_Selection').select_option('1000')
    for field in ('#EntryDate_From','#EntryDate_To'):
        await page.locator(field).fill(datetime.fromisoformat(exact_date(target['bank_date'])).strftime('%d-%m-%Y'))
    await page.locator('#Notes').fill(target['description'])
    for field in ('#GLAccountTypeCheckBoxList1','#GLAccountTypeCheckBoxList2','#GLAccountTypeCheckBoxList3'):await page.locator(field).check()
    async with page.expect_navigation(wait_until='load',timeout=60000):await page.locator('#Filter_btnApply').click()
    if await page.locator('#List_ps-select').count() and await page.locator('#List_ps-select').input_value()!='9999':
        async with page.expect_navigation(wait_until='load',timeout=60000):await page.locator('#List_ps-select').select_option('9999')
    snap=await ui_snapshot(page)
    return snap,checked(snap,target)


def save(app,day,state,data):
    with app._db_connect() as conn:
        conn.execute('UPDATE jnp_bank_automatic_runs SET state=%s,data=%s::jsonb,updated_at=now() WHERE division=%s AND processing_date=%s',(state,json.dumps(data),n.DIVISION,day))


@fence.owned_operation('maintenance')
async def click(app,day,page,data):
    require(not task_drain.requested(),'worker_draining')
    await asyncio.to_thread(save,app,day,'click_requested',data)
    async def native():
        async with page.expect_navigation(wait_until='load',timeout=90000):await page.locator('#btnAutomatic').click()
    await fence.browser_save(app,'maintenance',native)
    after,remaining=await selection(page,data['target'])
    data.update(after=after,open_before=1,remaining_open=len(remaining),matched=1-len(remaining),automatic_attempted=True)
    await asyncio.to_thread(save,app,day,'completed',data)
    if not remaining:
        with app._db_connect() as conn:
            conn.execute("UPDATE jnp_suspense_review SET details=details||%s::jsonb,observed_at=now() WHERE bank_line_id=%s",
                         (json.dumps({'status':'resolved','match_executed':True,'execution_label':'Exact Automatically uitgevoerd; bankregel niet meer open','next_action':'','automatic_processing_date':day.isoformat()}),data['target']['bank_line_id']))


async def run_queued(app):
    owner=fence.current_owner()
    if owner is None or owner.role!='maintenance' or task_drain.requested():return
    with app._db_connect() as conn:
        if not conn.execute("SELECT to_regclass('jnp_bank_automatic_runs')").fetchone()[0]:return
        if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]:return
        try:
            if conn.execute("SELECT 1 FROM jnp_bank_automatic_runs WHERE division=%s AND state IN ('inspecting','click_requested','uncertain') LIMIT 1",(n.DIVISION,)).fetchone():return
            row=conn.execute("SELECT processing_date,data FROM jnp_bank_automatic_runs WHERE division=%s AND state='queued' ORDER BY processing_date LIMIT 1",(n.DIVISION,)).fetchone()
            if not row:return
            day,data=row
            states=dict(conn.execute('SELECT stage,state FROM jnp_nightly_stages WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchall())
            if not all(states.get(s) in n.TERMINAL for s in n.STAGES[:6]):return
            save(app,day,'inspecting',data)
            try:
                require(app.DIVISION==n.DIVISION,'wrong_division')
                target=data['target'];validate_target(target)
                require(exact_date(target['bank_date'])<=day.isoformat(),'bank_date_outside_day')
                current=conn.execute('SELECT details FROM jnp_suspense_review WHERE bank_line_id=%s',(target['bank_line_id'],)).fetchone()
                require(current is not None,'target_no_longer_open')
                validate_target(current[0])
                fields=('bank_line_id','bank_entry_id','invoice_entry','reference','invoice_reference','amount','currency','account_code','bank_date','description','woo_order_id','payment_method','order_status')
                require(all(current[0].get(k)==target.get(k) for k in fields),'target_changed')
                require((datetime.now(timezone.utc)-datetime.fromisoformat(current[0]['observed_at'])).total_seconds()<3600,'stale_order_evidence')
                from operations.strict_order_matching import session
                async with session() as (context,page):
                    before,rows=await selection(page,target)
                    require(len(rows)==1,'target_not_uniquely_open')
                    data['before']=before
                    await click(app,day,page,data)
            except Exception as exc:
                prior=conn.execute('SELECT state FROM jnp_bank_automatic_runs WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchone()[0]
                data['error_type']=type(exc).__name__
                save(app,day,'uncertain' if prior in ('click_requested','completed') else 'blocked',data)
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
