from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from operations import tax_reference as tax, tax_allocation as rules, tax_agent
from operations import allocation_maintenance as maintenance, woo_iban_rules as woo

REFUND = 'Belastingdienst TERUGGAAF NR. 867393051O016240 OB.2EKWART26 (JAMES )'
BANK = dict(ID='00000000-0000-0000-0000-000000000001', EntryID='00000000-0000-0000-0000-000000000002',
    Description=REFUND, Date='2026-08-21', AmountDC='1950.00', GLAccountCode='1100',
    AccountCode='109372', Account='a44106d4-1f80-4de8-bab2-955ab61f40f4', AccountName='Verzameldebiteur BACS Bankoverschrijving')
GL='20f5985f-3eef-465c-80f8-5d49d3328862'
METADATA = dict(tax_account_id=tax.TAX_ACCOUNT_ID, accounts=[
    dict(ID=GL, Code=c, BalanceType='B', IsBlocked=False, Description=c)
    for c in ('1500','1770','1600')])


def test_ob_refund_is_identified_even_when_already_on_bacs():
    d=tax.classify_bank_line(BANK)
    assert (d['status'],d['tax_bucket'],d['tax_letter'],d['tax_year'],d['period_code']) == ('TAX_IDENTIFIED','btw','O',2026,'24')
    assert d['allocation_words']==REFUND
    assert not d.get('payment_reference')  # Never invent an O payment reference.
    assert maintenance.vat_refund(BANK,METADATA)=={'GLAccount':GL,'Words':REFUND}
    assert maintenance.order_reference(REFUND+' order 48721') is None


@pytest.mark.parametrize('description',[
    REFUND+' rente',REFUND+' verrekening',REFUND+' boete', REFUND+' loonheffing', REFUND+' OSS',
    REFUND.replace('867393051','802535860'), REFUND.replace('2EKWART26','1EKWART26'),
    REFUND.replace('2EKWART26','2EKWART25'), REFUND.replace('O016240','O016241'),
    REFUND+' 9253586208001120', REFUND+' 6739305619301120',
    REFUND+' 867393051O016210 OB.1EKWART26',
])
def test_uncertain_tax_refunds_stay_review_and_never_become_bacs(description):
    bank={**BANK,'Description':description}
    assert tax.classify_bank_line(bank)['status']=='REVIEW_TAX'
    assert maintenance.vat_refund(bank,METADATA) is None
    assert maintenance.order_reference(description+' order 48721') is None


def test_outgoing_or_nonfinite_refund_is_not_auto_bookable():
    for amount in ('-1950','0','NaN','Infinity'):
        assert maintenance.vat_refund({**BANK,'AmountDC':amount},METADATA) is None


def test_all_known_tax_evidence_blocks_generic_supplier_or_order_matching():
    supplier=dict(ID=tax.TAX_ACCOUNT_ID,Name='Belastingdienst',IsSupplier=True,EndDate=None)
    for evidence in ({'Account':tax.TAX_ACCOUNT_ID},{'AccountCode':'                 1'},
                     {'AccountName':'BELASTINGDIENST'},{'Description':'NL04RABO0200112244 order 48721'}):
        bank={**BANK,'Description':'order 48721','Account':None,'AccountCode':None,'AccountName':None,**evidence}
        assert tax.classify_bank_line(bank)['status']=='REVIEW_TAX'
        assert maintenance.supplier_proposal({**bank,'AmountDC':'-100'},[supplier],Counter({tax.TAX_ACCOUNT_ID:5})) is None


def test_refund_rule_generation_uses_gl_only_and_reconciles_without_conflict():
    d=tax.classify_bank_line(BANK)
    plan=rules.plan(tax_agent.POLICY,METADATA,[d],2026)
    assert len(plan)==31
    payload=plan[REFUND]['payload']
    assert payload=={'GLAccount':GL,'Words':REFUND}
    actual={**payload,'ID':BANK['ID']}
    assert rules.match_rule([actual],payload)==('confirmed',BANK['ID'])
    assert rules.match_rule([{**actual,'Account':BANK['Account']}],payload)[0]=='conflict'
    assert rules.match_rule([actual],plan['6739305619301120']['payload'])[0]=='missing'


def test_legacy_retirement_is_bound_to_observed_id_and_unchanged_fields():
    old=dict(ID='2a9fa4f3-56eb-462b-843a-919b4fed9e83',Account=tax.TAX_ACCOUNT_ID,AccountBankAccount='NL04RABO0200112244')
    assert rules.legacy_creditor_fallback(old,tax.TAX_ACCOUNT_ID)
    for change in ({'ID':BANK['ID']},{'Account':BANK['Account']},{'GLAccount':GL},{'Words':'specific criterion'}, {'VATCode':'21'}):
        assert not rules.legacy_creditor_fallback({**old,**change},tax.TAX_ACCOUNT_ID)
    assert rules.audit_rules([old],METADATA,[])==[{'rule_id':old['ID'],'reason':'tax_rule_without_ledger'}]
    wrong=dict(ID=BANK['ID'],Account=BANK['Account'],Words='867393051O016240')
    assert rules.audit_rules([wrong],METADATA,[tax.classify_bank_line(BANK)])[0]['reason']=='other_relation_matches_tax_description'


@pytest.mark.asyncio
async def test_tax_cannot_reach_any_direct_webshop_match_even_with_valid_order_number():
    from app import main
    bank={**BANK,'Description':REFUND+' order 48721'}
    with patch.object(main,'find_receivable',AsyncMock()) as lookup, \
         patch.object(main,'bank_line_by_id',AsyncMock(return_value=bank)):
        assert main.extract_order_number(bank['Description']) is None
        result=await main.classify_direct_woo_bank(bank)
        assert result['eligible_for_reimport_enrichment'] is False
        assert result['payment_method']=='TAX'
        detail=await main.candidate_detail(BANK['ID'])
        assert detail['status']=='TAX_IDENTIFIED' and detail['order_number'] is None
        with pytest.raises(main.HTTPException) as exc:
            await main.build_direct_match_plan(BANK['ID'])
        assert exc.value.status_code==409
        lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_queued_bacs_iban_job_cannot_recreate_tax_iban_rule():
    db=MagicMock(); api=AsyncMock()
    await woo.process(db,api,'job',{'iban':'NL04RABO0200112244'},'pending')
    api.account.assert_not_awaited()
    api.create.assert_not_awaited()
    assert "state='conflict'" in db.execute.call_args.args[0]
