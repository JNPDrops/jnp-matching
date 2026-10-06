# Derde zelfstandige worker: belastingregels

Gebouwd in concept-PR #82, niet live. De uitvoerblokkade blijft
`tax_handover_postgres_pending` tot de actuele PostgreSQL-proeven geslaagd zijn.
Een oudere groene run of lokaal overgeslagen databaseproef is onvoldoende.

## Bestaande scope

De worker scant bankregels alleen-lezen en onderhoudt de bestaande belasting-
toewijzingsregels volgens ongewijzigde `operations/tax_policy.json`. Herkenning,
RSIN-controle, perioden, grootboekcodes en verplichte Belastingdienstrelatie blijven
ongewijzigd. Geen nieuwe boekingen, afletteringen, herimports of relatieaanmaak.
Er wordt geen Automatically-, /cycle- of /research-route aangeroepen.

De schrijfacties zijn POST AllocationRule en DELETE van uitsluitend de reeds
vastgelegde oude IBAN-regel via `legacy_creditor_fallback`, nadat alle vervangende
regels bevestigd zijn. Een gelijke omschrijving of IBAN alleen is onvoldoende:
ook de bekende regel-ID, relatie en lege overige dimensies moeten overeenkomen.
De agent gebruikt dezelfde Allocation OAuth-verbinding en gedeelde refresh-locks.

## Eigenaarschap, stoppen en onzekerheid

Taakidentiteit `(3977752, tax)`, locaties `legacy-tax` en `worker-tax`. Standaard
blijft de webservice eigenaar. De headless worker wacht zonder Exact-aanvragen
tot overdracht. De bestaande tax cycle-lock blijft behouden.

De scanner stopt aan paginagrenzen zonder een gedeeltelijk resultaat als complete
scan op te slaan. De cursor en observaties worden samen opgeslagen na een complete
scan. Een afgeronde scan kan zijn cursor bewaren terwijl het daaropvolgende
regels-onderhoud door een stop wordt overgeslagen. Cadans, enabled-vlag, metadata,
oude regels en onzekerheden blijven in dezelfde tabellen staan.

Bij iedere aanmaak wordt één operation-ID gebruikt. Een volgende POST wacht op
teruglezing, de bevestigde toestand in `jnp_tax_rules` en de gekoppelde audit in
`jnp_tax_rule_attempts`. Dit vervangt teruglezing na een hele batch door teruglezing
per regel; het vraagt extra GETs, die onder hetzelfde gedeelde quotum en de
bestaande reserves vallen. Een budgetweigering vóór POST blijft pending; na POST
is een mislukte teruglezing onzeker. Timeout, cancellation en auditfouten geven
geen automatische tweede schrijfpoging of vrije overname.

Tax en allocation-maintenance delen daarnaast een PostgreSQL-lock per te
verwijderen regel. De bestaande cleanup-audit wordt vóór een DELETE beoordeeld:
deleting/uncertain/deleted wordt bij nog aanwezige regels nooit opnieuw verstuurd,
ook niet na een menselijke wijziging. Een andere eigenaar van de regellock leidt
tot overslaan, niet wachten in de eventloop. Een bevestigde verwijdering wordt
pas vrijgegeven na opgeslagen afwezigheid en de gekoppelde tax-audit.

Routing, Woo en tax drainen parallel met ieder 240 seconden; de web-lifespan
wacht maximaal 270 seconden. Render krijgt 300 seconden shutdown. Een vastgelopen
of onzekere taak moet worden beoordeeld; de CLI biedt geen force-reset.

## HTTP en dashboard

| Route | Uitvoering |
| --- | --- |
| GET /api/tax/status | Opgeslagen scanner-/regelstatus plus duurzame tax-owner, geen Exact-aanvraag |
| GET /api/tax/report | Bestaande operatorauthenticatie; observaties en grootboekmetadata uit PostgreSQL |

Geen tax-HTTP-route start financiële taken. Het rapport gebruikt geen proceslokale
grootboekcache meer. De dashboardpagina toont Belastingregels met dezelfde
heartbeat-, overdracht- en onzekerheidsweergave als de eerste twee workers.

## Configuratie en activering

Gebruik alleen `render/tax-worker.yaml`, niet de oude root-Blueprint of de hele
voorbereide workerlijst. Eén echte background worker `jnp-tax-worker`, native
Python, Frankfurt, `0.5c-512mb`, geen autoscaling, handmatige deployments.
Startcommando: `python -m app.worker --role tax`. De benodigde Allocation-env-vars
en interne DATABASE_URL worden via fromService/envVarKey verwezen; geen nieuwe
database, publieke databaseverbinding of API-registratie.

Begroot **$7 per maand** extra compute volgens [Render pricing](https://render.com/pricing),
op 5 oktober 2026 bevestigd via de officiële prijstabel en
[Render compute plans](https://render.com/docs/compute-plans).
Met routing en Woo samen is dat $21 per maand extra compute, bovenop bestaande
services en eventueel verbruik. Er zijn in deze fase geen resources aangemaakt.

Na geslaagde actuele databaseproeven kan uitsluitend de tax-gate worden verwijderd
met opnieuw uitgevoerde manifesttests. Vervolgens geldt de veilige eerste
web-bootstrap uit `docs/routing-worker-handover.md`: nog gecombineerde financiële
processen moeten aantoonbaar gedraind zijn vóór een deployment. Een workerstart
of lege log is geen bewijs dat dit al gebeurd is.

De Render-plugin biedt geen worker-create, Blueprint-apply of shell. Activering
vereist daarom een passende toegestane beheeractie met bovengenoemd bestand.
Voer de volgende opdrachten uitsluitend uit binnen de bestaande private
Render-omgeving, nadat gecoördineerde webcode actief is:

```sh
python -m app.agent_control --role tax status
python -m app.agent_control --role tax to-worker
python -m app.agent_control --role tax status
```

Verwacht desired_location=worker, execution_location=worker, lease_live=true,
draining=false en unresolved_writes=0. Controleer dat routing en Woo hun eigen
toewijzingen behouden. Geen claims voor andere rollen stoppen om tax over te dragen.
Pauzeren of teruggeven aan de gecoördineerde webservice:

```sh
python -m app.agent_control --role tax pause
python -m app.agent_control --role tax to-web
```

Geen terugrol naar ongecoördineerde oude webcode zolang een worker kan schrijven.
De functionele tax enabled-vlag blijft onafhankelijk van de uitvoerlocatie.
