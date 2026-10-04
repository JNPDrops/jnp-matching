# Paragon login probe

This probe answers one question: can a fresh browser running in the existing
Render service sign in using the stored email, username, password and TOTP seed?
It does not download transactions, import into Exact, or enable a daily job.

## Controls

- Set `PARAGON_LOGIN_PROBE_ID=paragon-login-20261004-v1` for this requested test.
- The test expires at 2026-10-04 13:00 UTC and claims its ID in PostgreSQL before
  any login attempt. The claim is never automatically reset or retried.
- Required secrets: `PARAGON_EMAIL`, `PARAGON_USERNAME`, `PARAGON_PASSWORD`,
  `PARAGON_TOTP_SECRET`. The last accepts a Base32 secret or TOTP provisioning URI.
- Uses a new non-persistent Chromium context. No saved sessions or cookies.
- Requires the password step to reach `/2fa`, submits one generated code, then
  verifies `/dashboard`, its heading and signed-in greeting.
- Reports only fixed stages and booleans under `PARAGON_LOGIN_PROBE` in logs.
  No secret values, exception messages, browser traces, screenshots or page
  contents are logged. The child receives no Exact or database credentials.
- The browser installer uses the pinned Playwright release's official download
  path. It does not install system packages or bypass security challenges. If
  browser prerequisites are missing, the test fails at the corresponding stage.
- The child and its browser processes are terminated on cancellation or timeout.

The native Render service currently installs `requirements.txt` during build.
The one-off probe installs Chromium headless shell in temporary runtime storage;
this is a bounded discovery test, not the production daily-worker design.

Unit validation: `python -m unittest operations.test_paragon_login_probe`.
