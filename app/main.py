








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
