import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

BASE_URL = os.getenv("EXACT_BASE_URL", "https://start.exactonline.nl").rstrip("/")
CLIENT_ID = os.getenv("EXACT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EXACT_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("EXACT_REDIRECT_URI", "")
DIVISION = int(os.getenv("EXACT_DIVISION", "3977752"))
SUSPENSE_GL_CODE = os.getenv("SUSPENSE_GL_CODE", "1360")
COLLECTIVE_DEBTOR_CODE = os.getenv("COLLECTIVE_DEBTOR_CODE", "100100")
ORDER_REF_PREFIX = os.getenv("ORDER_REF_PREFIX", "TD")
TOKEN_STORE_PATH = Path(os.getenv("TOKEN_STORE_PATH", "./exact_tokens.json"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-me")
ENABLE_ORDER_RULE_WRITES = os.getenv("ENABLE_ORDER_RULE_WRITES", os.getenv("ENABLE_ALLOCATION_RULE_WRITES", "false")).lower() == "true"
ENABLE_DIRECT_MATCH_WRITES = os.getenv("ENABLE_DIRECT_MATCH_WRITES", "false").lower() == "true"

AUTH_URL = f"{BASE_URL}/api/oauth2/auth"
TOKEN_URL = f"{BASE_URL}/api/oauth2/token"
API_V1 = f"{BASE_URL}/api/v1"
API_BETA = f"{BASE_URL}/api/v1/beta"
MATCHSETS_URL = f"{BASE_URL}/docs/XMLUpload.aspx"

@asynccontextmanager
async def lifespan(_app):
    from operations.automatic_debtor_routing import serve
    from app import main as app_module
    task = asyncio.create_task(serve(app_module))
    from operations.woo_iban_rules import serve as serve_iban
    iban_task = asyncio.create_task(serve_iban(app_module))
    try:
        yield
    finally:
        task.cancel()
        iban_task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await iban_task


app = FastAPI(title="JNP Matching", version="1.7.0", lifespan=lifespan)
from operations.woo_iban_rules import router as woo_iban_router
app.include_router(woo_iban_router)
from operations.allocation_connection import router as allocation_router
app.include_router(allocation_router)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=False, same_site="lax")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _require_config() -> None:
    missing = [name for name, value in {
        "EXACT_CLIENT_ID": CLIENT_ID,
        "EXACT_CLIENT_SECRET": CLIENT_SECRET,
        "EXACT_REDIRECT_URI": REDIRECT_URI,
    }.items() if not value]
    if missing:
        raise HTTPException(500, f"Missing configuration: {', '.join(missing)}")


def _db_connect():
    if not DATABASE_URL:
        return None
    return psycopg.connect(DATABASE_URL, autocommit=True)


def _ensure_token_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exact_oauth_tokens (
            singleton_key TEXT PRIMARY KEY,
            token_json JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _load_tokens() -> dict[str, Any] | None:
    if DATABASE_URL:
        try:
            with _db_connect() as conn:
                _ensure_token_table(conn)
                row = conn.execute(
                    "SELECT token_json FROM exact_oauth_tokens WHERE singleton_key = %s",
                    ("exact",),
                ).fetchone()
                if not row:
                    return None
                value = row[0]
                return value if isinstance(value, dict) else json.loads(value)
        except Exception as exc:
            raise HTTPException(500, f"Persistent Exact token store unavailable: {type(exc).__name__}")
    if not TOKEN_STORE_PATH.exists():
        return None
    return json.loads(TOKEN_STORE_PATH.read_text())


def _save_tokens(tokens: dict[str, Any]) -> None:
    expires_in = int(tokens.get("expires_in", 600))
    tokens["expires_at"] = int(time.time()) + expires_in - 30
    if DATABASE_URL:
        try:
            with _db_connect() as conn:
                _ensure_token_table(conn)
                conn.execute(
                    """
                    INSERT INTO exact_oauth_tokens (singleton_key, token_json, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (singleton_key)
                    DO UPDATE SET token_json = EXCLUDED.token_json, updated_at = NOW()
                    """,
                    ("exact", json.dumps(tokens)),
                )
            return
        except Exception as exc:
            raise HTTPException(500, f"Persistent Exact token store unavailable: {type(exc).__name__}")
    TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_STORE_PATH.write_text(json.dumps(tokens, indent=2))
    try:
        os.chmod(TOKEN_STORE_PATH, 0o600)
    except OSError:
        pass


async def _refresh_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
    _require_config()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(401, "No refresh token available; reconnect Exact Online.")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Exact token refresh failed: {resp.text[:500]}")
    new_tokens = resp.json()
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = refresh_token
    _save_tokens(new_tokens)
    return new_tokens


_token_access_lock = asyncio.Lock()


async def _access_token() -> str:
    # The routing worker and the new IBAN worker share a rotating OAuth token.
    async with _token_access_lock:
        return await _access_token_unlocked()


async def _access_token_unlocked() -> str:
    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(401, "Exact Online is not connected yet. Visit /login.")
    if int(tokens.get("expires_at", 0)) <= int(time.time()):
        tokens = await _refresh_tokens(tokens)
    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(401, "Stored Exact token is invalid.")
    return access_token


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        d = payload.get("d", payload)
        if isinstance(d, dict) and isinstance(d.get("results"), list):
            return d["results"]
        if isinstance(d, list):
            return d
    return []


def _extract_entity(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        d = payload.get("d", payload)
        if isinstance(d, dict):
            return d
    return {}


async def _request_json(method: str, url: str, params=None, payload=None) -> Any:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.request(
            method,
            url,
            params=params,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code == 401:
        await _refresh_tokens(_load_tokens() or {})
        return await _request_json(method, url, params, payload)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Exact API error: {resp.text[:1200]}")
    return resp.json() if resp.text.strip() else {}


async def exact_get(path: str, params: dict[str, str] | None = None) -> Any:
    return await _request_json("GET", f"{API_V1}/{DIVISION}/{path.lstrip('/')}", params=params)


async def exact_beta_get(path: str, params: dict[str, str] | None = None) -> Any:
    return await _request_json("GET", f"{API_BETA}/{DIVISION}/{path.lstrip('/')}", params=params)


async def exact_beta_post(path: str, payload: dict[str, Any]) -> Any:
    return await _request_json("POST", f"{API_BETA}/{DIVISION}/{path.lstrip('/')}", payload=payload)


def extract_order_number(description: str | None) -> str | None:
    if not description:
        return None
    explicit = re.search(r"(?i)(?:order\s*(?:td\s*)?#?|\btd\s*#?|#)\s*(\d{4,10})\b", description)
    if explicit:
        return explicit.group(1)
    trailing = re.search(r"\b(\d{4,10})\s*$", description.strip())
    return trailing.group(1) if trailing else None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


async def find_collective_debtor() -> dict[str, Any]:
    """Resolve the Exact account GUID for the webshop collective debtor.

    In this administration, server-side filtering ReceivablesList by AccountCode
    is not reliable, while the returned rows do contain AccountId + AccountCode.
    Therefore we deliberately fetch a bounded set and filter client-side.
    This keeps the GUID dynamic and avoids hardcoding an Exact account ID.
    """

    # 1) Preferred: scan open receivables and filter locally on AccountCode.
    receivable_params = {
        "$select": "AccountId,AccountCode,AccountName,EntryNumber,YourRef",
        "$top": "1000",
    }
    receivables = _extract_results(await exact_get("read/financial/ReceivablesList", receivable_params))
    matching = [
        r for r in receivables
        if str(r.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE
        and r.get("AccountId")
    ]
    receivable_ids = {str(r.get("AccountId") or "").strip() for r in matching}
    if len(receivable_ids) == 1:
        first = matching[0]
        return {
            "ID": next(iter(receivable_ids)),
            "Code": str(first.get("AccountCode") or COLLECTIVE_DEBTOR_CODE),
            "Name": str(first.get("AccountName") or ""),
            "Source": "ReceivablesList client-side scan",
            "Evidence": f"Entry {first.get('EntryNumber')} / {first.get('YourRef')}",
        }
    if len(receivable_ids) > 1:
        raise HTTPException(409, f"Meerdere Exact account-ID's gevonden voor code {COLLECTIVE_DEBTOR_CODE} in ReceivablesList.")

    # 2) Fallback: scan recent financial transaction lines and filter locally.
    tx_params = {
        "$select": "Account,AccountCode,AccountName,EntryNumber,YourRef",
        "$top": "1000",
        "$orderby": "EntryNumber desc",
    }
    tx_rows = _extract_results(await exact_get("financialtransaction/TransactionLines", tx_params))
    tx_matching = [
        r for r in tx_rows
        if str(r.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE
        and r.get("Account")
    ]
    tx_ids = {str(r.get("Account") or "").strip() for r in tx_matching}
    if len(tx_ids) == 1:
        first = tx_matching[0]
        return {
            "ID": next(iter(tx_ids)),
            "Code": str(first.get("AccountCode") or COLLECTIVE_DEBTOR_CODE),
            "Name": str(first.get("AccountName") or ""),
            "Source": "TransactionLines client-side scan",
            "Evidence": f"Entry {first.get('EntryNumber')} / {first.get('YourRef')}",
        }
    if len(tx_ids) > 1:
        raise HTTPException(409, f"Meerdere Exact account-ID's gevonden voor code {COLLECTIVE_DEBTOR_CODE} in TransactionLines.")

    raise HTTPException(
        409,
        f"Kon Exact account {COLLECTIVE_DEBTOR_CODE} niet dynamisch naar een account-ID herleiden. "
        "Gebruik /diagnose/48451 om de brondata te controleren.",
    )


async def find_receivable(order_number: str) -> list[dict[str, Any]]:
    expected_ref = f"{ORDER_REF_PREFIX}{order_number}"
    params = {
        "$filter": f"YourRef eq '{expected_ref}'",
        "$select": "AccountId,AccountCode,AccountName,Amount,AmountInTransit,CurrencyCode,Description,EntryNumber,InvoiceDate,InvoiceNumber,JournalCode,YourRef",
    }
    rows = _extract_results(await exact_get("read/financial/ReceivablesList", params))
    debtor_rows = [r for r in rows if str(r.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE]
    return debtor_rows if debtor_rows else rows


async def bank_lines_on_suspense(limit: int = 100) -> list[dict[str, Any]]:
    params = {
        "$filter": f"GLAccountCode eq '{SUSPENSE_GL_CODE}'",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,GLAccount,GLAccountCode,GLAccountDescription,OurRef,Modified",
        "$orderby": "Modified desc",
        "$top": str(max(1, min(limit, 500))),
    }
    return _extract_results(await exact_get("financialtransaction/BankEntryLines", params))


async def allocated_bank_lines(limit: int = 100) -> list[dict[str, Any]]:
    params = {
        "$filter": f"AccountCode eq '{COLLECTIVE_DEBTOR_CODE}'",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,GLAccount,GLAccountCode,GLAccountDescription,OurRef,Modified",
        "$orderby": "Modified desc",
        "$top": str(max(1, min(limit, 500))),
    }
    return _extract_results(await exact_get("financialtransaction/BankEntryLines", params))


async def bank_line_by_id(bank_line_id: str) -> dict[str, Any]:
    params = {
        "$filter": f"ID eq guid'{bank_line_id}'",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,GLAccount,GLAccountCode,GLAccountDescription,OurRef,Modified",
        "$top": "1",
    }
    rows = _extract_results(await exact_get("financialtransaction/BankEntryLines", params))
    if len(rows) != 1:
        raise HTTPException(404, "Bankregel niet meer gevonden in Exact.")
    return rows[0]


async def transaction_lines(entry_number: int) -> list[dict[str, Any]]:
    params = {
        "$filter": f"EntryNumber eq {int(entry_number)}",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,Currency,FinancialPeriod,FinancialYear,GLAccount,GLAccountCode,GLAccountDescription,JournalCode,YourRef",
    }
    return _extract_results(await exact_get("financialtransaction/TransactionLines", params))


async def transaction_lines_by_entry_id(entry_id: str) -> list[dict[str, Any]]:
    """Read-only lookup of accounting transaction lines for one Exact entry GUID."""
    params = {
        "$filter": f"EntryID eq guid'{entry_id}'",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,Currency,FinancialPeriod,FinancialYear,GLAccount,GLAccountCode,GLAccountDescription,JournalCode,YourRef",
        "$top": "200",
    }
    return _extract_results(await exact_get("financialtransaction/TransactionLines", params))


async def bank_entry_by_id(entry_id: str) -> dict[str, Any] | None:
    """Read-only lookup of the BankEntries parent record.

    We deliberately use $top=1 without guessing a field list, because Exact's
    BankEntries field set varies across API revisions. This endpoint never writes.
    """
    params = {"$filter": f"EntryID eq guid'{entry_id}'", "$top": "1"}
    rows = _extract_results(await exact_get("financialtransaction/BankEntries", params))
    return rows[0] if rows else None


async def cashflow_receivable_by_entry(entry_number: int) -> tuple[list[dict[str, Any]], str | None]:
    """Best-effort read-only Cashflow/Receivables enrichment.

    The core diagnostic does not depend on this resource. If an Exact tenant or
    API revision rejects one of these fields, we return the error as evidence
    rather than failing the entire diagnostic.
    """
    params = {
        "$filter": f"EntryNumber eq {int(entry_number)}",
        "$select": "Account,AccountCode,AmountDC,Currency,Description,EntryNumber,GLAccount,GLAccountCode,Status,TransactionID,TransactionEntryID",
        "$top": "10",
    }
    try:
        return _extract_results(await exact_get("cashflow/Receivables", params)), None
    except HTTPException as exc:
        return [], str(exc.detail)


async def diagnose_bank_line(bank_line_id: str, receivable_entry: int | None = None) -> dict[str, Any]:
    """Consolidated GET-only diagnostic for one exact imported bank line.

    This proves identity and allocation/matching preconditions without invoking
    POST/PUT/DELETE, beta allocation writes, or MatchSets uploads.
    """
    bank = await bank_line_by_id(bank_line_id)
    entry_id = str(bank.get("EntryID") or "")
    if not entry_id:
        raise HTTPException(409, "Bankregel bevat geen EntryID; diagnose kan niet eenduidig doorgaan.")

    header = await bank_entry_by_id(entry_id)
    tx_lines = await transaction_lines_by_entry_id(entry_id)

    description = str(bank.get("Description") or "")
    order_no = extract_order_number(description)
    recs = await find_receivable(order_no) if order_no else []
    if receivable_entry is not None:
        recs = [r for r in recs if int(r.get("EntryNumber") or 0) == int(receivable_entry)]
    target_rec = recs[0] if len(recs) == 1 else None

    target_entry = int(target_rec.get("EntryNumber") or 0) if target_rec else int(receivable_entry or 0)
    cashflow_recs: list[dict[str, Any]] = []
    cashflow_error: str | None = None
    if target_entry:
        cashflow_recs, cashflow_error = await cashflow_receivable_by_entry(target_entry)

    bank_amount = money(bank.get("AmountDC"))
    same_amount_lines = [t for t in tx_lines if abs(money(t.get("AmountDC"))) == abs(bank_amount)]
    same_line_number = [t for t in tx_lines if str(t.get("LineNumber")) == str(bank.get("LineNumber"))]
    id_matches = [t for t in tx_lines if str(t.get("ID") or "").lower() == bank_line_id.lower()]

    bank_account_code = str(bank.get("AccountCode") or "").strip() or None
    bank_gl_code = str(bank.get("GLAccountCode") or "").strip() or None
    rec_account_code = str((target_rec or {}).get("AccountCode") or "").strip() or None

    receivable_gl_code = None
    if cashflow_recs:
        receivable_gl_code = str(cashflow_recs[0].get("GLAccountCode") or "").strip() or None
    if not receivable_gl_code and target_entry:
        inv_txs = await transaction_lines(target_entry)
        debtor_txs = [t for t in inv_txs if str(t.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE]
        if len(debtor_txs) == 1:
            receivable_gl_code = str(debtor_txs[0].get("GLAccountCode") or "").strip() or None

    requires_allocation = bank_account_code != COLLECTIVE_DEBTOR_CODE or bank_gl_code == SUSPENSE_GL_CODE
    same_account = bank_account_code == COLLECTIVE_DEBTOR_CODE and rec_account_code == COLLECTIVE_DEBTOR_CODE
    same_gl = bool(bank_gl_code and receivable_gl_code and bank_gl_code == receivable_gl_code)
    uniquely_addressable = len(same_amount_lines) == 1 and len(same_line_number) == 1
    eligible = bool(target_rec) and not requires_allocation and same_account and same_gl and uniquely_addressable

    return {
        "read_only": True,
        "writes_executed": False,
        "bank_line_identity": {
            "id": bank.get("ID"),
            "entry_id": bank.get("EntryID"),
            "entry_number": bank.get("EntryNumber"),
            "line_number": bank.get("LineNumber"),
            "description": description,
            "amount_dc": str(bank_amount),
        },
        "bank_header": header,
        "bank_transaction_lines": tx_lines,
        "same_amount_line_count": len(same_amount_lines),
        "same_line_number_count": len(same_line_number),
        "bank_line_id_equals_transaction_line_id": bool(id_matches),
        "order_number": order_no,
        "target_receivable": target_rec,
        "target_receivable_count": len(recs),
        "cashflow_receivables": cashflow_recs,
        "cashflow_receivables_error": cashflow_error,
        "allocation_state": {
            "account_code": bank_account_code,
            "gl_account_code": bank_gl_code,
            "requires_allocation": requires_allocation,
        },
        "matchsets_preconditions": {
            "receivable_account_code": rec_account_code,
            "receivable_gl_account_code": receivable_gl_code,
            "same_account": same_account,
            "same_gl_account": same_gl,
            "same_amount_line_count": len(same_amount_lines),
            "same_line_number_count": len(same_line_number),
            "uniquely_addressable_in_transaction_lines": uniquely_addressable,
            "eligible": eligible,
        },
        "computed_result": {
            "allocation_required": requires_allocation,
            "matchsets_eligible": eligible,
            "reason": (
                f"Bankregel staat nog op {SUSPENSE_GL_CODE} en/of heeft debiteur {COLLECTIVE_DEBTOR_CODE} nog niet toegewezen."
                if requires_allocation else
                "Bankregel voldoet aan de berekende allocatievoorwaarden; verdere write-validatie blijft apart geblokkeerd."
            ),
        },
        "writes_enabled": {
            "order_rule": ENABLE_ORDER_RULE_WRITES,
            "direct_match": ENABLE_DIRECT_MATCH_WRITES,
        },
    }


async def diagnose_receivable(order_number: str) -> dict[str, Any]:
    """Read-only diagnostic: expose the raw Exact fields for one open receivable
    and its transaction lines, so we can identify the exact account GUID/field names
    used by this administration without guessing.
    """
    expected_ref = f"{ORDER_REF_PREFIX}{order_number}"

    # Do not filter by AccountCode here. We want the raw record Exact returns for
    # the known YourRef, including every field that can identify the debtor account.
    raw_params = {
        "$filter": f"YourRef eq '{expected_ref}'",
        "$select": "AccountId,AccountCode,AccountName,Amount,AmountInTransit,CurrencyCode,Description,EntryNumber,InvoiceDate,InvoiceNumber,JournalCode,YourRef",
        "$top": "10",
    }
    raw_payload = await exact_get("read/financial/ReceivablesList", raw_params)
    rows = _extract_results(raw_payload)

    tx_rows: list[dict[str, Any]] = []
    if rows:
        entry_number = rows[0].get("EntryNumber")
        if entry_number is not None:
            tx_params = {
                "$filter": f"EntryNumber eq {int(entry_number)}",
                "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,Currency,FinancialPeriod,FinancialYear,GLAccount,GLAccountCode,GLAccountDescription,JournalCode,YourRef",
                "$top": "50",
            }
            tx_payload = await exact_get("financialtransaction/TransactionLines", tx_params)
            tx_rows = _extract_results(tx_payload)

    interesting_keys = [
        "Account", "AccountId", "AccountCode", "AccountName",
        "EntryID", "EntryNumber", "InvoiceNumber", "JournalCode",
        "GLAccount", "GLAccountCode", "GLAccountDescription",
        "YourRef", "Description", "Amount", "AmountDC", "CurrencyCode",
    ]

    def pick(d: dict[str, Any]) -> dict[str, Any]:
        return {k: d.get(k) for k in interesting_keys if k in d}

    return {
        "division": DIVISION,
        "order_number": order_number,
        "expected_ref": expected_ref,
        "receivables_count": len(rows),
        "receivables_interesting": [pick(r) for r in rows],
        "receivables_raw": rows,
        "transaction_lines_count": len(tx_rows),
        "transaction_lines_interesting": [pick(r) for r in tx_rows],
        "transaction_lines_raw": tx_rows,
        "writes_enabled": {
            "order_rule": ENABLE_ORDER_RULE_WRITES,
            "direct_match": ENABLE_DIRECT_MATCH_WRITES,
        },
    }


async def allocation_rules() -> list[dict[str, Any]]:
    # Exact exposes AllocationRule as a beta endpoint and entity name is singular.
    try:
        return _extract_results(await exact_beta_get("cashflow/AllocationRule", {"$select": "ID,Account,AccountBankAccount,GLAccount,Words,Costcenter,Costunit,VATCode"}))
    except HTTPException as exc:
        if exc.status_code == 404:
            return []
        raise


async def open_webshop_receivables(limit: int = 500) -> list[dict[str, Any]]:
    """Return open webshop receivables for the collective debtor.

    The bank description is expected to contain only the numeric WooCommerce order
    number. Exact sales entries use YourRef TD<order>. We therefore create one
    allocation rule per open order, with Words=<order number>.
    """
    params = {
        "$select": "AccountId,AccountCode,AccountName,Amount,CurrencyCode,Description,EntryNumber,InvoiceDate,InvoiceNumber,JournalCode,YourRef",
        "$top": str(max(1, min(limit, 1000))),
    }
    rows = _extract_results(await exact_get("read/financial/ReceivablesList", params))
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("AccountCode") or "").strip() != COLLECTIVE_DEBTOR_CODE:
            continue
        m = re.fullmatch(rf"{re.escape(ORDER_REF_PREFIX)}(\d{{4,10}})", str(r.get("YourRef") or "").strip(), re.I)
        if not m:
            continue
        item = dict(r)
        item["OrderNumber"] = m.group(1)
        out.append(item)
    return out


async def order_rule_status(limit: int = 500) -> dict[str, Any]:
    account = await find_collective_debtor()
    receivables = await open_webshop_receivables(limit)
    all_rules = await allocation_rules()
    account_id = str(account["ID"]).lower()
    rules_by_word: dict[str, dict[str, Any]] = {}
    for rule in all_rules:
        if str(rule.get("Account") or "").lower() != account_id:
            continue
        word = str(rule.get("Words") or "").strip()
        if word:
            rules_by_word[word] = rule
    items = []
    for rec in receivables:
        order_no = rec["OrderNumber"]
        items.append({
            "order_number": order_no,
            "your_ref": rec.get("YourRef"),
            "entry_number": rec.get("EntryNumber"),
            "amount": str(money(rec.get("Amount"))),
            "currency": rec.get("CurrencyCode") or "EUR",
            "rule_exists": order_no in rules_by_word,
            "rule_id": (rules_by_word.get(order_no) or {}).get("ID"),
            "payload": {"Account": account["ID"], "Words": order_no},
        })
    return {
        "account": account,
        "items": items,
        "open_count": len(items),
        "missing_count": sum(1 for i in items if not i["rule_exists"]),
        "all_rules_count": len(all_rules),
    }


async def create_order_rule(order_number: str) -> dict[str, Any]:
    if os.getenv('REPLACE_ORDER_RULES_WITH_IBAN', 'false').lower() == 'true':
        raise HTTPException(410, 'Ordernummerregels zijn vervangen door IBAN-regels vanuit WooCommerce.')
    if not ENABLE_ORDER_RULE_WRITES:
        raise HTTPException(403, "Order-toewijzingsregels schrijven staat op slot.")
    if not re.fullmatch(r"\d{4,10}", order_number):
        raise HTTPException(400, "Ongeldig ordernummer.")
    account = await find_collective_debtor()
    recs = await find_receivable(order_number)
    if len(recs) != 1:
        raise HTTPException(409, f"Verwacht precies 1 openstaande post voor TD{order_number}, gevonden: {len(recs)}.")
    if str(recs[0].get("AccountCode") or "").strip() != COLLECTIVE_DEBTOR_CODE:
        raise HTTPException(409, "Openstaande post staat niet op de verzameldebiteur.")
    existing = await allocation_rules()
    account_id = str(account["ID"]).lower()
    for rule in existing:
        if str(rule.get("Account") or "").lower() == account_id and str(rule.get("Words") or "").strip() == order_number:
            return {"ok": True, "created": False, "message": "Regel bestond al.", "rule": rule, "order_number": order_number}
    payload = {"Account": account["ID"], "Words": order_number}
    created = _extract_entity(await exact_beta_post("cashflow/AllocationRule", payload))
    return {"ok": True, "created": True, "message": "Order-toewijzingsregel aangemaakt.", "rule": created, "order_number": order_number}


async def bank_first_candidates(limit: int = 200) -> dict[str, Any]:
    """Read-only bank-first reconciliation.

    Start with lines that are still on the suspense account, extract a possible
    WooCommerce order number from the bank description, then look up exactly
    TD<order> in the open receivables. A MATCH_CANDIDATE requires one and only
    one receivable on the collective debtor and an exact positive amount match.
    No Exact writes are performed here.
    """
    items: list[dict[str, Any]] = []
    for bank in await bank_lines_on_suspense(limit):
        description = str(bank.get("Description") or "")
        order_no = extract_order_number(description)
        bank_amount = money(bank.get("AmountDC"))
        item: dict[str, Any] = {
            "bank_line_id": bank.get("ID"),
            "bank_entry_id": bank.get("EntryID"),
            "bank_entry": bank.get("EntryNumber"),
            "bank_line_number": bank.get("LineNumber"),
            "bank_date": bank.get("Date"),
            "bank_modified": bank.get("Modified"),
            "description": description,
            "bank_amount": str(bank_amount),
            "order_number": order_no,
            "expected_ref": f"{ORDER_REF_PREFIX}{order_no}" if order_no else None,
            "receivable_entry": None,
            "receivable_amount": None,
            "account_code": None,
            "account_name": None,
            "status": "REVIEW_NO_ORDER",
            "reason": "Geen ordernummer herkend in de bankomschrijving.",
        }

        if bank_amount <= Decimal("0.00"):
            item.update(status="SKIP_NOT_RECEIPT", reason="Geen positieve bankontvangst; valt buiten deze eerste verkoopflow.")
            items.append(item)
            continue
        if not order_no:
            items.append(item)
            continue

        recs = await find_receivable(order_no)
        if len(recs) == 0:
            item.update(status="REVIEW_NOT_FOUND", reason=f"Geen openstaande post gevonden voor {ORDER_REF_PREFIX}{order_no}.")
        elif len(recs) > 1:
            item.update(status="REVIEW_MULTIPLE", reason=f"{len(recs)} openstaande posten gevonden voor {ORDER_REF_PREFIX}{order_no}.")
        else:
            rec = recs[0]
            rec_amount = money(rec.get("Amount"))
            account_code = str(rec.get("AccountCode") or "").strip()
            item.update(
                receivable_entry=rec.get("EntryNumber"),
                receivable_amount=str(rec_amount),
                account_code=account_code,
                account_name=rec.get("AccountName") or "",
            )
            if account_code != COLLECTIVE_DEBTOR_CODE:
                item.update(status="REVIEW_ACCOUNT", reason=f"Openstaande post staat op account {account_code or 'onbekend'}, niet {COLLECTIVE_DEBTOR_CODE}.")
            elif bank_amount != rec_amount:
                item.update(status="REVIEW_AMOUNT", reason=f"Bedrag wijkt af: bank {bank_amount} vs openstaand {rec_amount}.")
            else:
                item.update(status="MATCH_CANDIDATE", reason="Unieke TD-referentie op verzameldebiteur en bedrag exact gelijk.")
        items.append(item)

    return {
        "items": items,
        "total": len(items),
        "matches": sum(1 for i in items if i["status"] == "MATCH_CANDIDATE"),
        "reviews": sum(1 for i in items if i["status"].startswith("REVIEW_")),
        "skipped": sum(1 for i in items if i["status"].startswith("SKIP_")),
        "limit": limit,
    }


async def candidate_detail(bank_line_id: str) -> dict[str, Any]:
    """Read-only detail for one exact BankEntryLine.

    Re-fetches the specific bank line by GUID, parses the order number, then
    resolves the matching open receivable. This endpoint exists to prove that
    we can address one bank line unambiguously even when many lines share the
    same EntryNumber.
    """
    bank = await bank_line_by_id(bank_line_id)
    description = str(bank.get("Description") or "")
    order_no = extract_order_number(description)
    bank_amount = money(bank.get("AmountDC"))
    recs = await find_receivable(order_no) if order_no else []

    status = "REVIEW_NO_ORDER"
    reason = "Geen ordernummer herkend in de bankomschrijving."
    rec = None
    if bank_amount <= Decimal("0.00"):
        status = "SKIP_NOT_RECEIPT"
        reason = "Geen positieve bankontvangst; valt buiten deze eerste verkoopflow."
    elif order_no:
        if len(recs) == 0:
            status = "REVIEW_NOT_FOUND"
            reason = f"Geen openstaande post gevonden voor {ORDER_REF_PREFIX}{order_no}."
        elif len(recs) > 1:
            status = "REVIEW_MULTIPLE"
            reason = f"{len(recs)} openstaande posten gevonden voor {ORDER_REF_PREFIX}{order_no}."
        else:
            rec = recs[0]
            rec_amount = money(rec.get("Amount"))
            account_code = str(rec.get("AccountCode") or "").strip()
            if account_code != COLLECTIVE_DEBTOR_CODE:
                status = "REVIEW_ACCOUNT"
                reason = f"Openstaande post staat op account {account_code or 'onbekend'}, niet {COLLECTIVE_DEBTOR_CODE}."
            elif bank_amount != rec_amount:
                status = "REVIEW_AMOUNT"
                reason = f"Bedrag wijkt af: bank {bank_amount} vs openstaand {rec_amount}."
            else:
                status = "MATCH_CANDIDATE"
                reason = "Unieke TD-referentie op verzameldebiteur en bedrag exact gelijk."

    return {
        "bank": bank,
        "bank_line_id": bank.get("ID"),
        "bank_entry_id": bank.get("EntryID"),
        "bank_entry": bank.get("EntryNumber"),
        "bank_line_number": bank.get("LineNumber"),
        "description": description,
        "bank_amount": str(bank_amount),
        "order_number": order_no,
        "expected_ref": f"{ORDER_REF_PREFIX}{order_no}" if order_no else None,
        "receivable": rec,
        "receivables_count": len(recs),
        "status": status,
        "reason": reason,
        "read_only": True,
    }


async def classify_direct_woo_bank(bank: dict[str, Any]) -> dict[str, Any]:
    """Read-only classifier for direct WooCommerce customer bank receipts.

    This is intentionally one payment-method handler. Future PSP/payment methods
    can be added as separate handlers without changing this contract.
    """
    description = str(bank.get("Description") or "")
    amount = money(bank.get("AmountDC"))
    upper = description.upper()

    base = {
        "payment_method": "DIRECT_WOO_BANK",
        "handler": "direct_woo_bank_v1",
        "confidence": "NO_MATCH",
        "eligible_for_reimport_enrichment": False,
        "reason": "",
        "order_number": None,
        "expected_ref": None,
        "receivable_entry": None,
        "receivable_amount": None,
        "account_code": None,
        "account_name": None,
    }

    if amount <= Decimal("0.00"):
        base["reason"] = "Geen positieve bankontvangst."
        return base
    if "PAYNETICS" in upper:
        base["reason"] = "Paynetics/PSP-uitbetaling valt expliciet buiten DIRECT_WOO_BANK."
        return base

    order_no = extract_order_number(description)
    if not order_no:
        base["reason"] = "Geen WooCommerce-ordernummer herkenbaar in de bankomschrijving."
        return base

    expected_ref = f"{ORDER_REF_PREFIX}{order_no}"
    base["order_number"] = order_no
    base["expected_ref"] = expected_ref

    recs = await find_receivable(order_no)
    if len(recs) == 0:
        base["confidence"] = "REVIEW"
        base["reason"] = f"Geen openstaande post gevonden voor {expected_ref}."
        return base
    if len(recs) > 1:
        base["confidence"] = "REVIEW"
        base["reason"] = f"Meerdere openstaande posten gevonden voor {expected_ref}."
        return base

    rec = recs[0]
    rec_amount = money(rec.get("Amount"))
    account_code = str(rec.get("AccountCode") or "").strip()
    base.update(
        receivable_entry=rec.get("EntryNumber"),
        receivable_amount=str(rec_amount),
        account_code=account_code,
        account_name=rec.get("AccountName") or "",
    )

    if account_code != COLLECTIVE_DEBTOR_CODE:
        base["confidence"] = "REVIEW"
        base["reason"] = f"Openstaande post staat op {account_code or 'onbekend'}, niet {COLLECTIVE_DEBTOR_CODE}."
        return base
    if amount != rec_amount:
        base["confidence"] = "REVIEW"
        base["reason"] = f"Bedrag wijkt af: bank {amount} vs openstaand {rec_amount}."
        return base

    base["confidence"] = "HARD_MATCH"
    base["eligible_for_reimport_enrichment"] = True
    base["reason"] = "Unieke TD-referentie, verzameldebiteur en bedrag komen exact overeen."
    return base


async def reimport_enrichment_dry_run(bank_line_id: str) -> dict[str, Any]:
    """Read-only phase-1 plan for enriched reimport of one existing bank line.

    No bank entry is deleted, recreated, imported or matched. The endpoint only
    proves classification and the metadata that a later import adapter should
    inject once the supported Exact import mapping has been validated.
    """
    bank = await bank_line_by_id(bank_line_id)
    classification = await classify_direct_woo_bank(bank)

    proposed = None
    if classification["confidence"] == "HARD_MATCH":
        proposed = {
            "target_account_code": COLLECTIVE_DEBTOR_CODE,
            "matching_reference": classification["expected_ref"],
            "receivable_entry": classification["receivable_entry"],
            "preserve_bank_amount": str(money(bank.get("AmountDC"))),
            "preserve_original_description": str(bank.get("Description") or ""),
            "import_field_mapping": "RESEARCH_REQUIRED",
            "note": (
                "Dit is alleen de semantische verrijking. Het exacte CAMT/importveld "
                "waarmee Exact deze referentie tijdens import gebruikt, moet eerst "
                "officieel worden gevalideerd."
            ),
        }

    return {
        "read_only": True,
        "writes_executed": False,
        "phase": "REIMPORT_ENRICHMENT_DRY_RUN",
        "original_bank_line": {
            "id": bank.get("ID"),
            "entry_id": bank.get("EntryID"),
            "entry_number": bank.get("EntryNumber"),
            "line_number": bank.get("LineNumber"),
            "date": bank.get("Date"),
            "description": bank.get("Description"),
            "amount_dc": str(money(bank.get("AmountDC"))),
            "account_code": bank.get("AccountCode"),
            "gl_account_code": bank.get("GLAccountCode"),
            "our_ref": bank.get("OurRef"),
        },
        "classification": classification,
        "proposed_enrichment": proposed,
        "reimport_gate": {
            "ready_for_format_research": classification["confidence"] == "HARD_MATCH",
            "ready_for_financial_write": False,
            "delete_original_allowed": False,
            "reimport_allowed": False,
            "reason": "Eerst het ondersteunde Exact-importformaat en de idempotency/saldocontroles bewijzen.",
        },
        "writes_enabled": {
            "order_rule": ENABLE_ORDER_RULE_WRITES,
            "direct_match": ENABLE_DIRECT_MATCH_WRITES,
        },
    }


async def reimport_field_mapping_research(bank_line_id: str) -> dict[str, Any]:
    """Read-only phase-2 evidence for an enriched bank-statement reimport.

    Important: Exact Online documents the matching concepts (payment reference,
    Our ref., amount, booking date, relation), but public documentation does not
    currently prove a one-to-one CAMT.053 XML tag mapping for this tenant.
    Therefore candidate CAMT tags are kept explicitly unverified.
    """
    plan = await reimport_enrichment_dry_run(bank_line_id)
    cls = plan.get("classification") or {}

    candidates = [
        {
            "semantic": "payment_reference",
            "camt_candidate": "RmtInf/Strd/CdtrRefInf/Ref",
            "verification": "UNVERIFIED_FOR_EXACT_ONLINE",
            "value_for_golden_test": cls.get("expected_ref"),
            "why_candidate": "ISO 20022 structured creditor reference; Exact Online documents payment reference as a matching criterion.",
        },
        {
            "semantic": "end_to_end_id",
            "camt_candidate": "Refs/EndToEndId",
            "verification": "UNVERIFIED_FOR_EXACT_ONLINE",
            "value_for_golden_test": cls.get("expected_ref"),
            "why_candidate": "Common SEPA transaction identifier; Exact ecosystems use end-to-end/reference data for reconciliation, but the Exact Online import mapping must be proven.",
        },
        {
            "semantic": "unstructured_remittance",
            "camt_candidate": "RmtInf/Ustrd",
            "verification": "UNVERIFIED_FOR_EXACT_ONLINE",
            "value_for_golden_test": cls.get("expected_ref"),
            "why_candidate": "Could expose TD reference in bank narrative/remittance; not sufficient evidence that Exact maps it to Our ref. or payment reference.",
        },
    ]

    return {
        "read_only": True,
        "writes_executed": False,
        "phase": "REIMPORT_FIELD_MAPPING_RESEARCH",
        "bank_line_id": bank_line_id,
        "golden_case": {
            "payment_method": cls.get("payment_method"),
            "confidence": cls.get("confidence"),
            "order_number": cls.get("order_number"),
            "expected_ref": cls.get("expected_ref"),
            "receivable_entry": cls.get("receivable_entry"),
            "target_account_code": cls.get("account_code"),
            "amount": cls.get("receivable_amount"),
        },
        "exact_online_matching_evidence": {
            "documented_order": [
                "user allocation rules",
                "payment reference + amount",
                "Our ref. + amount",
                "booking date + amount",
                "known relation bank account",
            ],
            "supported_conclusion": (
                "Exact Online can allocate/match during bank import when reference metadata is recognised, "
                "but the public documentation reviewed so far does not identify the exact CAMT.053 XML tag "
                "that becomes Payment reference or Our ref. for this administration."
            ),
        },
        "camt_field_candidates": candidates,
        "recommended_next_experiment": {
            "type": "NON_PRODUCTION_FILE_IMPORT_TEST",
            "financial_write_allowed": False,
            "instructions": (
                "Create synthetic CAMT variants that differ only in one candidate reference tag, then import "
                "only in a safe test administration or other non-production context. Compare Exact's imported "
                "Payment reference/Our ref. fields and matching result. Do not delete or reimport production "
                "bank entry 26205149 until one mapping is proven and duplicate/saldo controls are validated."
            ),
        },
        "reimport_gate": {
            "ready_for_financial_write": False,
            "delete_original_allowed": False,
            "reimport_allowed": False,
            "field_mapping_proven": False,
        },
    }


async def run_legacy_dry_run(limit: int = 100) -> list[dict[str, Any]]:
    rows = []
    for bank in await bank_lines_on_suspense(limit):
        order_no = extract_order_number(bank.get("Description"))
        amount = money(bank.get("AmountDC"))
        result = {
            "bank_entry": bank.get("EntryNumber"), "bank_line_id": bank.get("ID"),
            "description": bank.get("Description") or "", "bank_amount": str(amount),
            "order_number": order_no, "expected_ref": f"{ORDER_REF_PREFIX}{order_no}" if order_no else None,
            "receivable_entry": None, "receivable_amount": None,
            "status": "REVIEW_NO_ORDER", "reason": "Geen webshopordernummer herkend.",
        }
        if order_no:
            recs = await find_receivable(order_no)
            if len(recs) == 1:
                rec_amount = money(recs[0].get("Amount"))
                result.update(receivable_entry=recs[0].get("EntryNumber"), receivable_amount=str(rec_amount))
                if rec_amount == amount:
                    result.update(status="BACKLOG_READY", reason="Match gevonden, maar bankregel staat nog op 1360; alleen handmatig verwerken in Exact.")
                else:
                    result.update(status="REVIEW_AMOUNT", reason="Bedrag wijkt af.")
            elif len(recs) == 0:
                result.update(status="REVIEW_NOT_FOUND", reason="Geen openstaande post gevonden.")
            else:
                result.update(status="REVIEW_MULTIPLE", reason=f"{len(recs)} openstaande posten gevonden.")
        rows.append(result)
    return rows


async def run_allocated_dry_run(limit: int = 100) -> list[dict[str, Any]]:
    rows = []
    for bank in await allocated_bank_lines(limit):
        order_no = extract_order_number(bank.get("Description"))
        bank_amount = abs(money(bank.get("AmountDC")))
        result = {
            "bank_entry": bank.get("EntryNumber"), "bank_line_id": bank.get("ID"),
            "description": bank.get("Description") or "", "bank_amount": str(bank_amount),
            "order_number": order_no, "expected_ref": f"{ORDER_REF_PREFIX}{order_no}" if order_no else None,
            "receivable_entry": None, "receivable_amount": None,
            "status": "REVIEW_NO_ORDER", "reason": "Geen webshopordernummer herkend.",
        }
        if not order_no:
            rows.append(result); continue
        recs = await find_receivable(order_no)
        if len(recs) == 0:
            result.update(status="REVIEW_NOT_FOUND", reason="Geen openstaande post gevonden (mogelijk al afgeletterd).")
        elif len(recs) > 1:
            result.update(status="REVIEW_MULTIPLE", reason=f"{len(recs)} openstaande posten gevonden.")
        else:
            rec = recs[0]
            rec_amount = abs(money(rec.get("Amount")))
            result.update(receivable_entry=rec.get("EntryNumber"), receivable_amount=str(rec_amount))
            if bank_amount == rec_amount:
                result.update(status="READY_DIRECT", reason="Bank is al aan verzameldebiteur toegewezen; directe aflettering mogelijk.")
            else:
                result.update(status="REVIEW_AMOUNT", reason=f"Bedrag wijkt af: bank {bank_amount} vs openstaand {rec_amount}.")
        rows.append(result)
    return rows


async def build_direct_match_plan(bank_line_id: str) -> dict[str, Any]:
    bank = await bank_line_by_id(bank_line_id)
    if str(bank.get("AccountCode") or "").strip() != COLLECTIVE_DEBTOR_CODE:
        raise HTTPException(409, "Bankregel is niet aan de verzameldebiteur toegewezen; directe match is geblokkeerd.")
    order_no = extract_order_number(bank.get("Description"))
    if not order_no:
        raise HTTPException(409, "Geen ordernummer herkenbaar in de bankomschrijving.")
    recs = await find_receivable(order_no)
    if len(recs) != 1:
        raise HTTPException(409, f"Verwacht precies 1 openstaande post, gevonden: {len(recs)}.")
    rec = recs[0]
    amount = abs(money(bank.get("AmountDC")))
    if amount != abs(money(rec.get("Amount"))):
        raise HTTPException(409, "Bankbedrag en openstaand bedrag zijn niet exact gelijk.")

    bank_txs = await transaction_lines(int(bank["EntryNumber"]))
    bank_candidates = [t for t in bank_txs if str(t.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE and abs(money(t.get("AmountDC"))) == amount]
    if len(bank_candidates) != 1:
        raise HTTPException(409, f"Kon de unieke debiteurenregel van de bankboeking niet bepalen ({len(bank_candidates)} kandidaten).")
    bank_tx = bank_candidates[0]

    invoice_txs = await transaction_lines(int(rec["EntryNumber"]))
    invoice_candidates = [t for t in invoice_txs if str(t.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE and abs(money(t.get("AmountDC"))) == amount]
    if len(invoice_candidates) != 1:
        account_guid = str(rec.get("AccountId") or "")
        invoice_candidates = [t for t in invoice_txs if account_guid and str(t.get("Account") or "") == account_guid and abs(money(t.get("AmountDC"))) == amount]
    if len(invoice_candidates) != 1:
        raise HTTPException(409, f"Kon de unieke debiteurenregel van de verkooppost niet bepalen ({len(invoice_candidates)} kandidaten).")
    invoice_tx = invoice_candidates[0]

    if str(bank_tx.get("GLAccountCode") or "") != str(invoice_tx.get("GLAccountCode") or ""):
        raise HTTPException(409, "Bankregel en verkooppost staan niet op dezelfde debiteuren-grootboekrekening.")
    if money(bank_tx.get("AmountDC")) + money(invoice_tx.get("AmountDC")) != Decimal("0.00"):
        raise HTTPException(409, f"Debiteurenregels salderen niet naar nul: {bank_tx.get('AmountDC')} + {invoice_tx.get('AmountDC')}.")

    return {
        "bank": bank, "bank_tx": bank_tx, "invoice_tx": invoice_tx,
        "receivable": rec, "order_number": order_no, "expected_ref": f"{ORDER_REF_PREFIX}{order_no}",
        "amount": amount, "debtor_gl_code": invoice_tx.get("GLAccountCode"),
        "debtor_account_code": invoice_tx.get("AccountCode") or COLLECTIVE_DEBTOR_CODE,
    }


def build_direct_match_xml(plan: dict[str, Any]) -> bytes:
    root = ET.Element("eExact", {"xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance", "xsi:noNamespaceSchemaLocation": "eExact-XML.xsd"})
    ms = ET.SubElement(ET.SubElement(root, "MatchSets"), "MatchSet")
    ET.SubElement(ms, "GLAccount", {"code": str(plan["debtor_gl_code"])})
    ET.SubElement(ms, "Account", {"code": str(plan["debtor_account_code"])})
    lines = ET.SubElement(ms, "MatchLines")
    for tx in [plan["invoice_tx"], plan["bank_tx"]]:
        ET.SubElement(lines, "MatchLine", {
            "finyear": str(tx["FinancialYear"]), "finperiod": str(tx["FinancialPeriod"]),
            "journal": str(tx["JournalCode"]), "entry": str(tx["EntryNumber"]),
            "amountdc": f"{money(tx['AmountDC']):.2f}",
        })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


async def upload_matchset(xml_payload: bytes) -> str:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        resp = await client.post(
            MATCHSETS_URL,
            params={"Topic": "MatchSets", "_Division_": str(DIVISION)},
            content=xml_payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/xml; charset=utf-8", "Accept": "application/xml,text/xml,*/*"},
        )
    if resp.status_code == 401:
        await _refresh_tokens(_load_tokens() or {})
        return await upload_matchset(xml_payload)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"MatchSets upload failed: {resp.text[:1200]}")
    text = resp.text.strip()
    try:
        parsed = ET.fromstring(text) if text else None
        messages = parsed.findall(".//Message") if parsed is not None else []
        if messages:
            details = " | ".join(" ".join(m.itertext()).strip() for m in messages)
            if "lines matched" not in details.lower():
                raise HTTPException(409, f"Exact MatchSets melding: {details}")
    except ET.ParseError:
        if "error" in text.lower():
            raise HTTPException(409, f"Exact MatchSets antwoord: {text[:1200]}")
    return text


async def execute_direct_match(bank_line_id: str) -> dict[str, Any]:
    if not ENABLE_DIRECT_MATCH_WRITES:
        raise HTTPException(403, "Direct afletteren staat op slot.")
    plan = await build_direct_match_plan(bank_line_id)  # full revalidation immediately before write
    response = await upload_matchset(build_direct_match_xml(plan))
    # Idempotency check: after a successful match, the receivable should no longer be open.
    still_open = await find_receivable(plan["order_number"])
    return {
        "ok": len(still_open) == 0,
        "order": plan["order_number"], "bank_entry": plan["bank_tx"].get("EntryNumber"),
        "invoice_entry": plan["invoice_tx"].get("EntryNumber"), "amount": str(plan["amount"]),
        "still_open_count": len(still_open), "matchsets_response": response[:500],
    }


@app.get("/health")
async def health():
    from operations.automatic_debtor_routing import STATUS
    return {"ok": True, "division": DIVISION, "version": "1.7.0", "order_rule_writes": ENABLE_ORDER_RULE_WRITES,
            "direct_match_writes": ENABLE_DIRECT_MATCH_WRITES, "debtor_routing": dict(STATUS)}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request, "connected": _load_tokens() is not None, "division": DIVISION,
        "suspense": SUSPENSE_GL_CODE, "debtor": COLLECTIVE_DEBTOR_CODE,
        "order_rule_writes": ENABLE_ORDER_RULE_WRITES, "direct_match_writes": ENABLE_DIRECT_MATCH_WRITES,
    })


@app.get("/login")
async def login(request: Request):
    _require_config()
    state = os.urandom(24).hex(); request.session["oauth_state"] = state
    query = urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code", "state": state})
    return RedirectResponse(f"{AUTH_URL}?{query}")


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    _require_config()
    if error: raise HTTPException(400, f"Exact authorization failed: {error}")
    expected_state = request.session.pop("oauth_state", None)
    if not code or not state or state != expected_state: raise HTTPException(400, "Invalid OAuth callback/state.")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    if resp.status_code >= 400: raise HTTPException(resp.status_code, f"Exact token exchange failed: {resp.text[:500]}")
    _save_tokens(resp.json()); return RedirectResponse("/")


@app.get("/diagnose/{order_number}")
async def diagnose_order(order_number: str):
    # Strictly read-only endpoint. It makes only GET requests to Exact.
    if not re.fullmatch(r"\d{4,10}", order_number):
        raise HTTPException(400, "Ordernummer moet uit 4-10 cijfers bestaan.")
    return await diagnose_receivable(order_number)




@app.get("/api/candidates")
async def api_candidates(limit: int = 200):
    """Machine-readable read-only candidate feed for the development agent."""
    return await bank_first_candidates(limit)


@app.get("/api/candidate/{bank_line_id}")
async def api_candidate_detail(bank_line_id: str):
    """Machine-readable read-only detail for one exact BankEntryLine."""
    return await candidate_detail(bank_line_id)


@app.get("/api/diagnostics/bank-line/{bank_line_id}")
async def api_bank_line_diagnostic(bank_line_id: str, receivable_entry: int | None = None):
    """GET-only consolidated Exact diagnostic. No financial writes are possible here."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", bank_line_id):
        raise HTTPException(400, "Ongeldige bankregel-GUID.")
    return await diagnose_bank_line(bank_line_id, receivable_entry)


@app.get("/api/reimport/dry-run/{bank_line_id}")
async def api_reimport_dry_run(bank_line_id: str):
    """Read-only enriched-reimport plan for one exact BankEntryLine."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", bank_line_id):
        raise HTTPException(400, "Ongeldige bankregel-GUID.")
    return await reimport_enrichment_dry_run(bank_line_id)


@app.get("/api/reimport/field-mapping/{bank_line_id}")
async def api_reimport_field_mapping(bank_line_id: str):
    """Read-only phase-2 research result for Exact Online import field mapping."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", bank_line_id):
        raise HTTPException(400, "Ongeldige bankregel-GUID.")
    return await reimport_field_mapping_research(bank_line_id)


@app.get("/api/safety")
async def api_safety():
    """Expose only non-secret safety state for automated regression checks."""
    return {
        "version": "1.7.0",
        "division": DIVISION,
        "suspense_gl_code": SUSPENSE_GL_CODE,
        "collective_debtor_code": COLLECTIVE_DEBTOR_CODE,
        "order_ref_prefix": ORDER_REF_PREFIX,
        "order_rule_writes": ENABLE_ORDER_RULE_WRITES,
        "direct_match_writes": ENABLE_DIRECT_MATCH_WRITES,
    }


@app.get("/candidates", response_class=HTMLResponse)
async def candidates_page(request: Request, limit: int = 200, only: str = "all"):
    limit = max(1, min(limit, 500))
    result = await bank_first_candidates(limit)
    items = result["items"]
    if only == "matches":
        items = [i for i in items if i["status"] == "MATCH_CANDIDATE"]
    elif only == "review":
        items = [i for i in items if i["status"].startswith("REVIEW_")]
    view = dict(result)
    view["items"] = items
    return templates.TemplateResponse("candidates.html", {"request": request, "result": view, "only": only})


@app.get("/candidate/{bank_line_id}", response_class=HTMLResponse)
async def candidate_detail_page(request: Request, bank_line_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", bank_line_id):
        raise HTTPException(400, "Ongeldige bankregel-GUID.")
    detail = await candidate_detail(bank_line_id)
    return templates.TemplateResponse("candidate_detail.html", {"request": request, "detail": detail})


@app.get("/dry-run", response_class=HTMLResponse)
async def dry_run_page(request: Request, limit: int = 100):
    return templates.TemplateResponse("dry_run.html", {"request": request, "results": await run_legacy_dry_run(limit), "limit": limit})


@app.get("/allocated", response_class=HTMLResponse)
async def allocated_page(request: Request, limit: int = 100):
    return templates.TemplateResponse("allocated.html", {"request": request, "results": await run_allocated_dry_run(limit), "limit": limit, "writes_enabled": ENABLE_DIRECT_MATCH_WRITES})


@app.get("/allocation-rule")
async def allocation_rule_redirect():
    return RedirectResponse("/order-rules")


@app.get("/order-rules", response_class=HTMLResponse)
async def order_rules_page(request: Request, limit: int = 500):
    status = await order_rule_status(limit)
    return templates.TemplateResponse("order_rules.html", {
        "request": request, "status": status, "writes_enabled": ENABLE_ORDER_RULE_WRITES,
    })


@app.post("/order-rules/{order_number}/create", response_class=HTMLResponse)
async def order_rule_create_page(request: Request, order_number: str):
    result = await create_order_rule(order_number)
    return templates.TemplateResponse("order_rule_done.html", {"request": request, "result": result})


@app.get("/direct-match/{bank_line_id}", response_class=HTMLResponse)
async def direct_match_preview_page(request: Request, bank_line_id: str):
    plan = await build_direct_match_plan(bank_line_id)
    return templates.TemplateResponse("direct_match_preview.html", {"request": request, "plan": plan, "writes_enabled": ENABLE_DIRECT_MATCH_WRITES})


@app.post("/direct-match/{bank_line_id}/execute", response_class=HTMLResponse)
async def direct_match_execute_page(request: Request, bank_line_id: str):
    result = await execute_direct_match(bank_line_id)
    return templates.TemplateResponse("direct_match_done.html", {"request": request, "result": result})
