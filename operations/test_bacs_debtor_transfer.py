import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from operations import bacs_debtor_transfer as m

SRC = "00000000-0000-0000-0000-000000000001"
DST = "00000000-0000-0000-0000-000000000002"
ENTRY = "00000000-0000-0000-0000-000000000003"


def fixture():
    ctx = {"accounts": {m.SOURCE: {"ID": SRC}, m.DESTINATION: {"ID": DST}},
           "condition": {"Code": "ba", "Description": "bacs", "PaymentMethod": "B"}}
    s = {
        "header": {"EntryID": ENTRY, "Customer": SRC, "EntryNumber": 1, "InvoiceNumber": 1,
                   "YourRef": "TD12345", "Description": "Order TD #12345", "PaymentCondition": "ba",
                   "Status": 20, "Type": 20, "Reversal": False, "Currency": "EUR", "AmountFC": 121,
                   "AmountDC": 121, "VATAmountFC": 21, "VATAmountDC": 21, "Modified": "v1"},
        "related": [{"EntryID": ENTRY, "Customer": SRC}],
        "open": [{"HID": "1", "EntryNumber": 1, "YourRef": "TD12345", "InvoiceNumber": 1,
                  "AccountId": SRC, "AccountCode": "            100100", "Amount": 121,
                  "AmountInTransit": 0, "CurrencyCode": "EUR"}],
        "cashflow": [{"ID": "cf1", "TransactionID": "tx1", "Account": SRC,
                     "AccountCode": "            100100", "YourRef": "TD12345", "InvoiceNumber": 1,
                     "IsFullyPaid": False, "Status": 20, "TransactionStatus": 20, "TransactionType": 20,
                     "TransactionIsReversal": False, "LastPaymentDate": None, "Currency": "EUR",
                     "AmountFC": -121, "AmountDC": -121, "TransactionAmountFC": 121,
                     "TransactionAmountDC": 121, "PaymentCondition": "PP"}],
        "lines": [{"ID": "sale1", "AmountFC": 100, "VATAmountFC": 21, "VATCode": "VH"}],
        "transactions": [{"ID": "tx1", "Account": SRC, "AmountFC": 121, "Status": 20, "Type": 20, "OffsetID": None},
                         {"ID": "tx2", "Account": SRC, "AmountFC": -121, "Status": 20, "Type": 20, "OffsetID": None}],
    }
    return ctx, s


def transferred(s):
    a = copy.deepcopy(s)
    a["header"].update(Customer=DST, Modified="v2")
    for r in a["transactions"]:
        r["Account"] = DST
    a["cashflow"][0].update(Account=DST, AccountCode="            109372")
    a["open"][0].update(AccountId=DST, AccountCode="            109372")
    a["related"][0]["Customer"] = DST
    return a


def plan(ctx, s):
    p = {"version": 1, "division": m.DIVISION, "source": m.SOURCE, "destination": m.DESTINATION,
         "created_at": m.utcnow(), "context": ctx, "eligible": [{**m.eligible(s, ctx), "snapshot": s}],
         "review": [], "paid_skipped": []}
    p["plan_sha256"] = m.digest(p)
    return p


def regeneration_fixture():
    ctx, s = fixture()
    s["header"]["DueDate"] = "/Date(1790812800000)/"
    s["cashflow"][0].update(ID="00000000-0000-0000-0000-000000000004",
        EntryID="00000000-0000-0000-0000-000000000005", PaymentReference="100100/1",
        PaymentConditionDescription="Prepaid", PaymentMethod="K", DueDate=s["header"]["DueDate"])
    for i, r in enumerate(s["transactions"]):
        r.update(LineNumber=i, DueDate=s["header"]["DueDate"])
    s["transactions"][1]["AmountFC"] = -100
    s["transactions"].append({**s["transactions"][1], "ID": "txvat", "LineNumber": 9999, "AmountFC": -21})
    a = transferred(s)
    a["cashflow"][0].update(ID="00000000-0000-0000-0000-000000000006",
        EntryID="00000000-0000-0000-0000-000000000007", PaymentCondition="ba",
        PaymentConditionDescription="bacs", PaymentMethod="B", PaymentReference="109372/1")
    a["open"][0]["HID"] = "3"
    a["transactions"][2]["DueDate"] = None
    return ctx, s, a


