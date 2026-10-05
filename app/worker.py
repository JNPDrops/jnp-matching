"""Headless role entrypoint. --check never imports financial implementations.

Execution is fail-closed until the runtime's distributed handover gates pass.
This command exposes no HTTP server, credentials or financial trigger endpoint.
"""
import argparse
import asyncio
import json
import signal

from app.runtime import BackgroundTasks, ROLES, SeparationNotReady, require_ready, role_manifest


async def wait_for_stop(runtime, stop, lease_lost=None):
    waiter = asyncio.create_task(stop.wait())
    lost_waiter = asyncio.create_task(lease_lost.wait()) if lease_lost is not None else None
    pending = set(runtime.tasks.values())
    names = {task: name for name, task in runtime.tasks.items()}
    continuous = {spec.name for spec in runtime.specs if spec.continuous}
    try:
        while not stop.is_set():
            sentinels = {waiter} | ({lost_waiter} if lost_waiter is not None else set())
            done, _ = await asyncio.wait(pending | sentinels, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                return
            if lost_waiter is not None and lost_waiter in done:
                raise RuntimeError("role_lease_lost:" + runtime.role)
            for task in done:
                name = names[task]
                if task.cancelled() or task.exception() is not None:
                    raise RuntimeError("background_task_failed:" + name) from None
                if name in continuous:
                    raise RuntimeError("continuous_task_exited:" + name)
                pending.remove(task)
    finally:
        waiter.cancel()
        if lost_waiter is not None:
            lost_waiter.cancel()
        await asyncio.gather(*[task for task in (waiter, lost_waiter) if task is not None], return_exceptions=True)


async def run(role):
    require_ready(role)
    # Importing the app does not enter its FastAPI lifespan.
    from app import main as app_module
    if role == 'routing':
        return await run_routing(app_module)
    if role == 'woo-rules':
        return await run_woo_rules(app_module)
    if role == 'tax':
        return await run_tax(app_module)
    if role == 'reports':
        from operations.reports_jobs import serve
        from operations.assigned_role import owner_for, supervise
        return await run_supervisor(app_module, supervise, owner_for(role, 'worker'), serve, role=role)
    if role == 'icepay':
        from operations.icepay_jobs import serve
        from operations.assigned_role import owner_for, supervise
        return await run_supervisor(app_module, supervise, owner_for(role, 'worker'), serve, role=role)
    if role == 'fibonatix':
        from operations.fibonatix_jobs import serve
        from operations.assigned_role import owner_for, supervise
        return await run_supervisor(app_module, supervise, owner_for(role, 'worker'), serve, role=role)
    if role == 'maintenance':
        from operations.allocation_maintenance import serve
        from operations.assigned_role import owner_for, supervise
        return await run_supervisor(app_module, supervise, owner_for(role, 'worker'), serve, role=role)
    from operations.worker_coordination import DurableRoleLease
    from operations.worker_write_fence import owner_scope
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    runtime = BackgroundTasks(app_module, role)
    lease = DurableRoleLease(app_module.DATABASE_URL, app_module.DIVISION, role)
    failed = False
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        await lease.start()
        with owner_scope(lease):
            await runtime.start()
        # One-shot historical tasks are not automatically restarted here.
        # Task-specific drain/ownership must be implemented before gates open.
        await wait_for_stop(runtime, stop, lease.lost)
    except BaseException:
        failed = True
        raise
    finally:
        await lease.mark_draining()
        await runtime.stop()
        await lease.close("error" if failed else "stopped")
        for signum in installed:
            loop.remove_signal_handler(signum)


async def run_routing(app_module):
    from operations.automatic_debtor_routing import serve
    from operations.routing_role import WORKER, supervise
    return await run_supervisor(app_module, supervise, WORKER, serve)


async def run_woo_rules(app_module):
    from operations.woo_iban_rules import serve
    from operations.assigned_role import owner_for, supervise
    return await run_supervisor(app_module, supervise, owner_for('woo-rules', 'worker'),
                                serve, role='woo-rules')


async def run_tax(app_module):
    from operations.tax_agent import serve
    from operations.assigned_role import owner_for, supervise
    return await run_supervisor(app_module, supervise, owner_for('tax', 'worker'),
                                serve, role='tax')


async def run_supervisor(app_module, supervise, owner, function, **kwargs):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        await supervise(app_module, stop, owner, function, **kwargs)
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=tuple(role for role in ROLES if role != "web"), required=True)
    parser.add_argument("--check", action="store_true", help="Print readiness without starting tasks")
    args = parser.parse_args(argv)
    if args.check:
        print(json.dumps(role_manifest(args.role), sort_keys=True))
        return 0
    try:
        asyncio.run(run(args.role))
    except SeparationNotReady as exc:
        parser.exit(2, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
