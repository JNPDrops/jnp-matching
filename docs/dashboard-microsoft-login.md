# Microsoft-login voor JNP Operations

Deze wijziging voegt `/dashboard/` toe aan de bestaande Render-webapp. Het dashboard gebruikt Microsoft Entra ID, met automatische leestoegang voor medewerkers uit de eigen Microsoft-organisatie en expliciete toegang voor gasten. Administratierechten blijven apart ingesteld. Er worden geen nieuwe Render-services aangemaakt.

## Status en afbakening

- De Microsoft-login en servercontroles zijn geïmplementeerd; activering vereist de onderstaande Entra-configuratie en een handmatige deployment.
- De werklijst is nog niet aangesloten op live uitzonderingen. Er worden geen fictieve transactievragen als werkelijke posten getoond. De productiepagina bevat geen financiële uitvoerknoppen die een bestaande agent/API kunnen aanroepen.
- Deze bescherming geldt voor `/dashboard/` en alle onderliggende routes. De bestaande agent-, Exact OAuth- en overige app-routes zijn niet gewijzigd en vallen niet automatisch onder deze login. Sluit live gegevens pas aan via dashboardroutes met dezelfde autorisatie; verwijs niet naar bestaande, mogelijk onbeveiligde financiële endpoints.
- Aanmelding is voor alle medewerkers (Entra Members) uit één Microsoft-organisatie zodra `DASHBOARD_ALLOW_TENANT_MEMBERS=true` is ingesteld. Honderden Exact-administraties kunnen hierbinnen worden toegewezen. Externe gebruikers worden voor deze eerste versie expliciet als gast in diezelfde organisatie toegelaten. Open toegang voor willekeurige Microsoft-organisaties is niet ingeschakeld.

## Microsoft Entra instellen

1. Ga naar https://entra.microsoft.com en kies de organisatie waarin de medewerkers hun zakelijke accounts hebben.
2. Maak onder **App registrations / App-registraties** een registratie `JNP Operations Dashboard`.
3. Kies **Accounts in this organizational directory only**. Dit beperkt de aanmelding tot die organisatie; gebruik geen `common`-authority.
4. Voeg bij platform **Web** precies deze redirect URI toe:

   `https://jnp-matching.onrender.com/dashboard/auth/callback`

   Dit is een andere callback dan de bestaande Exact-koppelingen. Implicit grant en public client flows zijn niet nodig.
5. Noteer de **Directory (tenant) ID** en **Application (client) ID**. Dit zijn geen geheimen.
6. Configureer een applicatiecredential. Gebruik voor productie bij voorkeur een certificaat, met alleen het openbare certificaat in Entra en het bijbehorende PFX-bestand als Render Secret File. MSAL ondersteunt ook een client secret voor een eerste proef. Plaats geheime waarden rechtstreeks in Render en nooit in de chat, repository of browsercode.
7. Kies onder **Enterprise applications → de app → Properties** bij **Assignment required?** voor **No**, zodat interne medewerkers niet afzonderlijk hoeven te worden toegewezen. De dashboardcode controleert vervolgens zelf of de gebruiker een medewerker of expliciet toegelaten gast is. Stel de gewenste MFA-eisen in via jullie Microsoft-beveiligingsbeleid. MFA wordt door Microsoft afgehandeld; het dashboard bewaart geen Microsoft-wachtwoorden of TOTP-sleutels.
8. Voeg onder **Token configuration → Add optional claim → ID** de claim **acct** toe. Als deze claim niet in de keuzelijst staat, voeg dan in het manifest aan `optionalClaims.idToken` de entry `{"name":"acct","essential":false}` toe; behoud bestaande claims. Microsoft geeft `0` voor een Member en `1` voor een Guest. Een ontbrekende claim verleent nooit automatisch toegang.
9. Voor medewerkers is geen individuele Object ID nodig, tenzij zij een andere rol of administraties moeten krijgen. Voor de externe gast is de Object ID van diens gastaccount in jullie organisatie wel nodig. Een e-mailadres wordt niet als autorisatiesleutel gebruikt.

