# Opsplitsing van JNP-agents — uitvoeringsdossier

Status: gedeelde basis, routeringsworker, Woo-bankregels-worker en tax-worker gebouwd in
concept-PR #82. Databasevalidatie en veilige eerste productieoverdracht staan nog
open; niet samengevoegd of uitgerold. Geen workers aangemaakt of geactiveerd.
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
| 2026-10-05 05:27 Europe/Amsterdam | Fase drie voorbereid op dezelfde actuele main/live cf75100f845d1296eaaa60f85225caf3c656e4f2, deployment dep-db1ebv6gekts73dec350: alle geïnventariseerde Exact REST/XML-applicatiecalls gebruiken nu de gedeelde budgetwrapper; financiële POST/PUT-calls worden niet automatisch herhaald. Een afzonderlijke native worker-Blueprint met zes handmatige services is toegevoegd. 121 tests geslaagd; twee lokale PostgreSQL-procesproeven overgeslagen. Productie en rollen zijn ongewijzigd. |

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
- In fase drie zijn main REST/XML, `bacs_debtor_transfer.Exact`, Woo-regels,
  tax, maintenance, tax-allocation, Allocation-probe, Fibonatix XML en de
  afzonderlijke ICEPAY-lees-/schrijfpaden aangesloten. OAuth-tokenuitwisseling
  en Metorik-requests vallen buiten het Exact-applicatiebudget. De resterende
  gate heet nu `shared_api_budget_postgres_test_pending`: integratie is gebouwd,
  maar procesoverlap tegen een echte test-PostgreSQL is nog niet bewezen.

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
2. Draai de geïntegreerde clients tegen een lokale/test-PostgreSQL en bewijs dat
   reservering, response-observatie en onzekere uitkomst over processen werken.
3. Implementeer per continue taak een drainpunt vóór een nieuwe claim/call; SIGTERM
   mag geen nieuwe taak starten en moet lopend werk afronden of onzeker vastleggen.
4. Laat de voorbereide Blueprint door Render valideren en pas hem pas toe nadat
   per rol de drain en execution-gate zijn vrijgegeven.
5. Activeer pas daarna één rol tegelijk volgens de overdrachtsprocedure. Tot die tijd
   blijft productie monolithisch en worden geen nieuwe Render-kosten gemaakt.

## Overdracht derde codefase

### Gedeelde verzending voor alle Exact-applicatiecalls

- `operations.worker_coordination.budgeted_http` reserveert vóór de daadwerkelijke
  HTTP-call in PostgreSQL en roept de aangeleverde zender exact één keer aan.
  Daarna worden uitsluitend gevalideerde quotaheaders opgeslagen. Bij transportfout
  of cancellation wordt de reservering `uncertain`; er volgt geen automatische
  financiële retry. Als reservering faalt, wordt de HTTP-call niet gestart.
- De Allocation-facade markeert zichzelf nu expliciet als koppeling `allocation`;
  overige clients gebruiken `main`. Hierdoor delen rollen hun limiet per werkelijke
  Exact-app, niet per proces. In de bestaande lokale éénprocesmodus zonder database
  blijft de helper compatibel; de headless rollen eisen zelf wel `DATABASE_URL`.
- De vroegere generieke 401-herhaling in `app.main` is beperkt tot GET. MatchSets
  en andere POST/PUT-writes krijgen na 401 of een onzekere transportuitkomst geen
  tweede poging. Bestaande taak-audits en readback blijven beslissend.
- Een broninventarisatie na de wijziging vindt geen directe Exact REST/XML-call
  buiten de wrapper. De overgebleven directe HTTP-calls zijn OAuth-tokenuitwisseling
  of externe Metorik-reads en verbruiken dit Exact-applicatiebudget niet.

### Voorbereide Render-configuratie

`render/agent-workers.yaml` is een **afzonderlijke, nog niet toegepaste** Blueprint.
Hij wijzigt de oude root-Blueprint niet en bevat uitsluitend echte `worker`-services,
native Python, branch `main`, `autoDeployTrigger: off`, één instance en 300 seconden
shutdownruimte. Bestaande waarden worden alleen met `fromService/envVarKey` vanaf
`jnp-matching` verwezen; er staan geen secretwaarden in Git. De bestaande interne
PostgreSQL wordt hergebruikt en niet publiek gemaakt.

