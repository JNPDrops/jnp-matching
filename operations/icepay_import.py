"""Read-only Exact preflight for the ICEPAY 1–3 October source.

No upload, match, repair, arbitrary endpoint or retry of a financial write.
"""
import asyncio
from decimal import Decimal
import re
from urllib.parse import urlsplit

from operations.icepay_journal_task import BASE, DIVISION, JOURNAL_ID, LEDGER_ID, UNALLOCATED_ID

RESOURCES = {'financial/Journals','financial/GLAccounts','crm/Accounts',
             'financialtransaction/TransactionLines'}


class PreflightStopped(Exception):
    pass


class Reader:
    def __init__(self, app):
        if app.DIVISION!=DIVISION or app.BASE_URL!=BASE:
            raise PreflightStopped('wrong_administration')
        self.app,self.calls = app,0

    async def rows(self, resource, params):
        self.resource=resource
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        url=f'{BASE}/api/v1/{DIVISION}/{resource}'
        seen,result=set(),[]
        while url:
            parsed=urlsplit(url)
            if (resource not in RESOURCES or parsed.scheme!='https' or parsed.netloc!='start.exactonline.nl'
                or parsed.path!=f'/api/v1/{DIVISION}/{resource}' or parsed.fragment
                or url in seen or self.calls>=40):
                raise PreflightStopped('read_budget_or_origin')
            seen.add(url); self.calls+=1
            token=await self.app._access_token()
            async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
                from operations.worker_coordination import budgeted_http
                response=await budgeted_http(self.app, 'icepay', 'GET',
                    lambda: client.get(url,params=params,headers={'Authorization':'Bearer '+token,'Accept':'application/json'}),
                    priority='routine', floor=200)
            if response.status_code!=200:
                raise PreflightStopped('http_'+str(response.status_code))
            raw=response.json(); data=raw.get('d')
            batch=data.get('results') if isinstance(data,dict) else data
            if not isinstance(batch,list):
                raise PreflightStopped('invalid_rows')
            result.extend(batch)
            if len(result)>6000:
                raise PreflightStopped('row_budget')
            url=raw.get('__next') or (data.get('__next') if isinstance(data,dict) else None)
            if url and not batch:
                raise PreflightStopped('empty_page')
            params=None
            await asyncio.sleep(2)
        return result


async def inspect_exact(payments):
    from app import main
    reader=Reader(main)
    evidence={}; summary={'financial_writes':0,'ready':False}
    try:
        evidence['journals']=await reader.rows('financial/Journals',{'$filter':"Code eq '27'"})
        journals=evidence['journals']
        if len(journals)!=1:
            raise PreflightStopped('journal_not_unique')
        journal=journals[0]
        if (journal.get('ID')!=JOURNAL_ID or journal.get('GLAccount')!=LEDGER_ID
            or journal.get('PaymentInTransitAccount')!=UNALLOCATED_ID or journal.get('Type')!=12
            or journal.get('Currency')!='EUR' or journal.get('IsBlocked') is not False):
            raise PreflightStopped('journal_changed')
        evidence['debtors']=await reader.rows('crm/Accounts',{'$filter':"Code eq '109419' or Code eq '"+'109419'.rjust(18)+"'",
            '$select':'ID,Code,Name,IsSales,Status'})
        debtors=[r for r in evidence['debtors'] if str(r.get('Code') or '').strip()=='109419']
        if len(debtors)!=1:
            raise PreflightStopped('debtor_not_unique')
        evidence['ledgers']=await reader.rows('financial/GLAccounts',{'$filter':"Code eq '1100' or Code eq '1317' or Code eq '1360'"})
        ledgers={str(r.get('Code') or '').strip():r for r in evidence['ledgers']}
        if set(ledgers)!={'1100','1317','1360'} or any(r.get('IsBlocked') is not False for r in ledgers.values()):
            raise PreflightStopped('ledger_changed')
        evidence['transactions']=await reader.rows('financialtransaction/TransactionLines',{
            '$filter':"FinancialYear eq 2026 and (JournalCode eq '27' or GLAccount eq guid'"+LEDGER_ID+"' or Account eq guid'"+debtors[0]['ID']+"')",
            '$select':'ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,Account,AccountCode,GLAccount,GLAccountCode,JournalCode,YourRef,PaymentReference,FinancialYear,FinancialPeriod',
            '$orderby':'EntryNumber,LineNumber'})
        source_ids={r['payment_id'] for r in payments}
        source_orders={r['order'] for r in payments if r.get('order')}
        id_hits=[]; order_hits=[]
        for row in evidence['transactions']:
            text=' '.join(str(row.get(k) or '') for k in ('Description','PaymentReference','YourRef'))
            if source_ids.intersection(re.findall(r'\b\d+\b',text)):
                id_hits.append(row['ID'])
            if str(row.get('GLAccountCode') or '').strip()=='1317' and source_orders.intersection(re.findall(r'(?:TD|Order\s*#\s*)(\d+)\b',text,re.I)):
                order_hits.append(row['ID'])
        evidence['duplicate_payment_lines']=id_hits
        evidence['potential_duplicate_order_lines']=order_hits
        bank=[r for r in evidence['transactions'] if str(r.get('GLAccountCode') or '').strip()=='1317']
        journal_lines=[r for r in evidence['transactions'] if str(r.get('JournalCode') or '').strip()=='27']
        summary.update(journal='27',debtor='109419',debtor_id=debtors[0]['ID'],debtor_name=debtors[0].get('Name'),
            bank_account_id=journal.get('BankAccountID'),journal_lines=len(journal_lines),
            journal_entries=sorted(set(r['EntryNumber'] for r in journal_lines)),
            existing_1317_lines=len(bank),existing_1317_net=str(sum((Decimal(str(r['AmountDC'])) for r in bank),Decimal('0.00'))),
            duplicate_payment_lines=len(id_hits),potential_duplicate_order_lines=len(order_hits),
            ready=not id_hits and not order_hits)
    except PreflightStopped as error:
        summary['reason']=str(error)
    except Exception:
        summary['reason']='runtime_error'
    summary['api_calls']=reader.calls
    summary['last_resource']=getattr(reader,'resource','none')
    return evidence,summary
