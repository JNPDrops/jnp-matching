"""Cross-process exclusion for the existing main Exact token row.

Only application code uses the values; diagnostics must never print them.
The lock covers load, refresh/exchange and persistence on the SAME connection.
Closing that connection always releases the session advisory lock.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import psycopg

MAIN_TOKEN_LOCK_ID = 3977752100200


class TokenStoreUnavailable(RuntimeError):
    pass


def prepared_tokens(tokens):
    if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
        raise TokenStoreUnavailable("invalid_token_response")
    if not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]:
        raise TokenStoreUnavailable("missing_refresh_token")
    result = dict(tokens)
    try:
        lifetime = int(result.get("expires_in", 600))
    except (TypeError, ValueError):
        raise TokenStoreUnavailable("invalid_token_lifetime") from None
    if lifetime <= 30:
        raise TokenStoreUnavailable("invalid_token_lifetime")
    result["expires_at"] = int(time.time()) + lifetime - 30
    return result


class DatabaseTokens:
    def __init__(self, connection):
        self.connection = connection

    async def load(self):
        cursor = await self.connection.execute(
            "SELECT token_json FROM exact_oauth_tokens WHERE singleton_key=%s", ("exact",))
        row = await cursor.fetchone()
        if not row:
            return None
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    async def save(self, tokens):
        await self.connection.execute(
            """INSERT INTO exact_oauth_tokens(singleton_key,token_json,updated_at)
               VALUES(%s,%s::jsonb,NOW()) ON CONFLICT(singleton_key)
               DO UPDATE SET token_json=EXCLUDED.token_json,updated_at=NOW()""",
            ("exact", json.dumps(tokens)))


class LocalTokens:
    """Compatibility for single-process local development, never split workers."""
    def __init__(self, load, save):
        self._load, self._save = load, save

    async def load(self):
        return self._load()

    async def save(self, tokens):
        self._save(tokens)


@asynccontextmanager
async def token_session(database_url, local_load, local_save, *, wait_seconds=10):
    if not database_url:
        # The caller holds its process-local lock; split execution forbids this.
        yield LocalTokens(local_load, local_save)
        return
    connection = None
    try:
        connection = await psycopg.AsyncConnection.connect(
            database_url, autocommit=True, connect_timeout=10)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            cursor = await connection.execute("SELECT pg_try_advisory_lock(%s)", (MAIN_TOKEN_LOCK_ID,))
            if (await cursor.fetchone())[0]:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TokenStoreUnavailable("token_lock_busy")
            await asyncio.sleep(0.05)
        await connection.execute("""CREATE TABLE IF NOT EXISTS exact_oauth_tokens (
            singleton_key TEXT PRIMARY KEY, token_json JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        yield DatabaseTokens(connection)
    finally:
        if connection is not None:
            await connection.close()


async def access_token(store, refresh, *, rejected_token=None):
    """Called under the shared lock; stale 401s reuse a newer stored token."""
    tokens = await store.load()
    if not tokens:
        raise TokenStoreUnavailable("exact_not_connected")
    current = tokens.get("access_token")
    if not isinstance(current, str) or not current:
        raise TokenStoreUnavailable("invalid_stored_token")
    if int(tokens.get("expires_at", 0)) <= int(time.time()) or (
        rejected_token is not None and current == rejected_token
    ):
        tokens = prepared_tokens(await refresh(tokens))
        # Save before returning/releasing the lock. Never retry an exchange on
        # persistence failure: rotation may already have happened at Exact.
        await store.save(tokens)
    return tokens["access_token"]
