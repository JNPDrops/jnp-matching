"""Headless role entrypoint. --check never imports financial implementations.

Execution is fail-closed until the runtime's distributed handover gates pass.
This command exposes no HTTP server, credentials or financial trigger endpoint.
"""
import argparse
import asyncio
import json
import signal

from app.runtime import BackgroundTasks, ROLES, SeparationNotReady, require_ready, role_manifest


async def wait_for_stop(runtime, stop):
    waiter = asyncio.create_task(stop.wait())
    pending = set(runtime.tasks.values())
    names = {task: name for name, task in runtime.tasks.items()}
    continuous = {spec.name for spec in runtime.specs if spec.continuous}
    try:
        while not stop.is_set():
            done, _ = await asyncio.wait(pending | {waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                return
            for task in done:
                name = names[task]
                if task.cancelled() or task.exception() is not None:
                    raise RuntimeError("background_task_failed:" + name) from None
                if name in continuous:
                    raise RuntimeError("continuous_task_exited:" + name)
                pending.remove(task)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def run(role):
    require_ready(role)
    # Importing the app does not enter its FastAPI lifespan.
    from app import main as app_module
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    runtime = BackgroundTasks(app_module, role)
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        await runtime.start()
        # One-shot historical tasks are not automatically restarted here.
        # Task-specific drain/ownership must be implemented before gates open.
        await wait_for_stop(runtime, stop)
    finally:
        await runtime.stop()
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
