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
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-me")
ENABLE_MATCH_WRITES = os.getenv("ENABLE_MATCH_WRITES", "false").lower() == "true"

AUTH_URL = f"{BASE_URL}/api/oauth2/auth"
TOKEN_URL = f"{BASE_URL}/api/oauth2/token"
API_V1 = f"{BASE_URL}/api/v1"
MATCHSETS_URL = f"{BASE_URL}/docs/XMLUpload.aspx"

app = FastAPI(title="JNP Matching", version="0.4.0")
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


def _load_tokens() -> dict[str, Any] | None:
    if not TOKEN_STORE_PATH.exists():
        return None
    return json.loads(TOKEN_STORE_PATH.read_text())


def _save_tokens(tokens: dict[str, Any]) -> None:
    TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    expires_in = int(tokens.get("expires_in", 600))
    tokens["expires_at"] = int(time.time()) + expires_in - 30
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


async def _access_token() -> str:
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


async def exact_get(path: str, params: dict[str, str] | None = None) -> Any:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.get(
            f"{API_V1}/{DIVISION}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if resp.status_code == 401:
        tokens = _load_tokens() or {}
        await _refresh_tokens(tokens)
        return await exact_get(path, params)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Exact API error: {resp.text[:800]}")
    return resp.json()


async def exact_post(path: str, payload: dict[str, Any]) -> Any:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            f"{API_V1}/{DIVISION}/{path.lstrip('/')}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code == 401:
        tokens = _load_tokens() or {}
        await _refresh_tokens(tokens)
        return await exact_post(path, payload)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Exact POST error: {resp.text[:1200]}")
    if not resp.text.strip():
        return {}
    return resp.json()


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


def exact_date(value: Any) -> str:
    if not value:
        return ""
    s = str(value)
    m = re.search(r"Date\((\d+)\)", s)
    if m:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()
    return s[:10]


async def find_receivable(order_number: str) -> list[dict[str, Any]]:
    expected_ref = f"{ORDER_REF_PREFIX}{order_number}"
    params = {
        "$filter": f"YourRef eq '{expected_ref}'",
        "$select": "AccountId,AccountCode,AccountName,Amount,AmountInTransit,CurrencyCode,Description,EntryNumber,InvoiceDate,InvoiceNumber,JournalCode,YourRef",
    }
    data = await exact_get("read/financial/ReceivablesList", params)
    rows = _extract_results(data)
    debtor_rows = [r for r in rows if str(r.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE]
    return debtor_rows if debtor_rows else rows


async def bank_lines(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    params = {
        "$filter": f"GLAccountCode eq '{SUSPENSE_GL_CODE}'",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,GLAccount,GLAccountCode,GLAccountDescription,OurRef,Modified",
        "$orderby": "Modified desc",
        "$top": str(limit),
    }
    data = await exact_get("financialtransaction/BankEntryLines", params)
    return _extract_results(data)


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




async def bank_entry_header(entry_id: str) -> dict[str, Any]:
    params = {
        "$filter": f"EntryID eq guid'{entry_id}'",
        "$select": "EntryID,EntryNumber,FinancialPeriod,FinancialYear,JournalCode,JournalDescription,Currency,Status,StatusDescription",
        "$top": "1",
    }
    rows = _extract_results(await exact_get("financialtransaction/BankEntries", params))
    if len(rows) != 1:
        raise HTTPException(409, f"Kon de bankboeking-header niet uniek bepalen ({len(rows)} kandidaten).")
    return rows[0]

async def transaction_lines(entry_number: int) -> list[dict[str, Any]]:
    params = {
        "$filter": f"EntryNumber eq {int(entry_number)}",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,Currency,FinancialPeriod,FinancialYear,GLAccount,GLAccountCode,GLAccountDescription,JournalCode,YourRef",
    }
    return _extract_results(await exact_get("financialtransaction/TransactionLines", params))


async def general_journals() -> list[dict[str, Any]]:
    params = {
        "$filter": "Type eq 90 and IsBlocked eq false",
        "$select": "Code,Description,Currency,IsBlocked,Type",
        "$orderby": "Code",
    }
    rows = _extract_results(await exact_get("financial/Journals", params))
    if rows:
        return rows
    # Some Exact tenants expose booleans differently; fall back and filter locally.
    params = {"$filter": "Type eq 90", "$select": "Code,Description,Currency,IsBlocked,Type", "$orderby": "Code"}
    return [r for r in _extract_results(await exact_get("financial/Journals", params)) if not r.get("IsBlocked")]


async def run_dry_match(limit: int = 100) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bank in await bank_lines(limit):
        description = bank.get("Description") or ""
        order_no = extract_order_number(description)
        bank_amount = money(bank.get("AmountDC"))
        result = {
            "bank_entry": bank.get("EntryNumber"),
            "bank_line_id": bank.get("ID"),
            "description": description,
            "bank_amount": str(bank_amount),
            "order_number": order_no,
            "expected_ref": f"{ORDER_REF_PREFIX}{order_no}" if order_no else None,
            "receivable_entry": None,
            "receivable_amount": None,
            "status": "REVIEW_NO_ORDER",
            "reason": "No webshop order number recognized in bank description.",
        }
        if not order_no:
            rows.append(result)
            continue
        receivables = await find_receivable(order_no)
        if len(receivables) == 0:
            result.update(status="REVIEW_NOT_FOUND", reason="No matching open receivable found.")
        elif len(receivables) > 1:
            result.update(status="REVIEW_MULTIPLE", reason=f"{len(receivables)} matching open receivables found.")
        else:
            rec = receivables[0]
            rec_amount = money(rec.get("Amount"))
            result["receivable_entry"] = rec.get("EntryNumber")
            result["receivable_amount"] = str(rec_amount)
            result["journal"] = rec.get("JournalCode")
            result["account_code"] = rec.get("AccountCode")
            if bank_amount == rec_amount:
                result.update(status="READY", reason="Unique reference match and exact amount match.")
            else:
                result.update(status="REVIEW_AMOUNT", reason=f"Amount differs: bank {bank_amount} vs receivable {rec_amount}.")
        rows.append(result)
    return rows


async def build_match_plan(bank_line_id: str) -> dict[str, Any]:
    bank = await bank_line_by_id(bank_line_id)
    if str(bank.get("GLAccountCode") or "") != SUSPENSE_GL_CODE:
        raise HTTPException(409, "Bankregel staat niet meer op de tussenrekening; niets doen.")
    order_no = extract_order_number(bank.get("Description"))
    if not order_no:
        raise HTTPException(409, "Geen ordernummer herkenbaar in de bankomschrijving.")
    receivables = await find_receivable(order_no)
    if len(receivables) != 1:
        raise HTTPException(409, f"Verwacht precies 1 openstaande post, gevonden: {len(receivables)}.")
    rec = receivables[0]
    bank_amount = money(bank.get("AmountDC"))
    rec_amount = money(rec.get("Amount"))
    if bank_amount <= 0 or rec_amount <= 0 or bank_amount != rec_amount:
        raise HTTPException(409, f"Bedragen sluiten niet exact: bank {bank_amount}, openstaand {rec_amount}.")

    # The BankEntryLines endpoint already gives us the exact 1360 allocation line
    # selected in the dry-run. TransactionLines does not always expose the bank
    # allocation as a separate 1360 line, so do not try to rediscover it there.
    if not bank.get("GLAccount") or str(bank.get("GLAccountCode") or "") != SUSPENSE_GL_CODE:
        raise HTTPException(409, "De geselecteerde bankregel mist de 1360-grootboekreferentie.")
    bank_header = await bank_entry_header(str(bank.get("EntryID")))

    inv_txs = await transaction_lines(int(rec["EntryNumber"]))
    debtor_candidates = [t for t in inv_txs if str(t.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE and abs(money(t.get("AmountDC"))) == rec_amount]
    if len(debtor_candidates) != 1:
        account_guid = str(rec.get("AccountId") or "")
        debtor_candidates = [t for t in inv_txs if account_guid and str(t.get("Account") or "") == account_guid and abs(money(t.get("AmountDC"))) == rec_amount]
    if len(debtor_candidates) != 1:
        raise HTTPException(409, f"Kon de unieke debiteurenregel van de factuur niet bepalen ({len(debtor_candidates)} kandidaten).")
    invoice_tx = debtor_candidates[0]

    if not invoice_tx.get("GLAccount") or not invoice_tx.get("Account"):
        raise HTTPException(409, "Debiteurenregel mist GLAccount of Account GUID.")
    # V1 only handles incoming webshop receipts. In Exact the imported bank line
    # is positive, while the 1360 counter-entry is a credit. Clearing 1360
    # therefore requires a positive (debit) allocation and a negative debtor line.
    bank_reverse = bank_amount
    debtor_payment = -money(invoice_tx.get("AmountDC"))
    if bank_reverse + debtor_payment != Decimal("0.00"):
        raise HTTPException(409, f"Voorgestelde memoriaalboeking is niet in balans ({bank_reverse} + {debtor_payment}).")

    return {
        "bank": bank,
        "order_number": order_no,
        "expected_ref": f"{ORDER_REF_PREFIX}{order_no}",
        "receivable": rec,
        "bank_header": bank_header,
        "invoice_tx": invoice_tx,
        "amount": bank_amount,
        "journal_year": int(bank_header.get("FinancialYear")),
        "journal_period": int(bank_header.get("FinancialPeriod")),
        "date": exact_date(bank.get("Date")),
        "currency": bank_header.get("Currency") or rec.get("CurrencyCode") or "EUR",
        "suspense_gl_guid": bank.get("GLAccount"),
        "suspense_gl_code": bank.get("GLAccountCode"),
        "debtor_gl_guid": invoice_tx.get("GLAccount"),
        "debtor_gl_code": invoice_tx.get("GLAccountCode"),
        "debtor_account_guid": invoice_tx.get("Account"),
        "debtor_account_code": invoice_tx.get("AccountCode") or COLLECTIVE_DEBTOR_CODE,
        "bank_reverse_amount": bank_reverse,
        "debtor_payment_amount": debtor_payment,
    }


def _odata_post_entity(payload: Any) -> dict[str, Any]:
    entity = _extract_entity(payload)
    if entity:
        return entity
    return payload if isinstance(payload, dict) else {}


async def create_allocation_journal(plan: dict[str, Any], journal_code: str) -> dict[str, Any]:
    description = f"JNP match {plan['expected_ref']} / bank {plan['bank'].get('EntryNumber')}"
    body = {
        "JournalCode": journal_code,
        "FinancialYear": plan["journal_year"],
        "FinancialPeriod": plan["journal_period"],
        "Currency": plan["currency"],
        "GeneralJournalEntryLines": [
            {
                "Date": plan["date"],
                "Description": description,
                "GLAccount": plan["suspense_gl_guid"],
                "AmountFC": float(plan["bank_reverse_amount"]),
            },
            {
                "Date": plan["date"],
                "Description": description,
                "GLAccount": plan["debtor_gl_guid"],
                "Account": plan["debtor_account_guid"],
                "AmountFC": float(plan["debtor_payment_amount"]),
            },
        ],
    }
    created = _odata_post_entity(await exact_post("generaljournalentry/GeneralJournalEntries", body))
    if not created.get("EntryNumber"):
        raise HTTPException(502, f"Exact maakte de memoriaalboeking aan maar gaf geen EntryNumber terug: {created}")
    return created


def build_matchsets_xml(plan: dict[str, Any], allocation: dict[str, Any], allocation_tx: dict[str, Any]) -> bytes:
    root = ET.Element("eExact", {
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "eExact-XML.xsd",
    })
    matchsets = ET.SubElement(root, "MatchSets")
    matchset = ET.SubElement(matchsets, "MatchSet")
    ET.SubElement(matchset, "GLAccount", {"code": str(plan["debtor_gl_code"])})
    ET.SubElement(matchset, "Account", {"code": str(plan["debtor_account_code"])})
    lines = ET.SubElement(matchset, "MatchLines")
    invoice_tx = plan["invoice_tx"]
    ET.SubElement(lines, "MatchLine", {
        "finyear": str(invoice_tx["FinancialYear"]),
        "finperiod": str(invoice_tx["FinancialPeriod"]),
        "journal": str(invoice_tx["JournalCode"]),
        "entry": str(invoice_tx["EntryNumber"]),
        "amountdc": f"{money(invoice_tx['AmountDC']):.2f}",
    })
    ET.SubElement(lines, "MatchLine", {
        "finyear": str(allocation_tx["FinancialYear"]),
        "finperiod": str(allocation_tx["FinancialPeriod"]),
        "journal": str(allocation_tx["JournalCode"]),
        "entry": str(allocation_tx["EntryNumber"]),
        "amountdc": f"{money(allocation_tx['AmountDC']):.2f}",
    })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


async def upload_matchset(xml_payload: bytes) -> str:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        resp = await client.post(
            MATCHSETS_URL,
            params={"Topic": "MatchSets", "_Division_": str(DIVISION)},
            content=xml_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/xml; charset=utf-8",
                "Accept": "application/xml,text/xml,*/*",
            },
        )
    if resp.status_code == 401:
        tokens = _load_tokens() or {}
        await _refresh_tokens(tokens)
        return await upload_matchset(xml_payload)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"MatchSets upload failed: {resp.text[:1200]}")
    text = resp.text.strip()
    # Empty <Messages/> is the normal success response. Any actual Message is treated as an error.
    try:
        parsed = ET.fromstring(text) if text else None
        messages = parsed.findall(".//Message") if parsed is not None else []
        if messages:
            details = " | ".join(" ".join((m.itertext())).strip() for m in messages)
            raise HTTPException(409, f"Exact MatchSets melding: {details}")
    except ET.ParseError:
        if "error" in text.lower():
            raise HTTPException(409, f"Exact MatchSets antwoord: {text[:1200]}")
    return text


async def execute_match(bank_line_id: str, journal_code: str) -> dict[str, Any]:
    if not ENABLE_MATCH_WRITES:
        raise HTTPException(403, "Schrijven staat nog op slot. Zet ENABLE_MATCH_WRITES=true pas na controle van de preview.")
    journals = {str(j.get("Code")): j for j in await general_journals()}
    if journal_code not in journals:
        raise HTTPException(409, "Gekozen memoriaaldagboek is niet beschikbaar of geblokkeerd.")

    # Full revalidation immediately before any write.
    plan = await build_match_plan(bank_line_id)
    allocation = await create_allocation_journal(plan, journal_code)
    allocation_entry = int(allocation["EntryNumber"])
    allocation_txs = await transaction_lines(allocation_entry)
    debtor_lines = [t for t in allocation_txs if str(t.get("GLAccountCode") or "") == str(plan["debtor_gl_code"]) and str(t.get("Account") or "") == str(plan["debtor_account_guid"]) and money(t.get("AmountDC")) == plan["debtor_payment_amount"]]
    if len(debtor_lines) != 1:
        raise HTTPException(502, f"Memoriaalboeking {allocation_entry} is aangemaakt, maar de debiteurenregel kon niet uniek worden teruggelezen. STOP en controleer Exact handmatig.")
    allocation_tx = debtor_lines[0]
    xml_payload = build_matchsets_xml(plan, allocation, allocation_tx)
    response_text = await upload_matchset(xml_payload)
    return {
        "ok": True,
        "order": plan["order_number"],
        "bank_entry": plan["bank"].get("EntryNumber"),
        "invoice_entry": plan["invoice_tx"].get("EntryNumber"),
        "allocation_entry": allocation_entry,
        "journal": journal_code,
        "amount": str(plan["amount"]),
        "matchsets_response": response_text[:500],
    }


@app.get("/health")
async def health():
    return {"ok": True, "division": DIVISION, "mode": "write-capable" if ENABLE_MATCH_WRITES else "write-locked"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    connected = _load_tokens() is not None
    return templates.TemplateResponse("index.html", {
        "request": request,
        "connected": connected,
        "division": DIVISION,
        "suspense": SUSPENSE_GL_CODE,
        "debtor": COLLECTIVE_DEBTOR_CODE,
        "writes_enabled": ENABLE_MATCH_WRITES,
    })


@app.get("/login")
async def login(request: Request):
    _require_config()
    state = os.urandom(24).hex()
    request.session["oauth_state"] = state
    query = urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })
    return RedirectResponse(f"{AUTH_URL}?{query}")


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    _require_config()
    if error:
        raise HTTPException(400, f"Exact authorization failed: {error}")
    expected_state = request.session.pop("oauth_state", None)
    if not code or not state or state != expected_state:
        raise HTTPException(400, "Invalid OAuth callback/state.")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Exact token exchange failed: {resp.text[:500]}")
    _save_tokens(resp.json())
    return RedirectResponse("/")


@app.get("/api/status")
async def api_status():
    token = await _access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_V1}/current/Me",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:800])
    return {"configured_division": DIVISION, "writes_enabled": ENABLE_MATCH_WRITES, "exact_current_me": resp.json()}


