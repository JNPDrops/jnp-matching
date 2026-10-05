# Vijfde worker: Fibonatix

Gebouwd voor de bestaande vaste import-/matchingdossiers, niet live. Nieuwe
perioden of bronnen worden niet verzonnen of opnieuw geïmporteerd. De adapter
is bruikbaar voor de bestaande commandotypes; de huidige historische autorisatie
is verlopen en wordt door deze migratie niet verlengd. Een generieke nieuwe
Paragon-transactie-exportadapter valt nog buiten de bestaande implementatie.

HTTP-startknoppen schrijven voortaan naar `jnp_agent_jobs`. Alleen de toegewezen
`fibonatix`-owner voert uit. Idempotency-Key voorkomt dubbele indiening; één
actieve opdracht per rol voorkomt overlappende opdrachten voor hetzelfde dossier.
Een verlopen opdracht wordt niet uitgevoerd. Verlaten running-opdrachten worden
uncertain, nooit automatisch opnieuw geclaimd. De UI toont duurzame taakstatus.

De bestaande batchclaim blijft gelden bij XML-import. Eén owned-operation loopt
van de POST tot de opgeslagen volledige teruglezing. UI-opslag voor strict
matching is per ontvangst afgeschermd; de gedeelde intent gaat vóór de klik,
bevestiging volgt pas na bestaande eigen-order-/factuurcontroles en dossieropslag.
Een klik alleen bewijst geen aflettering. De oorspronkelijke audits bevatten nu
ook operation-ID en queue-job-ID. Oude Automatically/settle-routes blijven verboden.

Voor overdracht: actuele PostgreSQL-suite zonder skips, veilige eerste bootstrap,
dan `python -m app.agent_control --role fibonatix to-worker`. Default blijft web.
Gebruik `status`, `pause` en `to-web` voor dezelfde rol; geen force-reset van
onzekere jobs/writes. De vaste bronidentiteit, vervaldatum en limiet van maximaal
vijf strict-items per opdracht blijven ongewijzigd.

`render/fibonatix-worker.yaml`: echte worker, native Python plus Chromium,
Frankfurt, 1c-2g, één instance, handmatige deploys, 300s shutdown. Bestaande interne
PostgreSQL, main OAuth en Exact-browsercredentials via env-verwijzingen; geen
secretwaarden in Git of HTTP-opdrachten. Begroot $25/maand extra compute volgens
de Render-prijslijst gecontroleerd op 5 oktober 2026. Niet aanmaken zolang geen
geautoriseerde uitvoerbare opdrachten en bewezen overdracht beschikbaar zijn.
De Render-plugin heeft geen worker-create, Blueprint-apply of shell.
