# Operationele controle tot en met 5 oktober 2026

Gecontroleerd op 6 oktober 2026 vanaf 01:00 Europe/Amsterdam. Cutoff exclusief
2026-10-06T00:00:00+02:00. Deze controle is geen verklaring dat de inhaalslag is
uitgevoerd. Er zijn door deze sessie geen financiële opdrachten gestart.

## Actueel vastgesteld

- Main b7fd86c3dbe369b630e8f8a0367c22837015b178; concept-PR #82 op
  e9086e9fe984596b0a039fe1e04ca9e831cd1051. Open PR #1 betreft oude CAMT-probes.
- Productie cf75100f845d1296eaaa60f85225caf3c656e4f2, live deployment
  dep-db1ebv6gekts73dec350. Dev blijft ca0b1a46fa0169ac911a8d18b585b642afeff82c,
  dep-davn2l60tbcc73ek6oa0. Geen lopende nieuwe deployment; beide autoDeploy uit.
  Geen afzonderlijke workers in de Render-service-inventaris.
- Publieke health/status op productie bevestigen actieve routering in `watching`,
  laatste scan 2026-10-05T23:00:16Z. JNP Allocation is configured/connected en meldt
  2173 resterende calls van 5000. Dit is een tijdgebonden waarneming, niet de
  garantie dat ieder uur of iedere andere verbinding hetzelfde budget heeft.
- De huidige processtatus telt 59 toegepaste routeringen sinds die processtart.
  Dit is GEEN aantal dat tijdens deze controle is uitgevoerd. De historische
  cleanup telt 1141 applied en 1 uncertain, plus 4 unclassified. De tabellen/scopes
  mogen niet worden opgeteld: overlap is niet onafhankelijk vastgesteld.
- De bankcontrole van 2026-10-05T22:22:16Z meldt 115 dashboarditems, 18 kandidaten
  (3 BACS, 15 PSP) en 21 ontbrekende inkoopfacturen. `bank_writes=false` en
  `automatically_executed=false`: kandidaten zijn geen uitgevoerde matches.
- Beide legacy handmatige financiële HTTP-featureflags melden false. Dit bewijst
  niet dat ieder eerder gestart verzoek of iedere financiële taak afgerond is.
- ICEPAY-log van 2026-10-04T21:13:31Z meldt `import_verified` voor de bestaande
  36 ontvangsten van 1–3 oktober; dus niet opnieuw importeren. De historische
  match-prepare-run van 21:37:22Z meldt nul saves. Dit is geen actuele uitspraak
  over menselijke correcties daarna. Die moeten eerst in Exact worden gelezen.
- Voor nieuwe perioden tot de cutoff en de actuele Fibonatix-matchstatus is geen
  volledige bron-/Exact-inventaris verkregen. Daarover geen aantallen invullen.

## Concrete uitvoeringsgrenzen

De bestaande ICEPAY-activeringen en Fibonatix-controlroutes zijn vastgelegd voor
historische dossiers en verlopen op 5 oktober 18:00 UTC. De huidige opdracht geeft
operationele toestemming, maar maakt de oude technische taak-ID's niet geschikt
voor nieuwe perioden. Geen expiry, hash, claim of oud dossier overschreven.

De read-only Render-PostgreSQL-tool is opnieuw geprobeerd met uitsluitend een
schema-inventarisatie. De tool wordt geblokkeerd omdat de database alle externe
verbindingen weigert. Geen IP-allowlist of publieke databasepoort aangepast.
Er is geen shell-/worker-create-/Blueprint-apply-actie beschikbaar; de dev-service
is geen shell. Er is geen toegestane browserfallback of andere bestaande
geauthenticeerde bedieningsroute aangetoond waarmee deze sessie de dossiers kan
lezen en nieuwe PSP-opdrachten kan starten zonder secrets uit te lezen.

Daarom geen env-merge/deploy als omweg: die herstart productie terwijl een veilige
complete drain niet bewezen is. De bestaande continue routering is niet gestopt.
Geen nieuwe boekingen, afletteringen, reimports of infrastructuur aangemaakt.
Geen /cycle of /research, geen tokens/env-dump, geen onzekere poging herhaald.

## Vervolg dat uitvoering mogelijk maakt

1. Een geautoriseerde interne uitvoerings-/beheerroute in de bestaande Render-
   omgeving met toegang tot dezelfde private DB en reeds opgeslagen credentials.
   Alleen status/taakresultaten teruggeven, nooit secrets of cookies.
2. Daarmee eerst bestaande PSP-bronnen, imports, actuele eigen-ordermatches,
   menselijke correcties en lopende taken teruglezen; overlap/uncertain uitsluiten.
3. Voor alleen nog ontbrekende perioden nieuwe duurzame taakidentiteiten en
   gevalideerde bronselecties maken; bestaande historische claims behouden.
   Pas daarna import/aflettering per eigen order, met daadwerkelijke readback.

De infrastructuurtests zijn geen operationele PSP-uitvoering. De eerste poging
van workflow 37372650914 (head e9086e9fe984596b0a039fe1e04ca9e831cd1051) was
geannuleerd zonder runner of stappen. Eén herstart is uitgevoerd en is GESLAAGD:
run_attempt=2, job 112019986193, voltooid rond 2026-10-05T23:03:42Z.

Resultaten uit de echte joblogs: 208 tests in tests/, 36 operations-unittests,
139 maintenance/Woo/tax/banktests, 27 Fibonatix/strict-tests en 29 ICEPAY-tests:
**439 geslaagd, nul overgeslagen**. De 21 eerder lokaal overgeslagen PostgreSQL-
proeven zijn nu dus uitgevoerd. De database is een synthetische PostgreSQL 16
CI-container, niet productie. Eén bestaande Starlette-deprecatiewaarschuwing.

De databasevalidatievoorwaarde is opgelost voor deze exacte codecommit. De
runtime-gates blijven ongewijzigd omdat de vereiste eerste globale drain en
uitvoeringsmogelijkheid nog niet bewezen zijn. Geen merge/deploy als gevolg
van alleen een geslaagde test. De dossierwijziging is documentatie en bevat
geen runtimecode; een nieuwe automatische teststart daarvoor is overbodig.

Bewijs: https://github.com/JNPDrops/jnp-matching/actions/runs/37372650914/attempts/2

Geen financiële klantrecords, order-/betalings-ID's, credentials of exports zijn
in dit document opgeslagen. Bewijsbronnen: Render-deployments/logs, /health,
/allocation/status en GitHub workflow/jobmetadata.
