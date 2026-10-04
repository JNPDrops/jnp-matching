# Belastingherkenning — James n Parson, 4 oktober 2026

Operatoropdracht: RSIN **867393051**, Exact-administratie **3977752**.
Herken betalingen van en aan de Belastingdienst en leid het laatste jaarcijfer
af naar het dichtstbijzijnde jaar. Deze uitbreiding verandert de bestaande
debiteurenroutering en Woo/bosci-regels niet.

## Werkende herkenning

- Controlecijfer van een 16-cijferig betalingskenmerk en de RSIN-elfproef.
- Vpb (`V`), btw (`B`), loonheffingen (`L`) en naheffingen (`F`/`A`).
- Ook een volledig herkenbaar aanslagnummer in de omschrijving wordt gelezen.
- Jaar: dichtst bij het **bankjaar**, zodat historische transacties bij een
  latere scan hetzelfde resultaat behouden. Bij exact vijf jaar verschil het
  eerdere jaar. Een expliciet tweecijferig jaar mag deze uitkomst niet tegenspreken.
- De oorspronkelijke bankdatum, het ondertekende bedrag, de betaalrichting,
  subnummers en periodecodes blijven behouden. Geen verschuiving van het boekjaar.
- Meerdere aanslagen, afwijkende RSINs, ongeldige/ontbrekende controlecijfers,
  willekeurige kenmerken en niet ondersteunde soorten blijven ter beoordeling.
- Rente, boetes, verrekeningen en kosten worden niet uit een nummer verzonnen.
  Naheffingen, definitieve/navorderings-Vpb en teruggaven vragen om de specificatie.

Bevestigde casus: `6739305619301120` wordt bij bankjaar 2026 herkend als
`8673.93.051.V.61.0112`, voorlopige Vpb 2026, periodecode `0112`.

## Ingebouwd in de bestaande agent

De bestaande kandidatenlijst herkent belastingregels **vóór** de selectie van
positieve webshopontvangsten. Belastingkenmerken worden niet meer als een
webshopordernummer opgezocht. `TAX_IDENTIFIED` betekent herkenning, geen boeking.

Een GET-only achtergrondtaak leest iedere 30 minuten gewijzigde belastingregels;
de eerste scan leest de historische selectie zonder datumgrens of `$top`-cap.
Alle `__next`-pagina's worden gevolgd. Een onvolledige scan verplaatst de cursor
niet. Een overlap van vijf minuten vangt gelijktijdige wijzigingen op. Rekening-
metadata wordt maximaal eenmaal per dag opnieuw gelezen. API-budgetreserve 150;
geen app/sleutelwissels of nieuwe infrastructuur. Bestaande pauzes blijven staan.

Opslag in bestaande database: `jnp_tax_control` en `jnp_tax_observations`.
`/api/tax/status` en `/health` tonen alleen operationele status en aantallen.
`/api/tax/report` toont de waarnemingen via de bestaande Allocation-operatorsessie.
De rapportage vermeldt expliciet of de maximaal 500 getoonde regels onvolledig zijn.
Logs bevatten alleen aantallen en kandidaat-grootboeknummers/omschrijvingen,
geen bankbedragen, betalingskenmerken, bankomschrijvingen of OAuth-gegevens.

## Rekeningvoorstel en grens aan automatisch boeken

`tax_policy.json` bevat de concrete rekeningmapping. Alleen een uniek bestaande,
niet-geblokkeerde balansrekening mag als geverifieerd rekeningvoorstel verschijnen.
Zonder geverifieerd nummer toont de agent bestaande kandidaten; hij kiest geen
rekening op alleen een gedeeltelijke naam. Een betaling of teruggaaf is geen
nieuwe btw-belaste kostenpost. De belastingsoort bepaalt niet de eventuele
uitsplitsing naar rente/boete of de bijbehorende reeds geboekte openstaande post.

**Er worden nog geen belastingboekingen of nieuwe Exact-toewijzingsregels geschreven.**
De officiële `BankEntryLines`-API ondersteunt GET/POST, geen PUT. `BankEntries`
ondersteunt GET/POST/DELETE, geen PUT. Verwijderen/herimporteren, een tweede bankboeking
of ongevraagde correctiememorialen zijn geen onderdeel van deze module.
De transportlaag verbiedt iedere Exact-write, ook bij een gewijzigde policy.

Exact `AllocationRule` ondersteunt wel een grootboek en herkenningswoorden voor
**geïmporteerde** banktransacties. Een vaste regel kent echter geen ingebouwde
controle op rente, gemengde betalingen of ontbrekende aanslagspecificaties. Ook
bewijst het aanmaken van een regel niet dat bestaande bankregels zijn bijgewerkt.
Voor automatische boeking moet de geverifieerde rekeningmapping worden gekoppeld
aan een ondersteunde import/toewijzingsstap die deze controles daadwerkelijk gebruikt.

## Validatie en bronnen

`python -m unittest operations.test_tax_reference -v`

Officiële bronnen gecontroleerd 4 oktober 2026:
- https://odb.belastingdienst.nl/wp-content/uploads/2025/07/Specificatie-Betalingskenmerk_bepaling_1.5.pdf
- https://www.acceptgiro.nl/wp-uploads/Cu_RR_Ac_Bijlage_G.A_Formspecs.pdf (gewogen modulus 11, paragraaf 3.5.3)
- https://www.betaalvereniging.nl/kennisbank/rekeningen-en-diensten/betalingskenmerk/
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialTransactionBankEntryLines
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialTransactionBankEntries
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CashflowAllocationRule
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialGLAccounts
