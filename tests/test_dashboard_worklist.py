import copy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch

from app.dashboard.worklist import (bank_case, strict_cases, finalize, money,
                                    PostgresWorklist, Conflict, DIVISION)
import test_dashboard_auth as auth_tests
USER, ORIGIN = auth_tests.USER, auth_tests.ORIGIN


NOW = datetime.now(timezone.utc).isoformat()
BANK = "11111111-2222-3333-4444-555555555555"


def sample_bank(**patches):
    raw = dict(bank_line_id=BANK, division=3977752, status="invoice_missing",
               reference="TD12345", currency="EUR", amount="90.99", account_code="100100",
               journal_code="26", reason="Eigen factuur nog niet aangetroffen")
    raw.update(patches)
    return bank_case(BANK, raw, NOW)


class ProjectionTest(unittest.TestCase):
    def test_missing_remainder_is_not_inferred_from_payment(self):
        item = sample_bank()
        self.assertEqual(item["amount"], "90.99")
        self.assertIsNone(item["remaining_amount"])
        self.assertIsNone(item["invoice_amount"])

    def test_signed_remainder_and_currency_preserved(self):
        item = sample_bank(currency="USD", remaining_bank_amount_signed="-20.12", invoice_open_amount_signed="5.01")
        self.assertEqual(item["currency"], "USD")
        self.assertEqual(item["remaining_amount"], "-20.12")
        self.assertEqual(item["invoice_remaining"], "5.01")
        self.assertIsNone(money("NaN"))
        self.assertIsNone(money("Infinity"))

    def test_no_foreign_division_or_secret_payload_exposed(self):
        self.assertIsNone(bank_case(BANK, {"division":9999}, NOW))
        item = sample_bank(payload={"credential":"not-for-dashboard"}, secret="not-for-dashboard")
        self.assertNotIn("not-for-dashboard", str(item))

    def test_scans_with_same_evidence_have_same_fingerprint(self):
        first = sample_bank()
        second = copy.deepcopy(first)
        second["observed_at"] = (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
        self.assertEqual(list(finalize([first]))[0]["fingerprint"], list(finalize([second]))[0]["fingerprint"])
        second["remaining_amount"]="1.00"
        self.assertNotEqual(first["fingerprint"], list(finalize([second]))[0]["fingerprint"])

    def test_newer_verified_same_identity_suppresses_old_bank_case(self):
        bank=sample_bank()
        bank["observed_at"]="2026-01-01T00:00:00+00:00"
        plan={"created_at":NOW,"receipts":[{"offset_id":BANK,"bank_line_id":"bank-other",
              "source_order":"TD12345","state":"matched_verified","amount":"90.99","trx":"12345678"}]}
        strict=strict_cases("job",plan,NOW)[0]
        rows=list(finalize([bank,strict]))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["id"],bank["id"])
        self.assertEqual(rows[0]["status"],"resolved")
        # Equal amount and order on a different identity must never deduplicate.
        other=sample_bank(bank_line_id="other")
        other["id"]="other"
        self.assertEqual(len(list(finalize([strict,other]))),2)

    def test_unknown_outcome_is_not_resolved(self):
        plan={"created_at":NOW,"receipts":[{"offset_id":BANK,"source_order":"TD1",
              "state":"match_requested","amount":"20","trx":"12345678"}]}
        item=strict_cases("job",plan,NOW)[0]
        self.assertEqual(item["execution_status"],"uncertain")
        self.assertEqual(item["status"],"open")
        self.assertIsNone(item["remaining_amount"])

    def test_snapshot_failure_is_distinct_from_empty(self):
        service=PostgresWorklist("unused")
        with patch.object(service,"read_source",side_effect=RuntimeError("private detail")), patch.object(service,"workflow",return_value={}):
            result=service.read(DIVISION)
        self.assertFalse(result["connected"])
        self.assertFalse(result["complete"])
        self.assertTrue(all(s["status"]=="error" for s in result["sources"]))
        self.assertNotIn("private detail",str(result))

    def test_unconfigured_division_never_queries_jnp(self):
        service=PostgresWorklist("unused")
        with patch.object(service,"read_source") as read:
            self.assertFalse(service.read("999999")["connected"])
            read.assert_not_called()
        with patch.dict("os.environ",{"EXACT_DIVISION":"999999"}),patch.object(service,"read_source") as read:
            self.assertFalse(service.read(DIVISION)["connected"])
            read.assert_not_called()

    def test_changed_source_reopens_saved_decision(self):
        service=PostgresWorklist("unused")
        item=sample_bank()
        with patch.object(service,"read_source",side_effect=lambda s:([item] if s=="bank" else [],{"status":"ready","observed_at":NOW})),patch.object(service,"workflow",return_value={item["id"]:({"status":"decided","fingerprint":"old","decision":"await_import"},3)}):
            result=service.read(DIVISION)
        self.assertEqual(result["items"][0]["status"],"open")
        self.assertTrue(result["items"][0]["source_changed"])
        self.assertEqual(result["items"][0]["revision"],3)

    def test_stale_source_revision_is_rejected_before_writing(self):
        service=PostgresWorklist("unused")
        item=list(finalize([sample_bank()]))[0]
        item["editable"]=True
        with patch.object(service,"read",return_value={"items":[item]}),patch.object(service,"connect") as connect:
            with self.assertRaises(Conflict):
                service.change(DIVISION,item["id"],{"oid":USER,"name":"Test"},"claim","","",0,"old")
            connect.assert_not_called()


