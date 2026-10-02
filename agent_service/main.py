import ast
import base64
import difflib
import json
import os
import re
import time
import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from openai import OpenAI

app = FastAPI(title="JNP Development Agent", version="1.5.0")

TARGET_URL = os.getenv("TARGET_URL", "https://jnp-matching.onrender.com").rstrip("/")
GITHUB_REPO = os.getenv("GITHUB_REPO", "JNPDrops/jnp-matching")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")
AGENT_APPLY_CHANGES = os.getenv("AGENT_APPLY_CHANGES", "false").lower() == "true"
MAX_AGENT_ITERATIONS = int(os.getenv("MAX_AGENT_ITERATIONS", "1"))
GOLDEN_ORDER = os.getenv("GOLDEN_ORDER", "48451")
GOLDEN_BANK_LINE_ID = os.getenv("GOLDEN_BANK_LINE_ID", "591003b9-cfb0-4158-b23c-fdc106801a5e")
GOLDEN_RECEIVABLE_ENTRY = int(os.getenv("GOLDEN_RECEIVABLE_ENTRY", "26722659"))
GOLDEN_AMOUNT = os.getenv("GOLDEN_AMOUNT", "78.60")


DEFAULT_RESEARCH_GOAL = """Investigate the safest supported way in Exact Online to take one already imported existing BankEntryLine that is still on suspense G/L 1360 with no debtor account, assign that specific bank line to debtor account 100100, and then match it directly against the already identified open receivable, without creating a general-journal/memorial entry. The golden case is bank line GUID 591003b9-cfb0-4158-b23c-fdc106801a5e, bank entry 26205149, line 75, description VERCAIGNE BO 48451, EUR 78.60, expected receivable TD48451 / entry 26722659 / account 100100. Research only: do not execute writes and do not propose relying on undocumented destructive calls without clearly labeling them unsupported."""

FORBIDDEN_CHANGED_LINE_PATTERNS = [
    r"ENABLE_.*WRITES",
    r"upload_matchset",
    r"MATCHSETS_URL",
    r"exact_beta_post",
    r"exact_post\(",
    r"@app\.post",
    r"/execute",
    r"build_direct_match_xml",
]

STARTUP_SELFTEST = os.getenv("STARTUP_SELFTEST", "true").lower() == "true"
STARTUP_SELFTEST_DELAY_SECONDS = int(os.getenv("STARTUP_SELFTEST_DELAY_SECONDS", "8"))


async def _startup_selftest_runner() -> None:
    """Run the full read-only regression suite after each agent deployment.

    Results are emitted as a single JSON log line so Render logs become the
    canonical place to verify deployments without requiring a browser click.
    Financial write flags are only read/validated by run_regression_tests().
    """
    await asyncio.sleep(max(0, STARTUP_SELFTEST_DELAY_SECONDS))
    try:
        result = await run_regression_tests()
        print("JNP_STARTUP_SELFTEST " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    except Exception as exc:
        print("JNP_STARTUP_SELFTEST_ERROR " + json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)


@app.on_event("startup")
async def startup_selftest() -> None:
    if STARTUP_SELFTEST:
        asyncio.create_task(_startup_selftest_runner())



def _github_headers() -> dict[str, str]:
    if not GITHUB_TOKEN:
        raise HTTPException(503, "GITHUB_TOKEN is not configured on the development-agent service.")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def github_get_main() -> tuple[str, str]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/app/main.py"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"ref": GITHUB_BRANCH}, headers=_github_headers())
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"GitHub read failed: {r.text[:500]}")
    data = r.json()
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


async def github_put_main(code: str, sha: str, message: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/app/main.py"
    payload = {
        "message": message,
        "content": base64.b64encode(code.encode()).decode(),
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, json=payload, headers=_github_headers())
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"GitHub commit failed: {r.text[:800]}")
    return r.json()


async def fetch_json(path: str) -> Any:
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        r = await client.get(f"{TARGET_URL}{path}")
    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


