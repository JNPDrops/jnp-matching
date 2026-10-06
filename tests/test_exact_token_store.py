import asyncio
from contextlib import asynccontextmanager
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from operations.exact_token_store import (
    TokenStoreUnavailable, access_token, prepared_tokens, token_session,
)


def expired():
    return {"access_token": "synthetic-old", "refresh_token": "synthetic-rotating", "expires_at": 0}


class MemoryStore:
    def __init__(self):
        self.tokens = expired()
        self.saved = []

    async def load(self):
        return copy.deepcopy(self.tokens)

    async def save(self, value):
        self.tokens = copy.deepcopy(value)
        self.saved.append(copy.deepcopy(value))


class Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class SharedDatabase:
    """Separate fake sessions with a shared PostgreSQL-style exclusive lock."""
    def __init__(self):
        self.owner = None
        self.tokens = expired()
        self.connections = []

    async def connect(self, *_, **__):
        database = self

        class Connection:
            closed = False

            async def execute(self, query, params=None):
                if "pg_try_advisory_lock" in query:
                    if database.owner in (None, self):
                        database.owner = self
                        return Cursor((True,))
                    return Cursor((False,))
                if database.owner is not self:
                    raise AssertionError("token access without owning shared lock")
                if query.startswith("SELECT token_json"):
                    return Cursor((copy.deepcopy(database.tokens),))
                if query.startswith("INSERT"):
                    database.tokens = json.loads(params[1])
                return Cursor(None)

            async def close(self):
                self.closed = True
                if database.owner is self:
                    database.owner = None

        conn = Connection()
        self.connections.append(conn)
        return conn


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_sessions_refresh_only_once(self):
        database = SharedDatabase()
        exchanged = []

        async def refresh(tokens):
            exchanged.append(tokens["refresh_token"])
            await asyncio.sleep(0.06)
            return {"access_token": "synthetic-new", "refresh_token": "synthetic-next", "expires_in": 600}

        async def worker():
            async with token_session("test-only", None, None) as store:
                return await access_token(store, refresh)

        with patch("psycopg.AsyncConnection.connect", new=database.connect):
            values = await asyncio.gather(worker(), worker())
        self.assertEqual(values, ["synthetic-new", "synthetic-new"])
        self.assertEqual(exchanged, ["synthetic-rotating"])
        self.assertEqual(len(database.connections), 2)
        self.assertTrue(all(conn.closed for conn in database.connections))
        self.assertIsNone(database.owner)

    async def test_stale_401_uses_other_process_new_token(self):
        store = MemoryStore()
        store.tokens = prepared_tokens({"access_token": "newer", "refresh_token": "next"})
        refresh = AsyncMock()
        self.assertEqual(await access_token(store, refresh, rejected_token="older"), "newer")
        refresh.assert_not_awaited()

    async def test_current_401_refreshes_and_saves_before_return(self):
        store = MemoryStore()
        store.tokens = prepared_tokens({"access_token": "current", "refresh_token": "next"})
        refresh = AsyncMock(return_value={"access_token": "new", "refresh_token": "later"})
        self.assertEqual(await access_token(store, refresh, rejected_token="current"), "new")
        self.assertEqual(store.saved[0]["access_token"], "new")

    async def test_save_failure_never_returns_or_reexchanges(self):
        store = MemoryStore()
        store.save = AsyncMock(side_effect=RuntimeError("database lost"))
        refresh = AsyncMock(return_value={"access_token": "new", "refresh_token": "later"})
        with self.assertRaises(RuntimeError):
            await access_token(store, refresh)
        refresh.assert_awaited_once()

    async def test_lock_timeout_closes_waiter_and_does_not_read(self):
        database = SharedDatabase()
        owner = object()
        database.owner = owner
        with patch("psycopg.AsyncConnection.connect", new=database.connect):
            with self.assertRaisesRegex(TokenStoreUnavailable, "token_lock_busy"):
                async with token_session("test-only", None, None, wait_seconds=0):
                    self.fail("unexpected lock acquisition")
        self.assertIs(database.owner, owner)
        self.assertTrue(database.connections[0].closed)

    async def test_cancellation_releases_shared_lock(self):
        database = SharedDatabase()
        entered = asyncio.Event()

        async def owner():
            async with token_session("test-only", None, None):
                entered.set()
                await asyncio.Event().wait()

        with patch("psycopg.AsyncConnection.connect", new=database.connect):
            task = asyncio.create_task(owner())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(database.owner)
        self.assertTrue(database.connections[0].closed)

    async def test_malformed_exchange_never_saved(self):
        for response in [{}, {"access_token": "a"}, {"access_token": "a", "refresh_token": "r", "expires_in": -1}]:
            store = MemoryStore()
            with self.assertRaises(TokenStoreUnavailable):
                await access_token(store, AsyncMock(return_value=response))
            self.assertEqual(store.saved, [])


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import main
        self.app = main
        self.store = MemoryStore()
        self.lock = asyncio.Lock()

        @asynccontextmanager
        async def session():
            async with self.lock:
                yield self.store

        self.session_patch = patch.object(main, "_main_token_session", session)
        self.session_patch.start()

    async def asyncTearDown(self):
        self.session_patch.stop()

    async def test_callback_and_refresh_serialize_and_code_not_logged(self):
        app, store = self.app, self.store
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(old):
            entered.set()
            await release.wait()
            return {"access_token": "refreshed", "refresh_token": "rotated"}

        client = AsyncMock()
        response = type("Response", (), {"status_code": 200, "json": lambda _: {
            "access_token": "callback-new", "refresh_token": "callback-rotating"}})()
        client.__aenter__.return_value.post.return_value = response
        request = Request({"type": "http", "query_string": b"code=private-code", "session": {"oauth_state": "state"}})
        with patch.object(app, "_exchange_refresh_tokens", refresh), patch.object(app, "_require_config"), patch.object(app.httpx, "AsyncClient", return_value=client):
            first = asyncio.create_task(app._access_token())
            await entered.wait()
            callback = asyncio.create_task(app.oauth_callback(request, code="synthetic", state="state"))
            await asyncio.sleep(0)
            client.__aenter__.return_value.post.assert_not_awaited()
            release.set()
            await asyncio.gather(first, callback)
        self.assertEqual(store.tokens["access_token"], "callback-new")
        self.assertEqual([row["access_token"] for row in store.saved], ["refreshed", "callback-new"])
        self.assertEqual(request.scope["query_string"], b"")

    async def test_persistent_401_retries_get_once_but_never_retries_xml_write(self):
        response = type("Response", (), {"status_code": 401, "text": "unauthorized"})()
        for call, retries in ((lambda: self.app._request_json("GET", "https://example.test"), 1),
                              (lambda: self.app.upload_matchset(b"synthetic XML"), 0)):
            client = AsyncMock()
            client.__aenter__.return_value.request.return_value = response
            client.__aenter__.return_value.post.return_value = response
            with patch.object(self.app, "_access_token", new=AsyncMock(return_value="synthetic")) as token, patch.object(self.app.httpx, "AsyncClient", return_value=client):
                with self.assertRaises(HTTPException) as error:
                    await call()
                self.assertEqual(error.exception.status_code, 401)
                self.assertEqual(sum(1 for args in token.call_args_list if "rejected_token" in args.kwargs), retries)
                self.assertEqual(client.__aenter__.await_count, retries + 1)


if __name__ == "__main__":
    unittest.main()
