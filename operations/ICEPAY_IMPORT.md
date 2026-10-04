# ICEPAY 1–3 oktober 2026: vaste ontvangstenimport

De browserbot heeft de complete PaymentTime-selectie met 38 IDs opgehaald.
Alle 38 OrderTime-waarden bevestigen CSV UTC tegenover Europe/Amsterdam in het
portaal. Van deze selectie zijn 36 betalingen geslaagd (€ 3.161,24), twee mislukt.
Er zijn geen refunds in de geselecteerde periode; alle geslaagde betalingen hebben
een ondubbelzinnige eigen Order #-referentie.

De vaste, privé opgeslagen XML is voorbereid door
`icepay-booking-20261001-03-prepare-v2` en heeft SHA-256
`69b2e05e690a3bc4663c9bb3850fb43a98a25fe12909efaa4dbfc1e9fa2deeef`.
De structuur komt uit de reeds succesvol verwerkte Fibonatix-bankimport; alle
boekingsinhoud is vervangen en de bron wordt vóór upload opnieuw gecontroleerd.

| Datum | Ontvangsten | Bedrag | Boeking |
|---|---:|---:|---:|
| 2026-10-01 | 11 | € 1.045,77 | 26270001 |
| 2026-10-02 | 15 | € 1.310,31 | 26270002 |
| 2026-10-03 | 10 | € 805,16 | 26270003 |

Administratie 3977752, bankboek 27 ICEPAY EUR, grootboek 1317, EUR.
Tegenboekingen gaan naar grootboek 1100 / verzameldebiteur 109419.
Iedere regel bevat het oorspronkelijke PaymentID en YourRef TD + de eigen order.
Deze taak bevat geen aflettering, kosten, oudere uitbetaling of openingssaldo.
Het septemberoverzicht en de oudere transfer worden als afzonderlijk bronbewijs
bewaard; alleen de Last Update-datum geeft onvoldoende grond voor een boekingsdatum.

## Eenmalige uitvoering

De gebruiker heeft het ophalen en inlezen in dit bankboek opgedragen.
Activeer alleen `ICEPAY_TRANSACTION_TASK_ID=icepay-booking-20261001-03-import-v1`
op de bestaande Render-service via env-merge. Deze wijziging triggert zelf één
deployment; auto-deploy blijft uit. De taak vervalt op 5 oktober 18:00 UTC.

De taak herhaalt de Exact-configuratie-/duplicaatcontrole en claimt de vaste batch
duurzaam voordat precies één XMLUpload-POST mag worden verstuurd. Bij een onzekere
respons wordt uitsluitend teruggelezen; een financiële write wordt nooit herhaald.
De opgeslagen XML en manifest moeten byte voor byte overeenkomen met de opnieuw
uit de gecontroleerde bron opgebouwde payload en vaste hash.

De readback vereist voor elk van de 36 betalingen één bankregel en één tegengestelde
debiteurregel, met juiste tekens, bedrag, datum, bankboek, boekingsnummer, periode,
debiteur en eigen orderreferentie. Alleen volledige readback geeft `import_verified`.
Expliciet opnieuw alleen lezen kan met `icepay-booking-20261001-03-reconcile-v1`.
Claims blijven altijd intact. Er is geen publieke uitvoeringsroute.

Validatie: 7 financiële batch-/readbacktests en 49 bron-/browsertests zijn geslaagd.
