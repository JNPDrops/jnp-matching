import json
import os
import re
import time
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

AUTH_URL = f"{BASE_URL}/api/oauth2/auth"
TOKEN_URL = f"{BASE_URL}/api/oauth2/token"
API_V1 = f"{BASE_URL}/api/v1"

app = FastAPI(title="JNP Matching", version="0.1.0")
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
    # Exact Online commonly returns OData v3 shapes: {"d":{"results":[...]}}
    if isinstance(payload, dict):
        d = payload.get("d", payload)
        if isinstance(d, dict) and isinstance(d.get("results"), list):
            return d["results"]
        if isinstance(d, list):
            return d
    return []


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


ORDER_PATTERNS = [
    re.compile(r"(?i)\b(?:TD\s*#?\s*)?(\d{4,10})\b"),
]


def extract_order_number(description: str | None) -> str | None:
    if not description:
        return None
    # Strongest pattern first: explicit ORDER/TD/# marker.
    explicit = re.search(r"(?i)(?:order\s*(?:td\s*)?#?|\btd\s*#?|#)\s*(\d{4,10})\b", description)
    if explicit:
        return explicit.group(1)
    # For the observed bank format "SURNAME FIRSTNAME 48451", accept a trailing 4-10 digit token.
    trailing = re.search(r"\b(\d{4,10})\s*$", description.strip())
    return trailing.group(1) if trailing else None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


async def find_receivable(order_number: str) -> list[dict[str, Any]]:
    expected_ref = f"{ORDER_REF_PREFIX}{order_number}"
    # Search first on the webshop reference only. In some Exact administrations
    # AccountCode on ReceivablesList does not equal the visible debtor number,
    # which made valid webshop invoices disappear from the result set.
    params = {
        "$filter": f"YourRef eq '{expected_ref}'",
        "$select": "AccountCode,AccountName,Amount,AmountInTransit,CurrencyCode,Description,EntryNumber,InvoiceDate,InvoiceNumber,JournalCode,YourRef",
    }
    data = await exact_get("read/financial/ReceivablesList", params)
    rows = _extract_results(data)

    # Prefer the configured collective debtor when Exact returns its code.
    debtor_rows = [r for r in rows if str(r.get("AccountCode") or "").strip() == COLLECTIVE_DEBTOR_CODE]
    return debtor_rows if debtor_rows else rows


async def bank_lines(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    params = {
        "$filter": f"GLAccountCode eq '{SUSPENSE_GL_CODE}'",
        "$select": "ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,AccountName,GLAccountCode,GLAccountDescription,OurRef,Modified",
        "$orderby": "Modified desc",
        "$top": str(limit),
    }
    data = await exact_get("financialtransaction/BankEntryLines", params)
    return _extract_results(data)


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


@app.get("/health")
async def health():
    return {"ok": True, "division": DIVISION, "mode": "read-only dry-run"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    connected = _load_tokens() is not None
    return templates.TemplateResponse("index.html", {
        "request": request,
        "connected": connected,
        "division": DIVISION,
        "suspense": SUSPENSE_GL_CODE,
        "debtor": COLLECTIVE_DEBTOR_CODE,
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
    return {"configured_division": DIVISION, "exact_current_me": resp.json()}


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
    return templates.TemplateResponse("dry_run.html", {"request": request, "results": results, "limit": limit})