Alleen `openid` en `profile` zijn nodig. De app vraagt geen toegang tot e-mail, bestanden of Microsoft Graph en vraagt geen `offline_access` aan.

## Instellingen in de bestaande Render-service

| Variabele | Waarde |
|---|---|
| `DASHBOARD_ENABLED` | `true` na voltooide configuratie |
| `DASHBOARD_PUBLIC_ORIGIN` | `https://jnp-matching.onrender.com` |
| `DASHBOARD_MICROSOFT_TENANT_ID` | Directory/tenant UUID |
| `DASHBOARD_MICROSOFT_CLIENT_ID` | Application/client UUID |
| `DASHBOARD_MICROSOFT_CERTIFICATE_PATH` | Pad naar het PFX Secret File bij certificaatauthenticatie |
| `DASHBOARD_MICROSOFT_CERTIFICATE_PASSWORD` | Alleen indien het PFX-bestand met een wachtwoord beveiligd is |
| `DASHBOARD_MICROSOFT_CLIENT_SECRET` | Alternatief voor certificaat, alleen server-side; niet nodig als certificaat ingesteld is |
| `DASHBOARD_ALLOW_TENANT_MEMBERS` | `true` voor automatische toegang voor medewerkers |
| `DASHBOARD_MEMBER_DIVISIONS_JSON` | Administraties die medewerkers standaard mogen lezen, bijvoorbeeld `{"3977752":"James n Parson B.V."}`; standaard `{}` geeft toegang tot de werkplek zonder administratiedata |
| `DASHBOARD_ACCESS_JSON` | Expliciete gasttoegang of afwijkende medewerkerrechten; `{}` is toegestaan wanneer medewerkers automatisch toegang krijgen |
| `DATABASE_URL` | Bestaande databaseverbinding, ongewijzigd hergebruiken |

Voorbeeld voor een expliciet toegelaten gast; vervang de voorbeeld-Object-ID door de werkelijke Object ID van de gast in jullie organisatie:

```json
{
  "33333333-3333-3333-3333-333333333333": {
    "role": "viewer",
    "divisions": {
      "3977752": "James n Parson B.V."
    }
  }
}
```

Medewerkers krijgen via de automatische regeling altijd de rol `viewer`. Een expliciete gebruikerstoewijzing gaat voor de standaardregeling. Om één gebruiker ook bij automatische medewerkerstoegang te blokkeren kan diens entry `{"disabled":true}` zijn. Rollen zijn `viewer`, `operator` en `admin`; geen enkele rol krijgt impliciet toegang tot andere administraties. Alle rollen zijn in deze eerste versie alleen aangesloten op leesroutes. Financiële beslis-/uitvoerroutes moeten later hun eigen rol-, administratie- en CSRF-controles krijgen. Voor honderden administraties en gebruikers moet deze aanvankelijke configuratie worden vervangen door beheerde toewijzingen in de database.

## Sessie- en toegangsbeveiliging

