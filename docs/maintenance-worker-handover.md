# Vierde worker: bankuitzonderingen en onderhoud

De rol `maintenance` gebruikt `legacy-maintenance` / `worker-maintenance` en dezelfde
administratie-gebonden claim, refresh-locks, quota en overdrachtsprocedure als tax.
De standaardtoewijzing blijft web. De uitvoergate is `maintenance_handover_postgres_pending`.

POSTs voor bestaande toewijzingsvoorstellen en de drie bestaande cleanup-paden
(duplicaten, oude BOSCI-regels en oude toegewezen bankregels) hebben nu een eigen
operation-ID, gedeelde schrijfcontrole en audit. Verwijdering bevestigt tevens de
queue-/archiefcheckpoint vóór vrijgave. Onzekere writes worden niet herhaald.
De bestaande Woo- en tax-cycle-locks blijven behouden naast de regellock.

Een stop beëindigt de scan op een leesgrens of na de huidige bevestigde schrijf-
operatie. De laatste volledige werklijst blijft staan bij een onderbroken scan.
Status en beveiligd rapport lezen PostgreSQL, niet de proceslokale cache.
De HTTP-routes zijn alleen-lezen; er is geen nieuwe financiële uitvoerroute.

Gebruik uitsluitend `render/maintenance-worker.yaml`: native Python, Frankfurt,
0.5c-512mb, één instance, handmatige deploy, 300s shutdown. Verwijzingen naar de
bestaande interne database, Allocation-OAuth en Metorik; geen nieuwe database.
Begroot $7/maand extra compute (Render-prijs gecontroleerd 5 oktober 2026).

Na actuele databasevalidatie en veilige eerste web-bootstrap:

```sh
python -m app.agent_control --role maintenance status
python -m app.agent_control --role maintenance to-worker
python -m app.agent_control --role maintenance status
```

Verwacht gewenste/actieve locatie worker, geldige heartbeat, geen draining of
onopgeloste write. Gebruik `pause` of `to-web` voor dezelfde rol om te stoppen of
terug te geven. Bestaande functionele enabled-vlag blijft gelden. Geen force-reset.
De Render-plugin kan deze worker niet aanmaken of shellopdrachten uitvoeren;
volg daarvoor dezelfde toegestane activeringsstap als in de eerdere runbooks.
Deze fase is gebouwd, niet uitgerold.
