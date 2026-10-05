"""Explicit background-task ownership; split execution is deliberately gated.

The legacy web lifespan uses exactly the original task set. Describing a role
does not authorize starting a second owner of its queues or historical jobs.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
import logging

log = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class TaskSpec:
    name: str
    role: str
    module: str
    function: str
    with_app: bool = False
    continuous: bool = False


TASKS = (
    TaskSpec("dashboard-readiness", "web", "app.dashboard.worklist", "readiness_probe"),
    TaskSpec("debtor-routing", "routing", "operations.automatic_debtor_routing", "serve", True, True),
    TaskSpec("woo-bank-rules", "woo-rules", "operations.woo_iban_rules", "serve", True, True),
    TaskSpec("tax-rules", "tax", "operations.tax_agent", "serve", True, True),
    TaskSpec("bank-maintenance", "maintenance", "operations.allocation_maintenance", "serve", True, True),
    TaskSpec("fibonetics-report", "reports", "operations.fibonetics_read_report", "run", True),
    TaskSpec("recent-import-report", "reports", "operations.recent_import_read_report", "run", True),
    TaskSpec("paragon-login-probe", "reports", "operations.paragon_login_probe", "run"),
    TaskSpec("exact-login-probe", "reports", "operations.exact_login_probe", "run"),
    TaskSpec("icepay-journal", "icepay", "operations.icepay_journal_task", "run", True),
    TaskSpec("icepay-fetch", "icepay", "operations.icepay_fetch_probe", "run"),
    TaskSpec("icepay-transactions", "icepay", "operations.icepay_transactions", "run"),
)
ROLES = ("web", "routing", "woo-rules", "tax", "maintenance", "fibonatix", "icepay", "reports")

# Remove these gates only as the corresponding handover requirements are proven.
# There is intentionally no environment-variable override for premature splitting.
# PostgreSQL process tests passed in GitHub run 37285557409 (2026-10-05).
# The legacy web process still needs the same ownership and write fencing.
COMMON_BLOCKERS = ("legacy_role_ownership_pending", "lease_write_fencing_pending",
                   "verified_drain_pending")
ROLE_BLOCKERS = {
    role: COMMON_BLOCKERS + (("http_job_queue_pending",) if role in {"web", "fibonatix"} else ())
    for role in ROLES
}


class SeparationNotReady(RuntimeError):
    pass


def task_specs(role):
    if role == "legacy":
        return TASKS
    if role not in ROLES:
        raise ValueError("unknown_runtime_role")
    return tuple(task for task in TASKS if task.role == role)


def role_manifest(role):
    specs = task_specs(role)
    return {"role": role, "tasks": [task.name for task in specs],
            "execution_ready": role == "legacy" or not ROLE_BLOCKERS[role],
            "blockers": [] if role == "legacy" else list(ROLE_BLOCKERS[role])}


def require_ready(role):
    manifest = role_manifest(role)
    if not manifest["execution_ready"]:
        raise SeparationNotReady("role_not_ready:" + role + ":" + ",".join(manifest["blockers"]))


def resolve(spec):
    return getattr(import_module(spec.module), spec.function)


class BackgroundTasks:
    def __init__(self, app_module, role="legacy", *, resolver=resolve):
        require_ready(role)
        self.app_module = app_module
        self.role = role
        self.specs = task_specs(role)
        self.resolver = resolver
        self.tasks = {}
        self.routing_stop = asyncio.Event()
        self.started = False

    async def start(self):
        if self.started:
            raise RuntimeError("runtime_already_started")
        # Resolve every import before starting anything: a missing module must
        # not leave a partially started collection of financial tasks behind.
        functions = [(spec, self.resolver(spec)) for spec in self.specs]
        self.started = True
        try:
            for spec, function in functions:
                if spec.role == "routing":
                    from operations.routing_role import LEGACY, WORKER, supervise
                    coroutine = supervise(self.app_module, self.routing_stop,
                                          LEGACY if self.role == 'legacy' else WORKER, function)
                else:
                    coroutine = function(self.app_module) if spec.with_app else function()
                try:
                    self.tasks[spec.name] = asyncio.create_task(coroutine, name="jnp:" + spec.name)
                except BaseException:
                    coroutine.close()
                    raise
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.routing_stop.set()
        routing = self.tasks.get("debtor-routing")
        if routing is not None and not routing.done():
            # Keep the routing cycle's existing advisory lock until the current
            # entry has completed. No next entry/cycle is admitted after stop.
            done, _ = await asyncio.wait({routing}, timeout=270)
            if not done:
                log.error("routing_drain_timeout_review_required")
        # Cancel all before awaiting any one. One failed task must not prevent
        # the remaining owners from releasing their existing locks/audits.
        for task in self.tasks.values():
            task.cancel()
        results = await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for name, result in zip(self.tasks, results):
            if isinstance(result, Exception):
                log.error("background_task_failed task=%s error_type=%s", name, type(result).__name__)


@asynccontextmanager
async def background_tasks(app_module):
    runtime = BackgroundTasks(app_module)
    await runtime.start()
    try:
        yield runtime
    finally:
        # HTTP-triggered Fibonatix tasks still belong to the legacy web process.
        # A real durable submission queue is required before removing this.
        from operations.fibonatix_import import shutdown
        try:
            await shutdown()
        finally:
            await runtime.stop()
