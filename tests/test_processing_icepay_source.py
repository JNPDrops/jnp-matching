import base64
import copy
import hashlib
import unittest
from datetime import date,datetime,timezone
from operations.processing_icepay_source import payload


class CutoffSourceTests(unittest.TestCase):
    def setUp(self):
        self.day=date(2026,10,10)
        self.start=datetime(2026,10,9,22,tzinfo=timezone.utc)
        self.end=datetime(2026,10,10,6,tzinfo=timezone.utc)
        self.raw=b'validated synthetic export'
        self.summary={'source_rows':4,'source_sha256':hashlib.sha256(self.raw).hexdigest()}
        self.status={'state':'downloaded','stage':'complete'}
        rows=[{'payment_id':str(i+1),'source_time_utc':stamp,'date':'2026-10-10',
            'status':status,'amount':'10.00','order':'12345'} for i,(stamp,status) in enumerate([
            ('2026-10-09T22:00:00+00:00','OK'),('2026-10-10T05:59:59+00:00','OK'),
            ('2026-10-10T06:00:00+00:00','OK'),('2026-10-10T05:00:00+00:00','OPEN')])]
        self.artifacts={'proof':{'period_from':'2026-10-10','period_through':'2026-10-10',
            'ui_payment_ids':['1','2','3','4'],'ui_payment_count':4},
            'payments_csv':base64.b64encode(self.raw).decode(),'timezone_proof':{'verified':True},
            'transactions':rows,'refunds':{'total':0}}

    def result(self,artifacts=None,summary=None,classify=None):
        return payload(self.day,self.start,self.end,self.status,artifacts or self.artifacts,
            summary or self.summary,classify or (lambda raw,rows:[r|{'kind':'receipt'} for r in rows]))

    def test_prior_midnight_included_cutoff_excluded_and_pending_not_imported(self):
        data=self.result()
        self.assertEqual([r['payment_id'] for r in data['candidates']],['1','2'])
        self.assertFalse(data['financial_writes'])
        self.assertEqual(data['artifacts'],self.artifacts)

    def test_changed_export_clock_or_identity_cannot_become_verified(self):
        for change in ('digest','clock','identity','count','day'):
            a=copy.deepcopy(self.artifacts);s=dict(self.summary)
            if change=='digest':s['source_sha256']='changed'
            if change=='clock':a['timezone_proof']['verified']=False
            if change=='identity':a['transactions'][0]['payment_id']='999'
            if change=='count':a['proof']['ui_payment_count']=5
            if change=='day':a['proof']['period_from']='2026-10-09'
            with self.subTest(change=change),self.assertRaises(ValueError):self.result(a,s)

    def test_unknown_negative_item_does_not_erase_an_independent_receipt(self):
        def classify(raw,rows):
            if rows[0]['payment_id']=='2':raise ValueError('unrecognized_nonreceipt')
            return [rows[0]|{'kind':'receipt'}]
        data=self.result(classify=classify)
        self.assertEqual([r['payment_id'] for r in data['candidates']],['1'])
        self.assertEqual(data['exceptions'],[{'payment_id':'2','reason':'source_item_policy_requires_review'}])

    def test_empty_source_needs_explicit_provider_count_proof(self):
        a={'proof':{'period_from':'2026-10-10','period_through':'2026-10-10',
            'ui_payment_ids':[],'ui_payment_count':0},'refunds':{'total':0}}
        data=self.result(a,{'source_rows':0})
        self.assertEqual(data['candidates'],[])
        with self.assertRaises(ValueError):self.result(a,{'source_rows':1})

    def test_interrupted_provider_capture_is_not_source_completion(self):
        self.status['state']='started'
        with self.assertRaisesRegex(ValueError,'not_downloaded'):self.result()
