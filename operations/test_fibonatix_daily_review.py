import unittest
from datetime import date
from operations.fibonatix_daily_review import classify
from operations.fibonatix_daily import ImportStore
from operations import test_fibonatix_daily as fixtures


class CoverageTests(unittest.TestCase):
    def fixture(self,status='processing'):
        r=fixtures.DailyImportTests().receipt()
        source=[{'TRX ID':r['payment_id'],'Type':'SL','Status(approved/declined)':'Approved',
                 'Status Code':'20000','Brand TRX ID':'123','Amount':'42.50'}]
        orders=[{'order_id':123,'order_number':'#901','status':status,'total_refunds':0,'payment_method':'wc_fibonatix'}]
        sale={'YourRef':'TD901','EntryNumber':26720001,'Type':20,'Reversal':False}
        opened=[{'EntryNumber':26720001,'Amount':42.50}]
        return r,source,orders,[sale],opened

    def coverage(self,status='processing',imported=False,eligible=False):
        r,source,orders,sales,opened=self.fixture(status)
        return classify(source,orders,[r] if eligible else [],fixtures.DailyImportTests().lines() if imported else [],sales,opened,date(2026,10,6))

    def test_waiting_payment_prevents_false_completion(self):
        c=self.coverage()
        self.assertFalse(c['complete']);self.assertEqual(c['receipts'][0]['state'],'source_order_review')
        self.assertEqual(c['receipts'][0]['open_sales'],[26720001])

    def test_later_completed_order_becomes_missing_import(self):
        c=self.coverage('completed',eligible=True)
        self.assertEqual(c['ready_ids'],['TEST0001']);self.assertFalse(c['complete'])

    def test_unvalidated_receipt_does_not_become_ready(self):
        c=self.coverage('refunded')
        self.assertEqual(c['ready_ids'],[]);self.assertEqual(c['receipts'][0]['state'],'source_order_review')

    def test_validated_payment_is_ready_regardless_of_order_status(self):
        for status in ['processing','refunded','cancelled']:
            self.assertEqual(self.coverage(status,eligible=True)['ready_ids'],['TEST0001'])

    def test_imported_payment_not_imported_again(self):
        c=self.coverage('completed',imported=True,eligible=True)
        self.assertTrue(c['complete']);self.assertFalse(c['ready_ids'])

    def test_sales_side_finds_receipt_absent_from_export(self):
        _,_,orders,sales,opened=self.fixture('completed')
        c=classify([],orders,[],[],sales,opened,date(2026,10,6))
        self.assertFalse(c['complete']);self.assertEqual(c['sales_without_day_payment'][0]['ref'],'TD901')

    def test_payment_on_other_day_is_explicit_not_claimed_missing_invoice(self):
        r,source,orders,_,_=self.fixture('completed')
        c=classify(source,orders,[r],fixtures.DailyImportTests().lines(),[],[],date(2026,10,6))
        self.assertTrue(c['complete']);self.assertEqual(c['receipts'][0]['open_sales'],[])

    def test_followup_store_does_not_update_original_import(self):
        class Conn:
            def __init__(self):self.queries=[]
            def execute(self,sql,args=()):self.queries.append((sql,args));return self
            def fetchone(self):return ('ok',)
        c=Conn();store=ImportStore(c,date(2026,10,6),'a'*64)
        store.insert({'test':True});store.claim();store.persist('verified',{'test':True})
        mutations=[sql for sql,_ in c.queries if sql.startswith(('INSERT','UPDATE'))]
        self.assertEqual(len(mutations),3)
        self.assertTrue(all('jnp_fibonatix_daily_followups' in sql for sql in mutations))
        self.assertTrue(all('jnp_fibonatix_daily_imports' not in sql for sql in mutations))

if __name__=='__main__':unittest.main()

class FollowupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_original_does_not_skip_followup_or_duplicate_receipt(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from operations import fibonatix_daily as d, worker_write_fence as fence
        from operations.fibonatix_daily_source import bounds
        day=date(2026,10,6);revision='a'*64
        original=fixtures.DailyImportTests().receipt()
        added=dict(original,payment_id='TEST0002',ref='TD902',woo_id=124)
        ledger=fixtures.DailyImportTests().lines()
        for row in ledger:row['FinancialYear']=2026
        class Cursor:
            def __init__(self,row):self.row=row
            def fetchone(self):return self.row
        class Conn:
            def __init__(self):self.original=('verified',True,{'sentinel':'retain'})
            def execute(self,sql,args=()):
                if 'pg_try_advisory_lock' in sql:return Cursor((True,))
                if 'SELECT window_start' in sql:return Cursor(bounds(day))
                if 'SELECT state,write_requested,data FROM jnp_fibonatix_daily_imports' in sql:return Cursor(self.original)
                if 'SELECT xml FROM' in sql:return Cursor((b'template',))
                return Cursor(None)
            def close(self):pass
        conn=Conn();persisted=[];xml_rows=[]
        class API:
            def __init__(self,app):self.calls=0
            async def rows(self,resource,params):
                if resource=='financial/Journals':return [dict(ID=d.JOURNAL,GLAccount=d.BANK_GL,Type=12,Currency='EUR',IsBlocked=False)]
                if resource=='crm/Accounts':return [dict(ID=d.DEBTOR,IsSales=True,Status='C')]
                raise AssertionError(resource)
        async def read_ledger(*args):return ledger
        def xml(rows,*args):xml_rows.extend(rows);return b'<synthetic/>'
        app=SimpleNamespace(DIVISION=d.DIVISION,BASE_URL=d.BASE,_db_connect=lambda:conn)
        job={'task_key':'jnp:3977752:fibonatix:2026-10-06','action':'daily_followup_prepare','params':{'date':day.isoformat(),'revision':revision}}
        with fence.owner_scope(SimpleNamespace(role='fibonatix')):
            with patch.object(d,'API',API),patch.object(d,'ledger',read_ledger),patch.object(d,'source',return_value=([original,added],[],{'exceptions':{}},'sha')),patch.object(d,'build_xml',xml),patch.object(d.ImportStore,'persist',lambda self,state,data:persisted.append((self.table,state,data))):
                result=await d.run(app,job)
        self.assertEqual(result['receipts'],1);self.assertEqual(result['existing'],1)
        self.assertEqual([r['payment_id'] for r in xml_rows],['TEST0002'])
        self.assertEqual(conn.original,('verified',True,{'sentinel':'retain'}))
        self.assertEqual(persisted[0][0],'jnp_fibonatix_daily_followups')
