import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch
from app import agent_control as control
from operations.assigned_role import owner_for

class AgentControlTests(unittest.TestCase):
    def test_only_selected_role_assignment_is_changed(self):
        for role in ('routing','woo-rules'):
            for action in ('status','to-web','to-worker','pause'):
                with self.subTest(role=role,action=action):
                    conn=MagicMock()
                    with patch.dict('os.environ',{'DATABASE_URL':'synthetic','EXACT_DIVISION':'3977752'}), \
                         patch('psycopg.connect') as connect, patch.object(control.c,'initialize') as init, \
                         patch.object(control,'request_handover') as assign, \
                         patch.object(control.c,'status_snapshot',return_value={'roles':[]}), \
                         contextlib.redirect_stdout(io.StringIO()):
                        connect.return_value.__enter__.return_value=conn
                        control.main(['--role',role,action])
                        if action=='status':
                            assign.assert_not_called();init.assert_not_called()
                            conn.execute.assert_called_once_with('SET default_transaction_read_only = on')
                        else:
                            target=None if action=='pause' else owner_for(role,'web' if action=='to-web' else 'worker')
                            assign.assert_called_once_with(conn,3977752,role,target)

    def test_unsupported_role_rejected_before_connect(self):
        with patch('psycopg.connect') as connect, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            control.main(['--role','fibonatix','to-worker'])
        connect.assert_not_called()
