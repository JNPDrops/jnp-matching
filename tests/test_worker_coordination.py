from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import worker_coordination as c


NOW = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []


class PolicyTests(unittest.TestCase):
    def test_header_parser_accepts_only_non_negative_integers(self):
        headers = {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4998",
                   "x-ratelimit-reset": "1791158400000", "x-ratelimit-minutely-limit": "60",
                   "x-ratelimit-minutely-remaining": "59", "x-ratelimit-minutely-reset": "bad",
                   "authorization": "must-not-be-read"}
        self.assertEqual(c.parse_headers(headers), {
            "daily_limit": 5000, "daily_remaining": 4998, "daily_reset_ms": 1791158400000,
            "minute_limit": 60, "minute_remaining": 59})

    def test_daily_and_minute_reserves_include_pending_requests(self):
        state = {"daily_remaining": 202, "daily_reset_ms": int((NOW + timedelta(hours=5)).timestamp()*1000),
                 "minute_remaining": 2, "minute_reset_ms": int((NOW + timedelta(seconds=30)).timestamp()*1000)}
        now_ms = int(NOW.timestamp()*1000)
        self.assertEqual(c.available(state, 2, floor=200, now_ms=now_ms), (False, "daily_reserve"))
        state["daily_remaining"] = 1000
        self.assertEqual(c.available(state, 2, floor=200, now_ms=now_ms), (False, "minute_reserve"))
        self.assertEqual(c.available(state, 1, floor=200, now_ms=now_ms), (True, None))

    def test_expired_or_missing_minute_headers_are_capped(self):
        state = {"daily_remaining": 0, "daily_reset_ms": int((NOW - timedelta(seconds=1)).timestamp()*1000),
                 "minute_remaining": 0, "minute_reset_ms": int((NOW - timedelta(seconds=1)).timestamp()*1000)}
        now_ms = int(NOW.timestamp()*1000)
        self.assertEqual(c.available(state, 0, floor=200, now_ms=now_ms, unknown_count=29), (True, None))
        self.assertEqual(c.available(state, 0, floor=200, now_ms=now_ms, unknown_count=30), (False, "unknown_minute_cap"))
        state["daily_remaining"], state["daily_reset_ms"] = 4999, int((NOW+timedelta(hours=5)).timestamp()*1000)
        self.assertEqual(c.available(state, 0, floor=200, now_ms=now_ms, unknown_count=30), (False, "unknown_minute_cap"))

    def test_invalid_identities_and_lease_values_fail_closed(self):
        for args in ((0, "main", "routing"), (3977752, "other", "routing"), (3977752, "main", "unknown")):
            with self.assertRaises(ValueError):
                c.validate_identity(*args)
        conn = MagicMock()
        with self.assertRaises(ValueError):
            c.claim_role(conn, 3977752, "routing", "bad owner")
        with self.assertRaises(ValueError):
            c.claim_role(conn, 3977752, "routing", "valid-owner", lease_seconds=301)


class LeaseTests(unittest.TestCase):
    def connection(self, role_row):
        conn = MagicMock()
        conn.transaction.return_value = nullcontext()
        conn.execute.side_effect = [Cursor(), Cursor(), Cursor(role_row)]
        return conn

    def test_live_other_owner_is_not_stolen(self):
        conn = self.connection((None, "worker-a", NOW + timedelta(seconds=30), False))
        with self.assertRaisesRegex(c.LeaseUnavailable, "already_owned"):
            c.claim_role(conn, 3977752, "routing", "worker-b", now=NOW)
        self.assertEqual(conn.execute.call_count, 3)

    def test_expired_owner_can_be_replaced(self):
        conn = self.connection((None, "worker-a", NOW - timedelta(seconds=1), False))
        conn.execute.side_effect = [Cursor(), Cursor(), Cursor((None, "worker-a", NOW-timedelta(seconds=1), False)), Cursor()]
        lease = c.claim_role(conn, 3977752, "routing", "worker-b", now=NOW)
        self.assertEqual(str(c.UUID(lease)), lease)
        update = conn.execute.call_args_list[-1]
        self.assertEqual(update.args[1][0], "worker-b")

    def test_desired_owner_and_drain_block_claim(self):
        for row in (("worker-a", None, None, False), (None, None, None, True)):
            conn = self.connection(row)
            with self.assertRaisesRegex(c.LeaseUnavailable, "not_assigned"):
                c.claim_role(conn, 3977752, "tax", "worker-b", now=NOW)

    def test_drained_role_transfers_only_to_desired_owner(self):
        conn = self.connection(("worker-b", None, None, True))
        conn.execute.side_effect = [Cursor(), Cursor(), Cursor(("worker-b", None, None, True)), Cursor()]
        lease = c.claim_role(conn, 3977752, "tax", "worker-b", now=NOW)
        self.assertEqual(str(c.UUID(lease)), lease)
        self.assertIn("draining=FALSE", conn.execute.call_args_list[-1].args[0])

    def test_heartbeat_sanitizes_detail_and_reports_drain(self):
        conn = MagicMock()
        conn.execute.return_value = Cursor((True,))
        lease = "11111111-1111-1111-1111-111111111111"
        result = c.heartbeat(conn, 3977752, "tax", "worker-a", lease, "ready",
                             {"phase": "scan", "queue_depth": 4, "secret": "hidden"}, now=NOW)
        self.assertTrue(result["draining"])
        stored = json.loads(conn.execute.call_args.args[1][3])
        self.assertEqual(stored, {"phase": "scan", "queue_depth": 4})

    def test_lost_lease_cannot_heartbeat_or_release(self):
        conn = MagicMock()
        conn.execute.return_value = Cursor(None)
        lease = "11111111-1111-1111-1111-111111111111"
        with self.assertRaisesRegex(c.LeaseUnavailable, "lease_lost"):
            c.heartbeat(conn, 3977752, "tax", "worker-a", lease, "ready", now=NOW)
        with self.assertRaisesRegex(c.LeaseUnavailable, "lease_lost"):
            c.release_role(conn, 3977752, "tax", "worker-a", lease)