async def run_regression_tests() -> dict[str, Any]:
    failures: list[str] = []
    observations: list[str] = []
    safety = await fetch_json("/api/safety")
    if safety.get("order_rule_writes") or safety.get("direct_match_writes"):
        failures.append("Financial write flags are enabled; development automation must remain read-only.")
    if str(safety.get("suspense_gl_code")) != "1360":
        failures.append(f"Unexpected suspense GL: {safety.get('suspense_gl_code')}")
    if str(safety.get("collective_debtor_code")) != "100100":
        failures.append(f"Unexpected collective debtor: {safety.get('collective_debtor_code')}")

    feed = await fetch_json("/api/candidates?limit=200")
    items = feed.get("items", [])
    golden = [i for i in items if str(i.get("order_number") or "") == GOLDEN_ORDER]
    if len(golden) != 1:
        failures.append(f"Golden order {GOLDEN_ORDER}: expected 1 candidate row, found {len(golden)}.")
    else:
        row = golden[0]
        observations.append(f"Golden row: {row}")
        checks = {
            "status": row.get("status") == "MATCH_CANDIDATE",
            "bank_line_id": row.get("bank_line_id") == GOLDEN_BANK_LINE_ID,
            "receivable_entry": int(row.get("receivable_entry") or 0) == GOLDEN_RECEIVABLE_ENTRY,
            "bank_amount": str(row.get("bank_amount")) in {GOLDEN_AMOUNT, "78.6"},
            "receivable_amount": str(row.get("receivable_amount")) in {GOLDEN_AMOUNT, "78.6"},
            "account_code": str(row.get("account_code")) == "100100",
        }
        for name, ok in checks.items():
            if not ok:
                failures.append(f"Golden order {GOLDEN_ORDER} failed check: {name}.")

    paynetics = [i for i in items if "PAYNETICS" in str(i.get("description") or "").upper()]
    if any(i.get("status") == "MATCH_CANDIDATE" for i in paynetics):
        failures.append("A Paynetics payout was incorrectly marked MATCH_CANDIDATE.")

    # v1.4 golden diagnostic: prove the exact imported bank line remains on
    # suspense and is not MatchSets-eligible before allocation. GET-only.
    try:
        diag = await fetch_json(
            f"/api/diagnostics/bank-line/{GOLDEN_BANK_LINE_ID}?receivable_entry={GOLDEN_RECEIVABLE_ENTRY}"
        )
        observations.append(
            "Golden diagnostic: " + json.dumps({
                "allocation_state": diag.get("allocation_state"),
                "matchsets_preconditions": diag.get("matchsets_preconditions"),
                "computed_result": diag.get("computed_result"),
            }, ensure_ascii=False)
        )
        if diag.get("read_only") is not True or diag.get("writes_executed") is not False:
            failures.append("Golden diagnostic is not explicitly read-only.")
        identity = diag.get("bank_line_identity") or {}
        if str(identity.get("id") or "") != GOLDEN_BANK_LINE_ID:
            failures.append("Golden diagnostic returned the wrong BankEntryLine ID.")
        if int(identity.get("line_number") or 0) != 75:
            failures.append("Golden diagnostic returned an unexpected bank line number.")
        allocation = diag.get("allocation_state") or {}
        if str(allocation.get("gl_account_code") or "") != "1360":
            failures.append("Golden diagnostic no longer sees the bank line on suspense G/L 1360.")
        if allocation.get("requires_allocation") is not True:
            failures.append("Golden diagnostic should report allocation_required=true.")
        computed = diag.get("computed_result") or {}
        if computed.get("matchsets_eligible") is not False:
            failures.append("Golden diagnostic incorrectly reports MatchSets eligibility before allocation.")
    except Exception as exc:
        failures.append(f"Golden read-only diagnostic failed: {exc}")

    bad_negative = []
    for i in items:
        try:
            amount = float(i.get("bank_amount") or 0)
        except Exception:
            continue
        if amount <= 0 and i.get("status") != "SKIP_NOT_RECEIPT":
            bad_negative.append(i.get("bank_line_id"))
    if bad_negative:
        failures.append(f"Non-positive receipts were not skipped: {bad_negative[:5]}")

    return {
        "ok": not failures,
        "failures": failures,
        "observations": observations,
        "summary": {k: feed.get(k) for k in ("total", "matches", "reviews", "skipped")},
        "safety": safety,
    }


