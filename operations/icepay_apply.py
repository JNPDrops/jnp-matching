"""One fixed ICEPAY receipt upload, followed by independent Exact readback.

No matching, payout, expense, opening balance, configurable XML or retry of a
write. A durable batch claim precedes the one XMLUpload request.
"""
import asyncio
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET

from operations.icepay_booking import PREPARE, JOB, EXPIRES, build_xml, read_source
from operations.icepay_import import BASE, DIVISION, inspect_exact

APPLY='icepay-booking-20261001-03-import-v1'
RECONCILE='icepay-booking-20261001-03-reconcile-v1'
EXPECTED_SHA='69b2e05e690a3bc4663c9bb3850fb43a98a25fe12909efaa4dbfc1e9fa2deeef'
LOG=logging.getLogger('uvicorn.error')


def claim_run(app, task):
    with app._db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_receipt_import_runs (
            task TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            artifacts JSONB NOT NULL DEFAULT '{}'::jsonb, summary JSONB NOT NULL DEFAULT '{}'::jsonb)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_receipt_import_writes (
            batch TEXT PRIMARY KEY, sha256 TEXT NOT NULL, attempted_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        return conn.execute('INSERT INTO icepay_receipt_import_runs(task) VALUES(%s) ON CONFLICT DO NOTHING RETURNING task',(task,)).fetchone() is not None


def claim_write(app):
    with app._db_connect() as conn:
        return conn.execute('''INSERT INTO icepay_receipt_import_writes(batch,sha256) VALUES(%s,%s)
            ON CONFLICT DO NOTHING RETURNING batch''',(JOB,EXPECTED_SHA)).fetchone() is not None


def save(app, task, artifacts, summary):
    with app._db_connect() as conn:
        conn.execute('UPDATE icepay_receipt_import_runs SET artifacts=%s::jsonb,summary=%s::jsonb WHERE task=%s',
                     (json.dumps(artifacts),json.dumps(summary),task))


def prepared(app):
    with app._db_connect() as conn:
        row=conn.execute('SELECT artifacts,summary FROM icepay_booking_runs WHERE task=%s',(PREPARE,)).fetchone()
    if not row or row[1].get('state')!='prepared' or not row[1].get('exact',{}).get('ready'):
        raise ValueError('not_prepared')
    data=row[0]
    blob=base64.b64decode(data['xml'],validate=True)
    if len(blob)>100000 or hashlib.sha256(blob).hexdigest()!=EXPECTED_SHA:
        raise ValueError('payload_hash_changed')
    source,template=read_source(app)
    if hashlib.sha256(template).hexdigest()!='18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2':
        raise ValueError('template_changed')
    source_lines=[line for line in ET.fromstring(template).findall('./GLTransactions/GLTransaction/GLTransactionLine')
        if line.find('GLAccount').get('code')=='1100' and line.find('Account').get('code')=='100100'
        and Decimal(line.findtext('Amount/Value'))>0]
    patterns={json.dumps({k:v for k,v in line.attrib.items() if k!='line'},sort_keys=True) for line in source_lines}
    current_patterns={json.dumps({k:v for k,v in line.attrib.items() if k!='line'},sort_keys=True)
        for line in ET.fromstring(blob).findall('./GLTransactions/GLTransaction/GLTransactionLine')}
    if len(patterns)!=1 or patterns!=current_patterns:
        raise ValueError('line_attribute_pattern_not_proven')
    source_rows=[r for r in source[1]['normalized_payments'] if r['status']=='OK']
    expected,manifest,_=build_xml(source_rows,template)
    if blob!=expected or manifest!=data['manifest']:
        raise ValueError('payload_source_mismatch')
    lines=[s.strip() for s in data.get('finance',{}).get('details',{}).get('T11930206',{}).get('text','').splitlines() if s.strip()]
    transfer_dates={}
    for label in ('Created Date','Last Update','Submission Date','Transfer Date'):
        indexes=[i for i,s in enumerate(lines) if s==label]
        value=lines[indexes[0]+1] if len(indexes)==1 and indexes[0]+1<len(lines) else ''
        transfer_dates[label]=value if len(value)<80 and (re.fullmatch(r'[\d:/.\-\s]+',value) or
            re.fullmatch(r'[A-Za-z]{3,9} \d{1,2}, 2026(?:,? \d{1,2}:\d\d(?::\d\d)?(?: [AP]M)?)?',value)) else '[not displayed]'
    return blob,manifest,transfer_dates


def exact_date(value):
    value=str(value or '')
    if value.startswith('/Date('):
        return datetime.fromtimestamp(int(re.search(r'-?\d+',value).group())/1000,timezone.utc).date().isoformat()
    return value[:10]


def reconcile(manifest, ledger):
    wanted={r['payment_id']:r for r in manifest}
    hits={key:[] for key in wanted}; verified=[]; errors=[]
    for row in ledger:
        text=' '.join(str(row.get(k) or '') for k in ('Description','PaymentReference'))
        for identifier in set(re.findall(r'\b\d+\b',text)).intersection(wanted):
            hits[identifier].append(row)
    for identifier,found in hits.items():
        if not found:
            continue
        source=wanted[identifier]
        bank=[r for r in found if str(r.get('GLAccountCode') or '').strip()=='1317']
        offset=[r for r in found if str(r.get('GLAccountCode') or '').strip()=='1100']
        if len(found)!=2 or len(bank)!=1 or len(offset)!=1:
            errors.append({'payment_id':identifier,'reason':'not_one_bank_and_offset'}); continue
        expected=Decimal(source['amount'])
        valid=(Decimal(str(bank[0]['AmountDC']))==expected and Decimal(str(offset[0]['AmountDC']))==-expected
            and str(offset[0].get('AccountCode') or '').strip()=='109419'
            and offset[0].get('Account')=='0492e907-6698-4281-98e5-c46e01ae9219'
            and offset[0].get('YourRef')==source['ref'])
        for row in found:
            valid=valid and (str(row.get('JournalCode') or '').strip()=='27'
                and int(row.get('EntryNumber') or 0)==source['entry']
                and int(row.get('FinancialYear') or 0)==2026 and int(row.get('FinancialPeriod') or 0)==10
                and exact_date(row.get('Date'))==source['date'] and row.get('Description')==source['description'])
        if valid:
            verified.append(identifier)
        else:
            errors.append({'payment_id':identifier,'reason':'field_mismatch'})
    occupied=sorted(set(int(r.get('EntryNumber') or 0) for r in ledger).intersection(r['entry'] for r in manifest))
    return {'verified_ids':sorted(verified),'matched_ids':sorted(k for k,v in hits.items() if v),
        'errors':errors,'occupied_entries':occupied,'complete':len(verified)==36 and not errors,
        'safe_to_import':not any(hits.values()) and not occupied}


async def run():
    task=os.environ.get('ICEPAY_TRANSACTION_TASK_ID')
    if task not in {APPLY,RECONCILE} or datetime.now(timezone.utc)>=EXPIRES:
        return
    from app import main
    if main.DIVISION!=DIVISION or main.BASE_URL!=BASE or not re.fullmatch(r'[0-9a-f]{64}',EXPECTED_SHA):
        return
    if not await asyncio.to_thread(claim_run,main,task):
        return
    artifacts={}; summary={'state':'preflight','write_attempted':False,'receipts':36,'total':'3161.24','journal':'27'}
    async def persist():
        await asyncio.to_thread(save,main,task,artifacts,summary)
        LOG.warning('ICEPAY_RECEIPT_IMPORT %s',json.dumps({'task':task,**summary},sort_keys=True))
    try:
        blob,manifest,transfer_dates=await asyncio.to_thread(prepared,main)
        summary['older_transfer_dates']=transfer_dates
        artifacts['manifest']=manifest
        before,preflight=await inspect_exact(manifest)
        artifacts['before']=before; summary['preflight']=preflight
        if 'reason' in preflight:
            raise ValueError('preflight_unavailable')
        check=reconcile(manifest,before['transactions']); artifacts['before_check']=check
        if check['complete']:
            summary.update(state='import_verified',verified_receipts=36,already_present=True)
            return
        if task==RECONCILE:
            summary.update(state='requires_review',verified_receipts=len(check['verified_ids']),errors=len(check['errors']))
            return
        if not preflight.get('ready') or not check['safe_to_import']:
            raise ValueError('existing_or_changed_entries')
        token=await main._access_token()
        if not await asyncio.to_thread(claim_write,main):
            summary['state']='prior_write_claim_reconcile_only'
            return
        summary.update(state='upload_requested',write_attempted=True,xml_sha256=EXPECTED_SHA)
        await persist()
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        try:
            async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
                from operations.worker_coordination import budgeted_http
                response=await budgeted_http(main, 'icepay', 'POST',
                    lambda: client.post(BASE+'/docs/XMLUpload.aspx',
                        params={'Topic':'GLTransactions','_Division_':str(DIVISION)},content=blob,
                        headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'}),
                    priority='critical', floor=200)
            summary['http_status']=response.status_code
            try:
                root=ET.fromstring(response.content)
                body=ET.tostring(root,encoding='unicode')[:30000] if root.tag in {'eExact','Messages','Message'} else 'unexpected_document'
            except ET.ParseError:
                body='non_xml_response'
            artifacts['upload_response']={'status':response.status_code,'body':body}
        except Exception:
            summary['upload_outcome']='unknown_reconcile_required'
        summary['state']='reading_back'; await persist()
        for attempt in range(3):
            after,after_summary=await inspect_exact(manifest)
            artifacts['after']=after; summary['after_read']=after_summary
            if 'reason' not in after_summary:
                result=reconcile(manifest,after['transactions']); artifacts['reconciliation']=result
                summary.update(verified_receipts=len(result['verified_ids']),conflicting_receipts=len(result['errors']))
                if result['complete']:
                    summary.update(state='import_verified',entries=[26270001,26270002,26270003])
                    return
            if attempt<2:
                await asyncio.sleep(15)
        summary['state']='requires_review_no_retry'
    except Exception as error:
        from operations.icepay_transactions import failure_location
        summary.update(state='blocked',failure=failure_location(error))
    finally:
        await persist()
