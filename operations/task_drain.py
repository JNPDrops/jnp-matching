"""Cooperative stop per owned task; an in-flight operation keeps its audit/lock."""
import asyncio
from contextvars import ContextVar

_stop = ContextVar("owned_task_stop", default=None)


def requested():
    event = _stop.get()
    return event is not None and event.is_set()


async def run(event, function, *args):
    token = _stop.set(event)
    try:
        return await function(*args)
    finally:
        _stop.reset(token)


async def wait(seconds):
    event = _stop.get()
    if event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
