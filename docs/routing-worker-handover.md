# Eerste zelfstandige worker: debiteurenroutering

Status: code gereed voor databasevalidatie; nog niet live overgedragen.
Gebruik uitsluitend `render/routing-worker.yaml` voor deze eerste stap. De oude
root-Blueprint en de zes-workers-Blueprint mogen hiervoor niet worden toegepast.

## Wat verandert

De web-lifespan en `python -m app.worker --role routing` gebruiken dezelfde
PostgreSQL-taakidentiteit `(3977752, routing)` en dezelfde bestaande routequeue,
control/cursor, audits, pauzes en financiële regels. `legacy-routing` en
`worker-routing` zijn locaties, geen afzonderlijke taken. Elke claim krijgt een
unieke lease-ID; ook een tweede instance met dezelfde locatienaam kan een levende
lease niet vervangen. Zonder toewijzing voert de worker geen routering uit.

De standaardtoewijzing is de webservice. Overdragen wijzigt uitsluitend
coördinatiemetadata. De huidige eigenaar krijgt drain, voltooit een reeds
toegelaten schrijfpoging met haar audit en stopt vóór de volgende boeking. Een
onzekere schrijfpoging blijft duurzaam geblokkeerd, ook na crash/restart/verlopen
lease. Er is geen force-retry of automatische verwijdering van dit bewijs.

De worker bedient geen HTTP-poort. Dashboard/login en alle andere financiële
agents blijven in de bestaande webservice. Regels: wc_fibonatics blijft 100100;
plisio 109377; bacs 109372; ic/icepay 109419; np_payments 109421;
suap_wordpresspayplugin 109422. Xcore en DIRECT_WOO_BANK blijven ongewijzigd.

## Eenmalige bootstrap — nog niet uitgevoerd

1. Laat de database-integratietests voor de exacte codecommit slagen. Controleer
   de actuele main, PR-status en Render-deployments; behoud parallelle wijzigingen.
2. De huidige productieversie kent deze rolclaims nog niet. Eerst alle bestaande
   financiële taken gecontroleerd laten stoppen/drainen en vaststellen dat geen
   schrijfactie of HTTP-gestarte financiële taak loopt. Een stille log alleen is
   geen bewijs. Deze migratie levert geen globale drain voor alle overige agents.
   Zonder dat bewijs geen web-deployment of processtop uitvoeren.
3. Merge de geteste versie. Start één handmatige web-deployment van die commit,
   pas nadat stap 2 bewezen is. Controleer live commit en gewenste shutdown-window
   (300 seconden). Auto-deploy blijft uit. Nieuwe webcode claimt standaard de
   routingrol; controleer duurzame status voordat een worker wordt toegewezen.
4. Maak via Render Dashboard een Blueprint vanuit JNPDrops/jnp-matching, branch
   main, pad `render/routing-worker.yaml`. Selecteer bestaande workspace en dezelfde
   netwerk-/projectomgeving als jnp-matching. Plan: 0.5 CPU / 512 MB, één instance,
   Frankfurt, native Python, geen autoscaling, handmatige deployments. Begroot
   $7/maand extra compute ([Render-prijstabel](https://render.com/pricing) gecontroleerd 5 oktober 2026;
   bevestig het actuele bedrag op het scherm vóór aanmaken).
   `fromService/envVarKey` hergebruikt de bestaande interne database en verbinding;
   kopieer/toon geen secrets. Bij onopgeloste referenties stoppen, geen nieuwe DB,
   Exact-app of debiteur aanmaken. Metorik en Allocation zijn nodig voor routering.
5. Eerste start wacht: gewenste eigenaar is nog web. Het starten van de worker
   zelf voert dus geen tweede routequeue uit. Controleer de exacte workercommit.

De plugin kan geen echte background worker of Blueprint aanmaken/toepassen en
heeft geen shell. Deze handelingen vereisen daarom een Render Dashboard/CLI-stap
met bestaande toegang. Er is geen publiek webproces als vervanging aangemaakt.

## Bediening na de bootstrap

Voer onderstaande commando's uit in de bestaande Render-omgeving met dezelfde
interne database, bijvoorbeeld via de Render Shell. Niet op een laptop zonder
deze verbinding; deel geen verbindingsstring. De commando's wijzigen geen boekingen.

```sh
python -m app.routing_control status
python -m app.routing_control to-worker
python -m app.routing_control status
```

Wacht tot `execution_location=worker`, `desired_location=worker`, `lease_live=true`,
`draining=false`, `unresolved_writes=0`. Tijdens drain kan dit maximaal een
heartbeat-interval (30s) plus de lopende operatie duren. De werktaak krijgt maximaal
240s om af te ronden; daarna stopt zij met een fout en blijven eventuele onzekere
schrijfpogingen geblokkeerd. Herhaal geen financiële schrijfpoging als herstel.
Een verlopen lease tijdens aangevraagde drain is geen bewijs van veilige vrijgave;
die situatie vereist technische beoordeling en wordt niet automatisch geforceerd.

Terug naar web of alleen routering pauzeren:

```sh
python -m app.routing_control to-web
python -m app.routing_control pause
```

Een herhaalde identieke opdracht is idempotent. Pauze/toewijzing overleeft een
restart. Gebruik voor rollback dezelfde gecoördineerde code in de webservice;
een oude image zonder eigenaarschap zou de beveiliging omzeilen en mag niet worden
teruggezet terwijl de worker kan schrijven. De bestaande operatorpauze in de
routecontrol blijft aanvullend van kracht en wordt niet door deze opdrachten gewist.

Het dashboard toont op **Verwerking** de duurzame rolstatus, eigenaar, gewenste
locatie, heartbeat en open schrijfpogingen. Ontbrekende of verlopen status wordt
niet als gezonde actieve worker gepresenteerd.
