# JNP Development Agent v1.1

Separate development-agent service for JNP Matching.

## Safe endpoints

- `GET /test` — deterministic read-only regression suite.
- `POST /cycle` — regression-repair cycle; with `AGENT_APPLY_CHANGES=false` no code is committed.
- `POST /research` — web-backed read-only research cycle focused on the Exact Online allocation problem. It reads the live regression state and current `app/main.py`, researches current sources with OpenAI web search, and returns an engineering report. It never writes to Exact and never commits code.

## Required secrets

- `OPENAI_API_KEY`
- `GITHUB_TOKEN`

## Safety

Keep `AGENT_APPLY_CHANGES=false` while using `/research`. Financial write paths remain outside autonomous modification.