class SelectionTests(unittest.TestCase):
    def test_observed_regeneration_requires_explicit_approval(self):
        ctx, s, a = regeneration_fixture()
        with self.assertRaises(m.Stop): m.check_after(s, a, ctx)
        m.check_after(s, a, ctx, accept_derived_changes=True)

    def test_approval_does_not_allow_business_or_linkage_changes(self):
        mutations = [
            lambda a: a["header"].update(DueDate=None),
            lambda a: a["transactions"][0].update(DueDate=None),
            lambda a: a["lines"][0].update(VATAmountFC=20),
            lambda a: a["cashflow"][0].update(TransactionID="other"),
            lambda a: a["cashflow"][0].update(PaymentReference="109372/WRONG"),
            lambda a: a["open"][0].update(YourRef="TD99999"),
            lambda a: a["open"][0].update(Amount=120),
            lambda a: a["cashflow"][0].update(ID=None),
        ]
        for mutate in mutations:
            ctx, s, a = regeneration_fixture()
            mutate(a)
            with self.assertRaises(m.Stop): m.check_after(s, a, ctx, True)

    def test_only_selected_hid_can_change_in_global_check(self):
        ctx, s, a = regeneration_fixture()
        item = m.eligible(s, ctx)
        other = {**s["open"][0], "HID": "2", "EntryNumber": 2, "YourRef": "OTHER"}
        before, after = s["open"] + [other], a["open"] + [copy.deepcopy(other)]
        m.check_balances(before, after, [item], ctx, True)
        after[1]["HID"] = "4"
        with self.assertRaises(m.Stop): m.check_balances(before, after, [item], ctx, True)
        with self.assertRaises(m.Stop): m.check_balances(before, a["open"] * 2 + [other], [item], ctx, True)

    def test_sales_header_bacs_is_authoritative_even_if_receivable_prepaid(self):
        ctx, s = fixture()
        self.assertEqual(m.eligible(s, ctx)["receivable_condition"], "PP")
        s["header"]["PaymentCondition"] = "PP"
        s["cashflow"][0]["PaymentCondition"] = "ba"
        with self.assertRaises(m.Stop):
            m.eligible(s, ctx)

    def test_paid_partial_split_credit_processed_and_unknown_are_blocked(self):
        mutations = [
            lambda s: s["open"].clear(),
            lambda s: s["open"][0].update(Amount=1),
            lambda s: s["cashflow"].append(copy.deepcopy(s["cashflow"][0])),
            lambda s: s["related"].append({"EntryID": "credit"}),
            lambda s: s["header"].update(Status=50),
            lambda s: s["header"].update(YourRef=None),
            lambda s: s["cashflow"][0].update(LastPaymentDate="a date"),
            lambda s: s["transactions"][0].update(OffsetID="linked"),
            lambda s: s["open"][0].update(AmountInTransit=1),
            lambda s: s["header"].update(Customer=DST),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                ctx, s = fixture()
                mutate(s)
                with self.assertRaises(m.Stop): m.eligible(s, ctx)

    def test_postcheck_only_allows_debtor_links_and_timestamp(self):
        ctx, s = fixture()
        m.check_after(s, transferred(s), ctx)
        for section, field, value in [("header", "VATAmountFC", 20), ("header", "PaymentCondition", "PP"),
                                      ("lines", "VATCode", "OTHER"), ("open", "Amount", 120),
                                      ("cashflow", "PaymentCondition", "ba"), ("transactions", "Account", SRC)]:
            with self.subTest(section=section, field=field):
                a = transferred(s)
                (a[section] if isinstance(a[section], dict) else a[section][0])[field] = value
                with self.assertRaises(m.Stop): m.check_after(s, a, ctx)

    def test_global_balance_detects_unrelated_change(self):
        ctx, s = fixture()
        item = m.eligible(s, ctx)
        before = s["open"] + [{**s["open"][0], "HID": "2", "EntryNumber": 2, "YourRef": "OTHER"}]
        after = transferred(s)["open"] + [copy.deepcopy(before[1])]
        m.check_balances(before, after, [item], ctx)
        after[1]["Amount"] = 0
        with self.assertRaises(m.Stop): m.check_balances(before, after, [item], ctx)

    def test_tampered_and_expired_plans_fail(self):
        ctx, s = fixture()
        p = plan(ctx, s)
        m.validate_plan(p, p["plan_sha256"])
        p["destination"] = "WRONG"
        with self.assertRaises(m.Stop): m.validate_plan(p, p["plan_sha256"])
        p = plan(ctx, s)
        p["created_at"] = "2020-01-01T00:00:00+00:00"
        p["plan_sha256"] = m.digest({k: v for k, v in p.items() if k != "plan_sha256"})
        with self.assertRaises(m.Stop): m.validate_plan(p, p["plan_sha256"])


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_regeneration_is_verified_and_logged(self):
        ctx, s, a = regeneration_fixture()
        p, api = plan(ctx, s), AsyncMock()
        with tempfile.TemporaryFile(mode="w+") as audit, \
             patch.object(m, "context", AsyncMock(return_value=ctx)), \
             patch.object(m, "balances", AsyncMock(side_effect=[s["open"], a["open"]])), \
             patch.object(m, "snapshot", AsyncMock(side_effect=[s, s, a, a])):
            result = await m.apply(api, p, p["plan_sha256"], audit, True)
            self.assertEqual(len(result["moved"]), 1)
            api.change_customer.assert_awaited_once_with(ENTRY, DST)
            audit.seek(0)
            events = [json.loads(line) for line in audit]
            self.assertTrue(events[0]["accept_exact_derived_changes"])
            self.assertEqual(events[-1]["event"], "complete")

    async def test_custom_reference_blocks_before_any_write(self):
        ctx, s, _ = regeneration_fixture()
        s["cashflow"][0]["PaymentReference"] = "CUSTOM"
        p, api = plan(ctx, s), AsyncMock()
        with tempfile.TemporaryFile(mode="w+") as audit, \
             patch.object(m, "context", AsyncMock(return_value=ctx)), \
             patch.object(m, "balances", AsyncMock(return_value=s["open"])), \
             patch.object(m, "snapshot", AsyncMock(return_value=s)):
            with self.assertRaises(m.Stop): await m.apply(api, p, p["plan_sha256"], audit, True)
            api.change_customer.assert_not_awaited()

    async def test_success_and_replay_never_double_apply(self):
        ctx, s = fixture()
        p = plan(ctx, s)
        api = AsyncMock()
        with tempfile.TemporaryFile(mode="w+") as audit, \
             patch.object(m, "context", AsyncMock(return_value=ctx)), \
             patch.object(m, "balances", AsyncMock(side_effect=[s["open"], transferred(s)["open"]])), \
             patch.object(m, "snapshot", AsyncMock(side_effect=[s, s, transferred(s), transferred(s)])):
            result = await m.apply(api, p, p["plan_sha256"], audit)
            self.assertEqual(result["preserved_open_totals"], {"EUR": "121"})
            api.change_customer.assert_awaited_once_with(ENTRY, DST)
        api.reset_mock()
        with tempfile.TemporaryFile(mode="w+") as audit, \
             patch.object(m, "context", AsyncMock(return_value=ctx)), \
             patch.object(m, "balances", AsyncMock(return_value=transferred(s)["open"])), \
             patch.object(m, "snapshot", AsyncMock(return_value=transferred(s))):
            with self.assertRaises(m.Stop): await m.apply(api, p, p["plan_sha256"], audit)
            api.change_customer.assert_not_awaited()

    async def test_ambiguous_put_is_not_retried_and_stops(self):
        ctx, s = fixture()
        p = plan(ctx, s)
        api = AsyncMock()
        api.change_customer.side_effect = m.Stop("timeout")
        with tempfile.TemporaryFile(mode="w+") as audit, \
             patch.object(m, "context", AsyncMock(return_value=ctx)), \
             patch.object(m, "balances", AsyncMock(return_value=s["open"])), \
             patch.object(m, "snapshot", AsyncMock(return_value=s)):
            with self.assertRaises(m.Stop): await m.apply(api, p, p["plan_sha256"], audit)
            api.change_customer.assert_awaited_once()
            audit.seek(0)
            self.assertIn("halt_inspect_outcome", audit.read())

    async def test_pagination_host_escape_is_blocked(self):
        app = type("App", (), {"DIVISION": m.DIVISION, "BASE_URL": m.BASE, "COLLECTIVE_DEBTOR_CODE": m.SOURCE})()
        api = m.Exact(app)
        api.request = AsyncMock(return_value={"d": {"results": [{"ID": "a"}], "__next": "https://example.org/steal"}})
        with self.assertRaises(m.Stop): await api.rows("crm/Accounts")
        api.request.assert_awaited_once()

    async def test_writer_is_customer_only_fixed_division(self):
        app = type("App", (), {"DIVISION": m.DIVISION, "BASE_URL": m.BASE, "COLLECTIVE_DEBTOR_CODE": m.SOURCE})()
        api = m.Exact(app)
        api.request = AsyncMock()
        await api.change_customer(ENTRY, DST)
        api.request.assert_awaited_once_with("PUT", f"{m.BASE}/api/v1/3977752/salesentry/SalesEntries(guid'{ENTRY}')", payload={"Customer": DST})


if __name__ == "__main__":
    unittest.main()