- Microsoft MSAL handelt authorization code, PKCE, state en nonce af. De callback gebruikt POST (`form_post`), waardoor autorisatiecodes niet in de URL/accesslogs terechtkomen.
- De server controleert tenant, issuer, audience, vervaldatum en de onveranderlijke Microsoft Object ID. Members met de juiste tenant en `acct=0` krijgen de ingestelde standaardleestoegang. Gasten krijgen uitsluitend hun expliciete toewijzing; een willekeurig account uit een andere organisatie wordt geweigerd.
- Cookies bevatten alleen een willekeurige sessiecode, zijn `Secure` en `HttpOnly` en gebruiken het `__Host-` prefix. De tijdelijke logincookie gebruikt `SameSite=None` voor de Microsoft POST-callback; de dashboardsessie gebruikt `SameSite=Lax`.
- In de bestaande PostgreSQL-database komt één nieuwe tabel `dashboard_sessions`. Daar staan hashes van cookiecodes en tijdelijke flow-/sessiegegevens. Een flow is tien minuten geldig en wordt atomisch eenmalig verbruikt. Een dashboardsessie duurt maximaal één uur, begrensd door de vervaldatum van de Microsoft ID-token.
- Microsoft access-/refresh-/ID-tokens worden niet in deze database of browser opgeslagen. De flow bevat kortstondig server-side state, nonce en PKCE-verifier; deze wordt bij gebruik verwijderd.
- Administratierechten worden op iedere request opnieuw beoordeeld. Een gasttoewijzing verwijderen of een gebruiker expliciet blokkeren in `DASHBOARD_ACCESS_JSON` werkt op de instances zodra de aangepaste configuratie is geladen. Het verwijderen van een afwijkende toewijzing voor een medewerker zet die medewerker terug naar de standaardleestoegang zolang automatische medewerkerstoegang actief is. Intrekken uitsluitend bij Microsoft beëindigt een al bestaande lokale sessie niet onmiddellijk; die verloopt uiterlijk na één uur.
- Uitloggen trekt de lokale sessie in en vereist een geldige CSRF-token en dezelfde origin. Dit meldt de gebruiker af bij JNP Operations; de Microsoft 365-sessie van andere apps blijft bestaan.
- Ontbrekende configuratie, een ontbrekende database of een mislukte Microsoft-aanmelding geeft nooit anonieme toegang. Er is geen ontwikkel-bypass in productie.

## Eén externe gast uitnodigen

1. Open **Entra ID → Users → New user → Invite external user**.
2. Gebruik het exact door Jasper opgegeven e-mailadres. Verstuur geen uitnodiging zolang de ontvanger niet is vastgesteld.
3. Laat het gebruikerstype **Guest** en nodig de persoon uit; de ontvanger accepteert de uitnodiging met het eigen account. Een aparte Microsoft-organisatie voor het dashboard is hiervoor niet nodig.
4. Neem de Object ID van dit gastaccount uit jullie eigen organisatie over in `DASHBOARD_ACCESS_JSON`, met alleen de afgesproken administraties en aanvankelijk `viewer`.
5. Test de gastaanmelding na acceptatie. Andere bestaande gastaccounts krijgen niet automatisch toegang.

Deze code maakt geen Microsoft-gebruikers aan en verstuurt geen uitnodigingen. De Entra-uitnodiging en de dashboardtoewijzing zijn twee aparte stappen.

## Activering en verificatie

1. Stel Entra en de bovenstaande serverconfiguratie in zonder geheime waarden te publiceren.
2. Controleer actuele main en bestaande deployments. Voeg deze wijziging samen en start één handmatige deployment; automatische deployments blijven uit.
3. Open `/dashboard/` in een privévenster: eerst Microsoft-aanmelding. Test een medewerker zonder individuele toewijzing, de expliciete gast, een niet-toegelaten gast én een account uit een andere organisatie.
4. Controleer dat alleen toegewezen administraties terugkomen en dat een directe aanvraag voor een andere administratie `403` geeft.
5. Controleer uitloggen en opnieuw openen. Sluit de werklijst pas daarna aan op live data en beslisacties.

Lokale regressietests (22 tests): `python -m unittest discover -s tests -p 'test_dashboard_auth.py' -v`.
Deze tests gebruiken een gecontroleerde identiteitsprovider en tijdelijke testopslag; een werkelijke Entra-aanmelding en PostgreSQL-integratietest blijven activeringscontroles.

Bronnen: [Microsoft web-app sign-in](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-web-app-sign-in), [MSAL Python](https://msal-python.readthedocs.io/en/latest/), [toegang beperken](https://learn.microsoft.com/en-us/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users), [acct-claim](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims-reference), [gasten uitnodigen](https://learn.microsoft.com/en-us/entra/external-id/add-users-administrator).
