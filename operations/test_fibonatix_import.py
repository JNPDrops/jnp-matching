"""Offline import safety tests. No credentials or network requests."""
import hashlib
from decimal import Decimal
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from operations import fibonatix_import as f


def fixture():
    return b'''<eExact><GLTransactions><GLTransaction entry="26260008"><Journal code="26"/><Date>2026-09-22</Date>
    <GLTransactionLine><Date>2026-09-22</Date><GLAccount code="1100"/><Account code="100100"/>
    <FinYear number="2026"/><FinPeriod number="9"/>
    <Amount><Currency code="EUR"/><Value>10.00</Value></Amount>
    <References><PaymentReference>ABCD1234</PaymentReference><YourRef>TD47403</YourRef></References>
    <Description>Order TD47403 | Woo 132103 | Betaling ABCD1234</Description>
    <Note>Order TD47403 | Woo 132103 | Betaling ABCD1234 Batch FIBO-20260922-20261002</Note>
    </GLTransactionLine></GLTransaction></GLTransactions></eExact>'''


def parse(blob):
    return f.parse_batch(blob,digest=hashlib.sha256(blob).hexdigest(),count=1,net=Decimal('10.00'))


def ledger(rows):
    r=rows[0]
    base=dict(EntryNumber=r['entry'],Date=r['date'],Description=r['description'],JournalCode='26',
              YourRef=r['ref'],FinancialYear=2026,FinancialPeriod=9,AccountCode='100100')
    return [dict(base,GLAccountCode='1316',AmountDC=10),dict(base,GLAccountCode='1100',AmountDC=-10)]


class BatchSafety(unittest.TestCase):
    def test_operator_routes_require_the_exact_secret_and_expire(self):
        with patch.dict(f.os.environ,{'EXACT_IMPORT_CONTROL_TOKEN':'x'*43}):
            for value in ['', 'Bearer '+'y'*43]:
                with self.assertRaises(HTTPException): f.authorize(SimpleNamespace(headers={'authorization':value}))
            with patch.object(f,'EXPIRES',f.datetime.max.replace(tzinfo=f.timezone.utc)):
                f.authorize(SimpleNamespace(headers={'authorization':'Bearer '+'x'*43}))
            with patch.object(f,'EXPIRES',f.datetime.min.replace(tzinfo=f.timezone.utc)):
                with self.assertRaises(HTTPException): f.authorize(SimpleNamespace(headers={'authorization':'Bearer '+'x'*43}))

    def test_actual_route_accepts_only_the_approved_hash(self):
        with self.assertRaises(ValueError): f.parse_batch(fixture())

    def test_routing_currency_and_visible_order_references(self):
        self.assertEqual(parse(fixture())[0]['trx'],'ABCD1234')
        for blob in [fixture().replace(b'100100',b'109372'),fixture().replace(b'EUR',b'USD'),
                     fixture().replace(b'Woo 132103',b'unknown'),fixture().replace(b'number="9"',b'number="10"')]:
            with self.assertRaises(ValueError): parse(blob)

    def test_duplicate_transaction_is_rejected(self):
        blob=fixture(); line=blob[blob.index(b'<GLTransactionLine>'):blob.index(b'</GLTransactionLine>')+20]
        blob=blob.replace(b'</GLTransaction>',line+b'</GLTransaction>')
        with self.assertRaises(ValueError):
            f.parse_batch(blob,digest=hashlib.sha256(blob).hexdigest(),count=2,net=Decimal('20'))

    def test_empty_journal_allows_import(self):
        self.assertTrue(f.compare(parse(fixture()),[])['safe_to_import'])

    def test_complete_import_is_verified_but_cannot_be_reimported(self):
        rows=parse(fixture()); result=f.compare(rows,ledger(rows))
        self.assertTrue(result['complete']); self.assertFalse(result['safe_to_import'])

    def test_partial_duplicate_wrong_amount_and_occupied_numbers_block(self):
        rows=parse(fixture()); actual=ledger(rows)
        for found in [actual[:1],actual*2,[dict(actual[0],AmountDC=9),actual[1]],
                      [dict(actual[0],Description='unrelated existing entry')]]:
            result=f.compare(rows,found)
            self.assertFalse(result['complete']); self.assertFalse(result['safe_to_import'])


