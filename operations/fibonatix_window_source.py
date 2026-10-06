"""Validate Paragon exports against own Woo identities and observed timezone.

No network, secrets or accounting writes. Order ID is never an order number.
"""
import csv,io,re
from collections import Counter
from datetime import datetime,timedelta
from decimal import Decimal
from operations.icepay_window_import import require

STATUS='Status(approved/declined)'

def parse(raw,orders,*,offset_hours,allowed_dates):
    require(offset_hours in (0,2),'unsupported_timezone_proof')
    rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    require(0<len(rows)<=10000,'invalid_source_size')
    require(len({r['TRX ID'] for r in rows})==len(rows),'duplicate_transaction_id')
    byid={str(o['order_id']):o for o in orders}
    require(len(byid)==len(orders),'duplicate_order_identity')
    accepted=[];excluded=Counter();outside=[]
    for r in rows:
        require(r['Brand']=='TheDrops' and r['Currency']=='EUR','wrong_source_brand_or_currency')
        require(re.fullmatch(r'[A-Za-z0-9]{6,32}',r['TRX ID']),'invalid_transaction_id')
        if r[STATUS]!='Approved':excluded[r[STATUS]]+=1;continue
        require(r['Status Code']=='20000' and r['Type']=='SL','non_receipt_requires_separate_review')
        date=(datetime.strptime(r['Display Time'],'%Y-%m-%d %H:%M:%S')+timedelta(hours=offset_hours)).date().isoformat()
        if date not in allowed_dates:outside.append(r['TRX ID']);continue
        oid=r['Brand TRX ID'];o=byid.get(oid)
        require(oid.isdigit() and r['Order Description']=='Order #'+oid and o is not None,'own_order_not_proven')
        number=str(o['order_number']).lstrip('#')
        require(re.fullmatch(r'\d+',number) and o['payment_method']=='wc_fibonatix' and o['currency']=='EUR','order_scope_changed')
        amount=Decimal(r['Amount'])
        require(amount.is_finite() and amount>0 and amount==amount.quantize(Decimal('.01')),'invalid_receipt_amount')
        accepted.append({'trx':r['TRX ID'],'payment_id':r['TRX ID'],'woo_id':int(oid),'ref':'TD'+number,'date':date,'amount':str(amount.quantize(Decimal('.01'))),'order_status':o['status'],'source_time':r['Display Time'],'fees_known':bool(r.get('MDR Fee')) and bool(r.get('TRX Fee'))})
    require(len({r['ref'] for r in accepted})==len(accepted),'multiple_receipts_same_order_requires_review')
    return accepted,{'source_rows':len(rows),'receipts':len(accepted),'excluded_statuses':dict(excluded),'outside_ids':outside,'total':str(sum((Decimal(r['amount']) for r in accepted),Decimal('0.00'))),'by_date':{day:{'count':sum(r['date']==day for r in accepted),'total':str(sum((Decimal(r['amount']) for r in accepted if r['date']==day),Decimal('0.00')))} for day in sorted(allowed_dates)}}

def timezone_offset(raw,ui_rows):
    """Require at least three distinct own PSP IDs shown in Amsterdam view.

    UI rows are (PSP ID, ISO date/time) extracted from the visible table, not
    guessed from order/payment times. Every correlated row must agree.
    """
    rows={r['TRX ID']:r for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))}
    require(len(ui_rows)>=3 and len({i for i,_ in ui_rows})==len(ui_rows),'insufficient_timezone_evidence')
    offsets=set()
    for trx,ui_time in ui_rows:
        require(trx in rows,'ui_source_id_missing')
        delta=datetime.fromisoformat(ui_time)-datetime.strptime(rows[trx]['Display Time'],'%Y-%m-%d %H:%M:%S')
        offsets.add(delta.total_seconds()/3600)
    require(len(offsets)==1 and next(iter(offsets)) in {0,2},'timezone_evidence_disagrees')
    return int(next(iter(offsets)))
