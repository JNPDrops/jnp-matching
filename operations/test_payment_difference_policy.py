from copy import deepcopy
from decimal import Decimal
import pytest
from operations import bank_resolution as r

def item(**kwargs):
    return dict(status='amount_review', gl_account='1100', currency='EUR',
                reference='TD90001', invoice_reference='TD90001', invoice_entry=990001,
                invoice_hid=990002, remaining_bank_amount='100.00',
                invoice_open_amount_signed='100.03', original_bank_amount='100.00',
                amount='100.00', bank_date='2026-10-07', **kwargs)

def order(**kwargs):
    return {**dict(order_number='#90001', order_id=90001, payment_method='bacs',
                 currency='EUR', total='100.00', total_refunds='0', status='completed',
                 order_created_at='2026-10-06T10:00:00Z'), **kwargs}

@pytest.mark.parametrize('due,approved', [('99.00',True),('101.00',True),
    ('100.03',True),('99.99',True),('98.99',False),('101.01',False),
    ('100.00',False),('0',False),('-1',False),('NaN',False),
    ('Infinity',False),('101.001',False)])
def test_inclusive_eur_limit_both_signs(due,approved):
    value=item(); value['invoice_open_amount_signed']=due
    result=r.approved_sales_difference(value)
    assert bool(result) is approved
    if result:
        assert result['executed'] is False
        assert abs(Decimal(result['difference'])) <= Decimal('1.00')

@pytest.mark.parametrize('changes', [
    {'reference':'TD90002'},{'invoice_reference':None},{'invoice_entry':None},
    {'invoice_hid':None},{'currency':'USD'},{'gl_account':'1400'},
    {'status':'refund_review'},{'status':'multiple_invoices'},
    {'status':'duplicate_review'},{'remaining_bank_amount':'NaN'}])
def test_unproven_or_non_sales_case_not_approved(changes):
    assert r.approved_sales_difference({**item(),**changes}) is None

def test_order_evidence_is_required_and_decision_is_not_execution():
    value=r.order_evidence(item(),order())
    assert value['status']=='bacs_match_candidate'
    assert value['payment_difference_approval']['approved'] is True
    assert value['match_executed'] is False and value['bank_write'] is False
    for change in ({'order_number':'#90002'},{'total_refunds':'1'},
                   {'payment_method':'other'},{'status':'cancelled'}):
        blocked=r.order_evidence(item(),order(**change))
        assert 'payment_difference_approval' not in blocked
        assert blocked['status']!='bacs_match_candidate'

def test_small_payment_shortfall_against_order_and_invoice():
    assert r.order_evidence(item(),order(total='100.03'))['status']=='bacs_match_candidate'
    assert r.order_evidence(item(),order(total='102'))['status']=='order_amount_review'

def test_psp_uses_same_policy_and_duplicate_invoice_guard_remains():
    values=[r.order_evidence(item(expected_methods=['wc_fibonatics']),order(payment_method='wc_fibonatics')) for _ in range(2)]
    assert all(v['status']=='psp_match_candidate' for v in values)
    assert all(v['status']=='multiple_payments_review' for v in r.block_shared_invoice_candidates(values))

def test_current_remaining_balance_is_used():
    value=item(); value.update(original_bank_amount='200.00',amount='200.00')
    assert r.order_evidence(value,order(total='200.00'))['payment_difference_approval']['difference']=='-0.03'
