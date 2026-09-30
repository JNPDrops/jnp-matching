# JNP Matching - Exact Online read-only prototype

Dit prototype zoekt bankregels op grootboek **1360** en koppelt een ordernummer uit de omschrijving aan een openstaande post op verzameldebiteur **100100** met `YourRef = TD{ordernummer}`.

## Belangrijk

- Deze versie is **read-only**: er wordt niets in Exact gewijzigd of afgeletterd.
- Zet `EXACT_CLIENT_SECRET` en tokens nooit in Git of chat.
- Division staat standaard op `3977752`.

## Exact app-registratie

De redirect URI moet exact gelijk zijn aan:

`https://<jouw-host>/oauth/callback`

Bijvoorbeeld na deployment op een host met URL `https://jnp-matching.onrender.com`:

`https://jnp-matching.onrender.com/oauth/callback`

## Environment variables

Kopieer `.env.example` naar `.env` voor lokale ontwikkeling, of configureer deze bij je host:

- `EXACT_CLIENT_ID`
- `EXACT_CLIENT_SECRET`
- `EXACT_REDIRECT_URI`
- `EXACT_DIVISION=3977752`
- `SESSION_SECRET`

## Start lokaal

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Voor Exact OAuth moet de redirect URI bereikbaar zijn op de URI die in Exact geregistreerd staat. Voor een cloud deployment gebruik je daarom de publieke HTTPS-hostnaam.

## Routes

- `/` - dashboard
- `/login` - start Exact OAuth
- `/oauth/callback` - OAuth callback
- `/api/status` - controleer verbinding en `current/Me`
- `/dry-run?limit=100` - visuele dry-run
- `/api/dry-run?limit=100` - JSON dry-run
- `/health` - health check

## Matchregels v0.1

1. Pak bankregels met `GLAccountCode = 1360`.
2. Herken een ordernummer in de bankomschrijving, bijvoorbeeld `VERCAIGNE BO 48451` -> `48451`.
3. Zoek Exact openstaande post met:
   - `AccountCode = 100100`
   - `YourRef = TD48451`
4. Alleen `READY` wanneer precies 1 openstaande post is gevonden en bedrag exact gelijk is.
5. Alle andere situaties worden `REVIEW_*` en er gebeurt niets.

## Volgende stap

Na validatie op historische transacties kan een aparte write-module worden toegevoegd. Die wordt bewust niet in dit prototype opgenomen totdat de exacte Exact-mutatie voor "Edit Match" op regels die nog op 1360 staan in een testadministratie is bevestigd.