class WorklistAccessTest(unittest.TestCase):
    sign_in = auth_tests.DashboardAuthTest.sign_in

    def setUp(self):
        auth_tests.DashboardAuthTest.setUp(self)
        self.path="/dashboard/api/divisions/3977752/worklist/"+"a"*32
        self.body={"action":"decide","decision":"await_import","note":"Factuur gevraagd", "revision":0,"fingerprint":"b"*64}

    def headers(self):
        return {"Origin":ORIGIN,"X-CSRF-Token":self.client.get('/dashboard/api/me').json()['csrf']}

    def test_viewer_cannot_save_even_with_valid_csrf(self):
        self.sign_in()
        self.settings=replace(self.settings,access={USER:{"role":"viewer","divisions":{"3977752":"JNP"}}})
        self.assertEqual(self.client.post(self.path,json=self.body,headers=self.headers()).status_code,403)

    def test_role_and_csrf_and_division_checked_before_save(self):
        self.sign_in()
        self.assertEqual(self.client.post(self.path,json=self.body).status_code,403)
        headers=self.headers()
        self.assertEqual(self.client.post(self.path.replace('3977752','999999'),json=self.body,headers=headers).status_code,403)
        headers['Origin']='https://attacker.example'
        self.assertEqual(self.client.post(self.path,json=self.body,headers=headers).status_code,403)

    def test_valid_save_reports_no_financial_execution(self):
        self.sign_in()
        response=self.client.post(self.path,json=self.body,headers=self.headers())
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['financial_execution_started'])

    def test_arbitrary_financial_payload_and_huge_body_refused(self):
        self.sign_in()
        self.assertEqual(self.client.post(self.path,json={**self.body,'execute':True},headers=self.headers()).status_code,400)
        self.assertEqual(self.client.post(self.path,json={**self.body,'note':'x'*20001},headers=self.headers()).status_code,413)


class WorkflowPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.saved = None
        self.events = []
        self.item = list(finalize([sample_bank()]))[0]
        self.item['editable'] = True
        owner = self

        class Cursor:
            def __init__(self, value=None): self.value=value
            def fetchone(self): return self.value

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params=None):
                import json
                if sql.startswith('SELECT state,revision'):
                    return Cursor(owner.saved)
                if sql.startswith('INSERT INTO dashboard_worklist_cases'):
                    owner.saved = (json.loads(params[2]), params[3])
                if sql.startswith('INSERT INTO dashboard_worklist_events'):
                    owner.events.append(json.loads(params[3]))
                return Cursor()
        self.connection = Connection()

    def service(self):
        service=PostgresWorklist('test-only')
        service.connect=lambda: self.connection
        service.read=lambda _: {'items':[self.item]}
        return service

    def test_decision_survives_new_service_and_keeps_evidence(self):
        actor={'oid':USER,'name':'Test reviewer'}
        service=self.service()
        result=service.change(DIVISION,self.item['id'],actor,'decide','Factuur opgevraagd','request_invoice',0,self.item['fingerprint'])
        self.assertFalse(result['financial_execution_started'])
        self.assertEqual(self.saved[0]['status'],'decided')
        self.assertEqual(self.saved[0]['decision'],'request_invoice')
        self.assertEqual(self.events[0]['source_snapshot']['reference'],'TD12345')
        self.assertEqual(self.events[0]['actor_oid'],USER)
        self.assertEqual(self.events[0]['execution_status'],'not_started')
        self.service().change(DIVISION,self.item['id'],actor,'claim','','',1,self.item['fingerprint'])
        self.assertEqual(self.saved[0]['assigned_to'],USER)
        self.assertEqual(len(self.events),2)
        self.assertEqual(self.saved[1],2)

    def test_second_editor_cannot_overwrite_first(self):
        actor={'oid':USER,'name':'Test reviewer'}
        service=self.service()
        service.change(DIVISION,self.item['id'],actor,'wait','Wacht op factuur','',0,self.item['fingerprint'])
        with self.assertRaises(Conflict):
            service.change(DIVISION,self.item['id'],actor,'decide','Tweede editor','await_import',0,self.item['fingerprint'])
        self.assertEqual(self.saved[0]['status'],'waiting')
        self.assertEqual(len(self.events),1)


if __name__ == '__main__':
    unittest.main()
