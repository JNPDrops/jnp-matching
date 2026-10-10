"""Pure validation of a dated Paragon source and its own Woo order evidence.

Import eligibility follows PSP success, not the current webshop order status.
A later refund does not erase the original receipt; refund transactions retain
their separate booking policy (Jasper, 2026-10-07 14:23 Europe/Amsterdam).

No network or bookkeeping writes. A display timezone is not a filter timezone:
Paragon resets its display timezone when applying a date filter. Require an
explicit UTC-to-Amsterdam comparison and filter aware instants, including DST.
"""
import csv
import io
import re
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Europe/Amsterdam')
STATUS = 'Status(approved/declined)'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def bounds(day):
    return (datetime.combine(day, time.min, ZONE).astimezone(timezone.utc),
            datetime.combine(day + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc))


def read_source(raw, day, *, utc_ui_proof):
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    require(0 < len(rows) <= 10000, 'source_size')
    byid = {r['TRX ID']: r for r in rows}
    require(len(byid) == len(rows), 'duplicate_psp_id')
    require(len(utc_ui_proof) >= min(3,len(rows)) and len({p['trx'] for p in utc_ui_proof}) == len(utc_ui_proof), 'timezone_proof_missing')
    for p in utc_ui_proof:
        require(p['trx'] in byid, 'timezone_proof_id_missing')
        stamp = datetime.strptime(byid[p['trx']]['Display Time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        shown = datetime.fromisoformat(p['amsterdam'])
        require(shown.tzinfo is not None and shown.utcoffset() == stamp.astimezone(ZONE).utcoffset() and shown == stamp, 'timezone_proof_disagrees')
    lo, hi = bounds(day)
    selected, outside = [], []
    for r in rows:
        require(re.fullmatch('[A-Za-z0-9]{6,32}', r['TRX ID']), 'invalid_psp_id')
        require(r['Brand'] == 'TheDrops' and r['Currency'] == 'EUR', 'source_scope')
        stamp = datetime.strptime(r['Display Time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        (selected if lo <= stamp < hi else outside).append(r)
    return selected, {'downloaded_rows': len(rows), 'day_rows': len(selected), 'outside_rows': len(outside),
                      'window_start': lo.isoformat(), 'window_end': hi.isoformat(),
                      'statuses': dict(Counter(r[STATUS] for r in selected))}


def prepare(raw, orders, day, *, utc_ui_proof):
    selected, summary = read_source(raw, day, utc_ui_proof=utc_ui_proof)
    byid = {str(o['order_id']): o for o in orders}
    require(len(byid) == len(orders), 'duplicate_order_id')
    candidates, exceptions = [], []
    for r in selected:
        reason = None
        if r[STATUS] != 'Approved':
            reason = 'source_' + r[STATUS].lower()
        elif r['Type'] not in {'SL','RF'}:
            reason = 'separate_policy_' + r['Type']
        elif r['Status Code'] != '20000':
            reason = 'unconfirmed_approval'
        oid = r['Brand TRX ID']
        o = byid.get(oid)
        if reason is None:
            if not oid.isdigit() or r['Order Description'] != 'Order #' + oid or o is None:
                reason = 'own_order_missing'
            elif o['payment_method'] != 'wc_fibonatix' or o['currency'] != 'EUR':
                reason = 'order_scope_mismatch'
        if reason is not None:
            exceptions.append({'payment_id': r['TRX ID'], 'woo_id': oid, 'reason': reason,
                               'order_status': o.get('status') if o else None})
            continue
        amount = Decimal(r['Amount'])
        require(amount.is_finite() and amount > 0 and amount == amount.quantize(Decimal('.01')), 'invalid_amount')
        number = str(o['order_number']).lstrip('#')
        require(re.fullmatch('[0-9]+', number), 'invalid_order_number')
        if r['Type']=='RF' and amount>Decimal(str(o['total'])):
            exceptions.append({'payment_id': r['TRX ID'], 'woo_id': oid, 'reason': 'order_amount_review'})
            continue
        candidates.append({'payment_id': r['TRX ID'], 'woo_id': int(oid), 'ref': 'TD' + number,
                           'date': day.isoformat(), 'amount': str((amount if r['Type']=='SL' else -amount).quantize(Decimal('.01'))),
                           'order_status': o['status'], 'source_status': r[STATUS], 'source_kind': r['Type'],
                           'source_status_code': r['Status Code'],
                           'source_time_utc':datetime.strptime(r['Display Time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).isoformat(),
                           'order_total_refunds':str(o.get('total_refunds','0'))})
    # A second successful PSP ID is another real receipt, even on the same
    # order. Import it; invoice allocation remains a separate native action.
    # Multiple refunds require cumulative original-receipt review first.
    duplicate_orders = {(r['woo_id'],r['source_kind']) for r in candidates if r['source_kind']=='RF' and sum((x['woo_id'],x['source_kind']) == (r['woo_id'],r['source_kind']) for x in candidates) > 1}
    for r in candidates:
        if (r['woo_id'],r['source_kind']) in duplicate_orders:
            exceptions.append({'payment_id': r['payment_id'], 'woo_id': r['woo_id'], 'reason': 'multiple_receipts_same_order'})
    candidates = [r for r in candidates if (r['woo_id'],r['source_kind']) not in duplicate_orders]
    summary.update(candidates=len(candidates), candidate_total=str(sum((Decimal(r['amount']) for r in candidates), Decimal('0.00'))),
                   exceptions=dict(Counter(e['reason'] for e in exceptions)))
    return candidates, exceptions, summary

