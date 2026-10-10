"""App-only Outlook transport. Never depends on a dashboard/browser session.

Configure a dedicated application with Exchange RBAC Application Mail.Send
scoped to exactly the configured sender mailbox. No directory-wide grant or
reuse of dashboard permissions is implied by this module.
"""
from dataclasses import dataclass, field
from email.utils import parseaddr
from urllib.parse import quote
from uuid import UUID


class DeliveryUncertain(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    tenant: str
    client: str
    sender: str
    recipient: str
    secret: str = field(default='', repr=False)
    certificate: str = field(default='', repr=False)
    certificate_password: str = field(default='', repr=False)

    @classmethod
    def from_env(cls, env):
        s = cls(env.get('JNP_GRAPH_TENANT_ID', ''), env.get('JNP_GRAPH_CLIENT_ID', ''),
                env.get('JNP_ALERT_FROM', ''), env.get('JNP_ALERT_TO', ''),
                env.get('JNP_GRAPH_CLIENT_SECRET', ''),
                env.get('JNP_GRAPH_CERTIFICATE_PATH', ''),
                env.get('JNP_GRAPH_CERTIFICATE_PASSWORD', ''))
        try:
            if any(str(UUID(v)) != v for v in (s.tenant, s.client)):
                raise ValueError()
            if bool(s.secret) == bool(s.certificate):
                raise ValueError()
            for v in (s.sender, s.recipient):
                if not v or any(ch.isspace() for ch in v) or parseaddr(v)[1] != v or v.count('@') != 1:
                    raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('graph_notification_configuration_invalid') from None
        return s


class GraphMail:
    def __init__(self, settings, *, client_factory=None, http_factory=None):
        self.settings = settings
        self.client_factory = client_factory
        self.http_factory = http_factory
        self._token = None

    def authenticate(self):
        """May fail before delivery intent; never logs provider response text."""
        try:
            factory = self.client_factory
            if factory is None:
                import msal
                factory = msal.ConfidentialClientApplication
            s = self.settings
            credential = s.secret
            if s.certificate:
                credential = {'private_key_pfx_path': s.certificate}
                if s.certificate_password:
                    credential['passphrase'] = s.certificate_password
            client = factory(s.client, client_credential=credential,
                             authority='https://login.microsoftonline.com/' + s.tenant,
                             enable_pii_log=False, timeout=10)
            result = client.acquire_token_for_client(scopes=['https://graph.microsoft.com/.default'])
            token = result.get('access_token')
            if not isinstance(token, str) or not token:
                raise ValueError()
            self._token = token
        except Exception:
            self._token = None
            raise ValueError('graph_notification_authentication_failed') from None

    def submit(self, message):
        """One POST, no redirect/retry. 202 means accepted, not delivered."""
        if not self._token:
            raise ValueError('graph_notification_not_authenticated')
        if str(message['From']) != self.settings.sender or str(message['To']) != self.settings.recipient:
            raise ValueError('graph_notification_address_mismatch')
        payload = {'message': {'subject': str(message['Subject']),
                   'body': {'contentType': 'Text', 'content': message.get_content()},
                   'toRecipients': [{'emailAddress': {'address': self.settings.recipient}}],
                   'internetMessageHeaders': [{'name': 'x-jnp-alert-id',
                                                'value': str(message['Message-ID'])}]},
                   'saveToSentItems': True}
        factory = self.http_factory
        if factory is None:
            import httpx
            factory = httpx.Client
        try:
            with factory(timeout=30, follow_redirects=False) as http:
                response = http.post('https://graph.microsoft.com/v1.0/users/' +
                                     quote(self.settings.sender, safe='') + '/sendMail',
                                     headers={'Authorization': 'Bearer ' + self._token}, json=payload)
            if response.status_code != 202:
                raise DeliveryUncertain()
        except Exception:
            # A transport exception or unexpected response cannot prove non-delivery.
            # Preserve intent for review instead of duplicating mail on restart.
            raise DeliveryUncertain('graph_notification_delivery_unconfirmed') from None
        finally:
            self._token = None
        return 'accepted'