| Voorstel | Rol | Plan | Richtprijs per maand op 5 oktober 2026 |
| --- | --- | --- | ---: |
| jnp-routing-worker | debtor-routing | 0.5c-512mb | $7 |
| jnp-woo-rules-worker | Woo-bankregels | 0.5c-512mb | $7 |
| jnp-tax-worker | tax-agent | 0.5c-512mb | $7 |
| jnp-maintenance-worker | bankuitzonderingen/maintenance | 0.5c-512mb | $7 |
| jnp-icepay-worker | ICEPAY inclusief browser | 1c-2g | $25 |
| jnp-reports-worker | leesrapporten/probes inclusief browser | 1c-2g | $25 |
| **Totaal indien alle zes later actief worden** |  |  | **$78** |

Fibonatix staat bewust niet als lege worker in de Blueprint: de bestaande opdrachten
worden nog via HTTP in de webservice gestart en missen een duurzame indieningsqueue.
Ook voor PSP's zonder adapter is niets geprovisioned. De kosten zijn de actuele
Render-computeprijzen, exclusief bestaand web/databasegebruik, bandwidth en belastingen.

### Validatie fase drie

Uitgevoerd zonder productie-DB/API, financiële handelingen of secrets:

```sh
python -m unittest discover -s tests -q
python -m unittest operations.test_automatic_debtor_routing operations.test_routing_transport operations.test_customer_only_routing operations.test_source_order_policy operations.test_allocation_connection -q
python -c "import yaml; ..."  # syntactische parse van zes worker-services
git diff --check
```

- Testmap: 87 ontdekt, 85 geslaagd en 2 expliciete PostgreSQL-procesproeven
  overgeslagen. Bestaande routing/transport/beleid/Allocation: 36 geslaagd.
  Totaal 121 geslaagd.
- Nieuwe tests bewijzen reserve-before-send, niet verzenden bij budgetweigering,
  onzeker markeren bij transportfout, geen verborgen retry na opslagfout, GET-only
  401-herhaling en de belangrijkste Blueprint-invarianten. De YAML is daarnaast
  met een onafhankelijke parser als zes services geladen.
- De Render-plugin heeft geen worker-create- of Blueprint-apply-actie. Daarom is
  niets aangemaakt, is geen $78/maand geactiveerd en is geen deploy gestart.
- De execution-gates blijven dicht wegens echte PostgreSQL-proef, bewezen drain
  en HTTP-taakqueue. Productie blijft alle huidige rollen uitvoeren in de bestaande
  webservice; dit voorkomt twee gelijktijdige schrijvers.

## Vervolg 5 oktober 2026, ochtend — routering en testomgeving

De derde fase was lokaal vastgelegd als `03e6dad`, maar na de onderbroken
GitHub-publicatie stond PR #82 nog op `f9dee1b3ebb91bdac017763ed5af24f1020e72ee`.
De ochtendcontrole bevestigde main/live `cf75100f845d1296eaaa60f85225caf3c656e4f2`;
er was geen deployment bezig. Deze vervolgcommit neemt de lokale fase drie mee.

- Routering heeft nu een eigen coöperatief stopsignaal. Bij afsluiten wacht de
  runtime maximaal 240 seconden op de lopende routering. De bestaande cycluslock
  en financiële audit blijven daarbij in gebruik. Vóór de volgende entry/cyclus
  wordt gestopt; een wachttijd van een minuut is direct onderbreekbaar. Bij
  overschrijding wordt expliciet `routing_drain_timeout_review_required` gelogd.
  Dit is nog geen bewezen productieoverdracht: gedeeld taakbezit in de legacy-webrol,
  write-fencing bij leaseverlies en een beheerpad voor drain blijven nodig.
- Quota-antwoorden uit hetzelfde of een ouder resetvenster kunnen de teller niet
  meer verhogen. Nog lopende reserveringen blijven meetellen als ze ouder zijn
  dan het laatst ontvangen antwoord. Onzekere requests tellen conservatief 24 uur
  mee; dat kan eerder pauzeren maar verleent geen extra API-ruimte.
