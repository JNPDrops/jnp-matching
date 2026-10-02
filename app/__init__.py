"""Temporary opt-in GET-only snapshot for a single marker import test."""
import os

if os.getenv("JNP_MARKER_TEST_SNAPSHOT") == "20261002-A":
    import asyncio
    import json
    import re
    from datetime import datetime, timezone
    from decimal import Decimal
    from . import main as m

    def rows(payload):
        d = payload.get("d", payload) if isinstance(payload, dict) else payload
        if isinstance(d, dict) and d.get("__next"):
            raise ValueError("Truncated result; snapshot requires a narrower query")
        if isinstance(d, dict):
            d = d.get("results", [])
        if not isinstance(d, list):
            raise ValueError("Unexpected Exact response shape")
        return d

    async def snapshot():
        await asyncio.sleep(3)
        try:
            if m.DIVISION != 3977752:
                raise ValueError("Wrong division")
            iban = os.environ["JNP_MARKER_TEST_IBAN"].replace(" ", "").upper()
            if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
                raise ValueError("Invalid bank IBAN")
            journal_rows = rows(await m.exact_get("financial/Journals", {
                "$select": "Code,Type,Currency,BankAccountIBAN,IsBlocked",
                "$filter": "Type eq 12", "$top": "100"
            }))
            selected = [j for j in journal_rows if str(j.get("BankAccountIBAN") or "").replace(" ", "").upper() == iban]
            if len(selected) != 1 or selected[0].get("IsBlocked"):
                raise ValueError("Expected one usable journal for source IBAN")
            journal = selected[0]
            code = str(journal["Code"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", code):
                raise ValueError("Invalid journal code")
            raw = await m.exact_get("financialtransaction/BankEntries", {
                "$filter": f"JournalCode eq '{code}'",
                "$select": "EntryID,EntryNumber,JournalCode,Currency,ClosingBalanceFC,FinancialYear,FinancialPeriod,Modified,Status",
                "$orderby": "FinancialYear desc,EntryNumber desc", "$top": "1"
            })
            entries = m._extract_results(raw)
            if len(entries) != 1 or entries[0].get("ClosingBalanceFC") is None:
                raise ValueError("No latest closing balance found")
            latest = entries[0]
            if latest.get("Currency") != "EUR":
                raise ValueError("Expected EUR bank journal")
            balance = Decimal(str(latest["ClosingBalanceFC"]))
            if not balance.is_finite() or balance != balance.quantize(Decimal("0.01")):
                raise ValueError("Invalid closing balance")
            probe_ref = "JNPTEST261002A"
            found = rows(await m.exact_get("financialtransaction/BankEntryLines", {
                "$filter": f"substringof('{probe_ref}',Description)",
                "$select": "ID,EntryID,EntryNumber,LineNumber,Description,AmountFC,AccountCode,GLAccountCode", "$top": "100"
            }))
            rule_rows = rows(await m.exact_beta_get("cashflow/AllocationRule", {
                "$filter": "Words eq 'DIRECT_WOO_BANK'", "$select": "ID,Words,Account,GLAccount", "$top": "100"
            }))
            result = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True, "bank_imports_executed": 0,
                "journal_code": code, "bank_iban_suffix": iban[-4:],
                "latest_entry": latest, "probe_reference": probe_ref,
                "existing_probe_rows": found, "marker_rules": rule_rows,
                "order_rule_writes": m.ENABLE_ORDER_RULE_WRITES,
                "direct_match_writes": m.ENABLE_DIRECT_MATCH_WRITES
            }
            print("JNP_MARKER_TEST_SNAPSHOT " + json.dumps(result, default=str), flush=True)
        except Exception as exc:
            print("JNP_MARKER_TEST_SNAPSHOT_ERROR " + json.dumps({"error_type": type(exc).__name__, "status": getattr(exc, "status_code", None)}), flush=True)

    @m.app.on_event("startup")
    async def start_marker_test_snapshot():
        m.app.state.marker_test_snapshot_task = asyncio.create_task(snapshot())
