"""Explicit background-task ownership; split execution is deliberately gated.

The legacy web lifespan retains ownership of every role by default. Describing a role
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
    TaskSpec("fibonatix-jobs", "fibonatix", "operations.fibonatix_jobs", "serve", True, True),
    TaskSpec("bank-maintenance", "maintenance", "operations.allocation_maintenance", "serve", True, True),
    TaskSpec("report-jobs", "reports", "operations.reports_jobs", "serve", True, True),
    TaskSpec("icepay-jobs", "icepay", "operations.icepay_jobs", "serve", True, True),
)
ROLES = ("web", "routing", "woo-rules", "tax", "maintenance", "fibonatix", "icepay", "reports")

# All background roles now use shared ownership and write admission. These are
# operational gates, not an environment-switch to bypass missing validation.
# Initial legacy production has none of these claims: bootstrapping still needs
# a proven stop/drain of *all* financial entrypoints before deployment.
COMMON_BLOCKERS = ("initial_legacy_web_drain_pending",)
ROLE_BLOCKERS = {
    role: COMMON_BLOCKERS + (role.replace('-', '_') + "_handover_postgres_pending",)
    for role in ROLES if role != "web"
}
ROLE_BLOCKERS["web"] = COMMON_BLOCKERS + ("manual_http_write_queue_or_disable_pending",)


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
        self.owned_stops = {role: asyncio.Event() for role in ("routing", "woo-rules", "tax", "maintenance", "fibonatix", "icepay", "reports")}
        self.routing_stop = self.owned_stops["routing"]
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
                if spec.role in self.owned_stops:
                    from operations.assigned_role import owner_for, supervise
                    owner = owner_for(spec.role, 'web' if self.role == 'legacy' else 'worker')
                    coroutine = supervise(self.app_module, self.owned_stops[spec.role],
                                          owner, function, role=spec.role)
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
        for stop in self.owned_stops.values():
            stop.set()
        owned = {self.tasks[spec.name] for spec in self.specs
                 if spec.role in self.owned_stops and spec.name in self.tasks
                 and not self.tasks[spec.name].done()}
        if owned:
            # Drain independent roles concurrently within the same 300s window.
            _, pending = await asyncio.wait(owned, timeout=270)
            if pending:
                log.error("owned_tasks_drain_timeout_review_required")
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
        # Compatibility cleanup for a task started by older in-process code.
        # Current HTTP submissions use the durable role queue.
        from operations.fibonatix_import import shutdown
        try:
            await shutdown()
        finally:
            await runtime.stop()
