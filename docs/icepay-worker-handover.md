# Zesde worker: ICEPAY

Gebouwd, niet live. De drie oude lifespan-starts zijn één toegewezen ICEPAY-rol
met duurzame opdrachten geworden. De catalogus bevat uitsluitend bestaande
taak-ID's voor inspectie, extractie, voorbereiding, import, reconciliatie en
matching. De adapter geeft de identiteit expliciet door; omgevingsvariabelen
worden niet veranderd om een andere taak te starten. Bestaande activeringsvlaggen
worden eenmalig als idempotente opdracht vastgelegd. Verlopen historische
opdrachten worden niet opnieuw gestart. Aanmaken van een bankdagboek is bewust
geen catalogusactie; de bestaande code daarvoor wordt niet door deze worker gestart.

XML-import blijft één poging per bestaande batchclaim. De gedeelde schrijfintentie
blijft open bij een onzekere uitkomst. UI-matching heeft per ontvangst een eigen
write fence, bewaarde poging en volledige teruglezing voordat de intent wordt
afgerond. Alleen de opgeslagen status van de specifieke oorspronkelijke taak
kan een queue-opdracht voltooien. Bestaande regels voor eigen order, credits en
uitzonderingen blijven gelden. Een geslaagde taak kan dus nog menselijke
uitzonderingen bevatten; completed betekent niet dat alle posten zijn afgeletterd.

De catalogus behandelt de al geïmplementeerde vaste perioden. Deze migratie maakt
geen nieuwe PSP-export, periode, journal, import of matchgroep. Voor nieuwe
perioden zijn aparte bron-/scopevalidatie en nieuwe taakidentiteiten nodig.

Activering: eerst volledige PostgreSQL-tests en bewezen eerste bootstrap/drain;
dan gate openen in een geteste commit en `python -m app.agent_control --role
icepay to-worker`. De queue, oude claims, audit en status blijven in dezelfde
interne PostgreSQL. `status`, `pause` en `to-web` gebruiken dezelfde rol.

`render/icepay-worker.yaml`: echte background worker, native Python en Chromium,
Frankfurt, één 1c-2g-instance ($25/maand extra compute, Render-prijslijst gecontroleerd
5 oktober 2026), handmatige deploys, 300 seconden shutdown. Alleen env-verwijzingen;
de optionele TOTP-/activeringsvelden alleen overnemen als die sleutels op de bron
bestaan. Geen credentials opvragen of tonen. Niet provisioneren zonder uitvoerbaar
werk. Plugin heeft geen create-worker, Blueprint-apply of shell; nog geen worker live.
