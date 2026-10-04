from collections import Counter
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from operations import allocation_maintenance as a

B='00000000-0000-0000-0000-000000000001'
E='00000000-0000-0000-0000-000000000002'
A='00000000-0000-0000-0000-000000000003'
G='00000000-0000-0000-0000-000000000004'


def bank(desc='Jan Voorbeeld order 48721', amount='85.00', **kw):
    return dict(ID=B, EntryID=E, Description=desc, AmountDC=amount, AmountFC=amount,
                Date='2026-10-03', GLAccountCode='1360', **kw)


def test_psp_and_refund_references_never_become_webshop_orders():
    assert a.order_reference('STICHTING ICEPAY bosci_63ff81cad611d31be7f9c27bd895c 1052415') is None
    assert a.order_reference('Paynetics Payout 2026-09-29') is None
    assert a.order_reference('Ines refund double payment 45842') is None
    assert a.order_reference('Jane 48721') == 'TD48721'
    assert a.order_reference('Jane 48721 48722') is None


def test_vat_refund_description_generates_balance_rule_without_invented_reference():
    metadata={'tax_account_id':A, 'accounts':[dict(ID=G, Code='1770', BalanceType='B', IsBlocked=False)]}
    b=bank('Belastingdienst teruggaaf omzetbelasting tweede kwartaal 2026')
    p=a.vat_refund(b,metadata)
    assert p == {'Account':A,'GLAccount':G,'Words':b['Description']}
    for d in ('Belastingdienst teruggaaf omzetbelasting met rente', 'Belastingdienst teruggaaf omzetbelasting en loonheffing',
              'Klant teruggaaf omzetbelasting', 'Belastingdienst teruggaaf omzetbelasting 9253586208001120'):
        assert a.vat_refund(bank(d),metadata) is None
    assert a.vat_refund(bank(b['Description'],amount='-85'),metadata) is None
    metadata['accounts'][0]['IsBlocked']=True
    assert a.vat_refund(b,metadata) is None


def test_exact_invoice_amount_is_required_not_small_difference_tolerance():
    rec=dict(YourRef='TD48721',AccountId=A,AccountCode='109372',CurrencyCode='EUR',Amount='85',InvoiceDate='2026-10-02')
    item=a.classify(bank(),[],[rec])
    assert item['status']=='order_evidence_needed'
    assert a.classify(bank(amount='84.99'),[],[rec])['status']=='amount_review'
    order=dict(order_number='#48721',payment_method='bacs',currency='EUR',status='processing',total='85',total_refunds='0')
    assert a.proposal(item,order,Counter({'TD48721':1}))=={'Account':A,'Words':item['description']}
    assert a.proposal(item,order,Counter({'TD48721':2})) is None
    assert a.proposal(item,{**order,'payment_method':'wc_fibonatix'},Counter({'TD48721':1})) is None


def test_supplier_name_needs_unique_relation_and_payment_history():
    supplier=dict(ID=A,Name='Miron B.V.',IsSupplier=True,EndDate=None)
    b=bank('Miron B.V. MSO26-107762',amount='-4580.58')
    assert a.supplier_proposal(b,[supplier],Counter({A:2}))=={'Account':A,'Words':b['Description']}
    assert a.supplier_proposal(b,[supplier],Counter({A:1})) is None
    assert a.supplier_proposal(b,[supplier,{**supplier,'ID':G}],Counter({A:2,G:2})) is None


def test_duplicate_rules_require_identical_dimensions_and_keep_one():
    rule=dict(ID=B,Account=A,Words='SOME WORDS')
    result=a.duplicates([rule,{**rule,'ID':E},{**rule,'ID':G,'VATCode':'21'}],preferred={E})
    assert [(x['ID'],keep['ID']) for x,keep in result]==[(B,E)]


def test_retirement_requires_90_days_allocated_payment_and_closed_invoice():
    words='bosci_'+'a'*32
    body=dict(bank_reference=words,payment_date='2026-06-01',order_number='48721',amount='85')
    b=bank(words,AccountCode='109372',Account=A);b['GLAccountCode']='1100'
    now=datetime(2026,10,4,tzinfo=timezone.utc)
    assert a.can_retire_payment(body,[b],set(),now)
    assert not a.can_retire_payment(body,[b],{'TD48721'},now)
    assert not a.can_retire_payment(body,[{**b,'GLAccountCode':'1360'}],set(),now)
    assert not a.can_retire_payment(body,[b,b],set(),now)
    assert not a.can_retire_payment({**body,'payment_date':'2026-10-01'},[b],set(),now)
    # Tax references and fixed IBAN rules have no expiry-by-age path.
    assert not a.can_retire_payment({'payment_date':'2025-01-01','iban':'NL00'},[b],set(),now)


@pytest.mark.asyncio
async def test_transport_rejects_bank_writes_and_unapproved_deletes():
    app=MagicMock();app._access_token=AsyncMock()
    api=a.MaintenanceAPI(app,{'remaining':1000})
    for method,url,payload in [('DELETE',a.ROOT+"(guid'"+B+"')",None),('PUT',a.ROOT,{}),
        ('POST',a.m.BASE+'/api/v1/3977752/financialtransaction/BankEntryLines',{}),
        ('POST',a.ROOT,{'Account':A,'Words':'guess'})]:
        with pytest.raises(a.m.Stop): await api.request(method,url,payload=payload)
    app._access_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_financial_report_is_not_public():
    from starlette.requests import Request
    with pytest.raises(a.HTTPException) as exc:
        await a.report(Request({'type':'http','session':{}}))
    assert exc.value.status_code==401


@pytest.mark.asyncio
async def test_existing_candidate_ui_also_defers_psp():
    from app import main
    with patch.object(main,'bank_lines_on_suspense',AsyncMock(return_value=[bank('STICHTING ICEPAY TRANSFER 1052415')])), \
         patch.object(main,'find_receivable',AsyncMock()) as lookup:
        result=await main.bank_first_candidates()
    assert result['items'][0]['status']=='DEFER_PSP'
    assert result['items'][0]['order_number'] is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_never_deletes_an_operator_modified_rule():
    old=dict(ID=B,Account=A,Words='exact original')
    api=MagicMock();api.allowed_deletes=set()
    api.rules=AsyncMock(return_value=[{**old,'Words':'operator change'}])
    api.request=AsyncMock()
    db=MagicMock()
    assert await a.delete_rule(db,api,old,'duplicate')=='changed_keep'
    api.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_cleanup_requires_keeper_to_still_exist():
    old=dict(ID=B,Account=A,Words='exact original')
    api=MagicMock();api.allowed_deletes=set();api.rules=AsyncMock(return_value=[old]);api.request=AsyncMock()
    assert await a.delete_rule(MagicMock(),api,old,'duplicate',E)=='keeper_missing_keep'
    api.request.assert_not_awaited()
