# Eenmalige aanmaak ICEPAY-bankboek

Jasper heeft op 4 oktober 2026 gevraagd de bestaande Render-bot een ICEPAY-bankboek
te laten maken. PR #36 is met afzonderlijke toestemming samengevoegd en handmatig
uitgerold als `dep-db19lrc9v7es73ejaf9g`.

De leescontrole van 18:29:55 UTC bevestigde:

| Instelling | Waarde |
| --- | --- |
| Exact-administratie | 3977752 |
| Bestaande ICEPAY-bankboeken | 0 |
| Vrij dagboeknummer | 27 |
| Bestaande grootboekrekening | 1317 – Icepay EUR, type Bank, niet geblokkeerd |
| Betalingen nog toe te wijzen | 1360 |
| Referentie | Fibonatix dagboek 26, rekening 1316, nog toe te wijzen 1360 |

De aanmaak gebruikt de bestaande Exact OAuth-koppeling binnen Render. De nieuwe
web-loginproef blijft ongewijzigd. Er worden geen webcredentials, cookies of tokens
uitgelezen of geretourneerd. De opdracht maakt uitsluitend dagboek 27, ICEPAY EUR,
type Bank, valuta EUR, rekening 1317, `PaymentInTransitAccount` 1360. Het interne
bankkenmerk is `ICEEUR`, vergelijkbaar met `FBNEUR` voor Fibonatix; dit is geen IBAN.

Er is geen openbare uitvoeringsroute. Aanmaak staat standaard uit. Na goedkeuring
van de concrete wijziging en controle van de actuele main/deploys, activeer op de
bestaande service met uitsluitend deze niet-geheime variabele:

`ICEPAY_JOURNAL_TASK_ID=icepay-journal-create-20261004-v1`

De Render-update van omgevingsvariabelen start zelf een deployment. Voeg samen
met bestaande variabelen (`replace=false`), en start geen tweede deployment.
Controleer dat de deployment de bedoelde commit bevat. De opdracht vervalt op
5 oktober 2026 om 18:00 UTC. Een blijvende databaseclaim voorkomt herhaling bij
herstart of gelijktijdige deployment. Verwijder deze claim niet.

Vlak vóór het schrijven worden de bestaande dagboeken, gekoppelde rekening en
vrije code opnieuw gecontroleerd. Een correct bestaand ICEPAY-bankboek leidt tot
`already_exists`. Een bezette code, afwijkende inrichting of een al bestaand los
bankkenmerk stopt de opdracht zonder aanmaak. Er is geen PUT/DELETE of fallback
naar andere API-sleutels. De enige write is één vastgelegde POST naar Journals.

Vóór die POST wordt `write_started` duurzaam opgeslagen. Een fout of timeout
wordt nooit automatisch herhaald: controleer dan eerst het resultaat in Exact.
Alleen een onafhankelijke GET met overeenkomende instellingen geeft
`created_verified`. De database `icepay_journal_tasks` en private Render-logs met
`ICEPAY_JOURNAL_TASK` bevatten de status, zonder geheime waarden. Boekingen,
saldi, aflettering, importjobs en bestaande agents worden niet gewijzigd.

De eerste inspectie gaf voor het optionele pad `cashflow/BankAccounts` een 404.
Dit is gecorrigeerd naar `crm/BankAccounts`, met een filter op uitsluitend het
betrokken bankkenmerk. Die leescontrole moet slagen voordat de POST wordt gedaan.

Lokale controle: `python -m unittest operations.test_icepay_journal_task -v`.
De aanmaak is pas live bewezen na de status `created_verified`.
