# Overdracht van de zeven achtergrondrollen

Status 5 oktober 2026: alle rollen zijn gebouwd en lokaal getest in PR #82; geen
nieuwe worker is aangemaakt of live. Dashboard en Microsoft-login blijven op de
bestaande webservice. De oorspronkelijke webservice bezit standaard alle rollen;
de database bewaart per administratie welke eigenaar gewenst en actief is.

## Voorbereide deployment-eenheden

| Volgorde | Rol | Blueprint | Instance | Extra compute bij continu draaien |
| --- | --- | --- | --- | --- |
| 1 | routing | render/routing-worker.yaml | 0.5c-512mb | $7/maand |
| 2 | woo-rules | render/woo-rules-worker.yaml | 0.5c-512mb | $7/maand |
| 3 | tax | render/tax-worker.yaml | 0.5c-512mb | $7/maand |
| 4 | maintenance | render/maintenance-worker.yaml | 0.5c-512mb | $7/maand |
| 5 | fibonatix | render/fibonatix-worker.yaml | 1c-2g | $25/maand |
| 6 | icepay | render/icepay-worker.yaml | 1c-2g | $25/maand |
| 7 | reports | render/reports-worker.yaml | 1c-2g | $25/maand |

Prijzen gecontroleerd op 5 oktober 2026 via https://render.com/pricing en
https://render.com/articles/production-rails-hosting-guide. Samen maximaal
$103/maand extra compute als alle zeven continu draaien, exclusief huidige web/DB
kosten en eventuele belasting. Activeer geen lege PSP-/reportworker: hun huidige
catalogi bevatten historische, verlopen opdrachten. Een nieuwe uitvoerbare periode
vereist eigen bronbewijs en taakidentiteit; geen oude batch of expiry overschrijven.
`render/agent-workers.yaml` is een referentiecatalogus, geen bulk-activatieadvies.

## Gates vóór de eerste deployment

1. Verifieer actuele main, branch/PR en beide Render-deployments opnieuw. Behoud
   parallelle commits. Laat de geïsoleerde PostgreSQL-suite op de exacte head
   volledig slagen. De lokale tests slaan deze databaseproeven expliciet over.
2. Bewijs een veilige eerste stop/drain van de huidige productiecode, inclusief
   HTTP-starts. De huidige live versie heeft nog geen nieuwe roleclaims. Een
   stille log, oud eindtijdstip, verlopen toestemming of lege in-memory status
   is geen bewijs van een beëindigde financiële operatie. Zie de inventaris in
   `docs/agent-http-entrypoints.md`, inclusief beide legacy handmatige knoppen.
3. Open alleen de bewezen runtime-gates in een aparte geteste wijziging. Merge
   pas na validatie. Start daarna exact één handmatige deployment van de gekozen
   actuele commit; niet tegelijk met een al lopende deployment.
4. Controleer nieuwe webstatus: zeven gewenste legacy-owners, geldige heartbeats,
   bestaande pauzes intact, geen onverwachte unresolved writes/jobs. De normale
   bestaande routerings- en boekhoudregels blijven dezelfde.

## Eén rol tegelijk verplaatsen

1. Maak de echte background worker vanuit zijn individuele Blueprint op branch
   main, in dezelfde Render-workspace/netwerkomgeving als jnp-matching. Native
   Python, Frankfurt, één instance, autoDeploy uit, shutdown 300 seconden.
   Hergebruik de bestaande interne database via fromService/envVarKey. Verwijder
   verwijzingen naar niet-bestaande optionele configuraties; lees/toon geen
   secretwaarden. Geen nieuwe database, publieke DB-poort of extra Exact-app.
2. Workerstart voert nog niets uit zolang desired_owner legacy is. Controleer
   de commit en wachtstatus. Voer vervolgens binnen de bestaande interne omgeving
   `python -m app.agent_control --role ROLE to-worker` uit (vervang ROLE).
