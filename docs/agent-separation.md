# Opsplitsing van JNP-agents — uitvoeringsdossier

Status: eerste runtime-/tokenfase gebouwd en lokaal getest in concept-PR #82;
nog niet samengevoegd of uitgerold. Geen workers aangemaakt of geactiveerd.
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
| 2026-10-05 01:10 Europe/Amsterdam | Gebouwd op actuele main 2fcf8c5dbd357ea80be8a41142b3daf2a81a1fb8 (PR #85): expliciete runtimecatalogus, beschermd headless startpunt en gedeelde main-tokenlock inclusief callback/401-paden. 95 tests geslaagd; één echte PostgreSQL-procesproef nog overgeslagen. Productie blijft dep-db1ddj8u01pc73duec3g, alle huidige rollen in de bestaande webservice. |
| 2026-10-05 03:04 Europe/Amsterdam | Fase twee gebouwd op main cf75100f845d1296eaaa60f85225caf3c656e4f2 (PR #87): duurzame role-leases/heartbeats, fail-closed leaseverlies, gedeelde Exact-budgetreserveringen en read-only dashboardstatus. 113 tests geslaagd; twee lokale PostgreSQL-procesproeven overgeslagen. Productie blijft dep-db1ebv6gekts73dec350; nog geen worker of schema geactiveerd. |

Vul dit dossier bij elk werkblok aan met concrete commit/PR, tests, deployment-ID,
daadwerkelijk actieve rollen en resterende beperkingen. Gebruik alleen technische
metadata; geen klantregels of secrets. Een gepland vervolg is geen voltooide migratie.

## Overdracht eerste codefase

Implementatie staat in dezelfde PR/branch als dit dossier. De commit met bericht
`refactor: extract guarded agent runtime and coordinate Exact token rotation`
bevat deze fase en neemt main tot en met PR #85 als tweede parent mee. Controleer
bij vervolg de daadwerkelijke branch-SHA en nieuwere main; niet terugzetten naar
een oud uitgangspunt.

### Gebouwd

- `app/runtime.py`: declaratieve catalogus van de twaalf bestaande lifespan-taken
  (inclusief dashboard-readiness), gegroepeerd per rol. De legacy-lifespan start
  dezelfde taken met dezelfde argumenten. Alle imports worden eerst opgelost;
  een importfout laat geen halve set verwerkers draaien. Bij afsluiten wordt elke
  taak geannuleerd en opgehaald, ook wanneer een andere taak faalt.
- `app/worker.py`: headless opdracht met SIGTERM/SIGINT-afhandeling en bewaking
  van onverwacht eindigende continue taken. Afgeronde eenmalige opdrachten worden
  niet opnieuw gestart. `python -m app.worker --role routing --check` toont alleen
  de taakselectie en ontbrekende voorwaarden, zonder imports van financiële code,
  databaseverbindingen of API-calls.
- Aparte rollen zijn bewust nog **niet uitvoerbaar**. De code blokkeert ze vóór
  imports/taakopstart; er is geen environment-override om dit te omzeilen.
  De webservice blijft expliciet `legacy`; nog geen rol/configuratie omgezet.
- `operations/exact_token_store.py`: async PostgreSQL advisory lock voor de
  bestaande main-tokenrij, op dezelfde verbinding als lezen en opslaan. Binnen
  die lock wordt de actuele token gelezen, eventueel vernieuwd en opgeslagen.
  De eigen Allocation-lock blijft ongewijzigd.
- Main OAuth-callback, normale tokenaanvraag en REST/XML-401-paden gebruiken nu
  dezelfde tokenafhandeling. Een verouderde 401 gebruikt een inmiddels opgeslagen
  nieuwere token; de oude onbeperkte 401-recursie is begrensd tot één herhaling.
  Netwerkfouten/5xx krijgen hierdoor geen nieuwe automatische retry.
- Tokenuitwisselingsfouten tonen geen provider-responsebody; de callback wist de
  eenmalige code uit de access-log-query. Lokale bestandsopslag blijft uitsluitend
  compatibiliteit voor één proces, geen alternatief voor gedeelde workers.

Dit is een gereed codeonderdeel, **geen bewijs van een afgeronde opsplitsing**.
Tokenvergrendeling alleen maakt financiële taakoverdracht nog niet veilig.

### Gecontroleerde overige uitvoerpaden

| Pad/onderdeel | Huidig eigenaarschap en vervolgstap |
| --- | --- |
| `/ops/fibonatix-20261002/{action}` | Start `asyncio.create_task(run(action))` in de webservice; bestaande `TASKS`, audit, vaste job-ID en advisory lock behouden. Vervangen door duurzame taakindiening vóór Fibonatix wordt gesplitst. |
| `/ops/fibonatix-20261002/strict/{mode}` | Start eveneens een webtaak; gebruikt legacy taskset/lock. Opnemen in dezelfde duurzame queue zonder historische opdrachten opnieuw aan te maken. |
| `/ops/fibonatix-20261002/audit_order_matches` | Alleen-lezen webtaak, maar deelt Fibonatix-eigenaarschap; meenemen bij drain. |
| `/order-rules/{order_number}/create` en `/direct-match/{bank_line_id}/execute` | Voeren vanuit HTTP zelf werkzaamheden uit onder bestaande flags. Niet vergeten bij het scheiden van web en financiële uitvoering. Geen endpoints aangeroepen tijdens deze fase. |
| Woo-regelontvangst | Bewaart al duurzame events; bestaande validatie/HMAC en entry-ID behouden. |
| ICEPAY lifespan | `icepay_transactions.run()` kiest ook journal/import/reconcile/match op bestaande activatie-ID's. Een nieuw proces mag die ID's niet resetten of historische runs herhalen. |
| Tax en maintenance | Lezen API-budget uit `automatic_debtor_routing.STATUS` in hetzelfde geheugen. Dit moet naar gedeelde opslag voordat deze rollen in aparte processen draaien. |

### Validatie

Uitgevoerd zonder productieverbindingen, financiële handelingen of secrets:

```sh
python -m unittest discover -s tests -q
python -m unittest operations.test_automatic_debtor_routing operations.test_routing_transport operations.test_customer_only_routing operations.test_source_order_policy operations.test_allocation_connection -q
git diff --check
```

- Dashboard/runtime/tokens: 60 ontdekt, 59 geslaagd en 1 expliciet overgeslagen.
- Bestaande routing/transport/beleid/Allocation-tests: 36 geslaagd.
- In totaal 95 geslaagd. Nieuwe tests bewijzen onder meer afzonderlijke testsessies
  met één tokenrefresh, callback versus refresh, verouderde 401, locktimeout,
  cancellation, niet teruggeven bij opslagfout, geen gedeeltelijke taakopstart,
  volledige cleanup, procesexit bij taakuitval en SIGTERM-afhandeling.
- Echte PostgreSQL-procesproef staat in `tests/test_exact_token_postgres.py`.
  Deze is nog **niet uitgevoerd**: lokaal is geen server beschikbaar en installatie
  liep vast op ontbrekende OS-rechten. De bestaande productie-DB is niet benaderd.
  De proef gebruikt uitsluitend een expliciete lokale `jnp_test_*`-database,
  een apart tijdelijk schema en synthetische tokens; nooit `DATABASE_URL`.

### Eerstvolgende werk

1. Voer de echte PostgreSQL-procesproef uit zodra een toegestane lokale testserver
   beschikbaar is. Controleer vervolgens tokenrotatie bij echte procesoverlap;
   geen productiecredentials nodig voor deze test.
2. Bouw gedeelde API-reserveringen/observaties per verbinding en administratie.
   Inventaris: `bacs_debtor_transfer.Exact`, `woo_iban_rules.ExactAPI`,
   `tax_agent.TaxAPI`, `allocation_maintenance.MaintenanceAPI`, main REST/XML,
   Fibonatix XML en ICEPAY import/matching/journal hebben deels eigen clients.
   Eén wrapper aanpassen is dus niet voldoende. Behoud de bestaande no-retry
   bescherming van financiële schrijfclients.
3. Bouw duurzaam role-owner/heartbeat/drain en HTTP-taakindiening; lees deze status
   vanuit het dashboard. Behoud bestaande gespecialiseerde locks/audits.
4. Pas pas daarna de execution-gates per bewezen rol aan, maak de concrete nieuwe
   worker-Blueprint met secretverwijzingen en kosten, en voer gefaseerde overdracht uit
   wanneer de benodigde Render-aanmaakactie werkelijk beschikbaar is.

Geen merge/deployment uitgevoerd in deze fase: echte gedeelde PostgreSQL-werking,
API-budgetten en veilige drain zijn nog niet voldoende bewezen voor activering.
Het bekende Render-aanmaakprobleem blijft staan; er zijn geen nieuwe kosten gemaakt.

## Overdracht tweede codefase

Deze fase voegt coördinatie toe maar opent geen execution-gate. De aparte rollen
blijven fail-closed totdat de gedeelde budgetlaag in iedere Exact-client zit, de
PostgreSQL-procesproeven slagen en drain per rol is bewezen.

### Duurzaam taakbezit

- `operations/worker_coordination.py` definieert één lease per administratie en
  rol met een willekeurige lease-ID, actieve eigenaar, gewenste volgende eigenaar,
  heartbeat, verloopmoment, drainstatus en beperkte operationele details.
- Claims en overdrachten worden per administratie/rol onder een transactionele
  PostgreSQL advisory lock uitgevoerd. Een levende andere eigenaar kan niet worden
  overgenomen. Een verlopen lease kan wel worden hersteld. Tijdens een expliciete
  overdracht kan na vrijgave uitsluitend de vooraf gekozen eigenaar claimen.
- De headless worker claimt vóór taakopstart, vernieuwt de lease periodiek en stopt
  de taken als de heartbeat faalt, de lease is verloren of drain wordt aangevraagd.
  Bij normale of foutieve exit wordt de lease vrijgegeven. Bestaande specifieke
  financiële locks/audits blijven daarnaast verplicht.
- De huidige webservice registreert zichzelf nog niet als afzonderlijke role-owner;
  dit is bewust onderdeel van de latere gefaseerde overdracht. De nieuwe workers
  kunnen door de execution-gates nog niet starten.

### Gedeeld Exact-API-budget

- Budgetstatus is gescheiden per administratie en Exact-koppeling (`main` of
  `allocation`). Een call reserveert atomisch vóór verzending met uitsluitend:
  request-ID, rol, prioriteit, methode en tijdstip. URL, payload, responsebody,
  klantgegevens en credentials worden niet opgeslagen.
- Dag- en minuutheaders worden numeriek gevalideerd. Een reservering telt andere
  nog lopende/onzekere calls mee. Na een response vervangt de nieuwste waarneming
  de teller; een niet-verzonden call wordt vrijgegeven; een onzekere netwerkuitkomst
  blijft conservatief gereserveerd totdat een latere providerwaarneming de teller
  opnieuw vastlegt.
- Zonder minuutheaders geldt voorlopig een gedeelde bovengrens van 30 reserveringen
  per minuut per koppeling. Bekende dagreserves worden eveneens gerespecteerd.
  Deze fallback voorkomt dat meerdere processen elk hun eigen 1,2-secondenlimiet
  gebruiken en samen de minuutlimiet overschrijden.
- De opslaglaag en beslisregels zijn gebouwd. De bestaande clients zijn nog niet
  allemaal omgezet: main REST/XML, `bacs_debtor_transfer.Exact`, Woo-regels,
  tax, maintenance, tax-allocation, Fibonatix XML en ICEPAY-paden moeten iedere
  call via deze laag reserveren/afronden met behoud van hun no-retry-regels.
  Daarom blijft `shared_api_budget_client_integration_pending` actief.

### Dashboard en gegevensgrens

- Het bestaande Microsoft-dashboardresultaat bevat voortaan een afzonderlijk
  `agents`-blok zodra de coördinatietabellen bestaan: duurzame rolstatus,
  leasegeldigheid en veilige budgetmetadata. Het blijft read-only en zet
  `financial_execution_enabled` niet aan.
- Interne owner-ID's worden voor weergave gehasht. Heartbeatdetails accepteren
  alleen een vaste lijst operationele velden; exceptions, providerresponses,
  URLs, tokens en payloads worden niet opgeslagen of getoond.
- Als de tabellen nog niet bestaan of de statusbron faalt, meldt het dashboard
  `available: false` zonder databaseadres of foutdetails. Dit is de huidige
  productie-uitkomst totdat een geteste migratie wordt geactiveerd.

### Validatie fase twee

Uitgevoerd met synthetische gegevens en zonder productie-DB/API:

```sh
python -m unittest discover -s tests -q
python -m unittest operations.test_automatic_debtor_routing operations.test_routing_transport operations.test_customer_only_routing operations.test_source_order_policy operations.test_allocation_connection -q
git diff --check
```

- Testmap: 79 ontdekt, 77 geslaagd, 2 PostgreSQL-procesproeven overgeslagen.
- Bestaande routing/transport/beleid/Allocation: 36 geslaagd.
- Totaal 113 geslaagd. Nieuwe tests dekken onder meer concurrerende budgetlogica,
  dag-/minuutreserve, ontbrekende headers, live/expired lease, gewenste overdracht,
  heartbeatfiltering, leaseverlies en dashboardafscherming.
- `tests/test_worker_coordination_postgres.py` start in een expliciete lokale
  `jnp_test_*`-database vier processen: twee concurreren om één role-lease en twee
  om de laatste API-budgetplaats. Per paar mag exact één proces winnen.
  De eerdere token-procesproef blijft daarnaast bestaan. Beide zijn nog niet lokaal
  uitgevoerd omdat een PostgreSQL-server ontbreekt; productie is niet gebruikt.

### Resterend vóór activering

1. Draai beide opt-in procesproeven op een lokale/test-PostgreSQL.
2. Integreer alle Exact-clients met reservering en veilige afronding. Schrijfcalls
   krijgen nooit een automatische retry; een onzekere call blijft voor readback.
3. Implementeer per continue taak een drainpunt vóór een nieuwe claim/call; SIGTERM
   mag geen nieuwe taak starten en moet lopend werk afronden of onzeker vastleggen.
4. Maak en valideer de afzonderlijke native-Python worker-Blueprint, inclusief
   handmatige deploys, secretverwijzingen, kleinste passende plannen, browservereisten,
   `maxShutdownDelaySeconds` en kosten.
5. Activeer pas daarna één rol tegelijk volgens de overdrachtsprocedure. Tot die tijd
   blijft productie monolithisch en worden geen nieuwe Render-kosten gemaakt.
