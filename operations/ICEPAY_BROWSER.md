# ICEPAY-bot op de bestaande Render-service

Doel van Jasper: de transacties van **1 t/m 3 oktober 2026** automatisch ophalen
uit ICEPAY en vervolgens in Exact-bankboek 27 importeren. De bron is
`https://portal.icepay.com`, account 88292 (James 'n Parson B.V.), TD.eu merchant
34950. Exact-administratie 3977752; bankboek 27 – ICEPAY EUR; bankgrootboek 1317;
nog toe te wijzen 1360; ICEPAY-verzameldebiteur 109419.

## Implementatie en actuele grens

Deze wijziging levert de herbruikbare ICEPAY-loginfunctie en een eenmalige
live-controle van de exportschermen Payments, Refunds en Statements. De
inlogvelden `form.email`, `form.password` en Sign in zijn op 4 oktober 2026 in
de portal waargenomen. De datum- en merchantfilters van de Payments-export
zijn nog niet live geïnspecteerd; daarom wordt hun werking niet aangenomen.

De proef bewaart uitsluitend zichtbare formuliermetadata in de bestaande
Postgres-database. Geen invoerwaarden, transactietabellen, HTML, screenshots,
cookies of geheimen. Met deze metadata kan de gerichte exportstap worden
afgerond. **Deze versie downloadt nog geen transacties en importeert niets in
Exact.** `passed` betekent alleen dat de login en formulierinspectie slaagden;
`transactions_downloaded` en `financial_writes` blijven false.

## Render-configuratie

Gebruik uitsluitend de bestaande service `jnp-matching`
(`srv-daun5mt9fdbs739jij7g`). Voeg via Render Environment toe; zet geen geheime
waarden in GitHub, chat, logs, screenshots of deze documentatie.

| Variabele | Waarde |
| --- | --- |
| `ICEPAY_WEB_USERNAME` | E-mailadres waarmee deze ICEPAY-account wordt geopend |
| `ICEPAY_WEB_PASSWORD` | Het ICEPAY-wachtwoord |
| `ICEPAY_WEB_TOTP_SECRET` | Alleen als ICEPAY authenticator-2FA vereist: de bijbehorende TOTP-sleutel of otpauth-URI |
| `ICEPAY_FETCH_PROBE_ID` | Niet-geheim: `icepay-fetch-20261001-03-v1` |

De eerste twee velden zijn vereist. Ontbrekende gegevens worden uitsluitend
bij naam gemeld voordat een loginpoging wordt geclaimd. De bot hergebruikt geen
Exact- of Paragon-wachtwoord. De TOTP-sleutel is optioneel en wordt alleen gebruikt
bij een expliciet authenticator/tweestaps-scherm. E-mailcodes, SSO, een onbekend
scherm en CAPTCHA worden niet omzeild.

Sla de geheime velden desgewenst eerst op met **Save only**. Na akkoord op deze
concrete codewijziging: controleer actuele main en lopende deployments, voeg de
PR samen en start één handmatige deployment. Het instellen van de niet-geheime
activatievariabele met de Render-connector start zelf een deployment; gebruik
`replace=false` en start daarna geen tweede deployment. Er komt geen nieuwe
infrastructuur. Bestaande instellingen en bots blijven bestaan.

De activatie vervalt op 5 oktober 2026 om 18:00 UTC. Eén blijvende claim voorkomt
herhaalde loginpogingen bij herstart of gelijktijdige deploys. Verwijder nooit
claims om een mislukte poging stilzwijgend te herhalen. Onderzoek de vaste
foutcode voordat een volgende expliciete proef wordt gemaakt.

## Uitvoering en resultaat

De parent installeert de bestaande gepinde Chromium headless-shell zonder
geheimen. Een geïsoleerd child-proces krijgt uitsluitend ICEPAY-inloggegevens,
geen Exact-, Metorik- of databasegeheimen. Het browserproces zelf krijgt geen
inloggeheimen via de omgeving. Ieder proces gebruikt een nieuw browserprofiel.
Navigatie en POST-verzoeken zijn beperkt tot `portal.icepay.com`; wachtwoorden
en 2FA-codes worden nooit teruggegeven of in ruwe fouten gelogd.

Private Render-logs onder `ICEPAY_FETCH_PROBE` bevatten alleen vaste statuscodes,
booleans en namen van ontbrekende variabelen. De tabel `icepay_fetch_probes`
bevat het resultaat en de formuliermetadata. Er is geen publieke uitvoerings-
of downloadroute. Voor een alleen-lezen controle:

```sql
SELECT probe_id, attempted_at, result, forms
FROM icepay_fetch_probes
WHERE probe_id = 'icepay-fetch-20261001-03-v1';
```

Lokale tests (synthetische gegevens, geen netwerk of echte login):

```sh
python -m unittest operations.test_icepay_browser -v
python -m py_compile operations/icepay_browser.py operations/icepay_fetch_probe.py app/main.py
```

## Vervolg na de live-controle

1. Selecteer de geobserveerde datumfilters voor 1–3 oktober en uitsluitend TD.eu
   (34950), controleer paginering/exportlimiet en dat de gehele periode is gedekt.
2. Haal betalingen en refunds op; neem geen declined/pending betalingen op alsof
   ze ontvangen zijn. Bewaar de originele export privé met hash en periodebewijs.
3. Gebruik PaymentID plus MerchantID voor ontdubbelen en PaymentTime voor de
   betaaldatum. Het technische OrderID is geen betrouwbaar webshopordernummer;
   valideer Order # uit Checkout reference/Description voor de TD-orderreferentie.
4. Controleer bestaande Exact-regels en beschikbare boekingsnummers, maak de
   concrete importbatch en importeer één keer met teruglezing. De Fibonatix-
   importcontroller is een voorbeeld; zijn vaste journal, debtor, hash en batch
   mogen niet stilzwijgend voor ICEPAY worden hergebruikt.

Geen live inlogsucces, volledige export of Exact-import claimen voordat die
afzonderlijke stappen werkelijk zijn uitgevoerd en gecontroleerd.
