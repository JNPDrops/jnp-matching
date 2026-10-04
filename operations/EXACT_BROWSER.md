# Exact-loginbot op de bestaande Render-service

De Exact-websessie verloopt onafhankelijk van de bestaande OAuth-koppeling.
Deze module voert gebruikersnaam, wachtwoord en een TOTP-code binnen Render
in en controleert daarna administratie 3977752 (James n Parson B.V.).

## Eenmalige proef

Geheimen blijven in Render Environment van `jnp-matching`:

| Variabele | Inhoud |
| --- | --- |
| `EXACT_WEB_USERNAME` | Exact-gebruikersnaam of e-mailadres |
| `EXACT_WEB_PASSWORD` | Wachtwoord van die Exact-gebruiker |
| `EXACT_WEB_TOTP_SECRET` | De bij dit Exact-account ingestelde TOTP-sleutel, of de volledige otpauth-URI |

Dit zijn andere gegevens dan `EXACT_CLIENT_ID` en `EXACT_CLIENT_SECRET` van de
OAuth-koppeling, en andere dan de Paragon-gegevens. Bestaande variabelen en
OAuth-tokens worden niet aangepast. Bewaar geen geheimen in GitHub of chat.

Activeer de proef met `EXACT_LOGIN_PROBE_ID=exact-login-20261004-v3` en voer één
handmatige deployment uit. De activatie vervalt op 5 oktober 2026 om 18:00 UTC.
Zonder de exacte activatiewaarde doet deze module niets. Ontbrekende of ongeldige
configuratie wordt gemeld voordat de poging wordt verbruikt.

Een unieke claim in de bestaande Postgres-database voorkomt een tweede poging
bij een herstart of gelijktijdige deployment. Een mislukte echte login wordt
niet automatisch herhaald. Verwijder geen claim om toch opnieuw te proberen;
onderzoek eerst de melding en gebruik daarna een expliciet nieuwe proef.

`/health` bevat `exact_login_probe` met alleen vaste statuscodes, de stap en
booleans. De applicatielog bevat dezelfde informatie onder `EXACT_LOGIN_PROBE`.
De tabel `exact_login_probes` bewaart het resultaat. Er is geen openbare
uitvoeringsroute. Een configuratiemelding noemt alleen ontbrekende variabelen.

`passed` vereist een zichtbare bedrijfsnaam, administratie 3977752 in de
MenuPortal-URL, navigatie, en een geladen ingelogde MainWindow. Een oude menubalk
met een inlogscherm in een iframe telt niet als een geslaagde aanmelding.

## Grenzen en vervolg

De login gebruikt een nieuw browserprofiel per proef, uitsluitend de Exact-
domeinen `start.exactonline.nl` en `login.exact.com`, en geen bewaarde cookies.
Wachtwoorden, codes, screenshots, paginainhoud en ruwe fouten worden niet
gelogd of teruggestuurd. Het browserproces erft de geheimen niet.

De eerste gebruikersnaamselector is in de huidige Exact-pagina waargenomen.
De eerste live poging bereikte de wachtwoordstap maar stopte bij de formulier-
doelcontrole, voordat het wachtwoord was ingevoerd. Versie 2 accepteert naast
Exact-HTTPS-formulieren uitsluitend de no-op `javascript:void(0)`-actie die
AJAX-inlogformulieren gebruiken. Formuliernavigatie en POST-verzoeken naar
andere hosts blijven geblokkeerd. Versie 2 bleef na gebruikersnaam steken;
versie 3 herstelt het oorspronkelijke toestaan van GET-verzoeken voor externe
resources, ook bij XHR. Alle eerdere claims blijven bewaard.
Wachtwoord en TOTP worden conservatief herkend via semantische invoervelden.
De huidige live wachtwoord- en TOTP-stappen zijn nog niet met deze bot getest.
Een onbekend scherm, SSO, CAPTCHA, foutmelding, gewijzigd domein of herhaalde
inlogstap stopt de poging. De bot kiest geen andere inlogmethode, reset geen
wachtwoord en wijzigt geen 2FA-instellingen.

Deze versie implementeert alleen de loginproef, net als de Paragon-proef.
`authenticate(page, credentials)` is herbruikbaar binnen een latere importjob.
XML-import, Automatically en financiële mutaties worden hier niet uitgevoerd.
De bestaande handmatige deployinstelling, debiteurenroutering en overige
agents blijven ongewijzigd. De Fibonatix-batch van 845 regels wacht nog op
live-controle, import en aflettering.

Lokaal testen zonder netwerk of geheimen:

```sh
python3 -m unittest operations.test_exact_browser operations.test_paragon_login_probe -v
```

De tests dekken logintransities, automatisch ingediende TOTP, herhaalde stappen,
geheimafscherming, doelcontrole, een verlopen iframe, CAPTCHA-stop, minimale
child-omgevingen en voorkomen van dubbele pogingen. Dit is geen bewijs van een
geslaagde live Exact-login; dat vereist `passed` uit de Render-proef.