@app.get("/api/dry-run")
async def dry_run(limit: int = 100):
    return {
        "mode": "read-only",
        "division": DIVISION,
        "rules": {
            "suspense_gl": SUSPENSE_GL_CODE,
            "collective_debtor": COLLECTIVE_DEBTOR_CODE,
            "reference": f"{ORDER_REF_PREFIX}{{order_number}}",
        },
        "results": await run_dry_match(limit),
    }


@app.get("/dry-run", response_class=HTMLResponse)
async def dry_run_page(request: Request, limit: int = 100):
    results = await run_dry_match(limit)
    return templates.TemplateResponse("dry_run.html", {
        "request": request,
        "results": results,
        "limit": limit,
        "writes_enabled": ENABLE_MATCH_WRITES,
    })


@app.get("/match/{bank_line_id}", response_class=HTMLResponse)
async def match_preview(request: Request, bank_line_id: str):
    plan = await build_match_plan(bank_line_id)
    journals = await general_journals()
    return templates.TemplateResponse("match_preview.html", {
        "request": request,
        "plan": plan,
        "journals": journals,
        "writes_enabled": ENABLE_MATCH_WRITES,
    })


@app.post("/match/{bank_line_id}/execute", response_class=HTMLResponse)
async def match_execute_page(request: Request, bank_line_id: str, journal_code: str):
    result = await execute_match(bank_line_id, journal_code)
    return templates.TemplateResponse("match_done.html", {"request": request, "result": result})
