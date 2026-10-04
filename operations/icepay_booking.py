"""Prepare the fixed verified ICEPAY receipt batch; no financial writes."""
import asyncio
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET

from operations.icepay_fetch_probe import environments, stop_child

PREPARE='icepay-booking-20261001-03-prepare-v1'
SOURCE='icepay-transactions-20261001-03-v15'
EXPIRES=datetime(2026,10,5,18,tzinfo=timezone.utc)
LOG=logging.getLogger('uvicorn.error')


def claim(app):
    with app._db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_booking_runs (
            task TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            artifacts JSONB NOT NULL DEFAULT '{}'::jsonb, summary JSONB NOT NULL DEFAULT '{}'::jsonb)''')
        return conn.execute('INSERT INTO icepay_booking_runs(task) VALUES(%s) ON CONFLICT DO NOTHING RETURNING task',(PREPARE,)).fetchone() is not None


def read_source(app):
    with app._db_connect() as conn:
        source=conn.execute('SELECT status,artifacts,summary FROM icepay_transaction_tasks WHERE job=%s',(SOURCE,)).fetchone()
        template=conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s',('FIBO-20260922-20261002',)).fetchone()
    if not source or source[0].get('state')!='downloaded' or source[2].get('td_ok_count')!=36 or source[2].get('td_ok_total')!='3161.24':
        raise ValueError('source_not_verified')
    if not template:
        raise ValueError('missing_xml_template')
    return source,bytes(template[0])


def template_schema(blob):
    if hashlib.sha256(blob).hexdigest()!='18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2':
        raise ValueError('changed_xml_template')
    root=ET.fromstring(blob)
    def node(element):
        children=[]; seen_line=False
        for child in element:
            if child.tag=='GLTransactionLine':
                if seen_line:
                    continue
                seen_line=True
            children.append(node(child))
        return {'tag':element.tag,'attributes':sorted(element.attrib),'text':bool((element.text or '').strip()),'children':children}
    first=root.find('./GLTransactions/GLTransaction')
    if first is None:
        raise ValueError('unexpected_xml_template')
    return {'root':root.tag,'root_attributes':sorted(root.attrib),'entry':node(first)}


def save(app, artifacts, summary):
    with app._db_connect() as conn:
        conn.execute('UPDATE icepay_booking_runs SET artifacts=%s::jsonb,summary=%s::jsonb WHERE task=%s',
            (json.dumps(artifacts),json.dumps(summary),PREPARE))


async def run():
    if os.environ.get('ICEPAY_TRANSACTION_TASK_ID')!=PREPARE or datetime.now(timezone.utc)>=EXPIRES:
        return
    from app import main
    if not await asyncio.to_thread(claim,main):
        return
    child=None; artifacts={}; summary={'financial_writes':0,'state':'preparing'}
    LOG.warning('ICEPAY_BOOKING %s',json.dumps({'task':PREPARE,**summary}))
    try:
        source,template=await asyncio.to_thread(read_source,main)
        artifacts['source_job']=SOURCE
        summary['xml_template_schema']=template_schema(template)
        from operations.icepay_transactions import parse_payments, verify_csv_timezone
        data=source[1]
        content=base64.b64decode(data['payments_csv'],validate=True)
        if hashlib.sha256(content).hexdigest()!='24191f5257cd1b2837aaf7c75fb1810f2b509bc346f42ddbf1de674918e835e8':
            raise ValueError('source_hash_changed')
        ids=data['proof']['ui_payment_ids']
        if not verify_csv_timezone(content,data['table_evidence'],ids)['verified']:
            raise ValueError('timezone_not_verified')
        rows,receipt_summary=parse_payments(content,38,ids,utc_to_amsterdam=True)
        if rows!=data['normalized_payments'] or receipt_summary['td_ok_count']!=36 or receipt_summary['td_ok_total']!='3161.24':
            raise ValueError('source_changed')
        artifacts['receipts']=[row for row in rows if row['status']=='OK']
        summary['receipts']=receipt_summary
        from operations.icepay_import import inspect_exact
        artifacts['exact'],summary['exact']=await inspect_exact(rows)
        base,child_env=environments(os.environ)
        child=await asyncio.create_subprocess_exec(sys.executable,'-m','playwright','install','chromium','--only-shell',
            env=base,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
        if await asyncio.wait_for(child.wait(),150)!=0:
            raise ValueError('browser_install')
        child=await asyncio.create_subprocess_exec(sys.executable,'-m','operations.icepay_finance','--worker',
            env=child_env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
        raw,_=await asyncio.wait_for(child.communicate(),200)
        if child.returncode!=0 or len(raw)>1000000:
            raise ValueError('finance_read_failed')
        from operations.icepay_finance import summarize
        artifacts['finance']=json.loads(raw); summary['finance']=summarize(artifacts['finance'])
        summary['state']='prepared'
    except Exception as error:
        from operations.icepay_transactions import failure_location
        summary.update(state='blocked',failure=failure_location(error))
    finally:
        await stop_child(child)
        await asyncio.to_thread(save,main,artifacts,summary)
        LOG.warning('ICEPAY_BOOKING %s',json.dumps({'task':PREPARE,**summary},sort_keys=True))
