import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from operations import bacs_debtor_transfer as m
from operations import metorik_bacs_evidence as e
from operations.test_bacs_debtor_transfer import regeneration_fixture, ENTRY, plan


def evidence_fixture():
    ctx, s, after = regeneration_fixture()
    ctx["metorik_prepaid_condition"] = {"Code": "PP", "Description": "Prepaid", "PaymentMethod": "K"}
    s["header"].update(PaymentCondition="PP", EntryDate="/Date(1790812800000)/")
    after["header"].update(PaymentCondition="PP", EntryDate="/Date(1790812800000)/")
    after["cashflow"][0].update(PaymentCondition="PP", PaymentConditionDescription="Prepaid", PaymentMethod="K")
    ev = {"entry_id": ENTRY, "reference": "TD12345", "order_id": 7,
          "order": {"order_id": 7, "order_number": "#12345", "payment_method": "bacs",
                    "currency": "EUR", "total": 121, "total_refunds": 0, "status": "completed",
                    "order_created_at": "2026-10-01T12:00:00Z", "order_updated_at": "2026-10-01T13:00:00Z"}}
    return ctx, s, after, ev


class EvidenceTests(unittest.TestCase):
    def test_prepaid_header_requires_bacs_order_proof_and_stays_prepaid(self):
        ctx, s, after, ev = evidence_fixture()
        with self.assertRaises(m.Stop): m.eligible(s, ctx)
        m.eligible(s, ctx, ev)
        m.check_after(s, after, ctx, True, ev)
        after["header"]["PaymentCondition"] = "ba"
        with self.assertRaises(m.Stop): m.check_after(s, after, ctx, True, ev)

    def test_wrong_method_identity_date_currency_total_refund_are_rejected(self):
        changes = [{"payment_method": "plisio"}, {"order_id": 8}, {"order_number": "#99999"},
                   {"order_created_at": "2026-09-29T12:00:00Z"}, {"currency": "USD"},
                   {"total": 120}, {"total_refunds": 1}, {"status": "refunded"}]
        for change in changes:
            with self.subTest(change=change):
                ctx, s, _, ev = evidence_fixture()
                ev["order"].update(change)
                with self.assertRaises(m.Stop): m.eligible(s, ctx, ev)

    def test_partial_and_existing_paid_parts_remain_blocked(self):
        for paid_part in (False, True):
            ctx, s, _, ev = evidence_fixture()
            s["open"][0]["Amount"] = 1
            if paid_part:
                s["cashflow"].append({**s["cashflow"][0], "IsFullyPaid": True, "Status": 50, "AmountFC": 0})
            with self.assertRaises(m.Stop): m.eligible(s, ctx, ev)

    def test_manifest_rejects_ambiguous_or_expanded_scope(self):
        row = {"entry_id": ENTRY, "reference": "TD12345", "order_id": 7}
        for manifest in ([row, row], [{**row, "reference": "OTHER"}], [{**row, "destination": "109377"}]):
            with self.assertRaises(m.Stop): e.manifest_rows(manifest)

    def test_preserved_prepaid_cashflow_and_invariants(self):
        ctx, s, after, ev = evidence_fixture()
        after["cashflow"][0].update(PaymentCondition="ba", PaymentConditionDescription="bacs", PaymentMethod="B")
        with self.assertRaises(m.Stop): m.check_after(s, after, ctx, True, ev)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_reader_batches_more_than_25_order_numbers(self):
        from uuid import UUID
        import json
        _, _, _, ev = evidence_fixture()
        manifest = [{"entry_id": str(UUID(int=i)), "reference": "TD" + str(12000+i), "order_id": i} for i in range(1,28)]
        store = {"name": "TheDrops.eu", "timezone": "Europe/Amsterdam", "currency": "EUR", "platform": "woocommerce"}
        client = AsyncMock()
        client.__aenter__.return_value = client
        def respond(url, params=None):
            if params is None:
                body = store
            else:
                numbers = json.loads(params["filters"])[0]["value"]
                self.assertLessEqual(len(numbers), 25)
                self.assertTrue(all(n.isdecimal() for n in numbers))
                rows = [{**ev["order"], "order_id": r["order_id"], "order_number": "#"+r["reference"][2:]} for r in manifest if r["reference"][2:] in numbers]
                body = {"data": rows, "pagination": {"current_page": 1, "per_page": 100, "has_more_pages": False}}
            return Mock(status_code=200, json=Mock(return_value=body))
        client.get.side_effect = respond
        with patch.dict(e.os.environ, {"METORIK_API_KEY": "unit-test-only"}), \
             patch.object(e.httpx, "AsyncClient", return_value=client), \
             patch.object(e.asyncio, "sleep", AsyncMock()):
            result = await e.read_orders(manifest)
        self.assertEqual(len(result["orders"]), 27)
        self.assertEqual(client.get.await_count, 3)

    async def test_live_reader_rejects_duplicate_missing_and_wrong_order_ids(self):
        _, _, _, ev = evidence_fixture()
        manifest = [{k: ev[k] for k in ("entry_id", "reference", "order_id")}]
        store = {"name": "TheDrops.eu", "timezone": "Europe/Amsterdam", "currency": "EUR", "platform": "woocommerce"}
        cases = [[ev["order"], ev["order"]], [], [{**ev["order"], "order_id": 8}]]
        for rows in cases:
            client = AsyncMock()
            client.__aenter__.return_value = client
            body = {"data": rows, "pagination": {"current_page": 1, "per_page": 100, "has_more_pages": False}}
            client.get.side_effect = [Mock(status_code=200, json=Mock(return_value=store)),
                                      Mock(status_code=200, json=Mock(return_value=body))]
            with patch.dict(e.os.environ, {"METORIK_API_KEY": "unit-test-only"}), \
                 patch.object(e.httpx, "AsyncClient", return_value=client), \
                 patch.object(e.asyncio, "sleep", AsyncMock()):
                with self.assertRaises(m.Stop): await e.read_orders(manifest)

    async def test_live_reader_projects_order_proof_and_uses_no_payment_filter(self):
        _, _, _, ev = evidence_fixture()
        manifest = [{k: ev[k] for k in ("entry_id", "reference", "order_id")}]
        store = {"name": "TheDrops.eu", "timezone": "Europe/Amsterdam", "currency": "EUR", "platform": "woocommerce"}
        client = AsyncMock()
        client.__aenter__.return_value = client
        body = {"data": [{**ev["order"], "billing_address_email": "excluded@example.invalid"}],
                "pagination": {"current_page": 1, "per_page": 100, "has_more_pages": False}}
        client.get.side_effect = [Mock(status_code=200, json=Mock(return_value=store)),
                                  Mock(status_code=200, json=Mock(return_value=body))]
        with patch.dict(e.os.environ, {"METORIK_API_KEY": "unit-test-only"}), \
             patch.object(e.httpx, "AsyncClient", return_value=client), \
             patch.object(e.asyncio, "sleep", AsyncMock()):
            result = await e.read_orders(manifest)
        self.assertEqual(result["orders"]["#12345"], ev["order"])
        self.assertNotIn("payment_method", client.get.call_args.kwargs["params"]["filters"])

    async def test_forged_evidence_without_live_manifest_never_writes(self):
        from operations.test_bacs_debtor_transfer import fixture
        ctx, s = fixture()
        p = plan(ctx, s)
        p["eligible"][0]["order_evidence"] = {"payment_method": "bacs"}
        p["plan_sha256"] = m.digest({k: v for k, v in p.items() if k != "plan_sha256"})
        api, audit = AsyncMock(), unittest.mock.Mock()
        with patch.object(m, "context", AsyncMock(return_value=ctx)):
            with self.assertRaises(m.Stop): await m.apply(api, p, p["plan_sha256"], audit, True)
        api.change_customer.assert_not_awaited()

    async def test_changed_metorik_evidence_stops(self):
        evidence = {"manifest": [], "orders": {"#1": {"payment_method": "bacs"}}}
        with patch.object(e, "read_orders", AsyncMock(return_value={**evidence, "orders": {}})):
            with self.assertRaises(m.Stop): await e.verify_again(evidence)


if __name__ == "__main__":
    unittest.main()
