# Nachtverwerking 7 oktober 2026 — onvoltooid uitvoeringscheckpoint

Dit is een technisch hervatdossier, geen financiële eindrapportage. Vastgelegd na verlies van de Render Web Shell-besturing op 8 oktober 2026. Een broncontrole of geslaagde CI is geen bewijs van import of aflettering.

## Vastgelegde dag en bewijs

- Administratie 3977752; verwerkingsdatum D=2026-10-07, afgeleid van geplande lokale uitvoering 2026-10-08 01:00 Europe/Amsterdam.
- Bronvenster [2026-10-06T22:00:00Z, 2026-10-07T22:00:00Z), overeenkomend met de Amsterdamse kalenderdag.
- De bestaande `nightly_batches.register` is uitgevoerd en de duurzame dag- en PSP-identiteiten zijn geregistreerd. Geen nieuwe infrastructuur of dubbele nachtketen gestart.
- Via de bestaande webservice-integratie is verse Paragon-brondata opgehaald en bewaard in `paragon_login_probes` onder `jnp:3977752:fibonatix:2026-10-07:source`; de Fibonatix-bronstatus in `jnp_daily_source_jobs` is verified.
- Export 267 regels; exact 123 vallen in het dagvenster: 86 Approved, 27 Declined, 10 Pending. Approved omvat transactieklassen; dit is uitdrukkelijk niet het aantal geïmporteerde positieve ontvangsten. Buiten het venster: 144. UI-aantal en vijf PSP-ID/tijdstipparen zijn gecontroleerd. Hash, oorspronkelijke CSV en vensterbewijs zijn opgeslagen.
- De Paragon-loginbrowser is daarna normaal afgesloten.
- Geen financiële import- of Automatically-opdracht is door deze uitvoering ingediend. De imports en follow-up van 6 oktober zijn niet gereset of herhaald.

## Onderbroken controle en exclusiviteit

De eerste Metorik-leesquery met `order_id`-filter gaf HTTP 422. Daarna is een gedocumenteerde gepagineerde `/orders`-leesquery ingediend, die uitsluitend de vereiste eigen order-ID's selecteert en het resultaat onder de bovenstaande bronidentiteit plus `:orders` bewaart. De besturingsverbinding viel weg voordat de uitkomst kon worden teruggelezen. Controleer eerst dit duurzame resultaat en eventuele lopende broncontrole; dien niet blind opnieuw in.

De coördinatiesessie op Fibonatix had `nightly_batches.lock_chain(conn)` verworven. Vrijgave is wegens uitval van de shellbesturing niet bevestigd. Controleer de bestaande sessie/lock vóór hervatten. Start geen tweede schrijver en beëindig geen onbekende productiesessie. De read-only orderscontrole op de webservice is geen import.

## ICEPAY-aanpassing

PR102 voegt een datumgeparameteriseerde adapter toe: afzonderlijke daily_source, daily_prepare, daily_import en daily_reconcile, bronbewijs, duurzame identiteit, bestaande write-fence en PSP-ID-deduplicatie. Geen hergebruik van de verlopen eenmalige 6-oktoberautorisatie. Aflettering blijft een afzonderlijke native Automatically-stap.

Codecommit `121b8ce4d3c6a95d968d51c278bb583bed64e94c` doorstond GitHub Actions-run 37700199471 (Agent separation tests). De zeven nieuwe synthetische tests slaagden ook lokaal. Een historische testklok is vastgezet op de oorspronkelijke autorisatiedatum; productie-expiry is niet verlengd.

PR102 is op dit checkpoint niet gemerged of gedeployed. Drain/overdracht van ICEPAY is nog niet gestart. Handmatige deployment mag pas na bewezen drain en controle van werkelijk geladen code en uitvoerende lease.

## Veilig hervatten

1. Herstel shellbesturing; lees bestaande nachtlock, dagstatus, opdrachten, write-fences en bovengenoemd orderbewijs terug. Behoud D=2026-10-07 en bestaande request-identiteiten.
2. Als eigen orderbewijs compleet en actueel is: dien Fibonatix daily_prepare in, controleer manifest inclusief afzonderlijke refunds en bestaande import-ID's, daarna pas daily_import. Lees Exact terug en wacht op de duurzame daily_review. Importeer succesvolle oorspronkelijke betalingen ongeacht webshopstatus.
3. Rond de ICEPAY-codecontrole en gecontroleerde worker-overdracht af; voer daarna daily_source, daily_prepare en daily_import uit met tussenliggende bewijzen. Geen bestaande import opnieuw uitvoeren. Unsupported refundvormen blokkeren in plaats van een positieve ontvangst te verzinnen.
4. Bevestig relevante dag-/batchgrenzen voor routing, Woo-regels, tax en maintenance. Hun processen hoeven niet te stoppen.
5. Pas vervolgens scoped native Automatically toe, inclusief de goedgekeurde absolute EUR 1-grens per eigen factuur/order en vereiste controles. Geen handmatige matching, strict_match of kruisordersaldering. De afgeronde Automatically-opdracht van 6 oktober niet herhalen.
6. Maak pas na terminale afhankelijke stappen de enige duurzame financiële dagrapportage. Vermeld concrete resterende blokkades; een onvoltooide dag blijft onvolledig.

De onafhankelijke ICEPAY-15-minutencontrole meldde om 2026-10-07T23:05:01Z `completed open_before=0 remaining_open=0`. Dit bewijst geen bronimport voor 7 oktober en telt niet als uitgevoerde dagaflettering van deze nachtketen.
