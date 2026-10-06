import unittest
import signal
import subprocess
import sys
from unittest.mock import MagicMock

from operations.legacy_cutover import (
    LOCKS, CutoverBlocked, acquire_all, verify_health, verify_jobs,
)


class LegacyCutoverTests(unittest.TestCase):
    def test_shutdown_signals_keep_guard_alive_until_explicit_abort(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "from unittest.mock import MagicMock; "
             "from operations.legacy_cutover import hold_connection; "
             "hold_connection(MagicMock(), 30)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('guard_ready', child.stdout.readline())
            child.send_signal(signal.SIGTERM)
            child.send_signal(signal.SIGHUP)
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            child.send_signal(signal.SIGINT)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn('guard_releasing_locks', out)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def test_unknown_or_enabled_manual_write_state_blocks(self):
        healthy = {"ok": True, "division": 3977752,
                   "order_rule_writes": False, "direct_match_writes": False}
        verify_health(healthy)
        for key in ("order_rule_writes", "direct_match_writes"):
            for value in (None, True, "false", 0):
                with self.subTest(key=key, value=value), self.assertRaises(CutoverBlocked):
                    verify_health({**healthy, key: value})
        with self.assertRaises(CutoverBlocked):
            verify_health({**healthy, "division": 123})

    def test_busy_owner_releases_only_our_partial_locks(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(True,), (False,)]
        with self.assertRaisesRegex(CutoverBlocked, "still_running"):
            with acquire_all(conn):
                self.fail("must not enter while another operation is running")
        self.assertEqual(conn.execute.call_count, 3)
        self.assertEqual(conn.execute.call_args.args,
                         ("SELECT pg_advisory_unlock(%s)", (LOCKS[0],)))

    def test_full_acquisition_is_held_until_context_exits(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (True,)
        with acquire_all(conn):
            self.assertEqual(conn.execute.call_count, len(LOCKS))
        released = [call.args[1][0] for call in conn.execute.call_args_list
                    if "unlock" in call.args[0]]
        self.assertEqual(released, list(reversed(LOCKS)))

    def test_verification_error_also_releases_all_locks(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (True,)
        with self.assertRaisesRegex(ValueError, "verification"):
            with acquire_all(conn):
                raise ValueError("verification")
        self.assertEqual(conn.execute.call_count, len(LOCKS) * 2)

    def test_job_metadata_does_not_resolve_uncertain_writes(self):
        for state in (None, "upload_requested", "match_requested", "unknown"):
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = (0,)
            conn.execute.return_value.fetchall.side_effect = [
                [(state, 1)], [("prepared", 1)],
            ]
            with self.subTest(state=state), self.assertRaises(CutoverBlocked):
                verify_jobs(conn)
            self.assertTrue(all(call.args[0].startswith("SELECT")
                                for call in conn.execute.call_args_list))

    def test_running_fibonatix_is_not_inferred_stopped_from_locks(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        with self.assertRaisesRegex(CutoverBlocked, "running_flag"):
            verify_jobs(conn)

    def test_only_terminal_metadata_passes_without_financial_mutations(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (0,)
        conn.execute.return_value.fetchall.side_effect = [
            [("import_verified", 2)], [("prepared", 1), ("group_verified", 5)],
            [("created_verified", 1)],
        ]
        self.assertEqual(verify_jobs(conn)["import_states"], {"import_verified": 2})
        self.assertTrue(all(call.args[0].startswith("SELECT")
                            for call in conn.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
