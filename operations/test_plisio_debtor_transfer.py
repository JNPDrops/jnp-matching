import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations.test_metorik_bacs_evidence import evidence_fixture


def fixture():
    ctx, before, after, ev = evidence_fixture()
    ctx['accounts']['109377'] = ctx['accounts'].pop('109372')
    ctx.update(payment_method='plisio', condition={'Code':'pl','Description':'plisio','PaymentMethod':'B'})
    ev['order']['payment_method'] = 'plisio'
    after['cashflow'][0].update(AccountCode='            109377', PaymentReference='109377/1')
    after['open'][0]['AccountCode'] = '            109377'
    return ctx, before, after, ev


class PlisioTests(unittest.TestCase):
    def test_plisio_route_preserves_prepaid_and_business_fields(self):
        ctx, before, after, ev = fixture()
        self.assertEqual(m.destination(ctx), '109377')
        item = m.eligible(before, ctx, ev)
        m.check_after(before, after, ctx, True, ev)
        m.check_balances(before['open'], after['open'], [item], ctx, True)
        after['header']['PaymentCondition'] = 'pl'
        with self.assertRaises(m.Stop): m.check_after(before, after, ctx, True, ev)

    def test_wrong_method_destination_and_partial_are_blocked(self):
        for change in ('bacs', 'destination', 'partial', 'paid_link'):
            ctx, before, after, ev = fixture()
            if change == 'bacs': ev['order']['payment_method'] = 'bacs'
            elif change == 'destination': ctx['accounts']['109372'] = ctx['accounts'].pop('109377')
            elif change == 'partial': before['open'][0]['Amount'] = 1
            else: before['cashflow'].append(copy.deepcopy(before['cashflow'][0]))
            with self.assertRaises(m.Stop): m.eligible(before, ctx, ev)

    def test_route_cannot_be_relabelled_in_rehashed_plan(self):
        ctx, before, _, ev = fixture()
        p = {'version':m.VERSION,'division':m.DIVISION,'source':m.SOURCE,'destination':'109372',
             'payment_method':'plisio','context':ctx,'created_at':m.utcnow(),'eligible':[]}
        p['plan_sha256'] = m.digest(p)
        with self.assertRaises(m.Stop): m.validate_plan(p,p['plan_sha256'])


class PlisioExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_plisio_apply_writes_only_verified_destination_once(self):
        ctx, before, after, ev = fixture()
        manifest = [{k:ev[k] for k in ('entry_id','reference','order_id')}]
        proof = {'manifest':manifest,'orders':{'#12345':ev['order']}}
        p = {'version':m.VERSION,'division':m.DIVISION,'source':m.SOURCE,'destination':'109377',
             'payment_method':'plisio','context':ctx,'created_at':m.utcnow(),
             'metorik_evidence':proof,'review':[],
             'eligible':[{**m.eligible(before,ctx,ev),'snapshot':before,'order_evidence':ev}]}
        p['plan_sha256'] = m.digest(p)
        api = AsyncMock()
        with tempfile.TemporaryFile(mode='w+') as audit, \
             patch.object(m,'context',AsyncMock(return_value=ctx)), \
             patch.object(e,'prepaid_context',AsyncMock()), \
             patch.object(e,'verify_again',AsyncMock()), \
             patch.object(m,'snapshot',AsyncMock(side_effect=[before,before,after,after])), \
             patch.object(m,'balances',AsyncMock(side_effect=[before['open'],after['open']])):
            result = await m.apply(api,p,p['plan_sha256'],audit,True)
            api.change_customer.assert_awaited_once_with(ev['entry_id'],ctx['accounts']['109377']['ID'])
            self.assertEqual(result['preserved_open_totals'],{'EUR':'121'})
            audit.seek(0)
            self.assertEqual(json.loads(audit.readlines()[-1])['event'],'complete')

    async def test_missing_or_ambiguous_target_never_proceeds(self):
        api=AsyncMock()
        api.rows.side_effect=[[{'ID':'00000000-0000-0000-0000-000000000001','Code':'100100','Name':'source','IsSales':True,'Status':'C'}],[]]
        with self.assertRaises(m.Stop): await m.context(api,'plisio')
        api.change_customer.assert_not_awaited()
