import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from app import routing_control
from operations.routing_role import LEGACY, WORKER

class RoutingControlTests(unittest.TestCase):
    def test_status_is_read_only_and_mutations_only_set_routing_assignment(self):
        for action,target in [('status',None),('to-worker',WORKER),('to-web',LEGACY),('pause',None)]:
            conn=MagicMock()
            with patch.dict('os.environ',{'DATABASE_URL':'synthetic','EXACT_DIVISION':'3977752'}), \
                 patch('psycopg.connect') as connect, \
                 patch.object(routing_control.c,'initialize') as initialize, \
                 patch.object(routing_control,'request_handover') as assign, \
                 patch.object(routing_control.c,'status_snapshot',return_value={'roles':[{'role':'routing'},{'role':'tax'}]}) as snapshot, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                connect.return_value.__enter__.return_value=conn
                self.assertEqual(routing_control.main([action]),0)
                if action=='status':
                    conn.execute.assert_called_once_with('SET default_transaction_read_only = on')
                    assign.assert_not_called()
                    initialize.assert_not_called()
                else:
                    assign.assert_called_once_with(conn,3977752,target)
                snapshot.assert_called_once_with(conn,3977752)
                self.assertEqual(json.loads(out.getvalue())['roles'],[{'role':'routing'}])

    def test_connection_failure_does_not_expose_details(self):
        with patch.dict('os.environ',{'DATABASE_URL':'synthetic','EXACT_DIVISION':'3977752'}), \
             patch('psycopg.connect',side_effect=RuntimeError('PRIVATE_CONNECTION_STRING')), \
             contextlib.redirect_stderr(io.StringIO()) as out, self.assertRaises(SystemExit) as error:
            routing_control.main(['status'])
        self.assertEqual(error.exception.code,2)
        self.assertNotIn('PRIVATE_CONNECTION_STRING',out.getvalue())
