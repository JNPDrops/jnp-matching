# Opsplitsing van JNP-agents — uitvoeringsdossier

Status: voorbereiding; geen workers aangemaakt, geen runtimewijziging geactiveerd.
Opdracht: gebruiker heeft op 5 oktober 2026 rond 00:09 Europe/Amsterdam toestemming
gegeven om de agents en modules afzonderlijk in te richten en hier vannacht aan te werken.
Deze toestemming omvat benodigde infrastructuur voor de opsplitsing, maar verandert
de bestaande boekhoudkundige regels of de status van historische opdrachten niet.

## Gecontroleerd uitgangspunt

- Repository: JNPDrops/jnp-matching.
- Main en Render live bij inventarisatie: 74cecb7ec129c1210dd5dc4b1eb4b1f6644458d7 (PR #80).
- Workspace: tea-daumujs1nsns73ettb1g.
- Web: jnp-matching, srv-daun5mt9fdbs739jij7g, Python native,
  start uvicorn app.main:app --host 0.0.0.0 --port $PORT, autoDeploy uit.
- Dev-service: jnp-dev-agent, srv-davca067bikc73dghn4g, Python native, autoDeploy uit.
  Dit is geen shell of beheerinterface voor Render.
- Bestaande interne PostgreSQL-opslag wordt hergebruikt. Geen databaseverbindingen
  openbaar maken voor deze migratie.
- Root render.yaml beschrijft een oudere Docker-runtime; productie gebruikt native
  Python. Deze Blueprint mag niet blind worden toegepast.
- De Render-plugin kan services/deploys lezen en handmatig deployen, maar heeft geen
  create-worker-, private-service- of Blueprint-apply-actie. Activering via de Render-UI
  of een daarvoor geschikte toegestane capability is nog niet bewezen beschikbaar.
  Geen publieke webservice vermommen als worker om deze beperking te omzeilen.

Controleer main, open PR's en de actuele deployments opnieuw voor elke wijziging.
Andere werkzaamheden kunnen gelijktijdig nieuwe commits of financiële taken opleveren.

## Beoogde taakverdeling

| Onderdeel | Uitvoeromgeving | Afbakening |
| --- | --- | --- |
| Dashboard en toegang | Bestaande webservice | Microsoft-login, werklijst, bestaande OAuth-callbacks en API; op termijn geen financiële achtergrondtaken |
| Debiteurenroutering | Afzonderlijke worker | Bestaande nieuwe-entry-queue en historische cursor, uitsluitend bestaande routeringsregels |
| Woo-bankregels | Afzonderlijke worker | Bestaande IBAN/bankherkenning, eigen voortgang en processtatus |
| Belastingregels | Afzonderlijke worker | Bestaande tax-agent, bestaande activeringsvoorwaarden behouden |
| Bankuitzonderingen | Afzonderlijke worker of beperkte onderhoudstaak | Bestaande allocation-maintenance; menselijke beoordeling blijft in dashboard |
| Fibonatix | Eigen PSP-worker | Ophalen/verwerken scheiden van HTTP-aanvragen; historische taken behouden |
| ICEPAY | Eigen PSP-worker | Ophalen/verwerken, journal/import/match-fasen en bestaande taak-ID's behouden |
| Leesrapporten en probes | Afzonderlijke on-demand/scheduled taken | Niet opnieuw starten bij elke webdeploy; oude opdrachten niet herhalen |
| Ontwikkelagent | Bestaande afzonderlijke service | Geen automatische financiële taken of aanroep van /cycle of /research |

Dit is de gewenste logische verdeling, geen verklaring dat iedere adapter al bestaat.
Plisio, NinjaPay, SUAP en andere PSP's krijgen dezelfde adapterinterface wanneer een
bruikbare gegevensbron bestaat. Geen lege betaalde workers provisioneren zonder werk.

Voor honderden administraties: afzonderlijke verbindingen, taken, sleutels voor
idempotentie en voortgang per administratie/PSP, met gedeelde workerpools per taaksoort.
Een aparte Render-service of Exact-app per administratie is niet het standaardmodel.
Meerdere administraties kunnen parallel werken; opdrachten die dezelfde verbinding
of boeking raken blijven gecoördineerd.

## Essentiële gedeelde onderdelen

1. Exact-tokenbeheer:
   - app.main._access_token gebruikt nu alleen een asyncio.Lock binnen één proces.
     Voor meerdere processen is gedeelde exclusie nodig bij refresh en OAuth-callback.
   - operations/allocation_connection.py heeft al een PostgreSQL advisory lock voor
     de eigen Allocation-verbinding; behoud dit en controleer alle codepaden.
   - Lees de actuele opgeslagen token pas onder de juiste lock. Persisteer een
     geroteerde token voordat de lock vrijkomt. Geen tokenkopieën in workerbestanden.
   - Voorkom blokkering van de eventloop door wachtende synchrone DB-locks.
   - Fouten en logs mogen geen token, secret, OAuth-code of gevoelige response tonen.
2. API-budget:
   - Gedeeld per Exact-verbinding en administratie; respecteer gemelde limieten,
     Retry-After en resetmomenten.
   - Alle REST/XML/browser-gerelateerde uitvoerpaden inventariseren; niet alleen één
     client voorzien van een teller terwijl andere clients onbeperkt doorwerken.
   - Administratiebrede ruimte blijft onbekend als Exact die niet rapporteert.
     Houd rekening met andere apps zoals Xcore.
   - Geen automatische nieuwe appregistraties of sleutelrotatie om quota te omzeilen.
3. Taakbezit:
   - Duurzame queue/cursor en atomische claim/lease per administratie, taaksoort en taak.
   - Bestaande locks, audits, write-intents en uncertain-status behouden.
   - Verloren lease tijdens een financiële aanvraag leidt tot readback/beoordeling,
     niet blind opnieuw schrijven.
   - Bij SIGTERM geen nieuwe opdrachten aannemen; in-flight werk gecontroleerd
     afronden of als onzeker vastleggen. Claimduur en shutdown passen bij de taak.
4. Gezondheid:
   - Duurzame heartbeat, laatste succesvolle taak, wachtreden, veilige foutcode en
     verbruikmetadata zichtbaar in dashboard; in-memory status is na scheiding onvoldoende.
   - Bestaande Microsoft-rollen en administratiescheiding behouden.
5. Secrets:
   - Geen bestaande waarden uitlezen, tonen, kopiëren naar Git of exporteren.
   - Blueprint mag noodzakelijke bestaande env-vars via fromService/envVarKey
     verwijzen zonder hun waarden uit te lezen; geef elke rol alleen benodigde keys.
   - Een tokenbroker is een mogelijke latere beperking van toegang; dupliceer geen
     refresh-tokenbezit alsof elke worker een onafhankelijke verbinding heeft.

## Uitvoeringsvolgorde en terugval

### Fase 1 — code en gedeelde bescherming

- Haal de taakopstart uit de monolithische lifespan naar expliciete taakrollen.
- Laat de standaardconfiguratie voorlopig het huidige gedrag behouden.
- Voeg een headless worker-entrypoint met taakselectie en SIGTERM-afhandeling toe.
- Inventariseer ook HTTP-routes die zelf asyncio-taken starten of financiële acties
  rechtstreeks uitvoeren; verplaatsen van uitsluitend lifespan is onvoldoende.
- Maak tokenvergrendeling, taakbezit en budgetbewaking geschikt voor meerdere processen.
- Gerichte tests: geen dubbele claim; tokenrefresh-race; callback versus refresh;
  budgetreset; onzekere schrijfpoging niet herhalen; herstart behoudt cursor;
  dashboard geeft workerstatus uit opslag weer.
- Financiële tests gebruiken fixtures/fakes. Geen nieuwe financiële uitvoering als test.

### Fase 2 — afzonderlijke configuratie

- Maak een nieuwe expliciete worker-Blueprint; wijzig bestaande runtime niet.
- Native Python, passende build dependencies, automatische deployments uit.
- Browserwerk heeft mogelijk grotere instances en geïnstalleerde browser nodig.
  Bepaal dit uit actuele code; geen aannames dat iedere PSP alleen HTTP nodig heeft.
- Begin met kleinste passende workerplannen; documenteer gekozen kosten per service
  voordat resources worden aangemaakt. Geen autoscaling of nieuwe database nodig
  voor de eerste opsplitsing.
- Nieuwe workers eerst gepauzeerd/zonder taakclaim laten opkomen.
- Geen nieuw account, nieuw dagboek of nieuwe grootboekrekening creëren.

### Fase 3 — gecontroleerde overdracht per rol

1. Controleer main, bestaande deployment en lopende taken.
2. Drain de oude rol: geen nieuwe claims, lopende opdracht gecontroleerd afronden.
3. Zet uitsluitend die rol uit in de webservice.
4. Start de nieuwe worker voor die rol met dezelfde duurzame taakidentiteit.
5. Controleer één actieve eigenaar, voortgang en technische fouten, zonder oude
   financiële taken of imports te herhalen.
6. Rollback: eerst nieuwe eigenaar stoppen/drainen, dan oude rol activeren. Bewaar
   dezelfde cursor, audits en uncertain-status. Nooit twee schrijvers tegelijk.

Als create-worker of Blueprint-apply niet beschikbaar blijkt, lever code, tests,
exacte Blueprint en activeringsstappen op. Meld dat productieactivering geblokkeerd is;
claim niet dat aparte workers live zijn.

## Ongewijzigde financiële uitgangspunten

- Administratie 3977752; andere administraties pas na eigen aansluiting/autoriteit.
- wc_fibonatics blijft 100100; plisio -> 109377; bacs -> 109372;
  ic/icepay -> 109419; np_payments -> 109421; suap_wordpresspayplugin -> 109422.
- Bestaande boekingen, correcties, task-ID's en menselijke beslissingen behouden.
- Eigen order/factuur/betaling bepaalt matching; gelijk bedrag of sluitend saldo
  alleen is geen bewijs. Zie operations/DASHBOARD_EXCEPTIONS.md en
  operations/source_order_policy.py.
- Geen cross-order-verrekening, herimport of nieuwe aflettering als onderdeel van
  de infrastructuurmigratie. Bestaande productieprocessen houden hun huidige scope.
- Xcore en DIRECT_WOO_BANK blijven ongemoeid.
- Nooit POST /cycle of POST /research.
- Microsoft-werklijst: tenantleden en Roman zijn nu viewer. Een persoonlijke Object ID
  voor een behandelaarsrol is een afzonderlijk verzoek, geen migratievoorwaarde.

## Voortgang

| Moment | Resultaat |
| --- | --- |
| 2026-10-05 00:16 Europe/Amsterdam | Repository, huidige services, main en deployments geïnventariseerd. Proceslokale main-tokenlock en ontbrekende Render-workeractie vastgesteld. Dit dossier gereed; geen productieconfiguratie gewijzigd. |

Vul dit dossier bij elk werkblok aan met concrete commit/PR, tests, deployment-ID,
daadwerkelijk actieve rollen en resterende beperkingen. Gebruik alleen technische
metadata; geen klantregels of secrets. Een gepland vervolg is geen voltooide migratie.
