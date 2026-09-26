# ytdj jukebox — požadavky a plán

Jediný zdroj pravdy pro vývoj kancelářského jukeboxu ytdj na Raspberry Pi.
Sestaveno ze zadání vlastníka (25. 9. 2026). Každý agent i každá změna se
měří podle tohohle dokumentu.

Úplný seznam všeho, co vlastník chtěl, se stavem: [POZADAVKY.md](POZADAVKY.md).

## Vize

> „Komplexní a promyšlená aplikace. Skvělý DJ, který se zavděčí úplně každému
> a udělá, co mu na očích uvidí." — „Plnit ta přání přesně je podstata
> funkce této aplikace." — „Jednoúčelové zařízení… musí to dělat skvěle."

Jukebox v kanceláři: víc kolegů píše přání přes web, ovládá se i dotykovým
displejem a kolečkem na repráku. Nesmí dělat ostudu.

Pravidla při rozhodování (v tomhle pořadí):
1. **Přesně splnit, co kdo napsal** (interpret = jeho hudba, ne jedna písnička).
2. **Chytře vyplnit, když nikdo nic nechce** (čas, den, kancelář, co tu lidi dohrávají).
3. **Učit se z reakcí** (přeskočení, dohrání) — nikdy proti výslovnému přání.
4. Rychlost a spolehlivost před efekty. Tvrdit jen to, co doloží data (log, měření).

## Požadavky

### A. Zařízení a systém
| ID | Požadavek | Stav |
|---|---|---|
| A1 | Raspberry Pi 3 B, Raspberry Pi OS Lite 64-bit, po zapnutí se vše spustí samo | ✅ ověřeno restartem |
| A2 | Jednoúčelové, zeštíhlené (bez GPU, bluetooth, apt/man-db timerů…) | ✅ `packaging/rpi/slim.sh` |
| A3 | Displej KeDei 3.5" v6.2 + dotyk, vlastní ovladač | ✅ `ytdj/panel/kedei.*` |
| A4 | USB repro Dell AC511, kolečko hlasitosti | ✅ |
| A5 | Krabička pro 3D tisk | 📝 rešerše hotová (Diamant Edition / Helska), výběr na vlastníkovi |

### B. Přehrávání a zvuk
| ID | Požadavek | Cíl (měřitelný) | Stav |
|---|---|---|---|
| B1 | Rychlé přeskakování i přes víc skladeb | skok na připravenou skladbu p90 < 1,5 s; nepřipravená p90 < 12 s | 🔄 resolver + 4 dopředu (měřeno 0,1 s / ~8–11 s) |
| B2 | Žádné lupání | 0 nahlášených lupnutí za den; 0 xrunů mimo start | 🔄 hledá se (viz C-lupání) |
| B3 | Čas skladby odpovídá tomu, co hraje | čas stojí při načítání | ✅ |
| B4 | Jedna hlasitost: kolečko, displej, web; pamatuje si ji | | ✅ |
| B5 | Podržené −/+ na displeji mění hlasitost | | ✅ |
| B6 | Skladba nesmí sama skončit / přeskočit bez důvodu | 0 nevysvětlených konců | ✅ opraveno (smyčka přeseedování) |
| B7 | Správná nahrávka (ne cover/live/neoficiální), pokud o ni nikdo nežádá | | ✅ katalog |

