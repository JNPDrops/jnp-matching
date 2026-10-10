import unittest
from unittest.mock import Mock, MagicMock

from operations.processing_graph_mail import Settings, GraphMail, DeliveryUncertain
from operations.processing_alerts import message, deliver_graph_one


def env():
    return dict(JNP_GRAPH_TENANT_ID='00000000-0000-0000-0000-000000000001',
                JNP_GRAPH_CLIENT_ID='00000000-0000-0000-0000-000000000002',
                JNP_GRAPH_CLIENT_SECRET='synthetic-secret',
                JNP_ALERT_FROM='sender@example.test', JNP_ALERT_TO='user@example.test')


class GraphTests(unittest.TestCase):
    def transport(self, status=202):
        token_client=Mock()
        token_client.acquire_token_for_client.return_value={'access_token':'synthetic-token'}
        client_factory=Mock(return_value=token_client)
        http=MagicMock()
        http.__enter__.return_value=http
        http.post.return_value.status_code=status
        http_factory=Mock(return_value=http)
        transport=GraphMail(Settings.from_env(env()),client_factory=client_factory,http_factory=http_factory)
        return transport,token_client,http,client_factory,http_factory

    def mail(self):
        return message(env(),'abc','jnp:3977752:batch:2026-10-10T0900','routing','routing_stale')

    def test_app_only_and_single_bounded_submission(self):
        transport,client,http,factory,http_factory=self.transport()
        transport.authenticate()
        client.acquire_token_for_client.assert_called_once_with(scopes=['https://graph.microsoft.com/.default'])
        self.assertEqual(transport.submit(self.mail()),'accepted')
        http_factory.assert_called_once_with(timeout=30,follow_redirects=False)
        http.post.assert_called_once()
        self.assertEqual(http.post.call_args.args[0],
                         'https://graph.microsoft.com/v1.0/users/sender%40example.test/sendMail')
        self.assertNotIn('synthetic-secret',str(http.post.call_args))
        self.assertIsNone(transport._token)

    def test_auth_failure_is_sanitized_and_never_sends(self):
        transport,client,http,_,_=self.transport()
        client.acquire_token_for_client.side_effect=RuntimeError('synthetic-secret')
        with self.assertRaisesRegex(ValueError,'^graph_notification_authentication_failed$'):
            transport.authenticate()
        http.post.assert_not_called()

    def test_unexpected_response_is_not_retried(self):
        for status in (302,401,403,429,500):
            transport,_,http,_,_=self.transport(status)
            transport.authenticate()
            with self.assertRaises(DeliveryUncertain):transport.submit(self.mail())
            http.post.assert_called_once()

    def test_timeout_is_sanitized_and_token_is_discarded(self):
        transport,_,http,_,_=self.transport()
        transport.authenticate()
        http.post.side_effect=RuntimeError('synthetic-token')
        with self.assertRaisesRegex(DeliveryUncertain,'^graph_notification_delivery_unconfirmed$'):
            transport.submit(self.mail())
        self.assertIsNone(transport._token)

    def test_configuration_rejects_injection_or_ambiguous_credential(self):
        for changes in ({'JNP_GRAPH_TENANT_ID':'../other'},
                        {'JNP_ALERT_TO':'user@example.test\nBcc:other@example.test'},
                        {'JNP_GRAPH_CERTIFICATE_PATH':'/synthetic.pfx'},
                        {'JNP_GRAPH_CLIENT_SECRET':''}):
            with self.assertRaises(ValueError):Settings.from_env(env()|changes)
        self.assertNotIn('synthetic-secret',repr(Settings.from_env(env())))

    def test_outbox_commits_intent_before_network_and_retains_uncertainty(self):
        conn=MagicMock()
        conn.__enter__.return_value=conn
        conn.execute.return_value.fetchone.side_effect=[(True,),
            ('abc','jnp:3977752:batch:2026-10-10T0900','routing','routing_stale')]
        app=Mock();app._db_connect.return_value=conn
        transport=Mock()
        def submit(mail):
            self.assertIn("state='sending'",conn.execute.call_args.args[0])
            self.assertEqual(conn.commit.call_count,2)
            raise DeliveryUncertain('uncertain')
        transport.submit.side_effect=submit
        self.assertEqual(deliver_graph_one(app,env(),lambda _:transport),'uncertain')
        transport.authenticate.assert_called_once()
        transport.submit.assert_called_once()
        sqls=[c.args[0] for c in conn.execute.call_args_list]
        self.assertTrue(any("state='uncertain' WHERE alert_key" in sql for sql in sqls))
        self.assertFalse(any("state='accepted'" in sql for sql in sqls))
        self.assertIn('pg_advisory_unlock',sqls[-1])

    def test_auth_failure_keeps_pending_without_delivery_intent(self):
        conn=MagicMock();conn.__enter__.return_value=conn
        conn.execute.return_value.fetchone.side_effect=[(True,),
            ('abc','jnp:3977752:batch:2026-10-10T0900','routing','routing_stale')]
        app=Mock();app._db_connect.return_value=conn
        transport=Mock();transport.authenticate.side_effect=ValueError('auth')
        with self.assertRaises(ValueError):deliver_graph_one(app,env(),lambda _:transport)
        transport.submit.assert_not_called()
        sqls=[c.args[0] for c in conn.execute.call_args_list]
        self.assertFalse(any("SET state='sending'" in sql for sql in sqls))
        self.assertIn('pg_advisory_unlock',sqls[-1])


if __name__=='__main__':unittest.main()
