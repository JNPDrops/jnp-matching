"""Temporary, opt-in execution of the specifically approved marker rule.

User approval: 2026-10-02 13:28:19 UTC. No public write endpoint is added.
Only one AllocationRule POST is possible; a durable ledger prevents replay.
Remove this module hook after verification. Existing financial gates stay shut.
"""
import os

_ACTION = "20261002-direct-woo-bank-100100"

if os.getenv("JNP_APPROVED_RULE_ACTION") == _ACTION:
    import asyncio
    import json
    from datetime import datetime, timezone
    from urllib.parse import urljoin, urlsplit
    from uuid import UUID

    import httpx
    import psycopg
    from . import main as _main

    _WORDS = "DIRECT_WOO_BANK"
    _DEADLINE = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
    _RULE_URL = "https://start.exactonline.nl/api/v1/beta/3977752/cashflow/AllocationRule"

    def _emit(state, **extra):
        print("JNP_APPROVED_MARKER_RULE " + json.dumps({
            "action": _ACTION, "state": state, "division": 3977752,
            "words": _WORDS, "account_code": "100100", **extra,
        }, ensure_ascii=True, default=str), flush=True)

    def _matches(rule, account_id):
        return (
            str(rule.get("Words") or "").strip() == _WORDS
            and str(rule.get("Account") or "").lower() == account_id.lower()
            and not any(rule.get(k) for k in (
                "AccountBankAccount", "GLAccount", "Costcenter", "Costunit", "VATCode"
            ))
        )

    async def _run():
        conn = None
        sent = False
        try:
            if datetime.now(timezone.utc) >= _DEADLINE:
                raise RuntimeError("Approval execution window has expired")
            if (_main.DIVISION != 3977752 or _main.COLLECTIVE_DEBTOR_CODE != "100100"
                    or _main.BASE_URL != "https://start.exactonline.nl"):
                raise RuntimeError("Wrong administration, debtor or Exact host")
            if _main.ENABLE_ORDER_RULE_WRITES or _main.ENABLE_DIRECT_MATCH_WRITES:
                raise RuntimeError("General financial write flags must remain disabled")
            if not _main.DATABASE_URL:
                raise RuntimeError("Persistent action ledger is required")
            conn = psycopg.connect(_main.DATABASE_URL, autocommit=True, connect_timeout=10)
            conn.execute("SET statement_timeout = '10s'")
            conn.execute("""CREATE TABLE IF NOT EXISTS jnp_approved_rule_actions (
                action_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                result_json JSONB, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
            locked = conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (3977752, 100100)).fetchone()[0]
            if not locked:
                _emit("SKIPPED_CONCURRENT_EXECUTION")
                return
            previous = conn.execute(
                "SELECT status, result_json FROM jnp_approved_rule_actions WHERE action_id=%s",
                (_ACTION,),
            ).fetchone()
            if previous and previous[0] == "verified":
                _emit("ALREADY_VERIFIED_NO_REPLAY", result=previous[1])
                return

            account = await _main.find_collective_debtor()
            account_id = str(UUID(str(account["ID"])))
            token = await _main._access_token()
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

                async def get_json(url, params=None):
                    response = await client.get(url, params=params, headers=headers)
                    if response.status_code != 200:
                        raise RuntimeError("Exact GET failed: HTTP " + str(response.status_code))
                    return response.json()

                # Verify the actual relation, not only the receivables-derived ID.
                account_data = await get_json(
                    "https://start.exactonline.nl/api/v1/3977752/crm/Accounts",
                    {"$filter": "ID eq guid'" + account_id + "'", "$select": "ID,Code,Name", "$top": "2"},
                )
                accounts = _main._extract_results(account_data)
                if (len(accounts) != 1 or str(accounts[0].get("Code") or "").strip() != "100100"
                        or str(accounts[0].get("ID") or "").lower() != account_id.lower()):
                    raise RuntimeError("Debtor GUID could not be independently verified")

                async def read_marker():
                    url = _RULE_URL
                    params = {"$select": "ID,Account,AccountBankAccount,GLAccount,Words,Costcenter,Costunit,VATCode"}
                    found = []
                    seen = set()
                    for _ in range(50):
                        if url in seen:
                            raise RuntimeError("Repeated allocation-rule page")
                        seen.add(url)
                        data = await get_json(url, params)
                        data = data.get("d", data)
                        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                            raise RuntimeError("Unexpected allocation-rule response; refusing to write")
                        found.extend(r for r in data["results"]
                                     if _WORDS.casefold() in str(r.get("Words") or "").casefold())
                        next_page = data.get("__next")
                        if not next_page:
                            return found
                        url = urljoin(_RULE_URL, next_page)
                        parts = urlsplit(url)
                        if (parts.scheme != "https" or parts.netloc != "start.exactonline.nl"
                                or parts.path.rstrip("/") != urlsplit(_RULE_URL).path):
                            raise RuntimeError("Unsafe allocation-rule pagination URL")
                        params = None
                    raise RuntimeError("Allocation-rule pagination limit exceeded")

                existing = await read_marker()
                if existing and (len(existing) != 1 or not _matches(existing[0], account_id)):
                    raise RuntimeError("Existing marker rule conflicts; no rule overwritten")
                if not existing and previous:
                    _emit("PREVIOUS_ATTEMPT_NOT_REPLAYED", previous_status=previous[0])
                    return
                if not existing:
                    # Record BEFORE sending. A crash/timeout must never repeat the POST.
                    conn.execute(
                        "INSERT INTO jnp_approved_rule_actions(action_id,status) VALUES (%s,'attempted')",
                        (_ACTION,),
                    )
                    _emit("POST_ATTEMPT", account_id=account_id, account_name=accounts[0].get("Name"))
                    sent = True
                    response = await client.post(
                        _RULE_URL, json={"Words": _WORDS, "Account": account_id}, headers=headers,
                    )
                    if response.status_code not in (200, 201, 202, 204):
                        # Include a bounded, redacted vendor message for diagnosis.
                        detail = ""
                        try:
                            error = response.json().get("error", {})
                            message = error.get("message", "")
                            detail = str(message.get("value", "") if isinstance(message, dict) else message)
                            for secret in (token, _main.CLIENT_SECRET):
                                if secret:
                                    detail = detail.replace(secret, "[REDACTED]")
                        except (ValueError, AttributeError):
                            pass
                        _emit("POST_REJECTED", http_status=response.status_code, detail=detail[:600])
                        return
                    _emit("POST_ACCEPTED", http_status=response.status_code)
                    for _ in range(3):
                        existing = await read_marker()
                        if existing:
                            break
                        await asyncio.sleep(2)
                if len(existing) != 1 or not _matches(existing[0], account_id) or not existing[0].get("ID"):
                    raise RuntimeError("Rule read-back did not prove the exact requested configuration")
                result = {
                    "rule_id": str(UUID(str(existing[0]["ID"]))),
                    "words": existing[0]["Words"], "account_id": account_id,
                    "account_code": "100100", "account_name": accounts[0].get("Name"),
                    "created_this_run": sent, "verified_by_get": True,
                    "bank_imports_executed": 0, "matchsets_executed": 0,
                    "order_rule_writes": _main.ENABLE_ORDER_RULE_WRITES,
                    "direct_match_writes": _main.ENABLE_DIRECT_MATCH_WRITES,
                }
                conn.execute("""INSERT INTO jnp_approved_rule_actions(action_id,status,result_json)
                    VALUES (%s,'verified',%s::jsonb) ON CONFLICT(action_id)
                    DO UPDATE SET status='verified',result_json=EXCLUDED.result_json,updated_at=NOW()""",
                    (_ACTION, json.dumps(result)),
                )
                _emit("VERIFIED", result=result)
        except Exception as exc:
            # Never log token-store contents or connection-string exceptions.
            _emit("FAILED", error_type=type(exc).__name__,
                  detail=str(exc)[:600] if type(exc) is RuntimeError else "See error type; secrets omitted",
                  post_attempted=sent, http_status=getattr(exc, "status_code", None))
        finally:
            if conn is not None:
                conn.close()  # Also releases the advisory lock.

    async def _start():
        _emit("STARTING")
        _main.app.state.approved_marker_rule_task = asyncio.create_task(_run())

    _main.app.add_event_handler("startup", _start)
