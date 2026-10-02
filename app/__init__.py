"""Temporary opt-in, GET-only Exact audit for the user's 30-day review.
No financial write routes, public data endpoints or secret exports are added.
"""
import os

ACTION = "20261002-100100-30d-v1"
if os.getenv("JNP_READONLY_AUDIT") == ACTION:
    import asyncio
    import json
    import re
    from collections import Counter
    from datetime import datetime, timezone
    from decimal import Decimal
    from urllib.parse import urljoin, urlparse
    import httpx
    from . import main as m

    ACCOUNT = "ec2af99c-809c-40e3-9057-8a31962ae1cf"
    START = "2026-09-03"
    END = "2026-10-03"
    PREFIX = "JNP_AUDIT30_V1"
    calls = 0

    def emit(kind, value):
        print(PREFIX + " " + kind + " " + json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":")), flush=True)

    def unpack(payload):
        body = payload.get("d", payload) if isinstance(payload, dict) else payload
        if isinstance(body, list):
            return body, None
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            return body["results"], body.get("__next") or payload.get("@odata.nextLink")
        raise ValueError("Expected OData collection")

    async def get(url, params=None):
        global calls
        allowed = m.API_V1 + "/3977752/"
        if not url.startswith(allowed) or urlparse(url).netloc != "start.exactonline.nl":
            raise ValueError("Read URL outside approved Exact division")
        calls += 1
        if calls > 200:
            raise ValueError("Read budget exceeded; audit incomplete")
        await asyncio.sleep(0.15)
        return await asyncio.wait_for(m._request_json("GET", url, params=params), timeout=55)

    async def all_rows(path, params):
        url = m.API_V1 + "/3977752/" + path
        output, seen = [], set()
        for page in range(80):
            payload = await get(url, params)
            rows, nxt = unpack(payload)
            output.extend(rows)
            if not nxt:
                return output
            url = urljoin(url, nxt)
            params = None
            if url in seen:
                raise ValueError("Repeated OData cursor; audit incomplete")
            seen.add(url)
        raise ValueError("Pagination limit exceeded; audit incomplete")

    def date_string(value):
        match = re.search(r"/Date\((-?\d+)", str(value))
        if match:
            return datetime.fromtimestamp(int(match.group(1))/1000, timezone.utc).date().isoformat()
        return str(value)[:10]

    def candidate_reference(description):
        text = str(description or "")
        if re.search(r"PAYNETICS|ICEPAY|MOLLIE|STRIPE|PAYPAL|ADYEN|JNPTEST", text, re.I):
            return None, "OTHER_METHOD_OR_TEST"
        explicit = set(re.findall(r"\bTD\s*#?\s*(\d{4,10})\b", text, re.I))
        if len(explicit) == 1:
            return "TD" + next(iter(explicit)), "EXPLICIT_TD"
        if len(explicit) > 1:
            return None, "MULTIPLE_ORDERS"
        numbers = set(re.findall(r"(?<![A-Za-z0-9])(\d{4,10})(?![A-Za-z0-9])", text))
        if len(numbers) == 1:
            return "TD" + next(iter(numbers)), "NUMERIC_CANDIDATE"
        return None, "AMBIGUOUS_OR_MISSING_ORDER"

    async def audit():
        began = datetime.now(timezone.utc).isoformat()
        try:
            if m.DIVISION != 3977752 or m.COLLECTIVE_DEBTOR_CODE != "100100":
                raise ValueError("Wrong administration or debtor")
            if m.ENABLE_ORDER_RULE_WRITES or m.ENABLE_DIRECT_MATCH_WRITES:
                raise ValueError("Financial write flags must remain false")
            account = await all_rows("crm/Accounts", {"$filter": "ID eq guid'"+ACCOUNT+"'", "$select":"ID,Code,Name", "$top":"1"})
            if len(account) != 1 or str(account[0].get("Code") or "").strip() != "100100":
                raise ValueError("Debtor GUID verification failed")
            emit("START", {"started_at":began,"division":3977752,"account_code":"100100","account_name":account[0].get("Name"),"from":START,"until_exclusive":END,"date_basis":"bank booking Date","exact_financial_writes":0})
            banks = await all_rows("financialtransaction/BankEntryLines", {
                "$filter":"Account eq guid'"+ACCOUNT+"' and Date ge datetime'"+START+"T00:00:00' and Date lt datetime'"+END+"T00:00:00'",
                "$select":"ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,GLAccountCode,OurRef,Modified",
                "$orderby":"Date,ID", "$top":"1000"})
            ids = set()
            for bank in banks:
                if str(bank.get("Account") or "").lower() != ACCOUNT or not START <= date_string(bank.get("Date")) < END:
                    raise ValueError("Server bank filter returned out-of-scope data")
                if bank["ID"] in ids:
                    raise ValueError("Duplicate bank ID in paginated data")
                ids.add(bank["ID"])
                bank.pop("__metadata", None)
            emit("BANKS_FETCHED", {"count":len(banks),"read_calls":calls,"date_filter_complete":True})
            sample = await all_rows("cashflow/Receivables", {"$filter":"Account eq guid'"+ACCOUNT+"'", "$top":"1"})
            if not sample:
                raise ValueError("No cashflow schema sample for debtor")
            keys = sorted(k for k in sample[0] if k != "__metadata")
            wanted = "ID,Account,AccountCode,AmountDC,AmountFC,Currency,Date,Description,EntryNumber,InvoiceNumber,YourRef,TransactionID,TransactionEntryID,Status,TransactionType,TransactionStatus,Source,PaymentReference,Modified,DueDate,EndDate,TransactionAmountDC,TransactionAmountFC,TransactionIsReversal".split(",")
            selected = [k for k in wanted if k in keys]
            if "TransactionID" not in selected or "YourRef" not in selected:
                raise ValueError("Cashflow schema lacks audit linkage fields")
            emit("CASHFLOW_SCHEMA", {"keys":keys,"selected":selected})
            linked = {}
            for offset in range(0, len(banks), 15):
                batch = banks[offset:offset+15]
                batch_ids = {str(b["ID"]).lower() for b in batch}
                terms = " or ".join("TransactionID eq guid'"+str(b["ID"])+"'" for b in batch)
                rows = await all_rows("cashflow/Receivables", {"$filter":"Account eq guid'"+ACCOUNT+"' and ("+terms+")", "$select":",".join(selected), "$top":"1000"})
                for row in rows:
                    key = str(row.get("TransactionID") or "").lower()
                    if key not in batch_ids or str(row.get("Account") or "").lower() != ACCOUNT:
                        raise ValueError("Cashflow filter returned unrelated receipt")
                    row.pop("__metadata", None)
                    linked.setdefault(key, []).append(row)
                emit("PROGRESS", {"bank_rows_examined":min(offset+15,len(banks)),"total":len(banks),"cashflow_rows":sum(len(v) for v in linked.values())})
            counts = Counter()
            report = []
            for index, bank in enumerate(banks, 1):
                cf = linked.get(str(bank["ID"]).lower(), [])
                expected, evidence = candidate_reference(bank.get("Description"))
                actual = sorted({str(c.get("YourRef") or "").strip() for c in cf if str(c.get("YourRef") or "").strip()})
                if evidence == "OTHER_METHOD_OR_TEST":
                    verdict = "OTHER_METHOD_OR_TEST"
                elif Decimal(str(bank.get("AmountDC") or 0)) <= 0:
                    verdict = "REFUND_OR_OTHER_DEBIT"
                elif not expected:
                    verdict = "NO_UNIQUE_ORDER"
                elif not cf:
                    verdict = "NO_CASHFLOW_LINK"
                elif len(cf) != 1 or len(actual) != 1:
                    verdict = "NO_UNIQUE_MATCHED_REFERENCE"
                elif actual[0].upper() == expected.upper():
                    verdict = "REFERENCE_EQUAL"
                else:
                    verdict = "REFERENCE_DIFFERENCE"
                counts[verdict] += 1
                row = {"n":index,"bank":bank,"cashflow":cf,"expected_ref_candidate":expected,"order_evidence":evidence,"actual_your_refs":actual,"result":verdict}
                report.append(row)
                emit("ROW", row)
            emit("COMPLETE", {"from":START,"until_exclusive":END,"started_at":began,"finished_at":datetime.now(timezone.utc).isoformat(),"bank_count":len(banks),"cashflow_rows":sum(len(v) for v in linked.values()),"counts":dict(counts),"bank_amount_dc_total":str(sum((Decimal(str(b.get("AmountDC") or 0)) for b in banks),Decimal(0))),"read_calls":calls,"all_bank_pages_read":True,"all_cashflow_pages_read":True,"exact_financial_writes":0,"limitations":["Period uses bank booking date, not date of reconciliation","Numeric order candidates need interpretation","Reference equality alone does not prove full monetary settlement","Credit-note and journal-only matches not covered by bank population"]})
        except Exception as exc:
            detail = str(getattr(exc,"detail","")).replace("\n"," ")[:350] if hasattr(exc,"detail") else str(exc)[:350]
            emit("ERROR", {"error_type":type(exc).__name__,"detail":detail,"read_calls":calls,"complete":False,"exact_financial_writes":0})

    @m.app.on_event("startup")
    async def start_readonly_audit():
        async def runner():
            await asyncio.sleep(8)
            await asyncio.wait_for(audit(), timeout=600)
        m.app.state.readonly_audit_task = asyncio.create_task(runner())
