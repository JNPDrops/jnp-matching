from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

import pytest
from operations import bank_resolution as r, allocation_maintenance as a

B='00000000-0000-0000-0000-000000000001'
E='00000000-0000-0000-0000-000000000002'
A='00000000-0000-0000-0000-000000000003'
T='00000000-0000-0000-0000-000000000004'
C='00000000-0000-0000-0000-000000000005'
J={'20':{'Code':'20','Type':12,'Description':'SWAN'},'70':{'Code':'70','Type':20},'60':{'Code':'60','Type':22}}


def bank(**kw):
    return dict(ID=B,EntryID=E,EntryNumber=26200001,LineNumber=1,Description='Jan order 48605',
        AmountDC='324',AmountFC='324',Date='2026-10-01',GLAccountCode='1100',Account=A,
        AccountCode='109372',Currency='EUR',JournalCode='20',JournalDescription='SWAN',**kw)


def invoice(**kw):
    return {**dict(HID=1,AccountId=A,AccountCode='109372',Amount='324',CurrencyCode='EUR',
        YourRef='TD48605',InvoiceDate='2026-10-03',EntryNumber=26722801,JournalCode='70'),**kw}


def review(b=None, recs=None, pays=None, peers=None):
    b=b or bank(); recs=recs if recs is not None else [invoice()]
    return r.review(b,a.classify(b,[],recs),recs,pays or [],J,peers or {})


def order(**kw):
    return {**dict(order_number='#48605',payment_method='bacs',total='324',total_refunds='0',
        currency='EUR',status='completed',order_created_at='2026-10-01T08:00:00+00:00'),**kw}


def test_payment_before_invoice_is_valid_but_before_order_is_not():
    item=review()
    assert item['status']=='order_evidence_needed'
    assert r.order_evidence(deepcopy(item),order())['status']=='bacs_match_candidate'
    assert r.order_evidence(deepcopy(item),order(order_created_at='2026-10-02T08:00:00Z'))['status']=='date_review'
    assert r.order_evidence(deepcopy(item),order(order_created_at=None))['status']=='date_review'
    assert item['bank_write'] is False


def test_partial_invoice_balance_is_not_written_off_and_order_explains_next_step():
    item=review(recs=[invoice(Amount='281')])
    assert item['status']=='amount_review' and item['difference']=='43.00'
    assert item['invoice_open_amount']=='281.00'
    item=r.order_evidence(item,order())
    assert item['status']=='amount_review'
    assert 'factuurimport' in item['next_action'] and item['match_executed'] is False


def test_partial_bank_balance_is_used_instead_of_original_payment():
    item=review(bank(OpenAmountDC='-281'),[invoice(Amount='281')])
    assert item['remaining_bank_amount']=='281.00' and item['original_bank_amount']=='324.00'
    assert r.order_evidence(item,order())['status']=='bacs_match_candidate'


def test_refund_and_cancelation_never_become_supplier_rules():
    for desc in ('Julien cancelation 44134','Steven cancellation 44015','Naim refund duplicate payment','Dachi overpayment 46757'):
        b={**bank(),'Description':desc,'AmountDC':'-30','AmountFC':'-30','GLAccountCode':'1360'}
        assert review(b)['status']=='refund_review'
        assert a.order_reference(desc) is None


def test_psp_journal_wins_over_plain_order_description_and_paypal_merchant_is_cost():
    b={**bank(),'JournalDescription':'Ninja Pay 48312','Description':'Order 48605','GLAccountCode':'1360'}
    assert review(b)['status']=='psp_deferred'
    b={**b,'JournalDescription':'SWAN','Description':'PAYPAL *CONNIEZHANG','AmountDC':'-167.85','AmountFC':'-167.85'}
    assert review(b)['status']=='supplier_review'
    assert review({**b,'Description':'PayPal Europe S.a.r.l.','AmountDC':'305.57','AmountFC':'305.57'})['status']=='psp_deferred'


def test_duplicate_requires_counterparty_or_reference_and_same_own_bank_currency():
    b=bank()
    full={**b,'ID':T,'Description':'Jan order 48605 bosci_'+'a'*29}
    peers=r.duplicate_candidates([b,full])
    assert peers[B]==[T] and peers[T]==[B]
    assert review(b,peers=peers)['status']=='duplicate_review'
    for other in ({**full,'JournalCode':'21'}, {**full,'Currency':'USD'}, {**full,'Description':'Other customer order 48999'}):
        assert not r.duplicate_candidates([b,other])


