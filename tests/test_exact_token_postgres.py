"""Opt-in real PostgreSQL test; isolated local test database ONLY.

Example: JNP_TEST_POSTGRES_DSN='host=127.0.0.1 dbname=jnp_test_tokens user=test'
No production DATABASE_URL is read, and no request goes to Exact.
"""
import asyncio
import multiprocessing
import os
import unittest
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg import sql

from operations.exact_token_store import access_token, token_session


def token_process(dsn, results):
    async def run():
        async with token_session(dsn, None, None) as store:
            async def refresh(_):
                await store.connection.execute("UPDATE refresh_count SET n=n+1")
                await asyncio.sleep(0.2)
                return {"access_token": "synthetic-new", "refresh_token": "synthetic-next"}
            return await access_token(store, refresh)
    try:
        results.put(asyncio.run(run()))
    except Exception as exc:
        results.put(type(exc).__name__)


@unittest.skipUnless(os.environ.get("JNP_TEST_POSTGRES_DSN"), "isolated local PostgreSQL not configured")
class PostgresTokenTests(unittest.TestCase):
    def test_two_processes_share_one_rotation(self):
        dsn = os.environ["JNP_TEST_POSTGRES_DSN"]
        settings = conninfo_to_dict(dsn)
        # Explicit host AND test database prevent service/PGHOST defaults from
        # silently connecting the integration test to a production server.
        self.assertIn(settings.get("host"), {"127.0.0.1", "localhost", "::1"})
        self.assertTrue(settings.get("dbname", "").startswith("jnp_test_"))
        self.assertNotIn("service", settings)
        self.assertNotIn("hostaddr", settings)
        schema = "tokens_" + uuid.uuid4().hex
        scoped = make_conninfo(dsn, options="-csearch_path=" + schema)
        processes = []
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            try:
                with psycopg.connect(scoped, autocommit=True) as conn:
                    conn.execute("CREATE TABLE refresh_count(n integer NOT NULL)")
                    conn.execute("INSERT INTO refresh_count VALUES(0)")
                    conn.execute("""CREATE TABLE exact_oauth_tokens(singleton_key text PRIMARY KEY,
                        token_json jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())""")
                    conn.execute("""INSERT INTO exact_oauth_tokens(singleton_key,token_json)
                        VALUES('exact','{"access_token":"synthetic-old","refresh_token":"synthetic-old-refresh","expires_at":0}')""")
                context = multiprocessing.get_context("spawn")
                results = context.Queue()
                processes = [context.Process(target=token_process, args=(scoped, results)) for _ in range(2)]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=20)
                    self.assertFalse(process.is_alive(), "token process did not finish")
                    self.assertEqual(process.exitcode, 0)
                self.assertEqual([results.get(timeout=2) for _ in processes], ["synthetic-new"] * 2)
                with psycopg.connect(scoped) as conn:
                    self.assertEqual(conn.execute("SELECT n FROM refresh_count").fetchone()[0], 1)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