3. Controleer met hetzelfde commando en actie `status`: gewenste/actieve locatie
   worker, heartbeat geldig, draining false, geen unresolved writes of uncertain
   jobs. De oude owner moet eerst zijn toegelaten operatie en audit afronden.
   Begin pas daarna aan de volgende rol.
4. Pauze: actie `pause`; terug: `to-web`. Een onzekere schrijfuitkomst wordt nooit
   automatisch gewist of herhaald. Gebruik bij rollback dezelfde gecoördineerde
   code; rolbezit terugzetten naar oude code zonder fencing is niet veilig.

De beschikbare Render-plugin kan deploymentstatus lezen en handmatig deployen,
maar geen echte worker aanmaken, Blueprint toepassen of shellcommando uitvoeren.
Die activeringshandelingen vragen de Render Dashboard/CLI-capability met bestaande
toegang. De dev-webservice is geen shell. Browserfallback is hier niet geautoriseerd.
Daarom zijn deze stappen voorbereid, niet uitgevoerd of als live aangemerkt.

## Aanvulling 6 oktober: toegankelijke Shell en eerste lockcontrole

De gebruiker heeft browserfallback en Render Web Shell inmiddels toegestaan.
In de hervatte migratiesessie is de bestaande webservice opnieuw aangemeld en
intern alleen-lezen gecontroleerd. Main is b7fd86c3; productie blijft cf75100f.
Beide handmatige HTTP-schrijfknoppen zijn false. De oorspronkelijke 439 CI-tests
zijn geslaagd; de latere vier PR82-commits wijzigen alleen documentatie.

`operations/legacy_cutover.py` is een afzonderlijke eerste-overdrachtscontrole
voor uitsluitend de gepinde bestaande productieversie. `check` verkrijgt alle
bestaande locks of stopt, controleert uitsluitend taakmetadata en geeft de locks
weer vrij. Een geslaagde check is nadrukkelijk geen voltooide drain en opent geen
runtimegate. `hold --seconds 600` kan dezelfde locks tijdelijk vasthouden. Het
commando verandert geen boekingen, wachtrijen, taakclaims, tokens of instellingen.
Zeven synthetische tests bewijzen onder meer weigering bij bezette locks,
onbekende taakstatus of actieve schrijfknoppen en vrijgave van gedeeltelijke locks.

Een hold in de Shell van de oude webservice leeft niet onafhankelijk van die
service. Gebruik een weggevallen Shell of afgelopen hold daarom nooit als
overdrachtsbewijs. Er moet een geverifieerde stop van de oude instantie zijn,
terwijl financiële uitvoering geblokkeerd blijft, vóór nieuwe uitvoerders starten.
Behoud de bestaande handmatige-deploy- en één-eigenaarvoorwaarden.

## Eerste productiestop 6 oktober 2026

De gebruiker gaf expliciet toestemming voor tijdelijk stilzetten/hervatten en
alle zeven afzonderlijke workers (samen $103/maand extra compute). Op 11:55 UTC
verkreeg de gepinde guard 51d6751 alle vijf legacy locks; beide handmatige
HTTP-schrijfknoppen waren false en alle gecontroleerde taakmetadata was terminaal.
De guard behield locks bij SIGTERM/SIGHUP. Render bevestigde daarna
`suspended: suspended`, `suspenders: [user]` voor srv-daun5mt9fdbs739jij7g
(updatedAt 2026-10-06T11:55:37.358851Z); de oude shell werd onbeschikbaar.

Daarmee kunnen de eenmalige startup-gates worden geopend. De bestaande
PostgreSQL-tests bewijzen ownership, drain en write-fencing; de volledige suite
moet ook op deze activeringscommit groen zijn vóór merge/deployment. Geen rol
wordt door het openen van een gate overgedragen: desired_owner blijft standaard
legacy tot de expliciete interne handover. Oude PSP-opdrachten worden niet
heropend. De zeven services mogen volgens deze specifieke gebruikersopdracht
worden ingericht, ook wanneer een historische catalogus momenteel niets uitvoert.
