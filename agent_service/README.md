# JNP Development Agent

Separate read-only development agent for JNP Matching.

## Safety model
- Never writes to Exact Online.
- Cannot change financial write paths in `app/main.py`; a diff gate rejects changes touching MatchSets, POST/execute routes, write flags, or Exact POST helpers.
- Works on one manually triggered cycle at a time.
- By default `AGENT_APPLY_CHANGES=false`, so it only proposes a patch.
- Golden regression case is WooCommerce order 48451 / bank-line GUID `591003b9-cfb0-4158-b23c-fdc106801a5e` / Exact sales entry `26722659` / EUR 78.60.

## Environment variables
Required to propose code changes:
- `OPENAI_API_KEY` (set directly in Render, never paste in chat)
- `GITHUB_TOKEN` fine-grained token with Contents read/write for only `JNPDrops/jnp-matching`

Recommended:
- `TARGET_URL=https://jnp-matching.onrender.com`
- `GITHUB_REPO=JNPDrops/jnp-matching`
- `GITHUB_BRANCH=main`
- `OPENAI_MODEL=gpt-5.6-sol`
- `AGENT_APPLY_CHANGES=false`
- `MAX_AGENT_ITERATIONS=1`

## Render service
Run this as a **separate** Render web service with auto-deploy disabled:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn agent_service.main:app --host 0.0.0.0 --port $PORT`

Keeping auto-deploy off prevents the agent from restarting itself when it commits a repair to the matching app.
