"""One-shot Exact-only debtor correction. Default action is a read-only plan.

SalesEntries.PaymentCondition is authoritative. This is not a payment, matching,
reimport, recurring job, web endpoint, or a change to the collective debtor.
"""
import argparse
import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlparse
from uuid import UUID

import httpx
import certifi

# Reuse the verified CA store across short-lived clients. Rebuilding it on every
# request caused about 24 MiB growth per 25 live requests on the Render runtime.
# This is HTTPX's documented equivalent of verify=True, not a weaker TLS policy.
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())

DIVISION = 3977752
SOURCE = "100100"
DESTINATION = "109372"  # Legacy bacs CLI default. Never change the collective debtor.
ROUTES = {"bacs": ("109372", "ba"), "plisio": ("109377", "pl"),
          "wc_fibonatix": ("109384", "fi")}
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


class ExactRequestError(Stop):
    """HTTP outcome without response bodies, URLs or credentials."""

    def __init__(self, method, status_code, limits=None):
        self.method, self.status_code = method, status_code
        self.limits = dict(limits or {})
        super().__init__(f"Exact {method} HTTP {status_code}; response suppressed; no retry")


class WritePaused(Stop):
    """Operator stopped routing before the HTTP write was sent."""


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
        self.limits = {}

    async def request(self, method, url, params=None, payload=None):
        await asyncio.sleep(max(0, 1.2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            token = await self.app._access_token()
            async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False,
                                         verify=TLS_CONTEXT) as client:
                response = await client.request(method, url, params=params, json=payload,
                    headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        except Exception:
            # PUT is never retried, including ambiguous network outcomes.
            raise Stop(f"Exact {method} transport/auth failure; inspect audit before retrying") from None
        self.limits = {name: int(response.headers[header]) for name, header in (
            ('remaining', 'x-ratelimit-remaining'), ('reset_ms', 'x-ratelimit-reset'))
            if header in response.headers and response.headers[header].isdigit()}
        if response.status_code not in ((200,) if method == "GET" else (200, 204)):
            raise ExactRequestError(method, response.status_code, self.limits)
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


def destination(ctx):
    method = ctx.get("payment_method", "bacs")
    require(method in ROUTES, "Payment method is not authorized")
    code, condition = ROUTES[method]
    require(ctx["condition"] == {"Code": condition, "Description": method, "PaymentMethod": "B"},
            "Payment method and Exact condition disagree")
    require(code in ctx["accounts"], "Destination does not match the authorized route")
    return code


async def context(api, payment_method="bacs"):
    require(payment_method in ROUTES, "Payment method is not authorized")
    target, condition_code = ROUTES[payment_method]
    accounts = {}
    for code in (SOURCE, target):
        rows = await api.rows("crm/Accounts", {"$filter": "Code eq " + quoted(code.rjust(18)),
                            "$select": "ID,Code,Name,IsSales,Status"})
        require(len(rows) == 1 and rows[0]["Code"].strip() == code
                and rows[0]["IsSales"] is True and rows[0]["Status"] == "C",
                f"Existing active sales debtor {code} missing or ambiguous")
        accounts[code] = {k: rows[0][k] for k in ("ID", "Code", "Name")}
        accounts[code]["ID"] = guid(accounts[code]["ID"])
    require(accounts[SOURCE]["ID"] != accounts[target]["ID"], "Debtors must differ")
    conditions = await api.rows("cashflow/PaymentConditions", {"$select": "Code,Description,PaymentMethod"})
    selected = [r for r in conditions if r["Description"] == payment_method]
    require(selected == [{"Code": condition_code, "Description": payment_method, "PaymentMethod": "B"}],
            "Exact payment condition missing, changed or ambiguous")
    return {"accounts": accounts, "condition": selected[0], "payment_method": payment_method}


async def route_contexts(api):
    """Resolve all fixed routes with two reads instead of repeating common reads."""
    codes = {SOURCE, *(route[0] for route in ROUTES.values())}
    rows = await api.rows('crm/Accounts', {'$filter': ' or '.join(
        'Code eq ' + quoted(code.rjust(18)) for code in sorted(codes)),
        '$select': 'ID,Code,Name,IsSales,Status'})
    require(all(r['Code'].strip() in codes for r in rows), 'Unexpected route debtor')
    accounts = {}
    for code in codes:
        selected = [r for r in rows if r['Code'].strip() == code]
        require(len(selected) == 1 and selected[0]['IsSales'] is True and selected[0]['Status'] == 'C',
                f'Existing active sales debtor {code} missing or ambiguous')
        accounts[code] = {k:selected[0][k] for k in ('ID','Code','Name')}
        accounts[code]['ID'] = guid(accounts[code]['ID'])
    require(len({r['ID'] for r in accounts.values()}) == len(codes), 'Route debtors must differ')
    conditions = await api.rows('cashflow/PaymentConditions', {'$select':'Code,Description,PaymentMethod'})
    result = {}
    for method, (target, condition) in ROUTES.items():
        selected = [r for r in conditions if r['Description'] == method]
        require(selected == [{'Code':condition,'Description':method,'PaymentMethod':'B'}],
                'Exact payment condition missing, changed or ambiguous')
        result[method] = {'accounts':{code:accounts[code] for code in (SOURCE,target)},
                          'condition':selected[0], 'payment_method':method}
    return result


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


def eligible(s, ctx, order_evidence=None):
    """Conservative executor subset; all other open bacs sales entries are reported."""
    destination(ctx)
    h = s["header"]
    source_id = ctx["accounts"][SOURCE]["ID"]
    require(h["Customer"] == source_id, "Debtor is no longer 100100")
    if order_evidence is None:
        require(h["PaymentCondition"] == ctx["condition"]["Code"], "Sales entry payment condition does not match route")
    else:
        from operations.metorik_bacs_evidence import validate_evidence
        validate_evidence(s, order_evidence, ctx.get("payment_method", "bacs"), ctx["condition"]["Code"])
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
    ids = [ctx["accounts"][code]["ID"] for code in (SOURCE, destination(ctx))]
    rows = await api.rows("read/financial/ReceivablesList", {
        "$filter": " or ".join(f"AccountId eq guid'{i}'" for i in ids), "$select": OPEN})
    require(all(r["AccountId"] in ids for r in rows), "Balance query returned another debtor")
    require(len({r["HID"] for r in rows}) == len(rows), "Duplicate open balance row")
    return sorted(rows, key=lambda r: r["HID"])


def check_balances(before, after, moved_ids, ctx, accept_derived_changes=False):
    # Complete source+destination population, not only the selected balance.
    expected = copy.deepcopy(before)
    wanted = {(i["entry_number"], i["reference"]) for i in moved_ids}
    for r in expected:
        if (r["EntryNumber"], r["YourRef"]) in wanted:
            require(r["AccountId"] == ctx["accounts"][SOURCE]["ID"], "Unexpected starting debtor")
            r["AccountId"] = ctx["accounts"][destination(ctx)]["ID"]
            r["AccountCode"] = destination(ctx)
        else:
            r["AccountCode"] = r["AccountCode"].strip()
    actual = copy.deepcopy(after)
    for r in actual:
        r["AccountCode"] = r["AccountCode"].strip()
    if accept_derived_changes:
        # Only selected rows may acquire a new HID. Match by the stable invoice key,
        # demand one row on each side, and compare every other field unchanged.
        for key in wanted:
            old = [r for r in expected if (r["EntryNumber"], r["YourRef"]) == key]
            new = [r for r in actual if (r["EntryNumber"], r["YourRef"]) == key]
            require(len(old) == len(new) == 1, "Ambiguous selected balance row")
            require(str(new[0]["HID"]).isdigit() and int(new[0]["HID"]) > 0, "Invalid new HID")
            old[0]["HID"] = new[0]["HID"]
    require(sorted(expected, key=lambda r: r["HID"]) == sorted(actual, key=lambda r: r["HID"]),
            "Global open-post population changed; inspect audit for concurrent activity")


def validate_derived_source(s, ctx, order_evidence=None):
    """Preflight for the explicitly approved, observed Exact regeneration only."""
    eligible(s, ctx, order_evidence)
    h, c = s["header"], s["cashflow"][0]
    condition = (c["PaymentCondition"], c["PaymentConditionDescription"], c["PaymentMethod"])
    require(condition in (("PP", "Prepaid", "K"),
                          (ctx["condition"]["Code"], ctx["condition"]["Description"], ctx["condition"]["PaymentMethod"])), "Unapproved source payment condition")
    require(c["PaymentReference"] == f"{SOURCE}/{h['EntryNumber']}", "Custom payment reference requires review")
    for r in s["transactions"]:
        if r["LineNumber"] == 9999:
            require(r["DueDate"] in (None, h["DueDate"])
                    and amount(r["AmountFC"]) == -amount(h["VATAmountFC"]),
                    "Unrecognized VAT summary line")


def check_after(before, after, ctx, accept_derived_changes=False, order_evidence=None):
    source_id = ctx["accounts"][SOURCE]["ID"]
    target_id = ctx["accounts"][destination(ctx)]["ID"]
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
        r["AccountCode"] = destination(ctx)
    for r in actual["cashflow"]:
        r["AccountCode"] = r["AccountCode"].strip()
    for r in expected["open"]:
        r["AccountId"] = target_id
        r["AccountCode"] = destination(ctx)
    for r in actual["open"]:
        r["AccountCode"] = r["AccountCode"].strip()
    expected["related"][0]["Customer"] = target_id
    if accept_derived_changes:
        validate_derived_source(before, ctx, order_evidence)
        require(len(actual["cashflow"]) == len(actual["open"]) == 1, "Regenerated receivable is ambiguous")
        c, a = expected["cashflow"][0], actual["cashflow"][0]
        for key in ("ID", "EntryID"):
            guid(a[key])
            c[key] = a[key]
        condition = ctx["condition"]
        if order_evidence is not None and before["header"]["PaymentCondition"] == "PP":
            condition = ctx["metorik_prepaid_condition"]
        c.update(PaymentCondition=condition["Code"],
                 PaymentConditionDescription=condition["Description"], PaymentMethod=condition["PaymentMethod"],
                 PaymentReference=f"{destination(ctx)}/{before['header']['EntryNumber']}")
        hid = actual["open"][0]["HID"]
        require(str(hid).isdigit() and int(hid) > 0, "Invalid regenerated HID")
        expected["open"][0]["HID"] = hid
        by_id = {r["ID"]: r for r in actual["transactions"]}
        require(len(by_id) == len(actual["transactions"]), "Duplicate financial line")
        for r in expected["transactions"]:
            if r["LineNumber"] == 9999 and by_id.get(r["ID"], {}).get("DueDate") is None:
                r["DueDate"] = None
    require(expected == actual, "Post-write mismatch: stop and inspect audit; no automatic rollback")


async def plan(api, payment_method="bacs"):
    ctx = await context(api, payment_method)
    source_id = ctx["accounts"][SOURCE]["ID"]
    headers = await api.rows("salesentry/SalesEntries", {
        "$filter": f"Customer eq guid'{source_id}' and PaymentCondition eq {quoted(ctx['condition']['Code'])}", "$select": HEADER})
    require(len({h["EntryID"] for h in headers}) == len(headers), "Duplicate sales entry")
    result = {"version": VERSION, "division": DIVISION, "source": SOURCE, "destination": destination(ctx), "payment_method": payment_method,
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
    require(p.get("payment_method", "bacs") == p["context"].get("payment_method", "bacs"), "Plan payment method mismatch")
    require((p["version"], p["division"], p["source"], p["destination"]) == (VERSION, DIVISION, SOURCE, destination(p["context"])), "Wrong plan scope")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["created_at"])).total_seconds()
    require(0 <= age <= 1800, "Plan expired (30 minutes)")
    require(len({i["entry_id"] for i in p["eligible"]}) == len(p["eligible"]), "Duplicate planned entry")


def append_audit(f, event):
    if hasattr(f, "persist_event"):
        f.persist_event({"at": utcnow(), **event})
        return
    f.write(json.dumps({"at": utcnow(), **event}, default=str) + "\n")
    f.flush()
    os.fsync(f.fileno())


async def apply(api, p, expected_sha, audit, accept_derived_changes=False):
    validate_plan(p, expected_sha)
    require(p.get("payment_method", "bacs") == "bacs" or "metorik_evidence" in p,
            "This route requires live-verified webshop evidence")
    ctx = await context(api, p.get("payment_method", "bacs"))
    if "metorik_evidence" in p:
        from operations.metorik_bacs_evidence import verify_again, prepaid_context, item_evidence
        await prepaid_context(api, ctx)
        await verify_again(p["metorik_evidence"])
        for item in p["eligible"]:
            require(item.get("order_evidence") == item_evidence(p["metorik_evidence"], item["entry_id"]),
                    "Planned order evidence does not match live-verified manifest")
    else:
        require(all("order_evidence" not in item for item in p["eligible"]),
                "Order evidence requires a live-verified manifest")
    require(ctx == p["context"], "Debtors or payment condition changed")
    completed = []
    append_audit(audit, {"event": "begin", "plan_sha256": expected_sha,
                         "accept_exact_derived_changes": accept_derived_changes})
    balance_before = await balances(api, ctx)
    append_audit(audit, {"event": "balances_before", "rows": balance_before})
    # Recheck ALL selected entries before the first write.
    for item in p["eligible"]:
        live = await snapshot(api, item["entry_id"])
        eligible(live, ctx, item.get("order_evidence"))
        if accept_derived_changes:
            validate_derived_source(live, ctx, item.get("order_evidence"))
        require(live == item["snapshot"], "Plan is stale; no write started")
    for item in p["eligible"]:
        before = await snapshot(api, item["entry_id"])
        eligible(before, ctx, item.get("order_evidence"))
        if accept_derived_changes:
            validate_derived_source(before, ctx, item.get("order_evidence"))
        require(before == item["snapshot"], "Entry changed before write")
        append_audit(audit, {"event": "write_intent", "entry_id": item["entry_id"], "before": before})
        try:
            await api.change_customer(item["entry_id"], ctx["accounts"][destination(ctx)]["ID"])
            after = await snapshot(api, item["entry_id"])
            append_audit(audit, {"event": "read_after_write", "entry_id": item["entry_id"], "after": after})
            check_after(before, after, ctx, accept_derived_changes, item.get("order_evidence"))
        except Exception:
            append_audit(audit, {"event": "halt_inspect_outcome", "entry_id": item["entry_id"], "completed": completed})
            raise
        completed.append({k: v for k, v in item.items() if k not in ("snapshot", "order_evidence")})
        append_audit(audit, {"event": "verified", "entry_id": item["entry_id"]})
        print(json.dumps({"progress": "verified", "entry_number": item["entry_number"]}), flush=True)
    # Verify the selected population once more; reruns cannot silently double-apply.
    for item in p["eligible"]:
        check_after(item["snapshot"], await snapshot(api, item["entry_id"]), ctx, accept_derived_changes, item.get("order_evidence"))
    balance_after = await balances(api, ctx)
    append_audit(audit, {"event": "balances_after", "rows": balance_after})
    check_balances(balance_before, balance_after, completed, ctx, accept_derived_changes)
    totals = {}
    for item in completed:
        totals[item["currency"]] = str(amount(totals.get(item["currency"], "0")) + amount(item["remaining"]))
    result = {"moved": completed, "preserved_open_totals": totals,
              "review": [{k: v for k, v in i.items() if k != "snapshot"} for i in p["review"]], "verified_at": utcnow()}
    append_audit(audit, {"event": "complete", **result})
    return result


@contextmanager
def persistent_runner_lock(app_module):
    conn = app_module._db_connect()
    require(conn is not None, "Persistent database is required for the runner lock")
    locked = False
    try:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (3977752100100,)).fetchone()[0]
        require(locked, "Another debtor routing process is running")
        yield
    finally:
        if locked:
            conn.execute("SELECT pg_advisory_unlock(%s)", (3977752100100,))
        conn.close()


