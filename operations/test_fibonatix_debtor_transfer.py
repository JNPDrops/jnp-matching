import copy
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations import automatic_debtor_routing as a
from operations.test_plisio_debtor_transfer import fixture as plisio_fixture


def fixture():
    ctx, before, after, ev = plisio_fixture()
    ctx['accounts']['109384'] = ctx['accounts'].pop('109377')
    ctx.update(payment_method='wc_fibonatix', condition={
        'Code':'fi', 'Description':'wc_fibonatix', 'PaymentMethod':'B'})
    ev['order']['payment_method'] = 'wc_fibonatix'
    after['cashflow'][0].update(AccountCode='            109384', PaymentReference='109384/1')
    after['open'][0]['AccountCode'] = '            109384'
    return ctx, before, after, ev


class FibonatixTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_customer_write_and_all_amounts_preserved(self):
        ctx, before, after, ev = fixture()
        item = m.eligible(before, ctx, ev)
        m.check_after(before, after, ctx, True, ev)
        m.check_balances(before['open'], after['open'], [item], ctx, True)
        proof = {'manifest':[{k:ev[k] for k in ('entry_id','reference','order_id')}],
                 'orders':{'#12345':ev['order']}}
        p = {'version':m.VERSION, 'division':m.DIVISION, 'source':m.SOURCE,
             'destination':'109384', 'payment_method':'wc_fibonatix', 'context':ctx,
             'created_at':m.utcnow(), 'metorik_evidence':proof, 'review':[],
             'eligible':[{**item,'snapshot':before,'order_evidence':ev}]}
        p['plan_sha256'] = m.digest(p)
        api = AsyncMock()
        with tempfile.TemporaryFile(mode='w+') as audit, \
             patch.object(m,'context',AsyncMock(return_value=ctx)), \
             patch.object(e,'prepaid_context',AsyncMock()), \
             patch.object(e,'verify_again',AsyncMock()), \
             patch.object(m,'snapshot',AsyncMock(side_effect=[before,before,after,after])), \
             patch.object(m,'balances',AsyncMock(side_effect=[before['open'],after['open']])):
            result = await m.apply(api,p,p['plan_sha256'],audit,True)
        api.change_customer.assert_awaited_once_with(ev['entry_id'],ctx['accounts']['109384']['ID'])
        self.assertEqual(result['preserved_open_totals'],{'EUR':'121'})

    async def test_fibonatix_cannot_apply_without_live_order_proof(self):
        ctx, _, _, _ = fixture()
        p = {'version':m.VERSION, 'division':m.DIVISION, 'source':m.SOURCE,
             'destination':'109384', 'payment_method':'wc_fibonatix', 'context':ctx,
             'created_at':m.utcnow(), 'eligible':[]}
        p['plan_sha256'] = m.digest(p)
        api=AsyncMock()
        with self.assertRaises(m.Stop): await m.apply(api,p,p['plan_sha256'],None,True)
        api.change_customer.assert_not_awaited()

    async def test_all_routes_must_resolve_before_automatic_processing(self):
        seen=[]
        async def context(api,method):
            seen.append(method)
            if method == 'wc_fibonatix': raise m.Stop('Missing destination')
            return {'accounts':{m.SOURCE:{'ID':'source'}}}
        with patch.object(m,'context',side_effect=context):
            with self.assertRaises(m.Stop): await a.validate_routes(AsyncMock())
        self.assertEqual(set(seen), {'bacs','plisio','wc_fibonatix'})

    async def test_wrong_method_target_partial_paid_or_amount_still_blocked(self):
        for change in ('method','destination','partial','paid_link','amount'):
            ctx, before, _, ev = fixture()
            if change=='method': ev['order']['payment_method']='plisio'
            elif change=='destination': ctx['accounts']['109377']=ctx['accounts'].pop('109384')
            elif change=='partial': before['open'][0]['Amount']=1
            elif change=='paid_link': before['cashflow'].append(copy.deepcopy(before['cashflow'][0]))
            else: ev['order']['total']=120.99
            with self.subTest(change=change):
                with self.assertRaises(m.Stop): m.eligible(before,ctx,ev)