def test_missing_supplier_invoice_excludes_other_bank_payments():
    b={**bank(),'Description':'Innosend IN-2026-09-788','GLAccountCode':'1400','AccountCode':'102402','AmountDC':'-4997.43','AmountFC':'-4997.43'}
    payment=invoice(Amount='-4999.18',JournalCode='20',YourRef=None,AccountCode='102402')
    assert review(b,pays=[payment])['status']=='supplier_invoice_missing'
    inv=invoice(Amount='4997.43',JournalCode='60',YourRef='IN-2026-09-788',AccountCode='102402')
    assert review(b,pays=[payment,inv])['status']=='supplier_match_candidate'
    assert review({**b,'Description':'Innosend'},pays=[inv])['status']=='supplier_amount_candidate'
    assert review(b,pays=[{**inv,'Amount':'4997.44'}])['status']=='amount_review'


def test_supplier_refund_requires_credit_and_currency_evidence():
    b={**bank(),'Description':'FEDEX credit CR123456','GLAccountCode':'1400','AmountDC':'25.07','AmountFC':'25.07'}
    inv=invoice(Amount='-25.07',JournalCode='60',YourRef='CR123456')
    assert review(b,pays=[inv])['status']=='supplier_match_candidate'
    assert review(b,pays=[{**inv,'Amount':'25.07'}])['status']=='invoice_direction_review'
    assert review({**b,'Currency':None},pays=[inv])['status']=='currency_review'


def test_invoice_bank_row_and_non_bacs_order_never_make_match_candidate():
    assert review(recs=[invoice(JournalCode='20')])['status']=='invoice_missing'
    assert r.order_evidence(review(),order(payment_method='ninja'))['status']=='payment_method_review'
    assert r.order_evidence(review(),order(total_refunds='1'))['status']=='order_refund_review'
    assert r.order_evidence(review(),order(total='325'))['status']=='order_amount_review'


@pytest.mark.asyncio
async def test_only_cashflow_backed_bank_lines_are_reviewed_and_identity_is_proven():
    flow=dict(ID=C,Account=A,AccountCode='109372',AmountDC='-281',AmountFC='-281',Currency='EUR',
        Status=20,TransactionID=T,TransactionEntryID=E,EntryNumber=26200001,GLAccountCode='1100')
    tx=dict(ID=T,EntryID=E,LineNumber=1,Account=A,GLAccountCode='1100',AmountDC='-324')
    b=bank()
    async def rows(resource,params):
        if resource=='cashflow/Receivables':return [flow]
        if resource=='cashflow/Payments':return []
        if resource.endswith('BankEntryLines'):return [b]
        if resource.endswith('/TransactionLines'):return [tx]
        raise AssertionError(resource)
    api=MagicMock();api.rows=AsyncMock(side_effect=rows)
    headers={E:dict(EntryID=E,JournalCode='20',Currency='EUR')}
    result,unresolved=await r.load_assigned(api,J,headers,a.BANK_FIELDS)
    assert not unresolved and result[0]['ID']==B and result[0]['OpenAmountDC']=='-281'
    tx['LineNumber']=2
    result,unresolved=await r.load_assigned(api,J,headers,a.BANK_FIELDS)
    assert result==[] and unresolved[0]['cashflow_id']==C


@pytest.mark.asyncio
async def test_all_new_resources_are_read_only_and_html_requires_operator():
    api=a.MaintenanceAPI(MagicMock(),{'remaining':1000})
    for resource in ('cashflow/Payments','cashflow/Receivables','financialtransaction/TransactionLines'):
        with pytest.raises(a.m.Stop):
            await api.request('POST',a.m.BASE+'/api/v1/3977752/'+resource,payload={})
    from starlette.requests import Request
    with pytest.raises(a.HTTPException) as exc:
        await a.review_page(Request({'type':'http','session':{}}))
    assert exc.value.status_code==401


@pytest.mark.asyncio
async def test_parent_header_reads_are_limited_to_current_candidate_ids():
    api=MagicMock();api.rows=AsyncMock(return_value=[dict(EntryID=E,JournalCode='20',Currency='EUR')])
    result=await r.load_headers(api,[E,E])
    assert result[E]['Currency']=='EUR'
    assert api.rows.await_args.kwargs['params']['$filter']=="EntryID eq guid'"+E+"'"
    api.rows.reset_mock()
    assert await r.load_headers(api,[])=={}
    api.rows.assert_not_awaited()
    api.rows.return_value=[dict(EntryID=C)]
    with pytest.raises(a.m.Stop):await r.load_headers(api,[E])


