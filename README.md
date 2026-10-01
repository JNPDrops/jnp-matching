# JNP Matching v1.3 — research-capable development agent

Adds a dedicated read-only development research cycle to the existing JNP development agent.

New endpoint on `jnp-dev-agent`:

- `POST /research` — researches, with web search, how Exact Online can safely allocate one already imported BankEntryLine to debtor 100100 and then match it without a memorial/general-journal workaround. It executes no financial writes and commits no code.

Existing matching application behavior is unchanged.
