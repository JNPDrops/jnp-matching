"""Bounded, non-financial guard for the first legacy-web shutdown.

This is NOT a migration approval or a force-drain. It waits for existing owners
to release their normal advisory locks, holds those same locks, and reports
bounded status only. Release/expiry means the legacy service may resume work.
The operator must stop the old service while the guard is still holding; never
infer a completed cutover from an expired guard or a disconnected shell.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from urllib.request import urlopen


LEGACY_COMMIT = "cf75100f845d1296eaaa60f85225caf3c656e4f2"
# Maintenance nests the Woo/tax locks. Take it first and never retain a partial
# acquisition while waiting, so an existing maintenance pass can finish.
LOCKS = (397775213600, 3977752109372, 3977752867393,
         3977752100100, 397775226)
TERMINAL_IMPORTS = frozenset({"prepared", "import_verified", "requires_review",
                              "prior_write_claim_reconcile_only", "failed"})
TERMINAL_MATCHES = frozenset({"prepared", "group_verified", "complete",
                             "incomplete", "no_eligible_items", "failed",
                             "blocked", "paused"})


class CutoverBlocked(RuntimeError):
    pass


def verify_health(value):
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise CutoverBlocked("legacy_health_unavailable")
    if value.get("division") != 3977752:
        raise CutoverBlocked("wrong_division")
    if any(value.get(key) is not False for key in
           ("order_rule_writes", "direct_match_writes")):
        raise CutoverBlocked("manual_http_writes_not_disabled")


@contextmanager
def acquire_all(conn):
    held = []
    try:
        for key in LOCKS:
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                raise CutoverBlocked("legacy_operation_still_running")
            held.append(key)
        yield
    finally:
        for key in reversed(held):
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def verify_jobs(conn):
    # Only metadata is selected. No tokens, source rows, exports or customer IDs.
    if conn.execute("SELECT count(*) FROM fibonatix_import_jobs "
                    "WHERE data->>'running'='true'").fetchone()[0]:
        raise CutoverBlocked("fibonatix_running_flag_requires_review")
    imports = conn.execute("SELECT summary->>'state', count(*) "
                           "FROM icepay_receipt_import_runs GROUP BY 1").fetchall()
    matches = conn.execute("SELECT summary->>'state', count(*) "
                           "FROM icepay_matching_runs GROUP BY 1").fetchall()
    for rows, allowed, message in (
        (imports, TERMINAL_IMPORTS, "icepay_import_requires_review"),
        (matches, TERMINAL_MATCHES, "icepay_match_requires_review"),
    ):
        if any(state not in allowed for state, _count in rows):
            raise CutoverBlocked(message)
    # Journal creation has no global lock. Its durable result must be terminal.
    journal = conn.execute("SELECT result->>'status', count(*) "
                           "FROM icepay_journal_tasks GROUP BY 1").fetchall()
    if any(state not in {"inspected", "already_exists", "created_verified"}
           for state, _count in journal):
        raise CutoverBlocked("icepay_journal_requires_review")
    return {"import_states": dict(imports), "match_states": dict(matches),
            "journal_states": dict(journal)}


def other_python_processes():
    # Do not inspect environments or print process command lines: they may
    # contain credentials. An unidentified Python job is a blocking condition.
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "comm").read_text().strip().lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if command.startswith("python"):
            found.append(int(entry.name))
    return found


def emit(state, **metadata):
    print(json.dumps({"state": state, "financial_writes": False,
                      **metadata}, sort_keys=True), flush=True)


def hold_connection(conn, seconds):
    """Retain locks across the old container's graceful shutdown signals.

    A terminal disconnect or SIGTERM must not reopen admission while uvicorn
    is still shutting down. Ctrl-C remains an explicit operator abort; expiry
    is bounded and never evidence that the old service stopped.
    """
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    emit("guard_ready", seconds=seconds)
    until = time.monotonic() + seconds
    while time.monotonic() < until and not stop.wait(min(5, max(0, until - time.monotonic()))):
        conn.execute("SELECT 1")
    emit("guard_releasing_locks", cutover_complete=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "hold"))
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 1800:
        parser.error("seconds must be between 1 and 1800")
    try:
        actual = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True).strip()
        if actual != LEGACY_COMMIT:
            raise CutoverBlocked("unexpected_live_commit")
        if other_python_processes():
            raise CutoverBlocked("other_python_job_requires_review")
        with urlopen("http://127.0.0.1:10000/health", timeout=10) as response:
            verify_health(json.load(response))
        import psycopg
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True,
                             connect_timeout=10) as conn:
            conn.execute("SET default_transaction_read_only=on")
            conn.execute("SET statement_timeout='10s'")
            with acquire_all(conn):
                metadata = verify_jobs(conn)
                emit("legacy_locks_held", legacy_commit=actual, **metadata)
                if args.action == "check":
                    emit("check_complete_releasing_locks", cutover_complete=False)
                    return 0
                hold_connection(conn, args.seconds)
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, CutoverBlocked) else type(error).__name__
        emit("blocked", reason=reason, cutover_complete=False)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
