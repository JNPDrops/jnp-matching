# Tweede zelfstandige worker: Woo-bankregels

Gebouwd in concept-PR #82; nog niet live. De uitvoerblokkade is
`woo_rules_handover_postgres_pending`. Eerst de databaseproeven op de actuele
commit laten slagen; een bestaande groene run van een oudere codefase is onvoldoende.

## Afbakening

Deze worker verwerkt uitsluitend de bestaande, geauthenticeerde Woo-betalingsbewijzen
uit `jnp_woo_iban_events`. Hij maakt binnen de reeds geautoriseerde scope IBAN- of
BOSCI-herkenningsregels voor de bestaande debiteur 109372 in administratie 3977752.
Geen factuurverplaatsing, bankimport, aflettering, verwijdering of rekeningaanmaak.
De bewijs-, datum-, rekening-, conflict- en Belastingdienstcontroles blijven gelden.

De HTTP-routes blijven in de webservice:

| Route | Werking na opsplitsing |
| --- | --- |
| POST /api/woocommerce/iban-rule | HMAC controleren, bewijs valideren, idempotent in dezelfde queue opslaan |
| POST /api/woocommerce/allocation-rule | Hetzelfde, met bewezen BOSCI-referentie |
| POST /api/woocommerce/iban-rule/check | Geauthenticeerde leescontrole; geen aanmaak |
| POST /api/woocommerce/allocation-rule/status | Geauthenticeerde leescontrole en bestaande queuestatus |

Geen nieuwe openbare uitvoerroute. De webshopplugin hoeft voor deze migratie niet
te veranderen. De ontvangst-secret blijft alleen nodig in de webservice en wordt
niet aan de worker doorgegeven. Main Exact OAuth blijft bij dezelfde registratie;
refresh/callback en quota zijn gedeeld over processen.

## Gedeeld taakbezit en afronden

`operations/assigned_role.py` bedient nu routing en woo-rules; de eerdere
routing-CLI en taakidentiteiten blijven bruikbaar. Woo gebruikt de identiteit
`(3977752, woo-rules)` met uitvoerlocaties `legacy-woo-rules` en `worker-woo-rules`.
Standaard is alleen de bestaande webservice toegewezen. Een nieuwe worker wacht
zonder Exact-aanvragen tot hij expliciet eigenaar mag worden.

Een overdracht/pauze voor Woo wijzigt de routingrol niet. De bestaande Woo cycle-lock
blijft aangehouden tot de lopende opdracht is afgerond. Stoppen wordt vóór elke
volgende queue-entry gecontroleerd. Bij processtop drainen routing en Woo parallel,
ieder met maximaal 240 seconden; de buitenste lifespan wacht maximaal 270 seconden.
De Render shutdown-window moet 300 seconden zijn. De overige rollen hebben nog
geen algemene veilige drain en blijven een voorwaarde bij de eerste web-deployment.

Aanmaak is pas afgerond na Exact-teruglezing, de bestaande queue-checkpoint én
opslag van `jnp_woo_rule_attempts`. Die tabel koppelt elke operation-ID duurzaam
met de oorspronkelijke event-ID; eerdere operation-ID's blijven ook na een
IBAN-naar-BOSCI-upgrade bestaan. Ze bevat geen tokens of kopie van betalingsgegevens.

Een time-out, cancellation, mislukte teruglezing of auditfout geeft geen automatische
herhaling. De gedeelde onopgeloste schrijfpoging verhindert nieuwe schrijftoelating
en overname. De rol stopt met verder verwerken; beoordelen blijft nodig.
Een expliciete budgetweigering vóór de POST houdt de entry daarentegen pending:
er is dan bewezen nog geen aanmaak verstuurd. Een budgetweigering tijdens
teruglezing ná een POST is wel een onzekere uitkomst.

## Configuratie en activering

Gebruik uitsluitend `render/woo-rules-worker.yaml` voor dit onderdeel:

- Echte background worker `jnp-woo-rules-worker`, native Python, Frankfurt.
- Start: `python -m app.worker --role woo-rules`.
- Eén instance, `0.5c-512mb`, geen autoscaling, handmatige deployments.
- Bestaande interne PostgreSQL en main Exact-configuratie via fromService/envVarKey.
- `ENABLE_WOO_IBAN_RULE_WRITES` wordt overgenomen, niet naar true geforceerd.
- Geen nieuwe Exact-app, database, dagboek of verzameldebiteur.
- Begroot **$7/maand** extra compute volgens [Render](https://render.com/pricing),
  gecontroleerd 5 oktober 2026. Twee kleine workers samen: $14/maand extra compute;
  bestaande services blijven apart meetellen. Er zijn nog geen nieuwe kosten gemaakt.

Na geslaagde actuele tests: veilige eerste web-bootstrap volgens
`docs/routing-worker-handover.md`, met bewezen drain van alle nog gecombineerde
financiële processen. Maak daarna deze worker aan via de Render Dashboard/Blueprint-
flow met het exacte bestand. De plugin ondersteunt geen worker-create, Blueprint-
apply of shell. Standaard blijft werk bij web, ook wanneer de worker gestart is.

Voer vervolgens in de bestaande Render-omgeving uit:

```sh
python -m app.agent_control --role woo-rules status
python -m app.agent_control --role woo-rules to-worker
python -m app.agent_control --role woo-rules status
```

Verwacht uiteindelijk execution_location=worker, desired_location=worker,
lease_live=true, draining=false, unresolved_writes=0. Ontbrekende status of een
verlopen heartbeat is geen bewijs van succesvolle overdracht. Controleer bij een
actieve routingworker tevens dat diens eigenaar ongewijzigd is gebleven.

Alleen Woo pauzeren of teruggeven aan de gecoördineerde webservice:

```sh
python -m app.agent_control --role woo-rules pause
python -m app.agent_control --role woo-rules to-web
```

Herhaalde gelijke opdrachten zijn idempotent. Niet terugrollen naar oude webcode
zonder gedeeld eigenaarschap terwijl de worker kan schrijven. Geen force-reset
van onzekere attempts; eerst beoordelen tegen het oorspronkelijke bewijs en Exact.
Het dashboard toont beide rollen uit duurzame opslag, met leesbare rolnamen.
