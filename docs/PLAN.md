# ytdj jukebox — požadavky a plán

Jediný zdroj pravdy pro vývoj kancelářského jukeboxu ytdj na Raspberry Pi.
Sestaveno ze zadání vlastníka (25. 9. 2026). Každý agent i každá změna se
měří podle tohohle dokumentu.

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
| C4 | Poctivé odpovědi (neslibovat, co nehraje; „nenašel jsem") | 🔄 agent DJ |
| C5 | Přání posluchače má vždy přednost před automatickými zásahy | ✅ (web ruší auto-přeseedování) |
| C6 | Když je potřeba lepší model, nasadit lepší | 🔄 posudek agenta DJ |
| C7 | „Hrát" bez přání a bez fronty = playlist podle času, dne, kanceláře a historie | 🔄 agent context |
| C8 | Učit se z přeskočení/dohrání bez přebíjení přání | 📋 |

### D. Víc lidí v kanceláři
| ID | Požadavek | Stav |
|---|---|---|
| D1 | Fronta přání se jménem autora | 📋 fáze 2 |
| D2 | Spravedlivé střídání mezi lidmi | 📋 |
| D3 | Volitelně „zařadit hned" (za právě hrající, bez přerušení) | 📋 |
| D4 | Přání se přijmou hned, žádné „DJ ještě dokončuje…" | 📋 (částečně ✅ C5) |
| D5 | Fronta vidět na webu i displeji; autor může své přání odebrat | 📋 |
| D6 | Automatické změny nálady jen v podkladovém rádiu, nikdy nemažou přání | 📋 |

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

### G. Bezpečnost a přihlášení (na konec)
| ID | Požadavek | Stav |
|---|---|---|
| G1 | Pi nezávislé na notebooku: vlastní Codex ✅, vlastní YouTube 📋 (`packaging/rpi/youtube-login.sh`) | |
| G2 | Web v kanceláři: kdo smí ovládat, jména, limity | 📋 |
| G3 | Heslo Wi-Fi ne v argumentech procesu; panel jako root; úklid SSH tunelu | 📋 |
| G4 | Jednoduchá obnova přihlášení (skripty + návod) | ✅ skripty, 📋 ověřit |

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
