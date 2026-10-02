# JNP Development Agent v1.2

The regression suite now also validates the v1.4 GET-only bank-line diagnostic for the golden case. The agent still performs no Exact financial writes.

## v1.5 automatic startup regression

The development agent now runs the complete read-only regression suite automatically after each deployment (default delay 8 seconds) and writes one structured log record beginning with `JNP_STARTUP_SELFTEST` to Render logs. This includes the v1.4 golden bank-line diagnostic for order 48451. No browser click is required.

Environment variables:
- `STARTUP_SELFTEST=true` (default)
- `STARTUP_SELFTEST_DELAY_SECONDS=8` (default)

The self-test performs GET/read-only calls only. It does not commit code and does not execute any Exact financial write.
