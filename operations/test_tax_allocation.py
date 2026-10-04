import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from operations import tax_allocation as m, tax_reference as tax, tax_agent

ACCOUNT = '00000000-0000-0000-0000-000000000001'
GL = '00000000-0000-0000-0000-000000000002'
RULE = '00000000-0000-0000-0000-000000000003'
WORDS = '6739305619301120'
PAYLOAD = {'Account': ACCOUNT, 'GLAccount': GL, 'Words': WORDS}
PROPOSALS = {WORDS: {'payload': PAYLOAD, 'tax_bucket': 'vpb'}}


def metadata():
    return {'tax_account_id': ACCOUNT, 'accounts': [
        {'ID': GL, 'Code': code, 'BalanceType': 'B', 'IsBlocked': False}
        for code in ('1500', '1770', '1600')]}


def test_confirmed_reference_generation_and_independent_roundtrip():
    assert m.reference('V', 2026, '0112', kind=1)['payment_reference'] == WORDS
    for letter, period, kwargs in [('B', '27', {'subnumber': '01'}), ('L', '12', {'subnumber': '02'})]:
        d = m.reference(letter, 2027, period, **kwargs)
        decoded = tax.decode_payment(d['payment_reference'], anchor_year=2027)
        assert (decoded['tax_letter'], decoded['tax_year'], decoded['period_code'], decoded['subnumber']) == (letter, 2027, period, kwargs['subnumber'])
    for letter, period, kwargs in [('V', '0112', {'kind': 6}), ('B', '71', {'subnumber': '01'}), ('L', '27', {'subnumber': '01'}), ('L', '01', {'subnumber': '00'}), ('F', '27', {'subnumber': '01'})]:
        with pytest.raises(tax.ReferenceError):
            m.reference(letter, 2026, period, **kwargs)


def test_plan_uses_verified_templates_without_inventing_payroll_number():
    plan = m.plan(tax_agent.POLICY, metadata(), [], 2026)
    assert len(plan) == 30
    assert {p['tax_year'] for p in plan.values()} == {2025, 2026, 2027}
    assert {p['tax_bucket'] for p in plan.values()} == {'vpb', 'btw'}
    assert all(set(p['payload']) == {'Account', 'GLAccount', 'Words'} for p in plan.values())
    assert WORDS in plan
    data = metadata(); data['accounts'][0]['IsBlocked'] = True
    with pytest.raises(m.transport.Stop):
        m.plan(tax_agent.POLICY, data, [], 2026)


def test_payroll_template_requires_validated_outgoing_evidence():
    d = m.reference('L', 2026, '03', subnumber='02')
    decision = tax.classify_bank_line({'Description': d['payment_reference'], 'AmountDC': '-500', 'Date': '2026-04-01'})
    plan = m.plan(tax_agent.POLICY, metadata(), [decision], 2026)
    payroll = [p for p in plan.values() if p['tax_bucket'] == 'loonheffingen']
    assert len(payroll) == 36
    assert all('.L.02.' in p['assessment_number'] for p in payroll)
    assert len(m.plan(tax_agent.POLICY, metadata(), [{**decision, 'status': 'REVIEW_TAX'}], 2026)) == 30


def test_existing_rule_conflicts_are_not_silently_overwritten():
    exact = {**PAYLOAD, 'ID': RULE}
    assert m.match_rule([exact], PAYLOAD) == ('confirmed', RULE)
    for change in ({'GLAccount': ACCOUNT}, {'Words': '6739 3056 1930 1120'}, {'VATCode': '21'}, {'Account': GL}):
        assert m.match_rule([{**exact, **change}], PAYLOAD)[0] == 'conflict'
    assert m.match_rule([exact, exact], PAYLOAD)[0] == 'conflict'
    assert m.match_rule([{'ID': RULE, 'Account': ACCOUNT, 'AccountBankAccount': 'NL04RABO0200112244'}], PAYLOAD)[0] == 'missing'


class DB:
    def __init__(self, state=None):
        self.rows = {} if state is None else {WORDS: {'state': state, 'payload': PAYLOAD}}
        self.result = []
    def execute(self, sql, params=()):
        if sql.startswith('INSERT'):
            self.rows.setdefault(params[0], {'state': 'pending', 'payload': json.loads(params[1])})
        elif sql.startswith('SELECT state,payload'):
            row = self.rows[params[0]]; self.result = [(row['state'], row['payload'])]
        elif sql.startswith('UPDATE'):
            self.rows[params[3]]['state'] = params[0]
        elif sql.startswith('SELECT state,COUNT'):
            from collections import Counter
            self.result = list(Counter(row['state'] for row in self.rows.values()).items())
        return self
    def fetchone(self): return self.result[0]
    def fetchall(self): return self.result


@pytest.mark.asyncio
async def test_reconcile_persists_intent_before_post_and_reads_back():
    db = DB(); api = MagicMock(limits={'remaining': 1000})
    api.rules = AsyncMock(side_effect=[[], [{**PAYLOAD, 'ID': RULE}]])
    async def create(*args, **kwargs):
        assert db.rows[WORDS]['state'] == 'creating'
    api.request = AsyncMock(side_effect=create)
    result = await m.reconcile(db, api, PROPOSALS)
    assert result['counts'] == {'confirmed': 1}
    api.request.assert_awaited_once_with('POST', m.ROOT, payload=PAYLOAD)


@pytest.mark.asyncio
@pytest.mark.parametrize('previous', ['creating', 'uncertain', 'confirmed', 'conflict'])
async def test_missing_rule_after_prior_intent_is_never_recreated(previous):
    db = DB(previous); api = MagicMock(limits={'remaining': 1000})
    api.rules = AsyncMock(return_value=[]); api.request = AsyncMock()
    await m.reconcile(db, api, PROPOSALS)
    api.request.assert_not_awaited()
    assert db.rows[WORDS]['state'] == 'uncertain'


@pytest.mark.asyncio
async def test_ambiguous_write_reconciles_without_repeat():
    db = DB(); api = MagicMock(limits={'remaining': 1000})
    api.rules = AsyncMock(side_effect=[[], [], [{**PAYLOAD, 'ID': RULE}]])
    api.request = AsyncMock(side_effect=TimeoutError())
    await m.reconcile(db, api, PROPOSALS)
    assert db.rows[WORDS]['state'] == 'uncertain'
    await m.reconcile(db, api, PROPOSALS)
    assert db.rows[WORDS]['state'] == 'confirmed'
    assert api.request.await_count == 1


@pytest.mark.asyncio
async def test_transport_rejects_bank_writes_and_unapproved_payloads_before_auth():
    app = MagicMock(); app._access_token = AsyncMock()
    api = m.RuleAPI(app, {WORDS: PAYLOAD}, {'remaining': 1000})
    for method, url, payload in [('PUT', m.ROOT, PAYLOAD), ('POST', m.ROOT.replace('/beta/', '/').replace('cashflow/AllocationRule', 'financialtransaction/BankEntryLines'), PAYLOAD),
        ('POST', m.ROOT, {**PAYLOAD, 'VATCode': '21'}), ('POST', m.ROOT, {**PAYLOAD, 'GLAccount': ACCOUNT})]:
        with pytest.raises(m.transport.Stop):
            await api.request(method, url, payload=payload)
    app._access_token.assert_not_awaited()