def safety_check_patch(old: str, new: str) -> tuple[bool, list[str], str]:
    issues: list[str] = []
    try:
        ast.parse(new)
    except SyntaxError as e:
        issues.append(f"Python syntax error: {e}")

    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))
    changed = [line[1:] for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    for line in changed:
        for pattern in FORBIDDEN_CHANGED_LINE_PATTERNS:
            if re.search(pattern, line, flags=re.I):
                issues.append(f"Forbidden financial/write-path change matched {pattern!r}: {line[:180]}")
    if len(new) < len(old) * 0.75:
        issues.append("Replacement unexpectedly removes more than 25% of app/main.py.")
    return not issues, issues, diff



def research_exact_allocation(source: str, tests: dict[str, Any], goal: str) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OPENAI_API_KEY is not configured on the development-agent service.")
    client = OpenAI()
    prompt = f"""
You are the research engineer for JNP Matching, a Dutch Exact Online bank reconciliation application.

GOAL
{goal}

STRICT SAFETY RULES
- Research and read-only diagnostics only. Never execute a write against Exact Online.
- Do not change GitHub code in this endpoint.
- Distinguish clearly between: officially documented API capability, third-party observations, UI-only behavior, and speculation.
- Prefer current Exact Online documentation/support material and current API behavior.
- Existing financial write flags must remain false.
- A solution that closes the invoice but leaves the original bank line open is NOT acceptable.
- A solution that creates a general journal / memorial workaround is NOT acceptable for the target design.
- Focus on addressing one specific existing BankEntryLine by GUID/EntryID/LineNumber.

CURRENT LIVE REGRESSION STATE
{json.dumps(tests, ensure_ascii=False, indent=2)}

CURRENT app/main.py
```python
{source}
```

Use web search to research Exact Online documentation, official support pages, current API references, and reputable technical evidence.
Return a concise engineering report with these exact headings:
1. Conclusion
2. Supported public API options
3. Unsupported / UI-only options
4. Evidence and source URLs
5. Safest next read-only experiment
6. Proposed code change for diagnostics only
7. Stop condition before any financial write

Be explicit if the public API cannot perform the required allocation of an already imported bank line.
"""
    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
    except Exception as e:
        raise HTTPException(502, f"Research model call failed: {e}")
    return {
        "goal": goal,
        "report": resp.output_text,
        "writes_executed": False,
        "code_committed": False,
        "apply_changes": AGENT_APPLY_CHANGES,
    }


def propose_fix(source: str, test_result: dict[str, Any]) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OPENAI_API_KEY is not configured on the development-agent service.")
    client = OpenAI()
    prompt = f"""
You are maintaining a Python FastAPI reconciliation application called JNP Matching.
Your job is restricted to READ-ONLY matching logic and diagnostics. Do not alter any financial write route,
Exact POST/XML MatchSets behavior, write flags, environment variable names, OAuth behavior, or execution endpoints.

Regression test result:
{json.dumps(test_result, ensure_ascii=False, indent=2)}

Return ONLY valid JSON with exactly these keys:
- reasoning: concise explanation
- replacement_main_py: the full corrected app/main.py source

Requirements:
- Preserve all existing endpoints and features unless a read-only bug requires adjustment.
- Keep financial writes disabled and untouched.
- Golden case: order 48451 must resolve uniquely from bank line GUID {GOLDEN_BANK_LINE_ID} to TD48451 / entry {GOLDEN_RECEIVABLE_ENTRY}, amount EUR {GOLDEN_AMOUNT}, account 100100.
- Paynetics payouts must never become MATCH_CANDIDATE in this flow.
- Non-positive bank lines must remain SKIP_NOT_RECEIPT.
- Make the smallest reasonable code change.

Current app/main.py:
```python
{source}
```
"""
    resp = client.responses.create(model=OPENAI_MODEL, input=prompt)
    try:
        data = json.loads(resp.output_text)
    except Exception as e:
        raise HTTPException(502, f"Model did not return valid JSON: {e}; output={resp.output_text[:1000]}")
    if not isinstance(data.get("replacement_main_py"), str):
        raise HTTPException(502, "Model response missing replacement_main_py.")
    return data


async def wait_for_target(timeout_seconds: int = 180) -> dict[str, Any]:
    start = time.time()
    last_error = ""
    while time.time() - start < timeout_seconds:
        try:
            safety = await fetch_json("/api/safety")
            return {"ready": True, "safety": safety}
        except Exception as e:
            last_error = str(e)
        await __import__("asyncio").sleep(5)
    return {"ready": False, "error": last_error}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(f"""
    <html><body style='font-family:system-ui;max-width:950px;margin:40px auto;line-height:1.5'>
    <h1>JNP Development Agent</h1>
    <p>This service is separate from JNP Matching and never performs financial writes.</p>
    <p><strong>Target:</strong> {TARGET_URL}<br><strong>Repo:</strong> {GITHUB_REPO} / {GITHUB_BRANCH}<br>
    <strong>Apply changes:</strong> {AGENT_APPLY_CHANGES}</p>
    <p><a href='/test'>Run regression tests</a></p>
    <p>POST <code>/cycle</code> runs one regression-repair cycle. With <code>AGENT_APPLY_CHANGES=false</code> it only proposes and safety-checks a patch.</p>
    <p>POST <code>/research</code> performs a web-backed, read-only engineering research cycle for the Exact bank-allocation problem. It never commits code or writes to Exact.</p>
    </body></html>
    """)


@app.get("/test")
async def test_endpoint():
    try:
        return await run_regression_tests()
    except Exception as e:
        raise HTTPException(502, str(e))



@app.post("/research")
async def research(goal: str | None = None):
    try:
        tests = await run_regression_tests()
        source, _sha = await github_get_main()
        effective_goal = (goal or DEFAULT_RESEARCH_GOAL).strip()
        result = research_exact_allocation(source, tests, effective_goal)
        return {
            "ok": True,
            "tests_green": tests.get("ok", False),
            "safety": tests.get("safety"),
            **result,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/cycle")
async def cycle():
    history: list[dict[str, Any]] = []
    for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
        tests = await run_regression_tests()
        history.append({"iteration": iteration, "tests": tests})
        if tests["ok"]:
            return {"ok": True, "message": "All regression tests already pass; no code change required.", "history": history}

        source, sha = await github_get_main()
        proposal = propose_fix(source, tests)
        replacement = proposal["replacement_main_py"]
        safe, issues, diff = safety_check_patch(source, replacement)
        history[-1]["proposal_reasoning"] = proposal.get("reasoning")
        history[-1]["safety_ok"] = safe
        history[-1]["safety_issues"] = issues
        history[-1]["diff_preview"] = diff[:12000]
        if not safe:
            return {"ok": False, "message": "Patch rejected by safety gate.", "history": history}
        if not AGENT_APPLY_CHANGES:
            return {"ok": False, "message": "Safe patch proposed; AGENT_APPLY_CHANGES=false so nothing was committed.", "history": history}

        commit = await github_put_main(replacement, sha, f"JNP dev-agent: repair read-only regression (iteration {iteration})")
        history[-1]["commit"] = commit.get("commit", {}).get("html_url")
        history[-1]["deployment_wait"] = await wait_for_target()
        # Continue loop and re-test after target becomes healthy.

    final = await run_regression_tests()
    return {"ok": final["ok"], "message": "Development cycle finished.", "final": final, "history": history}
