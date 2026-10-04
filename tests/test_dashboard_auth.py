import copy
from dataclasses import replace
import secrets
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dashboard.auth import (
    FLOW_COOKIE, SESSION_COOKIE, Settings, authorized_user,
    create_dashboard_app, microsoft_client,
)

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"
ORIGIN = "https://dashboard.example.test"


class MemorySessions:
    """Test-only store: production has no memory/session fallback."""
    def __init__(self):
        self.data = {}

    def put(self, kind, payload, ttl):
        handle = secrets.token_urlsafe(32)
        self.data[handle] = (kind, copy.deepcopy(payload), time.time() + ttl)
        return handle

    def get(self, handle, kind, *, consume=False):
        value = self.data.pop(handle, None) if consume else self.data.get(handle)
        if value and value[0] == kind and value[2] > time.time():
            return copy.deepcopy(value[1])
        return None

    def delete(self, handle):
        self.data.pop(handle, None)


class FakeMicrosoft:
    def __init__(self):
        self.result = {"id_token_claims": {
            "tid": TENANT, "oid": USER, "name": "Voorbeeldmedewerker",
            "aud": CLIENT, "iss": "https://login.microsoftonline.com/" + TENANT + "/v2.0",
            "exp": time.time() + 3600,
        }, "access_token": "never-send-this-to-browser"}
        self.exchange_count = 0
        self.error = False

    def initiate_auth_code_flow(self, **kwargs):
        assert kwargs["scopes"] == []
        assert kwargs["response_mode"] == "form_post"
        assert kwargs["redirect_uri"] == ORIGIN + "/dashboard/auth/callback"
        return {"state": "browser-bound-state", "nonce": "provider-nonce",
                "code_verifier": "server-only-pkce-verifier",
                "auth_uri": "https://login.microsoftonline.com/" + TENANT + "/oauth2/v2.0/authorize"}

    def acquire_token_by_auth_code_flow(self, flow, payload):
        self.exchange_count += 1
        if self.error:
            raise RuntimeError("nonce validation failed")
        return copy.deepcopy(self.result)


class DashboardAuthTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(TENANT, CLIENT, ORIGIN, "test-only-db",
                                 {USER: {"role": "operator", "divisions": {"3977752": "James n Parson B.V."}}},
                                 client_secret="test-only-credential")
        self.store = MemorySessions()
        self.microsoft = FakeMicrosoft()
        parent = FastAPI()
        parent.mount("/dashboard", create_dashboard_app(
            settings_provider=lambda: self.settings,
            store_factory=lambda _: self.store, client_factory=lambda _: self.microsoft))
        @parent.get("/health")
        def health():
            return {"ok": True}
        self.client = TestClient(parent, base_url=ORIGIN, follow_redirects=False)

    def sign_in(self):
        self.client.get("/dashboard/auth/start")
        return self.client.post("/dashboard/auth/callback", data={"state": "browser-bound-state", "code": "one-time-code"})

    def test_dashboard_and_all_api_paths_require_login(self):
        self.assertEqual(self.client.get("/dashboard/").headers["location"], "/dashboard/login")
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)
        self.assertEqual(self.client.get("/dashboard/api/divisions/3977752/worklist").status_code, 401)
        self.assertEqual(self.client.get("/dashboard/api/future-endpoint").status_code, 401)
        self.assertEqual(self.client.get("/health").json(), {"ok": True})

    def test_login_only_redirects_to_microsoft_and_hides_flow(self):
        response = self.client.get("/dashboard/auth/start?next=https://attacker.invalid")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("https://login.microsoftonline.com/"))
        cookie = response.headers["set-cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=none", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("server-only-pkce", cookie)
        self.assertNotIn("provider-nonce", cookie)

    def test_allowed_user_gets_only_assigned_divisions_and_no_tokens(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard/")
        data = self.client.get("/dashboard/api/me").json()
        self.assertEqual(data["divisions"], {"3977752": "James n Parson B.V."})
        self.assertNotIn("access_token", data)
        self.assertEqual(self.client.get("/dashboard/api/divisions/3977752/worklist").json()["connected"], False)
        self.assertEqual(self.client.get("/dashboard/api/divisions/999999/worklist").status_code, 403)
        page = self.client.get("/dashboard/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('<script nonce="', page.text)
        self.assertIn("script-src 'nonce-", page.headers["content-security-policy"])
        self.assertNotIn("DEMO-ORDER", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store, private")
        self.assertFalse(any(value[0] == "flow" for value in self.store.data.values()))
        self.assertNotIn("never-send-this-to-browser", str(self.store.data))

    def test_callback_requires_browser_cookie_and_matching_state(self):
        self.client.get("/dashboard/auth/start")
        response = self.client.post("/dashboard/auth/callback", data={"state": "different", "code": "code"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.microsoft.exchange_count, 0)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/dashboard/auth/callback", data={"state": "browser-bound-state", "code": "code"}).status_code, 403)

    def test_callback_is_one_time_even_if_old_cookie_is_replayed(self):
        self.client.get("/dashboard/auth/start")
        handle = self.client.cookies.get(FLOW_COOKIE)
        body = {"state": "browser-bound-state", "code": "code"}
        self.assertEqual(self.client.post("/dashboard/auth/callback", data=body).status_code, 303)
        self.client.cookies.clear()
        replay = self.client.post("/dashboard/auth/callback", data=body, headers={"Cookie": FLOW_COOKIE + "=" + handle})
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(self.microsoft.exchange_count, 1)

    def test_nonce_rejection_from_msal_denies_login(self):
        self.microsoft.error = True
        self.assertEqual(self.sign_in().status_code, 403)
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_wrong_tenant_audience_issuer_unknown_user_or_expiry_denied(self):
        for key, value in (("tid", "different-tenant"), ("aud", "other-app"),
                           ("iss", "https://attacker.invalid"), ("oid", "unknown-user"),
                           ("exp", time.time() - 1)):
            with self.subTest(key=key):
                self.setUp()
                self.microsoft.result["id_token_claims"][key] = value
                self.assertEqual(self.sign_in().status_code, 403)
                self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_provider_error_does_not_leak_description(self):
        self.microsoft.result = {"error": "denied", "error_description": "sensitive-provider-text"}
        response = self.sign_in()
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("sensitive-provider-text", response.text)

    def test_revoked_user_loses_access_with_existing_cookie(self):
        self.sign_in()
        self.settings = replace(self.settings, access={})
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_expired_sessions_denied(self):
        self.sign_in()
        for key, (kind, payload, _) in list(self.store.data.items()):
            self.store.data[key] = (kind, payload, time.time() - 10)
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_logout_requires_same_origin_and_csrf_then_revokes_session(self):
        self.sign_in()
        csrf = self.client.get("/dashboard/api/me").json()["csrf"]
        handle = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.client.post("/dashboard/auth/logout").status_code, 403)
        self.assertEqual(self.client.post("/dashboard/auth/logout", headers={"Origin": "https://attacker.invalid", "X-CSRF-Token": csrf}).status_code, 403)
        self.assertEqual(self.client.post("/dashboard/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code, 200)
        self.assertNotIn(handle, self.store.data)
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_malformed_callback_and_query_mode_do_not_authenticate(self):
        self.client.get("/dashboard/auth/start")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.client.post("/dashboard/auth/callback", content="state=x&state=y", headers=headers).status_code, 400)
        self.assertEqual(self.client.post("/dashboard/auth/callback", content="x" * 16385, headers=headers).status_code, 400)
        self.assertEqual(self.client.get("/dashboard/auth/callback?code=x&state=x").status_code, 405)

    def test_configuration_fails_closed_without_touching_database(self):
        app = create_dashboard_app(settings_provider=lambda: (_ for _ in ()).throw(ValueError("secret value")),
                                   store_factory=lambda _: self.fail("Database must not be accessed"))
        client = TestClient(app, base_url=ORIGIN)
        response = client.get("/api/me")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret value", response.text)

    def test_settings_reject_common_authority_and_unsafe_origins(self):
        self.settings.validate()
        for change in ({"tenant_id": "common"}, {"origin": "http://example.test"},
                       {"origin": "https://user:password@example.test"}, {"origin": ORIGIN + "/path"},
                       {"access": {}}, {"access": {USER: {"role": "admin", "divisions": {"*": "Everything"}}}}):
            with self.subTest(change=change):
                with self.assertRaises((ValueError, TypeError)):
                    replace(self.settings, **change).validate()

    def enable_member_access(self):
        self.settings = replace(self.settings, access={}, allow_tenant_members=True,
                                member_divisions={"3977752": "James n Parson B.V."})
        self.microsoft.result["id_token_claims"]["acct"] = 0

    def test_tenant_members_can_sign_in_without_individual_assignment(self):
        self.enable_member_access()
        self.settings.validate()
        self.assertEqual(self.sign_in().status_code, 303)
        data = self.client.get("/dashboard/api/me").json()
        self.assertEqual(data["role"], "viewer")
        self.assertEqual(data["divisions"], {"3977752": "James n Parson B.V."})
        self.assertEqual(self.client.get("/dashboard/api/divisions/999999/worklist").status_code, 403)

    def test_members_can_enter_without_receiving_any_administration(self):
        self.enable_member_access()
        self.settings = replace(self.settings, member_divisions={})
        self.settings.validate()
        self.assertEqual(self.sign_in().status_code, 303)
        self.assertEqual(self.client.get("/dashboard/api/me").json()["divisions"], {})
        self.assertEqual(self.client.get("/dashboard/api/divisions/3977752/worklist").status_code, 403)

    def test_member_mode_never_treats_unknown_or_guest_as_member(self):
        for acct in (1, "1", None, "", "member", False, True, 0.0, [], {}):
            with self.subTest(acct=acct):
                self.setUp()
                self.enable_member_access()
                self.microsoft.result["id_token_claims"]["acct"] = acct
                self.assertEqual(self.sign_in().status_code, 403)

    def test_wrong_tenant_still_rejected_in_member_mode(self):
        self.enable_member_access()
        self.microsoft.result["id_token_claims"]["tid"] = "44444444-4444-4444-4444-444444444444"
        self.assertEqual(self.sign_in().status_code, 403)

    def test_explicitly_invited_guest_uses_only_its_own_grant(self):
        self.enable_member_access()
        self.microsoft.result["id_token_claims"]["acct"] = 1
        self.settings = replace(self.settings, access={USER: {"role": "viewer", "divisions": {"7654321": "Guest assigned company"}}})
        self.assertEqual(self.sign_in().status_code, 303)
        self.assertEqual(self.client.get("/dashboard/api/me").json()["divisions"], {"7654321": "Guest assigned company"})
        self.assertEqual(self.client.get("/dashboard/api/divisions/3977752/worklist").status_code, 403)
        self.settings = replace(self.settings, access={})
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_explicit_block_overrides_automatic_member_access(self):
        self.enable_member_access()
        self.assertEqual(self.sign_in().status_code, 303)
        self.settings = replace(self.settings, access={USER: {"disabled": True}})
        self.settings.validate()
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)
        self.assertEqual(self.sign_in().status_code, 403)

    def test_turning_off_member_access_revokes_existing_member_session(self):
        self.enable_member_access()
        self.sign_in()
        self.settings = replace(self.settings, allow_tenant_members=False)
        self.assertEqual(self.client.get("/dashboard/api/me").status_code, 401)

    def test_real_client_configuration_uses_minimal_identity_scope(self):
        with patch("app.dashboard.auth.msal.ConfidentialClientApplication") as factory:
            microsoft_client(self.settings)
            self.assertEqual(factory.call_args.kwargs["exclude_scopes"], ["offline_access"])
            self.assertEqual(factory.call_args.kwargs["authority"], "https://login.microsoftonline.com/" + TENANT)
            self.assertFalse(factory.call_args.kwargs["enable_pii_log"])


if __name__ == "__main__":
    unittest.main()
