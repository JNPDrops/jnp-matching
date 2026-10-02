"""One-shot Exact-only debtor correction. Default action is a read-only plan.

SalesEntries.PaymentCondition is authoritative. This is not a payment, matching,
reimport, recurring job, web endpoint, or a change to the collective debtor.
"""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse
from uuid import UUID

import httpx

DIVISION = 3977752
SOURCE = "100100"
DESTINATION = "109372"
BASE = "https://start.exactonline.nl"
VERSION = 1
RESOURCES = {
    "crm/Accounts", "cashflow/PaymentConditions", "salesentry/SalesEntries",
    "salesentry/SalesEntryLines", "financialtransaction/TransactionLines",
    "cashflow/Receivables", "read/financial/ReceivablesList",
}
HEADER = "EntryID,Customer,EntryNumber,InvoiceNumber,EntryDate,DueDate,YourRef,Description,PaymentCondition,PaymentReference,Journal,Currency,AmountDC,AmountFC,VATAmountDC,VATAmountFC,Rate,Status,Type,Reversal,Modified"
LINES = "ID,EntryID,LineNumber,Description,GLAccount,AmountDC,AmountFC,VATAmountDC,VATAmountFC,VATBaseAmountDC,VATBaseAmountFC,VATCode,VATPercentage,Quantity,CostCenter,CostUnit,Project"
TRANSACTIONS = "ID,EntryID,LineNumber,Account,AmountDC,AmountFC,AmountVATFC,AmountVATBaseFC,Description,GLAccount,GLAccountCode,Currency,EntryNumber,InvoiceNumber,YourRef,PaymentReference,Date,DueDate,JournalCode,Status,Type,VATCode,VATPercentage,OffsetID"
CASHFLOW = "ID,EntryID,TransactionEntryID,TransactionID,Account,AccountCode,EntryNumber,InvoiceNumber,YourRef,AmountDC,AmountFC,TransactionAmountDC,TransactionAmountFC,Currency,IsFullyPaid,Status,TransactionStatus,TransactionType,TransactionIsReversal,LastPaymentDate,PaymentCondition,PaymentConditionDescription,PaymentMethod,PaymentReference,DueDate"
OPEN = "HID,AccountId,AccountCode,EntryNumber,InvoiceNumber,YourRef,Amount,AmountInTransit,CurrencyCode,InvoiceDate,JournalCode"


class Stop(RuntimeError):
    """Safe, deliberately non-sensitive failure message."""


def require(ok, message):
    if not ok:
        raise Stop(message)


def amount(value):
    require(value is not None and not isinstance(value, bool), "Missing/invalid amount")
    try:
        result = Decimal(str(value))
        require(result.is_finite(), "Non-finite amount")
        return result
    except (InvalidOperation, ValueError):
        raise Stop("Invalid amount") from None


def guid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise Stop("Invalid Exact identifier") from None


def quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def private_write(path, data):
    # Never overwrite a previous plan or audit. No credentials enter these files.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())