### C. DJ — přání a jejich výklad
| ID | Požadavek | Stav |
|---|---|---|
| C1 | Hrát přesně, co uživatel píše | 🔄 agent DJ |
| C2 | Správný výklad záměru: interpret / konkrétní píseň / víc interpretů / žánr, období / „jako X" / „víc takového" / „něco jiného" / „od X, ale ne Y" / „jen česky" | 🔄 agent DJ |
| C3 | Režim interpreta: „písničky od Midi Lidi" = fronta z jeho skladeb | 🔄 katalog ✅ (`set_artist`), DJ zapojení 🔄 |
| C4 | Poctivé odpovědi (neslibovat, co nehraje; „nenašel jsem"). Potvrzení přání skládá aplikace z toho, co se opravdu zařadilo („Zařadil jsem: …"); model nesmí slibovat režimy ani nastavení, která neexistují („nastavím střídání") | ✅ fronta přání (26. 9.) |
| C5 | Přání posluchače má vždy přednost před automatickými zásahy | ✅ (web ruší auto-přeseedování) |
| C6 | Když je potřeba lepší model, nasadit lepší | 🔄 posudek agenta DJ |
| C7 | „Hrát" bez přání a bez fronty = playlist podle času, dne, kanceláře a historie | 🔄 agent context |
| C8 | Učit se z přeskočení/dohrání bez přebíjení přání | 📋 |
| C9 | Otázka nebo stížnost na frontu („proč nehraje moje…", „kdy bude…", „to není demokracie") není přání: pravdivá odpověď ze stavu (co hraje a čí to je, kde jsou moje přání a ETA), žádná hudba navíc; kdo ≥ 10 min nic svého neslyšel, jde hned po hrající; telemetrie `request.meta` | ✅ |
| C10 | „Oboje" / „i X i Y" = obojí (skladba i interpret); „střídej X a Y" = jedno přání se dvěma interprety střídavě | ✅ |

### D. Víc lidí v kanceláři
| ID | Požadavek | Stav |
|---|---|---|
| D1 | Fronta přání se jménem autora | 📋 fáze 2 |
| D2 | Spravedlivé střídání mezi lidmi — pravidla viz „Pravidla fronty" níž | ✅ (26. 9.) |
| D3 | Volitelně „zařadit hned" (za právě hrající, bez přerušení) | 📋 |
| D4 | Přání se přijmou hned, žádné „DJ ještě dokončuje…" | 📋 (částečně ✅ C5) |
| D5 | Fronta vidět na webu i displeji; autor může své přání odebrat | 📋 |
| D6 | Automatické změny nálady jen v podkladovém rádiu, nikdy nemažou přání | 📋 |
| D7 | Podkres jde za naposledy SPLNĚNÝM přáním; režim interpreta v podkresu je omezený (10 skladeb / 40 min, pak „… a podobné"); po restartu se vypršelý neobnoví | ✅ |
| D8 | Nové přání člověka jde PŘED jeho starší, to zůstává; nahrazuje jen oprava („ne, radši…", „místo toho", „zruš…"), změna směru („něco jiného") nebo skoro stejný text | ✅ |

#### Pravidla fronty (26. 9., po ranním provozu)

Pi 26. 9. 9:00–9:50: přání „Parni Valjak" z displeje (12 skladeb) drželo
kancelář 52 minut, Robertova přeskočení jen pouštěla další skladbu toho
přání, podkres se k Parni Valjak vracel i po restartu.

1. **Kola.** Na řadě je ten, kdo nejdéle nic neslyšel; kolo = 2 skladby,
   když čekají i jiní (3, když ne). Kolo patří člověku: jeho nové přání
   dohraje rozehrané kolo.
2. **Rozpočet.** Přání s víc skladbami (interpret, oblíbené, playlist) zahraje,
   dokud mají jiní co hrát, nejvýš **4 skladby** celkem (z displeje **3**);
   pak jde za všechny ostatní a pokračuje, až nikdo jiný nečeká.
3. **Displej = jeden člověk.** Každé zavření přání na displeji je nová relace
   (id klienta), v pořadí se ale počítá jako jedno místo — jinak by byl
   pokaždé „nováček" a předběhl všechny. Menší rozpočet: displej je sdílené,
   anonymní zařízení a jeho přání často nikdo nehlídá; přezdívka z webu je
   konkrétní člověk, který si přání upraví sám.
4. **Přeskočení.** Přeskočí-li skladbu přání někdo jiný než autor, kolo toho
   přání hned končí (další hraje někdo jiný). Druhé cizí přeskočení přání
   ukončí („přeskočeno ostatními") a s ním i jeho podkres. Autorovo
   přeskočení = „další z mých". Tlačítko Další na displeji u přání
   z displeje = autor. Jedna skladba se počítá jednou: dvojí ťuknutí
   (tentýž člověk, tatáž skladba do 1,5 s) je jedno přeskočení; přeskočení
   rozhodnuté DJem („DJ") ani Další při výpadku se nepočítají.
5. **Moje přání.** Nové jde před moje starší (to zůstává za ním); „a pak …"
   / „přidej …" za ně. Nahrazuje jen oprava, změna směru nebo skoro stejný text.
6. **Otázka, nebo přání.** Otázku / stížnost na frontu („proč nehraje moje
   písničky?") DJ zodpoví podle skutečného stavu (u displeje jsou „moje"
   všechna přání z displeje). Je-li v ní jméno, které potvrdí katalog
   („proč nehraješ Kabát?", „kdy bude Bohemian Rhapsody", „nefér, chci
   Olympic"), je to přání: zařadí se (u stížnosti s „Beru to jako přání.");
   je-li to už ve frontě, řekne kdy.
7. **Podkres po přáních.** Když už žádné přání nemá co hrát (jakkoli
   skončilo i poslední — splněné, nenalezené, chyba, otázka), jde podkres za
   naposledy splněným přáním, pokud se od té doby nezměnil jinak.
8. **Restart.** Přání se ukládají až po přehrávači (playback.json); co podle
   historie nebo playback.json mezitím dohrálo, je po obnově hotové —
   nezazní dvakrát. Starý klíč displeje v pořadí („panel-…") = místo „panel".

### E. Ovládání
| ID | Požadavek | Stav |
|---|---|---|
| E1 | Displej: co hraje, hrát/pauza, další, hlasitost | ✅ |
| E2 | Displej: síť (IP, Wi-Fi, přihlášení k Wi-Fi, QR na web) | ✅ |
| E3 | Web na síti: http://192.168.0.24:8765 | ✅ (bez hesla — viz G) |

### F. Provoz a ladění
| ID | Požadavek | Stav |
|---|---|---|
| F1 | Logování všeho podstatného (přání, rozhodnutí, skladby, latence, zvuk, systém, panel) | 🔄 agenti A, B + DJ/katalog |
| F2 | Report pro ladění podle provozu | 🔄 agent A |
| F3 | Diagnostika jen podle dat | ✅ zásada |
| F4 | Mozek DJ bez studeného startu v práci: app-server Po–Pá 7–19 běží dál (když Pi zbývá ≥ 250 MB MemAvailable, měřeno s běžícím app-serverem, kontrola každou minutu mimo tah — pod tím se hned ukončí); jinak studený start dostane +15 s k 25 s rozpočtu. Vypršený rozpočet = „DJ nestihl odpovědět", ne „síť" | ✅ (26. 9. 9:23 chyba „síť" po studeném startu) |

### G. Bezpečnost a přihlášení (na konec)
| ID | Požadavek | Stav |
|---|---|---|
| G1 | Pi nezávislé na notebooku: vlastní Codex ✅, vlastní YouTube 📋 (`packaging/rpi/youtube-login.sh`) | |
| G2 | Web v kanceláři: kdo smí ovládat, jména, limity | 📋 |
| G3 | Heslo Wi-Fi ne v argumentech procesu; panel jako root; úklid SSH tunelu | 📋 |
| G4 | Jednoduchá obnova přihlášení (skripty + návod) | ✅ skripty, 📋 ověřit |

### H. Hlasování, oblíbené a vyřazené
Přání vlastníka: černá listina na interpreta i konkrétní píseň, vždy s tím, kdo ji přidal; přidat
hrající skladbu / interpreta přímo z webu; seznam oblíbených; o vyřazení rozhoduje víc lidí.
Hlasy bez stavu navíc — stav se počítá z hlasů (`ytdj/votes.py`, tabulka `votes` ve state.db).

| ID | Požadavek | Stav |
|---|---|---|
| H1 | 👍/👎 skladbě, 👎 interpretovi; jeden hlas na člověka (id klienta, jméno = přezdívka přes kancelářský filtr), změna i stažení kdykoli; u každého hlasu kdo a kdy | ✅ backend (`/api/votes`), 📋 UI webu |
| H2 | Jiné nahrání / verze téže písně = tatáž píseň; interpret sedí na každého uvedeného („A, B & C", „feat.") | ✅ |
| H3 | Skladba vyřazená: ≥ `ban_song_votes` (2) lidí 👎 a víc 👎 než 👍; jeden 👎 = upozaděná; 👍 a víc 👍 než 👎 = oblíbená. Interpret vyřazený: ≥ `ban_artist_votes` (3) lidí 👎; oblíbený: ≥ `favourite_artist_votes` (2) různých lidí 👍 a víc 👍 než 👎 — jeden 👍 celého interpreta z něj oblíbeného neudělá (rozhodnutí vlastníka 26. 9.). Hlasy nestárnou, změnou hlasů se položka sama vrátí | ✅ |
| H4 | Podkres a DJ vyřazené nenabízí, upozaděné méně, oblíbené i dřív než po `repeat_days`; při vyřazení zmizí z fronty podkres (přání nikdy) a hrající podkres se přeskočí | ✅ |
| H5 | Výslovné přání vyřazené skladby / interpreta se splní — s poctivou poznámkou „(pozn.: vyřazená hlasováním — Petr, Jana)"; ze sady (interpret, oblíbené) vyřazené skladby vypadnou | ✅ |
| H6 | „Pusť oblíbené" / „oblíbené kanceláře" / „pusť moje oblíbené" bez modelu; DJ a rozjezd (C7) znají oblíbené a vyřazené kanceláře | ✅ backend, 📋 čip na webu |
| H7 | Ochrana: jeden hlas na klienta, nejvýš 30 hlasů / 10 min; telemetrie `vote.*` | ✅ |

## Fáze

| Fáze | Obsah | Výstup / akceptace |
|---|---|---|
| **1 (běží)** | DJ výklad přání + režim interpreta; logování (jádro, panel, DJ, katalog); chytrý start (modul) | testy všech scénářů C2; reálný prompt na Pi → ▶ odpovídá přání; report funguje na Pi |
| **2** | Fronta přání pro víc lidí (D1–D6); zapojení chytrého startu (C7) do webu i displeje; UI fronty | testy fronty (souběh 3 lidí, spravedlnost, „zařadit hned"); na Pi 3 souběžná přání bez chyby |
| **3** | Kvalita: lupání (B2) do nuly; latence (B1) podle reportu; volba modelu (C6) | cíle z tabulky B splněny podle telemetrie |
| **4** | Nezávislá revize celé aplikace: kancelářské scénáře, výpadky sítě/Codexu/YouTube, restart, UX | seznam nálezů → opravy → znovu revize, dokud nic podstatného |
| **5** | Pilot v kanceláři 1–2 dny, ladění podle reportu | report bez nevysvětlených chyb; žádná ostuda |
| **6** | Bezpečnost a přihlášení (G), finální dokumentace, záloha SD karty | vše v G ✅ |

## Jak se pracuje

- Orchestrátor zadává práci Opus agentům s jasným vlastnictvím souborů,
  přebírá jen ověřené výsledky (testy, čtení diffu, měření na Pi, reálný
  scénář), neuspokojivé vrací.
- Nasazení na Pi: jen commitnutý kód, před restartem zkušební import celého
  ytdj přímo na Pi; restarty hudby co nejméně.
- Každý nález a oprava se dokládá daty (log, telemetrie, měření).
