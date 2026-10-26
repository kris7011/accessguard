# AccessGuard — prototype 0.1

En lokal, kørbar prototype til kontrol af adgang mellem kunder. Arbejdsnavn, ikke et registreret eller undersøgt varemærke. Ikke et færdigt sikkerhedsprodukt.

## Kom i gang på Windows

Udpak projektet til `C:\AccessGuard`, så `guard.py` ligger direkte i mappen. Kræver Python 3.11 eller nyere. Ingen ekstra Python-pakker eller abonnementer.

```powershell
cd C:\AccessGuard
py -3 guard.py
Start-Process .\reports\report.html
py -3 -m unittest discover -s tests -v
```

På Linux/macOS bruges `python3` i stedet for `py -3`.

## Hvad der faktisk virker

- Et ægte HTTP-demo-API startes på en tilfældig port på 127.0.0.1 og lukkes bagefter.
- To syntetiske kunder har hver et dokument og en testtoken.
- Fem tilfælde testes: begge kunders egen adgang, adgang på tværs i begge retninger samt anonym adgang.
- Ejersvar kontrolleres først. Uautoriseret adgang bekræftes kun, hvis det beskyttede syntetiske mærke faktisk returneres.
- Samme tests køres mod en sårbar og en rettet implementering.
- Resultater gemmes som JSON og HTML. Tokens og fulde dokumentindhold skrives ikke i rapporten.
- Fejl, ukendte svar og manglende adgang for den retmæssige ejer giver INCONCLUSIVE, ikke grønt lys.

Forventet demo: sårbar version 3 PASS / 2 FAIL; rettet version 5 PASS. Exit 0 betyder, at den samlede før/efter-demo lykkedes, ikke at den sårbare version er sikker.

## AI-status — vigtigt

Standardkørslen er en deterministisk baseline uden AI eller ML. Den skal sikre, at evidens og testmotor fungerer, før en model får indflydelse.

En valgfri rigtig Ollama-adapter er implementeret:

```powershell
py -3 guard.py --ollama-model NAVNET-PAA-DIN-INSTALLEREDE-MODEL
```

Den bruger `http://127.0.0.1:11434/api/chat`. Ollama og en egnet model skal allerede være installeret. Modellen får kun syntetiske testbeskrivelser. Den må ændre rækkefølgen af de fem godkendte tests; den må ikke ændre regler, mål eller udelade tests. Ugyldigt modeloutput afvises. Fejl skjules ikke bag en automatisk fallback.

Adapterens kontrakt er testet med mock-svar. En rigtig model er IKKE kørt under udviklingen af denne levering. Dette er derfor endnu ikke en færdig autonom agent. Ingen træning eller påstand om løbende forbedring i version 0.1.

API-reference: https://docs.ollama.com/api/chat

## Afgrænsning

Ingen adgang til dine eksisterende projekter er nødvendig. Demoen understøtter ikke eksterne mål, produktionsdata, kodeændringer hos kunder eller scanning af internettet. Demo-login med faste tokens er kun en testfixture og må ikke bruges som produktions-login.

Testresultaterne viser kun de bevidst indbyggede scenarier. De siger ikke noget om detektionsraten på ukendte systemer. 403/404 regnes som afvisning; sidekanaler, delvise datalæk, skriverettigheder og rolleeskalering dækkes ikke.

## Næste tekniske milepæle

1. Separat datasæt af API-fixtures med ukendte fejlkombinationer og negative kontroller.
2. Kundens eksplicitte adgangspolitik og testidentiteter som input; tilladte mål, rategrænser og credential-håndtering.
3. Agent, der vælger fra en udvidet værktøjskasse ud fra observerede resultater; evalueres mod baselinen.
4. Forslag til kodeændringer med regressionstest i separat branch, aldrig automatisk produktion.
5. ML-rangering ud fra menneskeligt bekræftede labels, med dataadskillelse pr. applikation og holdout-evaluering. Ingen kundedata bruges til træning uden aftale.

Vi fortsætter kun mod salg, når præcision, dækningsgrad, tidsforbrug og kundeværdi kan dokumenteres. Konkurrencemæssig fordel og betalingsvillighed er endnu ikke valideret.