class Exact:
    def __init__(self, app_module):
        require(app_module.DIVISION == DIVISION and app_module.BASE_URL == BASE,
                "Wrong Exact administration or host")
        require(app_module.COLLECTIVE_DEBTOR_CODE == SOURCE, "Collective debtor changed")
        self.app = app_module
        self.last_request = 0.0

    async def request(self, method, url, params=None, payload=None):
        await asyncio.sleep(max(0, 1.2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            token = await self.app._access_token()
            async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
                response = await client.request(method, url, params=params, json=payload,
                    headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        except Exception:
            # PUT is never retried, including ambiguous network outcomes.
            raise Stop(f"Exact {method} transport/auth failure; inspect audit before retrying") from None
        require(response.status_code in ((200,) if method == "GET" else (200, 204)),
                f"Exact {method} HTTP {response.status_code}; response suppressed; no retry")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise Stop("Invalid Exact JSON; no retry") from None

    async def rows(self, resource, params=None):
        require(resource in RESOURCES, "Read resource is not allowed")
        root = f"{BASE}/api/v1/{DIVISION}/{resource}"
        url, seen, rows = root, set(), []
        for _ in range(1000):
            require(url not in seen, "Repeated pagination URL")
            seen.add(url)
            u = urlparse(url)
            require(u.scheme == "https" and u.netloc == "start.exactonline.nl"
                    and u.path == urlparse(root).path, "Unsafe pagination URL")
            p = await self.request("GET", url, params=params)
            require(isinstance(p, dict) and "d" in p, "Invalid Exact collection")
            d = p["d"]
            batch = d.get("results") if isinstance(d, dict) else d
            require(isinstance(batch, list), "Invalid Exact rows")
            rows.extend({k: v for k, v in r.items() if k != "__metadata"} for r in batch)
            url = p.get("__next") or (d.get("__next") if isinstance(d, dict) else None)
            if not url:
                return rows
            require(bool(batch), "Empty intermediate page")
            params = None
        raise Stop("Incomplete pagination")

    async def change_customer(self, entry_id, destination_id):
        # The only financial write in this module. Never falls back to another API.
        url = f"{BASE}/api/v1/{DIVISION}/salesentry/SalesEntries(guid'{guid(entry_id)}')"
        await self.request("PUT", url, payload={"Customer": guid(destination_id)})


async def context(api):
    accounts = {}
    for code in (SOURCE, DESTINATION):
        rows = await api.rows("crm/Accounts", {"$filter": "Code eq " + quoted(code.rjust(18)),
                            "$select": "ID,Code,Name,IsSales,Status"})
        require(len(rows) == 1 and rows[0]["Code"].strip() == code
                and rows[0]["IsSales"] is True and rows[0]["Status"] == "C",
                f"Existing active sales debtor {code} missing or ambiguous")
        accounts[code] = {k: rows[0][k] for k in ("ID", "Code", "Name")}
        accounts[code]["ID"] = guid(accounts[code]["ID"])
    require(accounts[SOURCE]["ID"] != accounts[DESTINATION]["ID"], "Debtors must differ")
    conditions = await api.rows("cashflow/PaymentConditions", {"$select": "Code,Description,PaymentMethod"})
    bacs = [r for r in conditions if r["Description"] == "bacs"]
    require(len(bacs) == 1 and bacs[0]["PaymentMethod"] == "B", "Exact bacs condition missing or ambiguous")
    return {"accounts": accounts, "condition": bacs[0]}


async def snapshot(api, entry_id):
    entry_id = guid(entry_id)
    headers = await api.rows("salesentry/SalesEntries", {"$filter": f"EntryID eq guid'{entry_id}'", "$select": HEADER})
    require(len(headers) == 1, "Sales entry missing or ambiguous")
    h = headers[0]
    result = {"header": h}
    for name, resource, fields, key in (
        ("lines", "salesentry/SalesEntryLines", LINES, "EntryID"),
        ("transactions", "financialtransaction/TransactionLines", TRANSACTIONS, "EntryID"),
        ("cashflow", "cashflow/Receivables", CASHFLOW, "TransactionEntryID"),
    ):
        rows = await api.rows(resource, {"$filter": f"{key} eq guid'{entry_id}'", "$select": fields})
        require(len({r["ID"] for r in rows}) == len(rows), "Duplicate Exact row")
        result[name] = sorted(rows, key=lambda r: r["ID"])
    result["open"] = await api.rows("read/financial/ReceivablesList", {
        "$filter": f"EntryNumber eq {int(h['EntryNumber'])} and YourRef eq {quoted(h['YourRef'])}", "$select": OPEN})
    result["related"] = await api.rows("salesentry/SalesEntries", {
        "$filter": "YourRef eq " + quoted(h["YourRef"]), "$select": "EntryID,Customer,EntryNumber,AmountFC,Type,Reversal"})
    return result


def eligible(s, ctx):
    """Conservative executor subset; all other open bacs sales entries are reported."""
    h = s["header"]
    source_id = ctx["accounts"][SOURCE]["ID"]
    require(h["Customer"] == source_id, "Debtor is no longer 100100")
    require(h["PaymentCondition"] == ctx["condition"]["Code"], "Sales entry is not bacs")
    ref = h["YourRef"]
    require(isinstance(ref, str) and re.fullmatch(r"TD[0-9]{4,10}", ref), "Unproven webshop reference")
    require(h["Description"] == "Order TD #" + ref[2:], "Webshop description does not match reference")
    require(h["Status"] == 20 and h["Type"] == 20 and h["Reversal"] is False, "Processed/credit/reversal sales entry")
    require(len(s["related"]) == 1 and s["related"][0]["EntryID"] == h["EntryID"], "Multiple/credit entries for order")
    require(len(s["open"]) == 1 and len(s["cashflow"]) == 1, "Paid, split, matched or ambiguous receivable")
    o, c = s["open"][0], s["cashflow"][0]
    require(o["AccountId"] == source_id and o["AccountCode"].strip() == SOURCE
            and c["Account"] == source_id, "Debtor fields disagree")
    require(o["YourRef"] == ref == c["YourRef"] and o["InvoiceNumber"] == h["InvoiceNumber"] == c["InvoiceNumber"], "Invoice identifiers disagree")
    require(o["CurrencyCode"] == h["Currency"] == c["Currency"], "Currency mismatch")
    require(c["IsFullyPaid"] is False and c["Status"] == 20 and c["TransactionStatus"] == 20
            and c["TransactionType"] == 20 and c["TransactionIsReversal"] is False
            and c["LastPaymentDate"] is None, "Payment/matching/processed state requires review")
    require(amount(o["AmountInTransit"]) == 0, "Amount in transit")
    remaining = amount(o["Amount"])
    require(remaining > 0 and remaining == amount(h["AmountFC"]) == amount(c["TransactionAmountFC"])
            and remaining == -amount(c["AmountFC"]), "Partial payment, credit or amount mismatch")
    require(s["lines"] and s["transactions"], "Missing sales/financial lines")
    require(amount(h["AmountDC"]) == amount(c["TransactionAmountDC"]) == -amount(c["AmountDC"]), "Default currency amount mismatch")
    require(all(r["Account"] in (None, source_id) and r["OffsetID"] is None for r in s["transactions"]), "Other account or linked transaction requires review")
    require(all(r["Status"] == 20 and r["Type"] == 20 for r in s["transactions"]), "Financial lines are not open sales lines")
    require(sum((amount(r["AmountFC"]) for r in s["transactions"]), Decimal(0)) == 0,
            "Financial entry is not balanced")
    require(any(r["ID"] == c["TransactionID"] and r["Account"] == source_id for r in s["transactions"]), "Receivable transaction line missing")
    return {"entry_id": h["EntryID"], "entry_number": h["EntryNumber"], "reference": ref,
            "currency": h["Currency"], "remaining": str(remaining),
            "sales_condition": h["PaymentCondition"], "receivable_condition": c["PaymentCondition"]}


async def balances(api, ctx):
    ids = [ctx["accounts"][code]["ID"] for code in (SOURCE, DESTINATION)]
    rows = await api.rows("read/financial/ReceivablesList", {
        "$filter": " or ".join(f"AccountId eq guid'{i}'" for i in ids), "$select": OPEN})
    require(all(r["AccountId"] in ids for r in rows), "Balance query returned another debtor")
    require(len({r["HID"] for r in rows}) == len(rows), "Duplicate open balance row")
    return sorted(rows, key=lambda r: r["HID"])


def check_balances(before, after, moved_ids, ctx):
    # Complete source+destination population, not only the selected balance.
    expected = copy.deepcopy(before)
    wanted = {(i["entry_number"], i["reference"]) for i in moved_ids}
    for r in expected:
        if (r["EntryNumber"], r["YourRef"]) in wanted:
            require(r["AccountId"] == ctx["accounts"][SOURCE]["ID"], "Unexpected starting debtor")
            r["AccountId"] = ctx["accounts"][DESTINATION]["ID"]
            r["AccountCode"] = DESTINATION
        else:
            r["AccountCode"] = r["AccountCode"].strip()
    actual = copy.deepcopy(after)
    for r in actual:
        r["AccountCode"] = r["AccountCode"].strip()
    require(expected == actual, "Global open-post population changed; inspect audit for concurrent activity")


def check_after(before, after, ctx):
    source_id = ctx["accounts"][SOURCE]["ID"]
    target_id = ctx["accounts"][DESTINATION]["ID"]
    expected = copy.deepcopy(before)
    expected["header"]["Customer"] = target_id
    # Audit timestamps are not business invariants.
    expected["header"].pop("Modified", None)
    actual = copy.deepcopy(after)
    actual["header"].pop("Modified", None)
    for r in expected["transactions"]:
        if r["Account"] == source_id:
            r["Account"] = target_id
    for r in expected["cashflow"]:
        r["Account"] = target_id
        r["AccountCode"] = DESTINATION
    for r in actual["cashflow"]:
        r["AccountCode"] = r["AccountCode"].strip()
    for r in expected["open"]:
        r["AccountId"] = target_id
        r["AccountCode"] = DESTINATION
    for r in actual["open"]:
        r["AccountCode"] = r["AccountCode"].strip()
    expected["related"][0]["Customer"] = target_id
    require(expected == actual, "Post-write mismatch: stop and inspect audit; no automatic rollback")


async def plan(api):
    ctx = await context(api)
    source_id = ctx["accounts"][SOURCE]["ID"]
    headers = await api.rows("salesentry/SalesEntries", {
        "$filter": f"Customer eq guid'{source_id}' and PaymentCondition eq {quoted(ctx['condition']['Code'])}", "$select": HEADER})
    require(len({h["EntryID"] for h in headers}) == len(headers), "Duplicate sales entry")
    result = {"version": VERSION, "division": DIVISION, "source": SOURCE, "destination": DESTINATION,
              "created_at": utcnow(), "context": ctx, "eligible": [], "review": [], "paid_skipped": []}
    for h in sorted(headers, key=lambda r: r["EntryID"]):
        s = await snapshot(api, h["EntryID"])
        if not s["open"]:
            result["paid_skipped"].append(h["EntryID"])
            continue
        try:
            item = eligible(s, ctx)
            item["snapshot"] = s
            result["eligible"].append(item)
        except Stop as e:
            result["review"].append({"entry_id": h["EntryID"], "entry_number": h["EntryNumber"], "reason": str(e)})
    result["plan_sha256"] = digest(result)
    return result


def validate_plan(p, expected_sha):
    unsigned = {k: v for k, v in p.items() if k != "plan_sha256"}
    require(p["plan_sha256"] == expected_sha == digest(unsigned), "Plan checksum mismatch")
    require((p["version"], p["division"], p["source"], p["destination"]) == (VERSION, DIVISION, SOURCE, DESTINATION), "Wrong plan scope")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["created_at"])).total_seconds()
    require(0 <= age <= 1800, "Plan expired (30 minutes)")
    require(len({i["entry_id"] for i in p["eligible"]}) == len(p["eligible"]), "Duplicate planned entry")


def append_audit(f, event):
    f.write(json.dumps({"at": utcnow(), **event}, default=str) + "\n")
    f.flush()
    os.fsync(f.fileno())


async def apply(api, p, expected_sha, audit):
    validate_plan(p, expected_sha)
    ctx = await context(api)
    require(ctx == p["context"], "Debtors or bacs condition changed")
    completed = []
    append_audit(audit, {"event": "begin", "plan_sha256": expected_sha})
    balance_before = await balances(api, ctx)
    append_audit(audit, {"event": "balances_before", "rows": balance_before})
    # Recheck ALL selected entries before the first write.
    for item in p["eligible"]:
        live = await snapshot(api, item["entry_id"])
        eligible(live, ctx)
        require(live == item["snapshot"], "Plan is stale; no write started")
    for item in p["eligible"]:
        before = await snapshot(api, item["entry_id"])
        eligible(before, ctx)
        require(before == item["snapshot"], "Entry changed before write")
        append_audit(audit, {"event": "write_intent", "entry_id": item["entry_id"], "before": before})
        try:
            await api.change_customer(item["entry_id"], ctx["accounts"][DESTINATION]["ID"])
            after = await snapshot(api, item["entry_id"])
            append_audit(audit, {"event": "read_after_write", "entry_id": item["entry_id"], "after": after})
            check_after(before, after, ctx)
        except Exception:
            append_audit(audit, {"event": "halt_inspect_outcome", "entry_id": item["entry_id"], "completed": completed})
            raise
        completed.append({k: v for k, v in item.items() if k != "snapshot"})
        append_audit(audit, {"event": "verified", "entry_id": item["entry_id"]})
    # Verify the selected population once more; reruns cannot silently double-apply.
    for item in p["eligible"]:
        check_after(item["snapshot"], await snapshot(api, item["entry_id"]), ctx)
    balance_after = await balances(api, ctx)
    append_audit(audit, {"event": "balances_after", "rows": balance_after})
    check_balances(balance_before, balance_after, completed, ctx)
    totals = {}
    for item in completed:
        totals[item["currency"]] = str(amount(totals.get(item["currency"], "0")) + amount(item["remaining"]))
    result = {"moved": completed, "preserved_open_totals": totals, "review": p["review"], "verified_at": utcnow()}
    append_audit(audit, {"event": "complete", **result})
    return result


async def run(args):
    from app import main as app_module
    api = Exact(app_module)
    # Existing Render instance only. Never creates infrastructure or alters flags.
    lock_path = Path("/tmp/jnp-bacs-debtor-transfer.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("Another bacs transfer is running") from None
        if args.action == "plan":
            result = await plan(api)
            private_write(args.output, result)
            print(json.dumps({**{k: v for k, v in result.items() if k not in ("context", "eligible")},
                "eligible": [{k: v for k, v in i.items() if k != "snapshot"} for i in result["eligible"]]}))
        else:
            p = json.loads(args.plan.read_text())
            # Existing audit file means prior attempt; never replay blindly.
            with os.fdopen(os.open(args.audit, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as audit:
                print(json.dumps(await apply(api, p, args.expect_sha256, audit)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--output", type=Path, required=True)
    a = sub.add_parser("apply")
    a.add_argument("--plan", type=Path, required=True)
    a.add_argument("--expect-sha256", required=True)
    a.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as e:
        parser.exit(2, (str(e) if isinstance(e, Stop) else "Operation stopped; inspect local audit. Error details suppressed.") + "\n")


if __name__ == "__main__":
    main()
