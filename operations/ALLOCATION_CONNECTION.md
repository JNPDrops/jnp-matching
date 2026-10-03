# JNP Allocation — optional read-only quota probe

This second OAuth connection only checks API response limits. No routing worker,
financial write, key rotation or automatic quota fallback uses it.

## Register and store

Register `JNP Allocation` in Exact with this exact Redirect URI:

`https://jnp-matching.onrender.com/oauth/allocation/callback`

In the existing Render service `jnp-matching`, open **Environment** and add:

| Key | Value |
| --- | --- |
| `EXACT_ALLOCATION_CLIENT_ID` | New application's Client ID |
| `EXACT_ALLOCATION_CLIENT_SECRET` | New application's Client Secret |

The optional `EXACT_ALLOCATION_REDIRECT_URI` must equal the URL above; that is
already the default. Do not replace `EXACT_CLIENT_ID`, `EXACT_CLIENT_SECRET`, or
the primary callback configuration. Choose **Save only**; an assistant starts a
manual deployment after checking existing deployments. Never send secrets in chat.

After the configured version is live, open:

`https://jnp-matching.onrender.com/allocation/login`

Complete the Exact authorization with access to administration 3977752. OAuth
exchanges and renews tokens server-side. The new app's tokens are kept under a
client-specific `exact_allocation:` key in the existing `exact_oauth_tokens`
table. The primary `exact` token row is not read or updated by this connection.

## Probe and result

The callback performs one read-only request to `3977752/crm/Accounts`, requesting
only one ID. The response body is discarded. The stored public result contains
HTTP status, timestamp and any rate-limit headers Exact supplies. Missing headers
remain null; the shared administration allowance is not inferred from the app's
remaining allowance.

`GET /allocation/status` returns cached metadata and does not call Exact or load
access/refresh tokens. This allows the assistant to inspect the last result
without handling credentials.

For another probe, open `/allocation` in the authorized browser and press the
button. `POST /allocation/probe` requires the same signed session, a CSRF token,
and recent authorization. Requests are serialized in-process and with a separate
Postgres advisory lock; results are reused for at least 60 seconds. No scheduled
probing or background token refresh is enabled. Reconnect if Exact requires it.

OAuth query parameters are removed from the callback request scope before the
response/access log. Token responses and errors never appear in status or logs.