@pytest.mark.asyncio
async def test_hourly_run_enriches_assigned_items_and_preserves_write_boundaries():
    from unittest.mock import patch
    from operations import metorik_bacs_evidence
    import json
    now=datetime(2026,10,4,tzinfo=timezone.utc)
    b=bank(OpenAmountDC='-324',OpenEvidence='cashflow_status_20')
    async def rows(resource,params):
        return {'financialtransaction/BankEntryLines':[], 'read/financial/ReceivablesList':[invoice()],
            'read/financial/PayablesList':[], 'financial/Journals':list(J.values()),
            'financialtransaction/BankEntries':[dict(EntryID=E,JournalCode='20',Currency='EUR')]}[resource]
    # Include Code in fixture metadata, as the real Journals resource does.
    journals=[dict(v,Code=k) for k,v in J.items()]
    old_rows=rows
    async def rows(resource,params):
        return journals if resource=='financial/Journals' else await old_rows(resource,params)
    api=MagicMock();api.post_count=0;api.rows=AsyncMock(side_effect=rows);api.rules=AsyncMock(return_value=[])
    api.request=AsyncMock()
    saved=[]
    def execute(sql,args=None):
        cursor=MagicMock()
        if sql.startswith('SELECT metadata'):cursor.fetchone.return_value=({'tax_account_id':C,'accounts':[]},)
        elif sql.startswith('SELECT cache,'):cursor.fetchone.return_value=({'suppliers':[],'history_counts':{}},now)
        elif sql.startswith('SELECT next_cleanup'):cursor.fetchone.return_value=(now+timedelta(days=1),)
        elif sql.lstrip().startswith('INSERT INTO jnp_suspense_review'):saved.append(json.loads(args[1]))
        return cursor
    conn=MagicMock();conn.execute.side_effect=execute
    with patch.object(r,'load_open_bank_items',AsyncMock(return_value=([b],[]))), \
         patch.object(metorik_bacs_evidence,'lookup_orders',AsyncMock(return_value={'orders':{'#48605':order()}})):
        summary=await a.run(MagicMock(),conn,api,now)
    assert summary['bank_lines_1360']==0 and summary['assigned_open_bank_lines']==1
    assert summary['classifications']=={'bacs_match_candidate':1}
    assert summary['bank_writes'] is False and summary['automatically_executed'] is False
    assert saved[0]['next_action'].startswith('Letter bestaande bankregel')
    api.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_item_lists_recover_bank_receipts_omitted_by_cashflow_api():
    b=bank()
    open_bank=invoice(HID=77,Amount='-281',JournalCode='20',InvoiceDate=b['Date'],YourRef=None,EntryNumber=b['EntryNumber'])
    api=MagicMock();api.rows=AsyncMock(return_value=[b])
    headers={E:dict(EntryID=E,JournalCode='20',Currency='EUR')}
    result,unresolved=await r.load_open_bank_items(api,[open_bank],[],J,headers,a.BANK_FIELDS)
    assert not unresolved and result[0]['ID']==B and result[0]['OpenAmountDC']=='281'
    assert result[0]['OpenEvidence']=='open_items_list_hid_77'
    assert api.rows.await_args.kwargs['params']['$filter']=='EntryNumber eq 26200001'
    # A second indistinguishable receipt is not silently selected.
    api.rows.return_value=[b,{**b,'ID':T}]
    result,unresolved=await r.load_open_bank_items(api,[open_bank],[],J,headers,a.BANK_FIELDS)
    assert not result and len(unresolved)==1
    # A bank payment on another account is not the same open item.
    api.rows.return_value=[{**b,'Account':C}]
    result,unresolved=await r.load_open_bank_items(api,[open_bank],[],J,headers,a.BANK_FIELDS)
    assert not result and unresolved


@pytest.mark.asyncio
async def test_payable_open_payment_uses_payable_sign_and_excludes_psp_journal():
    b={**bank(),'GLAccountCode':'1400','AmountDC':'-85','AmountFC':'-85'}
    payment=invoice(HID=88,Amount='-85',JournalCode='20',InvoiceDate=b['Date'],YourRef=None,EntryNumber=b['EntryNumber'])
    api=MagicMock();api.rows=AsyncMock(return_value=[b])
    headers={E:dict(EntryID=E,JournalCode='20',Currency='EUR')}
    result,unresolved=await r.load_open_bank_items(api,[],[payment],J,headers,a.BANK_FIELDS)
    assert not unresolved and result[0]['OpenAmountDC']=='-85'
    psp={**J,'20':{**J['20'],'Description':'Plisio'}}
    api.rows.reset_mock()
    result,unresolved=await r.load_open_bank_items(api,[],[payment],psp,headers,a.BANK_FIELDS)
    assert not result and not unresolved
    api.rows.assert_not_awaited()
