"""Single-tenant Microsoft sign-in with opaque, server-side sessions.

No Microsoft tokens, PKCE verifiers or credentials are sent to dashboard JS.
The cookie is only a random handle; PostgreSQL stores its SHA-256 digest.
"""
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import msal
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

SESSION_COOKIE = "__Host-jnp_dashboard"
FLOW_COOKIE = "__Host-jnp_dashboard_flow"
FLOW_TTL = 600
SESSION_TTL = 3600
HANDLE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ROLES = {"viewer", "operator", "admin"}


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    client_id: str
    origin: str
    database_url: str = field(repr=False)
    access: dict = field(repr=False)
    client_secret: str = field(default="", repr=False)
    certificate_path: str = field(default="", repr=False)
    certificate_password: str = field(default="", repr=False)
    allow_tenant_members: bool = False
    member_divisions: dict = field(default_factory=dict)

    @property
    def callback(self):
        return self.origin + "/dashboard/auth/callback"

    @classmethod
    def from_env(cls):
        if os.getenv("DASHBOARD_ENABLED", "false").lower() != "true":
            raise ValueError("Dashboard disabled")
        settings = cls(
            tenant_id=os.getenv("DASHBOARD_MICROSOFT_TENANT_ID", ""),
            client_id=os.getenv("DASHBOARD_MICROSOFT_CLIENT_ID", ""),
            origin=os.getenv("DASHBOARD_PUBLIC_ORIGIN", "").rstrip("/"),
            database_url=os.getenv("DATABASE_URL", ""),
            access=json.loads(os.getenv("DASHBOARD_ACCESS_JSON", "{}")),
            client_secret=os.getenv("DASHBOARD_MICROSOFT_CLIENT_SECRET", ""),
            certificate_path=os.getenv("DASHBOARD_MICROSOFT_CERTIFICATE_PATH", ""),
            certificate_password=os.getenv("DASHBOARD_MICROSOFT_CERTIFICATE_PASSWORD", ""),
            allow_tenant_members=os.getenv("DASHBOARD_ALLOW_TENANT_MEMBERS", "false").lower() == "true",
            member_divisions=json.loads(os.getenv("DASHBOARD_MEMBER_DIVISIONS_JSON", "{}")),
        )
        settings.validate()
        return settings

    def validate(self):
        if str(UUID(self.tenant_id)) != self.tenant_id or str(UUID(self.client_id)) != self.client_id:
            raise ValueError("Use canonical tenant and application UUIDs")
        url = urlsplit(self.origin)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment or url.port not in (None, 443)):
            raise ValueError("Dashboard origin must be a fixed HTTPS origin")
        if not self.database_url or not (self.client_secret or self.certificate_path):
            raise ValueError("Server-side configuration incomplete")
        if not isinstance(self.access, dict) or (not self.access and not self.allow_tenant_members):
            raise ValueError("Explicit users or tenant member access required")
        self._validate_divisions(self.member_divisions, allow_empty=True)
        for oid, grant in self.access.items():
            if str(UUID(oid)) != oid or not isinstance(grant, dict):
                raise ValueError("Access must use Entra object UUIDs")
            if "disabled" in grant and type(grant["disabled"]) is not bool:
                raise ValueError("Disabled must be boolean")
            if grant.get("disabled") is True:
                continue
            if grant.get("role") not in ROLES:
                raise ValueError("Unknown dashboard role")
            self._validate_divisions(grant.get("divisions"))

    @staticmethod
    def _validate_divisions(divisions, *, allow_empty=False):
        if not isinstance(divisions, dict) or (not divisions and not allow_empty):
            raise ValueError("Explicit divisions required")
        for division, name in divisions.items():
            if (not isinstance(division, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", division)
                    or not isinstance(name, str) or not name.strip()):
                raise ValueError("Invalid division mapping")


def access_grant(settings, user):
    """User is from validated MSAL claims or our server-side session store."""
    if not user or user.get("tid") != settings.tenant_id:
        return None
    oid = user.get("oid")
    if not isinstance(oid, str):
        return None
    try:
        if str(UUID(oid)) != oid:
            return None
    except ValueError:
        return None
    if oid in settings.access:
        grant = settings.access[oid]
        return None if grant.get("disabled") is True else grant
    # Never infer membership from an email domain or from a missing claim.
    # acct is the optional Microsoft claim: 0=member, 1=guest.
    acct = user.get("account_type")
    if settings.allow_tenant_members and type(acct) in (int, str) and acct in (0, "0"):
        return {"role": "viewer", "divisions": settings.member_divisions}
    return None


class PostgresSessions:
    def __init__(self, database_url):
        self.database_url = database_url
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self):
        conn = psycopg.connect(self.database_url, autocommit=True, connect_timeout=5)
        conn.execute("SET statement_timeout = '5s'")
        if not self._ready:
            with self._lock:
                if not self._ready:
                    conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_sessions (
                        handle_hash TEXT PRIMARY KEY, kind TEXT NOT NULL,
                        payload JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL
                    )""")
                    conn.execute("CREATE INDEX IF NOT EXISTS dashboard_sessions_expiry ON dashboard_sessions (expires_at)")
                    self._ready = True
        return conn

    @staticmethod
    def digest(handle):
        return hashlib.sha256(handle.encode()).hexdigest()

    def put(self, kind, payload, ttl):
        handle = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("DELETE FROM dashboard_sessions WHERE expires_at <= NOW()")
            conn.execute(
                "INSERT INTO dashboard_sessions VALUES (%s, %s, %s::jsonb, NOW() + %s * INTERVAL '1 second')",
                (self.digest(handle), kind, json.dumps(payload), ttl),
            )
        return handle

    def get(self, handle, kind, *, consume=False):
        if not handle or not HANDLE.fullmatch(handle):
            return None
        with self._connect() as conn:
            if consume:
                # Atomic consumption prevents callback replay across app instances.
                sql = "DELETE FROM dashboard_sessions WHERE handle_hash=%s AND kind=%s AND expires_at>NOW() RETURNING payload"
            else:
                sql = "SELECT payload FROM dashboard_sessions WHERE handle_hash=%s AND kind=%s AND expires_at>NOW()"
            row = conn.execute(sql, (self.digest(handle), kind)).fetchone()
        return row[0] if row else None

    def delete(self, handle):
        if handle and HANDLE.fullmatch(handle):
            with self._connect() as conn:
                conn.execute("DELETE FROM dashboard_sessions WHERE handle_hash=%s", (self.digest(handle),))


@lru_cache(maxsize=4)
def postgres_sessions(database_url):
    return PostgresSessions(database_url)


def microsoft_client(settings):
    credential = settings.client_secret
    if settings.certificate_path:
        credential = {"private_key_pfx_path": settings.certificate_path}
        if settings.certificate_password:
            credential["passphrase"] = settings.certificate_password
    # A short-lived MSAL client means no persistent Microsoft token cache.
    return msal.ConfidentialClientApplication(
        settings.client_id, client_credential=credential,
        authority="https://login.microsoftonline.com/" + settings.tenant_id,
        exclude_scopes=["offline_access"], enable_pii_log=False, timeout=10,
    )


def authorized_user(settings, claims):
    """Evaluate only claims returned by MSAL's completed, validated code flow."""
    if (claims.get("tid") != settings.tenant_id
            or claims.get("iss") != "https://login.microsoftonline.com/" + settings.tenant_id + "/v2.0"
            or claims.get("aud") != settings.client_id
            or not isinstance(claims.get("exp"), (int, float))
            or claims["exp"] <= time.time()):
        return None
    user = {"oid": claims.get("oid"), "tid": settings.tenant_id,
            "name": str(claims.get("name") or "Medewerker")[:200],
            "account_type": claims.get("acct")}
    return user if access_grant(settings, user) else None