- Een gedeelde budgetweigering wordt in routering als wachten afgehandeld, niet
  als een verzonden/transportfout. Gelijktijdige eerste schema-initialisatie is
  onder een PostgreSQL advisory lock gebracht.
- Blueprint: `off` is expliciet een string, routering verwijst naar de bestaande
  Metorik-key en de main-API-workers verwijzen ook naar EXACT_REDIRECT_URI.
- `.github/workflows/agent-separation-tests.yml` is voorbereid: PostgreSQL 16 als
  tijdelijke GitHub-service, uitsluitend een `jnp_test_agents`-database, synthetische
  credentials en repository-read-rechten. Geen productiesecrets of Exact-acties.
  De workflow draait de bestaande twee procesproeven plus een echte databaseproef
  voor vertraagde quota-antwoorden en een nog lopende reservering.

Lokale validatie: 92 tests ontdekt, 89 geslaagd, 3 PostgreSQL-proeven overgeslagen;
daarnaast 36 bestaande routing/transport/beleid/Allocation-tests geslaagd. Totaal
125 geslaagd. Blueprint syntactisch geladen; zes handmatige deployments bevestigd.
Een lokale PostgreSQL-installatie is opnieuw geprobeerd en faalt op OS-rechten.
Pas een geslaagde GitHub-run geldt als bewijs van de drie databaseproeven.

Geen execution-gate vrijgegeven, geen merge, deployment of infrastructuur gemaakt.
Eerst de CI-uitkomst controleren en daarna gedeelde legacy-role-claims en veilige
drainbediening bouwen. Vervolgens één rol uitschakelen in web en dezelfde rol
starten als worker. De plugin heeft nog geen worker-create/Blueprint-apply; een
Render-UI- of CLI-stap blijft nodig wanneer de code voor overdracht gereed is.

### Vastgelegd en werkelijk getest

Codecommit: `f255e1e0843d5cfd961d0d144264f0a68ed76071`, opgeslagen in PR #82.
GitHub Actions is daadwerkelijk uitgevoerd met tijdelijke PostgreSQL 16:

- [Push-run 37285557409](https://github.com/JNPDrops/jnp-matching/actions/runs/37285557409): success.
- [PR-run 37285558302](https://github.com/JNPDrops/jnp-matching/actions/runs/37285558302): success.
- Logs bevestigen 92 + 36 = **128 tests geslaagd, geen skips**. De drie eerder
  geblokkeerde PostgreSQL-proeven zijn dus nu uitgevoerd: één refresh bij twee
  processen, één winnaar bij concurrerende role-/budgetclaims, en conservatief
  omgaan met vertraagde quota-antwoorden en nog lopende requests.

De drie PostgreSQL-testgates zijn daarom vervangen door de concrete resterende
voorwaarden: `legacy_role_ownership_pending`, `lease_write_fencing_pending` en
`verified_drain_pending`. Geen rol is hiermee uitvoerbaar gemaakt. De volgende
codefase moet legacy en worker hetzelfde eigenaarschap laten gebruiken, dat
eigenaarschap vlak vóór een schrijfactie controleren, en de beheeractie voor
drain/overdracht duurzaam vastleggen. De huidige routing-stop is hiervoor een
bouwsteen, geen volledige productieoverdracht. HTTP-gestarte Fibonatix-taken
houden bovendien hun afzonderlijke queue-gate.

## Vervolg 5 oktober 2026, avond — eigenaarschap bij verzending

Bij hervatting stond PR #82 nog op `ee73f52bcd4ebb89e3d3714cd3ce8e6cd466d37f`.
Main/live was ongewijzigd `cf75100f845d1296eaaa60f85225caf3c656e4f2`, deployment
`dep-db1ebv6gekts73dec350`. Alleen PR #82 en de oudere probe-PR #1 stonden open.
Er was geen nieuwere implementatie of lopende deployment om mee te concurreren.

### Nieuwe codefase: duurzame schrijfblokkade

- `operations/worker_write_fence.py`: headless taken erven via een ContextVar hun
  role-owner. Bij een schrijfactie wordt de actuele eigenaar/lease en drainstatus
  in PostgreSQL gecontroleerd, onder dezelfde role-lock als een overname. De
  databaseklok bepaalt of de lease nog geldig is. Een andere administratie,
  database of ontbrekende operation-adapter wordt geweigerd vóór verzending.
- De tabel `jnp_worker_writes` bewaart een operation-ID, administratie, rol,
  lease-ID, verbinding, methode, tijdstippen en status. Geen URL, payload, token
  of klantgegevens. Een toegelaten schrijfpoging blijft duurzaam `unresolved`
  totdat de eigen operation-adapter haar HTTP-bevestiging én audit heeft voltooid.
- Een nieuwe role-claim wordt bij zo'n onopgeloste schrijfpoging geweigerd, ook
  bij een verlopen lease of na vrijgave/drain. Geen automatische vrijgave op tijd,
  herhaling of force-reset. Een crash tussen toelating en verzending kan daarom
  conservatief handmatige beoordeling vereisen, zonder dat er iets is verstuurd.
- `customer_only_routing.change_selected` is de eerste aangesloten adapter:
  precies één PUT, met dezelfde operation-ID in `write_intent` en
  `customer_applied`. De bestaande audit/queue worden eerst opgeslagen, daarna
  wordt de schrijfblokkade vrijgegeven. Bij transportfout, cancellation, fout
  tijdens auditopslag of niet-succesvolle HTTP-response blijft de blokkade staan.
- De headless worker geeft de owner-context mee aan zijn taken. Andere owned
  schrijfpaden zonder operation-adapter worden bewust geweigerd. De bestaande
  legacy-runtime heeft nog geen owner-context en houdt zijn huidige werking.
- De read-only duurzame dashboardstatus bevat per rol `unresolved_writes`.
  Een verlopen heartbeat verbergt dus niet dat er nog beoordeling nodig is.

### Tests en resterende afbakening

Lokaal: 107 tests ontdekt, 101 geslaagd, 6 PostgreSQL-tests overgeslagen; daarnaast
alle 36 bestaande routing/transport/beleid/Allocation-tests geslaagd. Totaal
137 lokaal geslaagd. `git diff --check` geslaagd. Geen echte financiële acties.

Drie nieuwe PostgreSQL-procesproeven controleren: geen overname met een lopende
schrijfpoging ondanks leaseverloop, geen vrijgave door drain/restart, en atomische
uitsluiting tussen schrijftoelating en overname. GitHub CI moet deze zes
databaseproeven nog voor de nieuwe commit uitvoeren; resultaat hieronder vastleggen.

Deze fase sluit **niet** de migratie af. Legacy moet nog dezelfde rolclaims
gebruiken; een duurzaam bedieningspad voor drain/overdracht en operation-adapters
voor de overige schrijvers ontbreken nog. De bestaande gespecialiseerde locks,
cursors, pauzes, historische taakstatus en boekhoudregels blijven ongewijzigd.
Alle execution-gates blijven dicht; geen merge/deployment, nieuwe worker of kosten.
Productie voert de huidige rollen nog in de bestaande webservice uit.

## Vervolg 5 oktober 2026 — eerste routingoverdracht gebouwd

Startpunt PR #82 `f84a87e24fc4d7a1164404b7c908d94913585311`. Actuele main
`b7fd86c3dbe369b630e8f8a0367c22837015b178` (PR #88) is geïntegreerd: bankboekgroepen,
ontbrekende inkoopfacturen en controle van webshoporderstatus blijven behouden.
Productie blijft op `cf75100f845d1296eaaa60f85225caf3c656e4f2`, deployment
`dep-db1ebv6gekts73dec350`; geen nieuwe deployment gestart.

- Web en worker gebruiken nu één routing-supervisor met vaste locaties en unieke
  lease-epochs. Standaard blijft routing bij web; een worker zonder toewijzing wacht.
- Een tweede instance met dezelfde ownernaam mag een levende lease niet vervangen.
- `app.routing_control`: status, to-worker, to-web en pause; alleen duurzame
  coördinatiemetadata. Idempotente opdrachten, geen nieuwe financiële API-route.
- Stop/overdracht wacht op de huidige operatie/audit, houdt de bestaande cycle-lock
  vast en start geen volgende post. Time-out wordt een fout, geen blinde retry.
- Dashboard Verwerking toont nu ook de duurzame uitvoeringslocatie, gewenste
  locatie, heartbeat en onzekere schrijfpogingen.
- `render/routing-worker.yaml` bevat alleen de eerste echte worker, met bestaande
  env-verwijzingen, intern PostgreSQL, één kleine instance en handmatige deploys.
  Exacte bootstrap/overdracht/rollback: `docs/routing-worker-handover.md`.

Lokale lifecycle-tests geslaagd. Nieuwe PostgreSQL-overdrachtstests zijn toegevoegd.
De eerdere f84 GitHub-runs 37368223408 en 37368229710 eindigden zonder uitgevoerde
jobstappen (job cancelled), en gelden dus niet als geslaagde databasevalidatie.
De routing execution-gate blijft dicht tot de nieuwe databasevalidatie slaagt.
Andere rollen blijven eveneens geblokkeerd; HTTP-gestarte Fibonatix en andere
financiële taken zijn niet naar workers verplaatst. Geen merge of live activatie
zonder voldoende validatie en bewezen veilige bootstrap/drain. Geen infra/kosten
of boekhoudmutaties door deze ontwikkelstap.

### Vastlegging en validatie van deze bouwstap

Eerste codecommit: `cc61299f9bf1fc43ba4e5d7def4a306bd7316e42` (merge-parent
`b7fd86c3dbe369b630e8f8a0367c22837015b178` behoudt de actuele dashboardwijzigingen).
Daarna aangescherpt: een fout na schrijftoelating stopt de rol bij de eerstvolgende
grens. Daardoor blijft hij niet verder API-ruimte verbruiken terwijl zijn eerdere
schrijfpoging beoordeling nodig heeft. Geen automatische financiële herhaling.
CLI-status zet de databaseverbinding expliciet op read-only; de beheercommando's
zijn apart getest. De routing-gate heet nu alleen `routing_handover_postgres_pending`:
de implementatievoorwaarden zijn gebouwd, de databaseproeven zijn nog niet bewezen.

Uitgevoerd op 5 oktober 2026:

- `python -m pytest tests -q`: **122 passed, 9 skipped**. De negen skips zijn alle
  lokale PostgreSQL-proeven; ze zijn niet als geslaagd meegeteld. De nieuwe tests
  omvatten SIGTERM, wachten zonder toewijzing, drain-timeout, foutafhandeling,
  CLI-beperking en dashboardstatus bij ontbrekende/verlopen/onzekere gegevens.
- Bestaande routing-, transport-, customer-only-, orderbeleid- en Allocation-suite:
  **36 passed**. Totaal **158 lokaal geslaagd**; geen echte Exact-schrijfacties.
- `render/routing-worker.yaml` gevalideerd met `jsonschema` tegen de officiële
  `https://render.com/schema/render.yaml.json`: **geldig**. Dit bevestigt de
  structuur, niet de aanwezigheid/rechten van env-referenties in de workspace.
- Nieuwe GitHub-runs voor cc61299: `37370140037` en `37370145083` stonden nog queued.
  [GitHub Status](https://www.githubstatus.com/) meldt sinds 5 oktober 19:11 UTC
  een Actions-storing; update 19:50 UTC bevestigt vertraging bij het toewijzen van
  hosted runners. Dit verklaart de wachtende uitvoering, niet een bewezen testfout.
- Render opnieuw gecontroleerd: productie blijft live op cf75100 / dep-db1ebv6gekts73dec350.
  Geen worker aangemaakt, geen merge of deployment uitgevoerd, geen extra kosten.

Volgende stap: controleer de GitHub-run van de actuele branch-head. Vereist zijn
alle negen PostgreSQL-proeven plus de regressies, zonder database-skips. Pas na
succes mag uitsluitend de routing-gate leeg worden gemaakt, met opnieuw de
bijbehorende manifest-/runtime-tests aangepast en uitgevoerd. Andere roles blijven
geblokkeerd. Daarna bootstrap volgens `docs/routing-worker-handover.md`; geen
productieherstart zonder bewezen drain van de nog samengestelde webservice.

Laatste codecommit van deze fase: `925f6b802edd6334382b400289a811b2c1ca4033`
(`test: verify routing control and stop on unresolved writes`). De hierboven
beschreven 158 lokale tests en schema-validatie betreffen deze code. Opvolgende
wijzigingen die alleen dit dossier bijwerken, veranderen de runtime niet.


## Tweede onderdeel, 5 oktober 2026 — Woo-bankregels-worker

Startpunt `324595a97a4947ecfbe3c6ab9058615f031e744f`. Main blijft
`b7fd86c3dbe369b630e8f8a0367c22837015b178`; productie blijft
`cf75100f845d1296eaaa60f85225caf3c656e4f2` / `dep-db1ebv6gekts73dec350`.
Geen parallelle wijzigingen of lopende deployments aangetroffen bij hervatting.

### Gebouwd

- Gedeelde `assigned_role`-supervisor voor routing en Woo. De eerdere routing-
  API/CLI blijft compatibel, inclusief identiteiten, queue, cursor en stopgedrag.
- Legacy-web en headless Woo gebruiken `(3977752, woo-rules)`, met afzonderlijke
  locaties legacy-woo-rules/worker-woo-rules. Default is web, nieuwe worker wacht.
- `app.agent_control --role routing|woo-rules status|to-worker|to-web|pause`.
  Opdrachten wijzigen alleen de geselecteerde taak; status gebruikt read-only SQL.
- Woo controleert de stop bij cycle-/entrygrenzen. Beide rollen drainen parallel;
  bestaande Woo cycle-lock blijft tot het einde van de huidige opdracht behouden.
- `create_confirmed_rule` is een owned-operation-adapter. De gedeelde intent wordt
  pas vrijgegeven na POST, bewezen bestaande eigen regel bij readback, queue-done
  en auditopslag. Een geslaagde POST met mislukte teruglezing wordt niet afgewikkeld.
- `jnp_woo_rule_attempts` bewaart operation-ID, event-ID, status en bevestigde rule-ID.
  Historische operation-ID's blijven bestaan bij IBAN-naar-BOSCI-upgrades. Geen
  tokens, secrets of betalingsbody in deze extra tabel.
- POST-budgetweigering vóór transport blijft pending. Time-out, cancellation,
  readbackfout of auditfout blijft onzeker en stopt nieuwe writes/overname.
- De bestaande HMAC-ontvangstroutes schrijven alleen in dezelfde duurzame queue;
  statusroutes blijven lezend. Er is geen nieuwe openbare financiële uitvoerroute.
- Dashboard toont routing en Woo-bankregels uit duurzame status. Afzonderlijke
  `render/woo-rules-worker.yaml`: één echte worker, handmatige deployments, 300s
  shutdown, intern PostgreSQL en noodzakelijke main OAuth-env-verwijzingen. Geen
  ontvangstsecret aan de worker; bestaande enable-vlag wordt niet geforceerd.
- Runbook: `docs/woo-rules-worker-handover.md`. Begrote extra compute $7/maand;
  niets aangemaakt of live geactiveerd. Geen boekhoudwijzigingen als migratietest.

### Validatie en grenzen

Nieuwe synthetische tests controleren: readback vóór afronding, mislukte audit,
POST-time-out, cancellation, verschil tussen budgetweigering vóór/ná POST,
historisch onzekere jobs niet herhalen, stoppen zonder volgende entry, behouden
cycle-lock, rolisolatie, CLI en Woo-SIGTERM. Twee nieuwe PostgreSQL-proeven controleren
concurrerende gelijke Woo-owners en de volledige duurzame write-/readbackketen,
plus queuebehoud, pause en onafhankelijke routingrol.

De CI-workflow installeert pytest voor de bestaande Woo-tests en voert deze ook uit.
Lokale resultaten en definitieve commit worden hieronder vastgelegd. De elf
PostgreSQL-proeven vereisen nog uitvoering op de actuele code; beide afzonderlijke
execution-gates blijven dicht. Andere rollen behouden hun bestaande blokkades.
Geen merge, deploy, worker-create of onzekere financiële retry in deze stap.


Lokale eindvalidatie tweede onderdeel:

- `pytest tests operations/test_woo_iban_rules.py -q`: **196 passed, 11 skipped**.
- Bestaande routing/transport/customer-only/orderbeleid/Allocation-suite: **36 passed**.
- `pytest operations/test_tax_ledger_routing.py -q`: **19 passed**; uitsluiting van
  Belastingdienst-IBAN's en bestaande tax-/bankregelgrenzen blijven behouden.
- Totaal **251 lokaal geslaagd**. Elf echte PostgreSQL-proeven expliciet niet
  uitgevoerd wegens ontbrekende lokale testdatabase; geen live database gebruikt.
- Zowel routing-worker.yaml als woo-rules-worker.yaml geldig tegen het officiële
  Render JSON Schema. Dit is geen live validatie van env-verwijzingen of netwerk.
- De bestaande Woo HTTP-contracten en HMAC/idempotentietests slagen. De nieuwe
  beheer-CLI biedt uitsluitend routering en Woo aan; andere rollen worden geweigerd.

De GitHub-workflow voert deze suites en de echte PostgreSQL-proeven samen uit met
uitsluitend tijdelijke synthetische data. Controleer vóór samenvoegen de actuele
head, de testuitkomst en de veilige eerste productieoverdracht. De uitrolgrens uit
het eerste onderdeel blijft gelden; er zijn nog geen zelfstandige workers live.


Vastgelegde codecommit tweede onderdeel: `f0c382dbb18db12afd1daa2315a1327e8a4630fd`
(`feat: separate Woo rule worker with durable ownership and audited writes`).
Alle 251 lokale tests en beide Blueprint-schema-validaties hierboven betreffen
precies deze code. Een volgende uitsluitend documentaire checkpointcommit wijzigt
de runtime niet. De head-tests in GitHub moeten nog worden gecontroleerd.


## Derde onderdeel, 5 oktober 2026 — belastingregels-worker

Startpunt `c8e894dc7fa435c669d48470d666e49e4826e490`. Main is ongewijzigd
`b7fd86c3dbe369b630e8f8a0367c22837015b178`; productie blijft live op
`cf75100f845d1296eaaa60f85225caf3c656e4f2`, deployment `dep-db1ebv6gekts73dec350`.
Er zijn geen parallelle branchwijzigingen of lopende deployments aangetroffen.

### Gebouwd en begrensd

- Tax toegevoegd aan de gedeelde toegewezen rollen, headless entrypoint, beheer-CLI
  en duurzame dashboardstatus. Identiteit `(3977752, tax)`, default legacy-tax;
  worker-tax voert pas na expliciete overdracht werk uit.
- Scanner stopt tussen pagina's; onvolledige scans leveren geen nieuwe cursor.
  Bestaande enabled-vlag, cadans, metadata, observaties en historische regels blijven.
- Iedere POST krijgt eigen eigenaarscontrole, audit en bevestiging vóór de volgende
  POST. `jnp_tax_rule_attempts` koppelt operation-ID en de bestaande regelidentiteit.
  Geen tweede poging na onzekere uitkomst. Extra GETs voor teruglezing per regel
  gebruiken het gedeelde quotum; de bestaande 40-POST-grens en reserves blijven.
- Ook de bestaande DELETE van één expliciet bekende oude belastingregel is nu
  meegenomen. Tax en maintenance delen een regellock en respecteren historische
  deleting/uncertain-status. De adapter slikt een onzekere verwijdering niet in.
- /api/tax/status leest opgeslagen scanner-/regelstatus en role-heartbeat.
  /api/tax/report gebruikt opgeslagen grootboekmetadata, met bestaande auth.
  De tax-routes starten geen financiële achtergrondtaken.
- Eén eigen `render/tax-worker.yaml`, native Python, handmatige deploy, kleinste
  passende 0.5c-512mb, Allocation-env-verwijzingen, bestaande private database.
  Kostenraming $7/maand extra; drie voorbereide kleine workers samen $21/maand.
- Runbook: `docs/tax-worker-handover.md`. Geen wijziging aan belastingbeleid,
  debiteurenregels, Microsoft-login, Xcore, DIRECT_WOO_BANK of ordermatchingbeleid.

### Validatie en uitrolgrens

Lokale synthetische proeven testen bevestiging vóór vrijgave, individuele
operation-ID's, mislukte readback/audit, timeout, cancellation, budget vóór/ná POST,
stop tijdens een regel, partial pagination, CLI, SIGTERM en database-status.
Vier nieuwe echte PostgreSQL-proeven testen procesconcurrentie, behoud van
pauze/cursor/historie, auditkoppeling, geblokkeerde overdracht bij onzekerheid,
gedeelde DELETE-lock en behoud van scan-cursor bij onderbreking.

De actuele GitHub-runs van het tweede onderdeel (`37371032416`, `37371026785`)
stonden bij controle nog queued. De nieuwe fase houdt daarom de tax-, routing- en
Woo-uitvoergates dicht. Bestaande groene oudere runs gelden niet als bewijs voor
deze code. Geen merge, deployment, processtop of worker-create uitgevoerd.
Definitieve lokale resultaten en commit volgen hieronder.


Lokale eindvalidatie derde onderdeel:

- `pytest tests operations/test_woo_iban_rules.py operations/test_tax_allocation.py
  operations/test_tax_reference.py operations/test_tax_ledger_routing.py
  operations/test_allocation_maintenance.py -q`: **275 passed, 15 skipped**.
- Bestaande routing-/transport-/customer-only-/orderbeleid-/Allocation-suite:
  **36 passed**. Totaal **311 lokaal geslaagd**; geen echte Exact-aanvraag als test.
- Alle drie afzonderlijke worker-Blueprints gevalideerd tegen het officiële
  Render JSON Schema: geldig. Geen live bewijs van env-referenties/netwerk.
- Vijftien echte PostgreSQL-tests niet lokaal uitgevoerd wegens ontbrekende
  geïsoleerde testdatabase. De vier tax-proeven zijn toegevoegd aan dezelfde
  CI-suite met uitsluitend tijdelijke synthetische data.
- Belastingbeleid, debiteurenroutering en strikt eigen-orderbeleid zijn bytegelijk
  aan het startpunt. De veilige initiële web-overdracht blijft vereist.

Volgende bouwstap: allocation-maintenance als eigen rol onderbrengen, inclusief
alle eigen POST/DELETE-paden, duurzame status en pauzes. De gedeelde DELETE-lock
is hiervoor al aanwezig. Open geen execution-gate en start geen productie-
deployment voordat de actuele databaseproeven en initiële drain bewezen zijn.


Vastgelegde codecommit derde onderdeel: `87576b263e2ca745ad6d0e211776f1576678b1d4`
(`feat: separate tax worker with confirmed writes and durable status`).
De 311 lokale tests en drie Blueprint-schema-validaties hierboven betreffen deze
code. De nieuwe actuele CI-run moet nog worden uitgevoerd/gecontroleerd; de
PostgreSQL- en uitrolgates blijven dicht. Deze checkpointaanvulling wijzigt geen runtime.


## Vierde onderdeel, 5 oktober 2026 — maintenance

Startpunt 508cdc86206fb7d98fd6fe213d8206c86525ffea. Main b7fd86c en live cf75100
zijn opnieuw gecontroleerd en ongewijzigd; de vorige GitHub-runs staan queued.
Maintenance is nu een toegewezen rol met headless supervisor, drain, per-write
controle en `jnp_maintenance_rule_attempts`. Alle bestaande POSTs en cleanup-
DELETEs worden bevestigd en geaudit vóór vrijgave. De werkvoorraad en statussen
blijven duurzaam; onvolledige scans vervangen geen volledig dashboardoverzicht.
De functionaliteit, bewijsregels en functionele pauzes blijven behouden. Een oude
instructie in nieuwe regelvoorstellen verwees naar de inmiddels verboden
Automatically-route; die verwijst nu naar beoordeling van de eigen order/factuur.

Lokale gecombineerde pytest-suite: 304 passed, 15 skipped vóór toevoeging van
één extra PostgreSQL-proef voor maintenance. Die extra proef verifieert de echte
operation-/write-auditkoppeling, overnameblokkade en onafhankelijkheid van tax.
De actuele databasevalidatie staat nog open; alle uitvoergates blijven dicht.
Nieuwe handleiding en Blueprint: docs/maintenance-worker-handover.md en
render/maintenance-worker.yaml. Begrote extra compute $7/maand, nog geen kosten.
Geen merge, deployment, infrastructuuraanmaak of financiële uitvoering in deze fase.
