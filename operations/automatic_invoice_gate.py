"""Fresh own-invoice and debtor proof before native Automatically.

Read-only. The receipt set must come from the verified current native selection.
This is a prerequisite, never a substitute for native matching or its readback.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID


class InvoiceNotReady(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise InvoiceNotReady(code)


def amount(value):
    try:
        result = Decimal(str(value))
        require(result.is_finite() and result == result.quantize(Decimal('.01')), 'invalid_current_amount')
        return result
    except (ValueError, InvalidOperation):
        raise InvoiceNotReady('invalid_current_amount') from None


def references(receipts):
    require(bool(receipts) and len(receipts) <= 500, 'invalid_native_receipt_count')
    refs = [r.get('ref') or r.get('order') for r in receipts]
    require(all(isinstance(r, str) and re.fullmatch(r'TD[0-9]{4,10}', r) for r in refs), 'invalid_source_reference')
    require(len(set(refs)) == len(refs), 'multiple_selected_receipts_for_order')
    ids = [r.get('payment_id') for r in receipts]
    require(all(isinstance(p, str) and p for p in ids) and len(set(ids)) == len(ids), 'duplicate_or_missing_payment_id')
    require(all(amount(r.get('amount')) > 0 for r in receipts), 'not_a_positive_selected_receipt')
    return refs


def assess(receipts, sales, opened, debtor_id):
    refs = references(receipts)
    proof = []
    for receipt, ref in zip(receipts, refs):
        own = [s for s in sales if s.get('YourRef') == ref]
        require(len(own) == 1, 'own_sales_entry_missing_or_ambiguous')
        sale = own[0]
        require(sale.get('Type') == 20 and sale.get('Reversal') is False, 'own_sale_refund_or_reversal_requires_review')
        require(sale.get('Currency') == 'EUR', 'own_sale_currency_not_eur')
        require(sale.get('Customer') == debtor_id, 'own_sale_on_wrong_debtor')
        # Query open items across debtors, not only the desired destination.
        items = [r for r in opened if r.get('YourRef') == ref]
        require(len(items) == 1, 'own_open_item_missing_or_ambiguous')
        item = items[0]
        require(item.get('AccountId') == debtor_id and item.get('EntryNumber') == sale.get('EntryNumber'),
                'own_open_item_identity_mismatch')
        remaining = amount(item.get('Amount'))
        received = amount(receipt['amount'])
        require(remaining > 0, 'own_invoice_already_closed_or_credit')
        # A paid-down invoice needs explicit partial-payment evidence before a
        # further native action; do not silently reuse its original total.
        require(amount(sale.get('AmountFC')) >= remaining, 'own_invoice_remaining_exceeds_original')
        require(abs(received - remaining) <= Decimal('1.00'), 'own_invoice_difference_above_one_euro')
        require(bool(sale.get('EntryID')), 'own_invoice_identity_missing')
        proof.append({'reference': ref, 'payment_id': receipt['payment_id'],
                      'entry_id': sale['EntryID'], 'entry_number': sale['EntryNumber'],
                      'debtor_id': debtor_id, 'currency': 'EUR',
                      'receipt_amount': str(received), 'remaining_amount': str(remaining),
                      'difference': str(received - remaining)})
    return proof


async def verify(api, receipts, debtor_code):
    require(debtor_code in {'100100','109419'}, 'unsupported_psp_debtor')
    refs = references(receipts)
    accounts = await api.rows('crm/Accounts', {'$filter': "Code eq '" + debtor_code.rjust(18) + "'",
                                            '$select': 'ID,Code,IsSales,Status'})
    require(len(accounts) == 1 and str(accounts[0].get('Code', '')).strip() == debtor_code
            and accounts[0].get('IsSales') is True and accounts[0].get('Status') == 'C',
            'current_route_debtor_not_valid')
    debtor = accounts[0]['ID']
    try:
        UUID(debtor)
    except (ValueError, TypeError, AttributeError):
        raise InvoiceNotReady('current_route_debtor_not_valid') from None
    sales, opened = [], []
    for offset in range(0, len(refs), 20):
        query = ' or '.join("YourRef eq '" + r + "'" for r in refs[offset:offset+20])
        sales.extend(await api.rows('salesentry/SalesEntries', {'$filter': query,
            '$select': 'EntryID,EntryNumber,YourRef,Customer,Currency,AmountFC,Type,Reversal'}))
        opened.extend(await api.rows('read/financial/ReceivablesList', {'$filter': query,
            '$select': 'AccountId,EntryNumber,YourRef,Amount'}))
    return {'checked_at': datetime.now(timezone.utc).isoformat(),
            'division': 3977752, 'debtor_code': debtor_code,
            'invoices': assess(receipts, sales, opened, debtor)}


def partition(receipts,sales,opened,debtor_id):
    """Scope an exception to its own receipt instead of suppressing good orders.

    This function is read-only eligibility evidence. It neither changes the
    historical mismatch dossier nor permits a broad native selection. A native
    adapter must select only the returned eligible IDs and prove the readback.
    """
    from collections import Counter
    refs=Counter(r.get('ref') or r.get('order') for r in receipts)
    ids=Counter(r.get('payment_id') for r in receipts)
    eligible,exceptions=[],[]
    for row in receipts:
        ref=row.get('ref') or row.get('order')
        pid=row.get('payment_id')
        try:
            require(refs[ref]==1,'multiple_selected_receipts_for_order')
            require(ids[pid]==1,'duplicate_or_missing_payment_id')
            require(row.get('refund_review') is not True,'own_order_refund_requires_review')
            proof=assess([row],sales,opened,debtor_id)[0]
            eligible.append({'receipt':row,'invoice':proof})
        except InvoiceNotReady as exc:
            exceptions.append({'reference':ref,'payment_id':pid,'reason':str(exc)})
    return eligible,exceptions