def cookie(response, name, value, ttl, *, flow=False):
    response.set_cookie(name, value, max_age=ttl, path="/", secure=True,
                        httponly=True, samesite="none" if flow else "lax")


def clear_cookie(response, name):
    response.delete_cookie(name, path="/", secure=True, httponly=True,
                           samesite="none" if name == FLOW_COOKIE else "lax")


def create_dashboard_app(settings_provider=Settings.from_env,
                         store_factory=postgres_sessions,
                         client_factory=microsoft_client):
    dashboard = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    assets = Path(__file__).parent

    @dashboard.middleware("http")
    async def protect(request, call_next):
        # All routes, including future endpoints/assets, are private by default.
        route = request.url.path.removeprefix(request.scope.get("root_path", ""))
        try:
            settings = settings_provider()
            store = store_factory(settings.database_url)
            request.state.settings, request.state.store = settings, store
            request.state.nonce = secrets.token_urlsafe(24)
            if route not in ("/login", "/auth/start", "/auth/callback"):
                session = await run_in_threadpool(store.get, request.cookies.get(SESSION_COOKIE), "session")
                grant = access_grant(settings, session)
                if not grant:
                    response = (JSONResponse({"detail": "Aanmelden vereist"}, status_code=401)
                                if route.startswith("/api/") or request.method != "GET"
                                else RedirectResponse("/dashboard/login", status_code=303))
                    clear_cookie(response, SESSION_COOKIE)
                else:
                    request.state.user, request.state.grant = session, grant
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except Exception:
            # Never expose SQL, provider responses, auth codes or credentials.
            response = HTMLResponse("<h1>Dashboard tijdelijk niet beschikbaar</h1><p>De Microsoft-koppeling is nog niet volledig ingesteld of bereikbaar.</p>", status_code=503)
        nonce = getattr(request.state, "nonce", "")
        response.headers.update({
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        return response

    @dashboard.get("/login", response_class=HTMLResponse)
    def login_page():
        return (assets / "login.html").read_text()

    @dashboard.get("/auth/start")
    def auth_start(request: Request):
        settings, store = request.state.settings, request.state.store
        flow = client_factory(settings).initiate_auth_code_flow(
            scopes=[], redirect_uri=settings.callback, response_mode="form_post",
            prompt="select_account",
        )
        # No token or authorization code is returned in a URL/access log.
        if not isinstance(flow.get("state"), str) or not flow["state"]:
            return JSONResponse({"detail": "Aanmelden niet beschikbaar"}, status_code=503)
        target = urlsplit(flow["auth_uri"])
        if target.scheme != "https" or target.hostname != "login.microsoftonline.com":
            return JSONResponse({"detail": "Aanmelden niet beschikbaar"}, status_code=503)
        store.delete(request.cookies.get(FLOW_COOKIE))
        handle = store.put("flow", flow, FLOW_TTL)
        response = RedirectResponse(flow["auth_uri"], status_code=303)
        cookie(response, FLOW_COOKIE, handle, FLOW_TTL, flow=True)
        return response

    @dashboard.post("/auth/callback")
    async def callback(request: Request):
        # POST + SameSite=None flow cookie supports Microsoft's form_post safely.
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return JSONResponse({"detail": "Ongeldige aanmelding"}, status_code=400)
        body = b""
        async for part in request.stream():
            body += part
            if len(body) > 16384:
                return JSONResponse({"detail": "Ongeldige aanmelding"}, status_code=400)
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=12)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"detail": "Ongeldige aanmelding"}, status_code=400)
        if any(len(v) != 1 for v in values.values()):
            return JSONResponse({"detail": "Ongeldige aanmelding"}, status_code=400)
        payload = {k: v[0] for k, v in values.items()}
        return await run_in_threadpool(finish_login, request, payload)

    def finish_login(request, payload):
        settings, store = request.state.settings, request.state.store
        flow = store.get(request.cookies.get(FLOW_COOKIE), "flow", consume=True)
        failed = JSONResponse({"detail": "Aanmelden mislukt of geen toegang. Start opnieuw via het dashboard."}, status_code=403)
        clear_cookie(failed, FLOW_COOKIE)
        if not flow or not secrets.compare_digest(flow.get("state", ""), payload.get("state", "")):
            return failed
        try:
            result = client_factory(settings).acquire_token_by_auth_code_flow(flow, payload)
        except (ValueError, RuntimeError):
            return failed
        user = authorized_user(settings, result.get("id_token_claims", {})) if not result.get("error") else None
        if not user:
            return failed
        user["csrf"] = secrets.token_urlsafe(32)
        ttl = max(1, min(SESSION_TTL, int(result["id_token_claims"]["exp"] - time.time())))
        store.delete(request.cookies.get(SESSION_COOKIE))
        handle = store.put("session", user, ttl)
        response = RedirectResponse("/dashboard/", status_code=303)
        clear_cookie(response, FLOW_COOKIE)
        cookie(response, SESSION_COOKIE, handle, ttl)
        return response

    @dashboard.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return (assets / "index.html").read_text().replace("<script>", '<script nonce="' + request.state.nonce + '">')

    @dashboard.get("/api/me")
    def me(request: Request):
        return {"name": request.state.user["name"], "role": request.state.grant["role"],
                "divisions": request.state.grant["divisions"], "csrf": request.state.user["csrf"],
                "worklist_connected": False}

    @dashboard.get("/api/divisions/{division}/worklist")
    def worklist(division: str, request: Request):
        if division not in request.state.grant["divisions"]:
            return JSONResponse({"detail": "Geen toegang tot deze administratie"}, status_code=403)
        # Live exception data/actions need a separately reviewed backend integration.
        return {"division": division, "connected": False, "items": []}

    @dashboard.post("/auth/logout")
    def logout(request: Request):
        supplied = request.headers.get("x-csrf-token", "")
        if (request.headers.get("origin") != request.state.settings.origin
                or not supplied or not secrets.compare_digest(supplied, request.state.user["csrf"])):
            return JSONResponse({"detail": "Ongeldig verzoek"}, status_code=403)
        request.state.store.delete(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"redirect": "/dashboard/login"})
        clear_cookie(response, SESSION_COOKIE)
        return response

    return dashboard