class NetworkSafety(unittest.IsolatedAsyncioTestCase):
    async def test_withdrawn_cross_order_actions_stop_before_any_browser_or_write(self):
        with patch.object(f,'state',return_value={'reconciliation':{'complete':True}}),patch.object(f,'claim') as claim:
            for args in [{'automatic':True},{'settle':True}]:
                with self.assertRaises(HTTPException) as raised:
                    await f.browser_snapshot(**args)
                self.assertIn('same_source_order_required',raised.exception.detail)
            with self.assertRaises(HTTPException):
                await f.settle_paid_invoice(None)
        claim.assert_not_called()

    async def test_audit_flags_misallocated_source_and_keeps_correct_rows(self):
        common=dict(GLAccountCode='1316',AccountCode=' 100100',EntryID='entry',EntryNumber=1,AmountDC=10)
        rows=[dict(common,ID='a',Description='Order TD10001 | Woo 20001 | Betaling ABCD1234',YourRef='TD10002'),
              dict(common,ID='b',Description='Order TD10003 | Woo 20003 | Betaling EFGH5678',YourRef='TD10003'),
              dict(common,ID='c',Description='Order TD10004 | refund',YourRef='TD10005',AccountCode=None)]
        result=f.order_match_candidates(rows)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['source_order'],'TD10001')
        self.assertEqual(result[0]['allocated_reference'],'TD10002')

    async def test_automatic_stops_before_browser_without_verified_import(self):
        with patch.object(f,'state',return_value={'reconciliation':{'complete':False}}):
            with self.assertRaises(HTTPException): await f.browser_snapshot(automatic=True)

    async def test_receivables_use_verified_id_not_padded_code(self):
        with patch.object(f,'read_all',AsyncMock(return_value=[])) as read:
            await f.receivables()
        self.assertEqual(read.await_args.args[1]['$filter'],"AccountId eq guid'"+f.DEBTOR+"'")

    async def test_wrong_bank_and_missing_references_block_automatic(self):
        rows=parse(fixture())
        for controls in [[],[{'id':'BankAccount','value':'{'+f.BANK+'}'},{'id':'Notes','value':'icepay'}],
                         [{'id':'BankAccount','value':'{'+f.BANK+'}'},{'id':'Notes','value':f.JOB}]]:
            with self.assertRaises(HTTPException): f.verify_statements({'controls':controls,'rows':[]},rows)

    async def test_all_pages_are_read_without_total_top_limit(self):
        prefix='https://start.exactonline.nl/api/v1/3977752/'
        app=SimpleNamespace(API_V1=prefix.rsplit('/3977752/',1)[0],
            exact_get=AsyncMock(return_value={'d':{'results':[1],'__next':prefix+'x?$skiptoken=a'}}),
            _request_json=AsyncMock(return_value={'d':{'results':[2]}}),
            _extract_results=lambda page:page['d']['results'])
        with patch.object(f,'application',return_value=app):
            self.assertEqual(await f.read_all('x',{}),[1,2])
            await f.ledger()
        self.assertNotIn('$top',app.exact_get.await_args.args[1])

    async def test_foreign_pagination_is_never_requested(self):
        app=SimpleNamespace(API_V1='https://start.exactonline.nl/api/v1',
            exact_get=AsyncMock(return_value={'d':{'results':[],'__next':'https://evil.invalid/'}}),
            _extract_results=lambda page:page['d']['results'],_request_json=AsyncMock())
        with patch.object(f,'application',return_value=app):
            with self.assertRaises(HTTPException): await f.read_all('x',{})
        app._request_json.assert_not_awaited()

    async def test_import_stops_before_any_write_if_preflight_is_ambiguous(self):
        with patch.object(f,'preflight',AsyncMock(return_value={'complete':False,'safe_to_import':False})), \
             patch.object(f,'claim') as claim,patch.object(f.httpx,'AsyncClient') as client:
            with self.assertRaises(HTTPException): await f.import_xml()
        claim.assert_not_called(); client.assert_not_called()

    async def test_verified_existing_import_is_not_uploaded(self):
        with patch.object(f,'preflight',AsyncMock(return_value={'complete':True})),patch.object(f,'update'), \
             patch.object(f,'claim') as claim,patch.object(f.httpx,'AsyncClient') as client:
            await f.import_xml()
        claim.assert_not_called(); client.assert_not_called()

    async def test_ambiguous_upload_is_not_retried(self):
        client=SimpleNamespace(post=AsyncMock(side_effect=TimeoutError()))
        context=SimpleNamespace(__aenter__=AsyncMock(return_value=client),__aexit__=AsyncMock(return_value=None))
        from unittest.mock import MagicMock
        manager=MagicMock();manager.__aenter__=context.__aenter__;manager.__aexit__=context.__aexit__
        app=SimpleNamespace(BASE_URL='https://start.exactonline.nl',_access_token=AsyncMock(return_value='fixture-secret'))
        with patch.object(f,'preflight',AsyncMock(return_value={'complete':False,'safe_to_import':True})), \
             patch.object(f,'application',return_value=app),patch.object(f,'claim') as claim, \
             patch.object(f,'update'),patch.object(f,'payload',return_value=fixture()), \
             patch.object(f,'reconcile',AsyncMock(return_value={'complete':False})) as reconcile, \
             patch.object(f.httpx,'AsyncClient',return_value=manager):
            await f.import_xml()
        claim.assert_called_once_with('xml_upload');client.post.assert_awaited_once();reconcile.assert_awaited_once()


if __name__=='__main__': unittest.main()