class ReservationTests(unittest.TestCase):
    def reserve_connection(self, state, pending=0):
        conn = MagicMock()
        conn.transaction.return_value = nullcontext()
        row = tuple(state.get(k) for k in ("daily_limit", "daily_remaining", "daily_reset_ms",
                    "minute_limit", "minute_remaining", "minute_reset_ms", "observed_at",
                    "unknown_window_started_at", "unknown_window_count"))
        conn.execute.side_effect = [Cursor(), Cursor(), Cursor(row), Cursor((pending,)), Cursor(), Cursor()]
        return conn

    def test_reservation_is_atomic_metadata_only(self):
        state = {"daily_limit": 5000, "daily_remaining": 4999,
                 "daily_reset_ms": int((NOW+timedelta(hours=4)).timestamp()*1000),
                 "minute_limit": 60, "minute_remaining": 59,
                 "minute_reset_ms": int((NOW+timedelta(seconds=50)).timestamp()*1000),
                 "observed_at": NOW-timedelta(seconds=1), "unknown_window_started_at": NOW, "unknown_window_count": 0}
        conn = self.reserve_connection(state, pending=2)
        reservation = c.reserve_request(conn, 3977752, "allocation", "routing", "PUT", priority="critical", floor=50, now=NOW)
        self.assertEqual(reservation.connection, "allocation")
        insert = conn.execute.call_args_list[-2]
        self.assertIn("jnp_exact_api_reservations", insert.args[0])
        self.assertNotIn("url", insert.args[0].lower())
        self.assertEqual(insert.args[1][2:6], ("allocation", "routing", "critical", "PUT"))

    def test_reservation_refuses_daily_floor_before_insert(self):
        state = {"daily_remaining": 201, "daily_reset_ms": int((NOW+timedelta(hours=4)).timestamp()*1000),
                 "minute_remaining": 50, "minute_reset_ms": int((NOW+timedelta(seconds=50)).timestamp()*1000),
                 "observed_at": NOW, "unknown_window_started_at": NOW, "unknown_window_count": 0}
        conn = self.reserve_connection(state, pending=1)
        with self.assertRaisesRegex(c.BudgetDeferred, "daily_reserve"):
            c.reserve_request(conn, 3977752, "allocation", "routing", "GET", floor=200, now=NOW)
        self.assertEqual(conn.execute.call_count, 4)

    def test_response_observation_overrides_budget_but_not_payload(self):
        conn = MagicMock()
        conn.transaction.return_value = nullcontext()
        conn.execute.side_effect = [Cursor(), Cursor(("reserved",)), Cursor(), Cursor()]
        reservation = c.Reservation("11111111-1111-1111-1111-111111111111", 3977752, "allocation", "routing", "GET")
        result = c.complete_request(conn, reservation, {"x-ratelimit-remaining": "4812", "body": "private"}, now=NOW)
        self.assertEqual(result, {"state": "observed", "observed": {"daily_remaining": 4812}})
        update = conn.execute.call_args_list[-1]
        self.assertNotIn("body", update.args[0])
        self.assertNotIn("private", str(update.args))

    def test_missing_headers_remains_uncertain_until_later_observation(self):
        conn = MagicMock()
        conn.transaction.return_value = nullcontext()
        conn.execute.side_effect = [Cursor(), Cursor(("reserved",)), Cursor()]
        reservation = c.Reservation("11111111-1111-1111-1111-111111111111", 3977752, "main", "woo-rules", "POST")
        self.assertEqual(c.complete_request(conn, reservation, {}, outcome="uncertain", now=NOW)["state"], "uncertain")


class DurableWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_heartbeat_sets_event(self):
        lease = c.DurableRoleLease("test-only", 3977752, "routing", owner="worker-a", lease_seconds=15)
        lease.lease_id = "11111111-1111-1111-1111-111111111111"
        lease._stop = __import__("asyncio").Event()
        async def timeout(coroutine, **_):
            coroutine.close()
            raise __import__("asyncio").TimeoutError
        with patch.object(lease, "_connection_call", side_effect=c.LeaseUnavailable("lost")), \
             patch("operations.worker_coordination.asyncio.wait_for", side_effect=timeout):
            await lease._heartbeat_loop()
        self.assertTrue(lease.lost.is_set())


if __name__ == "__main__":
    unittest.main()