async def run(args):
    from app import main as app_module
    api = Exact(app_module)
    # Existing Render instance only. Never creates infrastructure or alters flags.
    lock_path = Path("/tmp/jnp-bacs-debtor-transfer.lock")
    with persistent_runner_lock(app_module), lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("Another bacs transfer is running") from None
        if args.action == "plan":
            if args.metorik_manifest:
                from operations.metorik_bacs_evidence import evidence_plan
                result = await evidence_plan(api, json.loads(args.metorik_manifest.read_text()), args.payment_method, args.reviewed_line_link)
            else:
                require(args.payment_method == "bacs", "This route requires verified webshop evidence")
                result = await plan(api, args.payment_method)
            private_write(args.output, result)
            print(json.dumps({**{k: v for k, v in result.items() if k not in ("context", "eligible", "review", "metorik_evidence")},
                "review": [{k: v for k, v in i.items() if k != "snapshot"} for i in result["review"]],
                "eligible": [{k: v for k, v in i.items() if k not in ("snapshot", "order_evidence")} for i in result["eligible"]]}))
        else:
            p = json.loads(args.plan.read_text())
            # Existing audit file means prior attempt; never replay blindly.
            with os.fdopen(os.open(args.audit, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as audit:
                print(json.dumps(await apply(api, p, args.expect_sha256, audit, args.accept_exact_derived_changes)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--payment-method", choices=tuple(ROUTES), default="bacs")
    p.add_argument("--reviewed-line-link", action="append", default=[],
                   help="Explicit entry ID with a separately reviewed <=2 cent difference; requires complete live product/discount/shipping evidence")
    p.add_argument("--metorik-manifest", type=Path,
                   help="Explicit approved entry/order IDs; verify bacs against live Metorik before selecting")
    a = sub.add_parser("apply")
    a.add_argument("--plan", type=Path, required=True)
    a.add_argument("--expect-sha256", required=True)
    a.add_argument("--audit", type=Path, required=True)
    a.add_argument("--accept-exact-derived-changes", action="store_true",
                   help="Explicit approval for observed receivable IDs, bacs condition, generated reference and VAT-line due-date changes")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as e:
        parser.exit(2, (str(e) if isinstance(e, Stop) else "Operation stopped; inspect local audit. Error details suppressed.") + "\n")


if __name__ == "__main__":
    # Keep Stop identity shared with the evidence module when invoked with -m.
    from operations.bacs_debtor_transfer import main as cli_main
    cli_main()
