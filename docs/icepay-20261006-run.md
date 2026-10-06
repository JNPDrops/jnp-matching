# ICEPAY-verwerking 6 oktober 2026

Expliciete eenmalige opdracht: transacties van 6 oktober ophalen, ontbrekende
ontvangsten importeren en daarna Exact Automatically uitvoeren. De daggrens is
Europe/Amsterdam; de bron bevat wat op het vastgelegde ophaalmoment beschikbaar is.

`icepay_source_window` maakt een nieuwe, unieke bronclaim voor uitsluitend deze
dag. De oorspronkelijke CSV, UI-ID-selectie, tijdzonevergelijking en afgeleide
bedragen blijven in de private database. Verlopen eerdere opdrachten blijven
ongewijzigd. Een bestaande export kan expliciet worden opgehaald zonder opnieuw
een export aan te vragen.

Queue-taak `icepay-process-20261006-v1` gebruikt deze onveranderlijke bron. Zij
controleert bedragen, datums en ID's opnieuw, bouwt de bestaande XML-structuur voor
bankboek 27 / debiteur 109419, en vergelijkt met Exact. Bestaande of ambigue
ontvangsten blokkeren een nieuwe upload. Een bewezen volledig aanwezige batch
wordt overgeslagen. Refunds moeten afzonderlijk zijn beoordeeld; de ontvangsttaak
kan alleen doorgaan als de bron geen refunds bevat.

Voorbereiding en upload hebben aparte duurzame dossiers. Er is één uploadclaim
per batch en de upload gebruikt de gedeelde API-limieten en schrijfbeveiliging
van de toegewezen ICEPAY-worker. Een onzekere schrijfuitkomst wordt niet opnieuw
uitgevoerd. Alleen een volledig geverifieerde import kan Automatically starten.
Er worden geen handmatige matches of kruispostboekingen tussen orders gemaakt.

De taak wordt expliciet ingediend; zij heeft geen dagelijkse herhaalplanning.
De al bestaande Automatically-controle blijft iedere 15 minuten actief.
