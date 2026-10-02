# JNP bankimportproef

## Wat is gebouwd?
Een zelfstandige Python-generator voor synthetische CAMT.053.001.02-bestanden. Geen netwerkverkeer, geen importfunctie, geen wijziging aan bestaande JNP-routes of write-flags. Een aanroep maakt precies een bestand met een nieuwe testontvangst; standaard EUR 0,01, begrensd op EUR 1,00.

Varianten:
- control: geen te onderzoeken referentie (nulmeting).
- structured: de testreferentie staat uitsluitend in RmtInf/Strd/CdtrRefInf/Ref.
- end_to_end: uitsluitend in Refs/EndToEndId.
- unstructured: uitsluitend in RmtInf/Ustrd.

De herkenbare referentie is JNPTEST-MAP-<run_id>. Document-/transactie-identificaties verschillen noodzakelijk per variant. Boekdatum, rekening en bedrag moeten gelijk worden gehouden voor een vergelijkbare proef. Bij opeenvolgende imports moet het opgegeven beginsaldo aansluiten op de voorgaande werkelijke toestand; de generator kan die toestand niet zelf kennen.

## Status en beperkingen
Zes lokale tests zijn uitgevoerd en geslaagd: aantallen/saldo, referentie-isolatie, geen echte orderreferentie, ongeldige invoer, negatieve saldi en unieke documentidentificaties. Geen validatie tegen een volledig XSD-schema uitgevoerd. Geen van de bestanden is in Exact Online geimporteerd. Acceptatie van dit CAMT-profiel en de veldmapping in Exact Online zijn ONBEVESTIGD. XML die lokaal parseert is niet automatisch een door Exact geaccepteerd bankbestand.

Een originele, eerder geaccepteerde bankexport is de beste volgende bron om het daadwerkelijke formaat/bankprofiel te controleren. Een MT940-export is geen bewijs van CAMT-ondersteuning. De gegevens uit een export zijn een profielvoorbeeld, geen toestemming om de echte transacties nogmaals te importeren.

Voor een concrete proef zijn de juiste ontvangende IBAN, de boekdatum, het beginsaldo en een nog ongebruikt afschriftnummer nodig. Vul die niet fictief in voor productie. De generator kent bestaande afschriftnummers en openstaande posten niet en claimt geen botsingscontrole of automatische terugdraaiing.

## Uitvoeren
Python 3.10 of hoger; alleen standaardbibliotheek.

```sh
cd tools/camt_probe
python -m unittest -v
python camt_probe.py --iban '<ontvangende-IBAN>' --opening-balance '<beginsaldo>' --statement-number 8001 --booking-date 2026-10-02 --run-id 20261002A --variant structured --output proef-a.xml
```

8001 is uitsluitend een voorbeeld: kies een werkelijk ongebruikt nummer. De generator overschrijft geen bestaand uitvoerbestand. Gebruik een nieuw run_id voor een nieuwe proefreeks.

## Meten na een daadwerkelijke import
Leg vast: import geaccepteerd/afgewezen, nieuw Exact EntryID/EntryNumber/LineNumber, aanwezige Payment reference/OurRef/omschrijving, toegewezen AccountCode/GLAccountCode en eventuele matchwijzigingen. Controleer na verwijdering van de testpost ook eventueel geraakte openstaande posten. Verwijder geen bestaande echte bankboeking voor deze proef.

Deze eerste reeks meet waar een referentie wordt ingelezen, NIET of de echte webshopfactuur wordt afgeletterd. JNP zoekt webshopreferenties in YourRef; dat veld mag niet zonder bewijs worden gelijkgesteld aan OurRef. Voor de latere allocatie-/matchingproef moet ook de referentie op de verkooppost en de gekozen debiteur worden gecontroleerd.

De synthetische referentie wordt niet als SCOR/ISO 11649 RF-referentie geclaimd. Een bank- of importprofiel kan strengere eisen stellen dan deze generator. Pas het profiel pas aan op basis van documentatie of een geverifieerde importuitkomst.
