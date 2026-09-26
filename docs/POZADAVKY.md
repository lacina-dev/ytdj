# Všechno, co vlastník chtěl — kontrolní seznam

Úplný seznam požadavků vlastníka (25.–26. 9. 2026) s aktuálním stavem.
Aktualizuje se při každém nasazení. Podrobná pravidla a cíle jsou
v [PLAN.md](PLAN.md); tady je jen „na nic nezapomenout".

Stav: ✅ hotovo a ověřeno na Pi · ☑️ hotovo v kódu, čeká na nasazení ·
🔄 rozpracováno · 📋 čeká · ⚠️ známý problém

Stav k 26. 9. 2026 17:00 (na Pi běží 5d7e1ca).

## Zařízení a systém

| # | Požadavek (slovy vlastníka) | Stav | Doklad / kde |
|---|---|---|---|
| 1 | Nainstalovat OS na Pi 3B, zvolit ho tak, „aby s ničím nebyl problém" | ✅ | Raspberry Pi OS Lite 64-bit (Trixie) |
| 2 | Rozchodit displej KeDei 3.5" v6.2 na GPIO | ✅ | vlastní ovladač `ytdj/panel/kedei.*` |
| 3 | Najít krabičku pro 3D tisk | ✅ rešerše | Diamant Edition (printables 225813), Helska (thingiverse 3025088), úchyty KeDei (printables 445996) — výběr na vlastníkovi |
| 4 | „Jednoúčelové zařízení", nespouštět zbytečné služby, vejít se do paměti | ✅ | `packaging/rpi/slim.sh`; volno 430–470 MB RAM za provozu |
| 5 | Pi nezávislé na notebooku: vlastní přihlášení Codexu | ✅ | znovu přihlášeno 26. 9. ~8:20 (token zneplatněn v noci) |
| 6 | Pi nezávislé na notebooku: vlastní přihlášení YouTube | 📋 G | zatím cookies z notebooku; `packaging/rpi/youtube-login.sh` |
| 7 | „Jednoduchá cesta", jak přihlášení obnovit | ✅ skripty, 📋 ověřit YouTube | `packaging/rpi/codex-login.sh`, `youtube-login.sh` |
| 8 | Web dostupný pro kolegy na síti | ✅ | Pi je na Wi-Fi: http://192.168.88.234:8765 (adresa se mění se sítí; aktuální ukazuje síťová obrazovka a QR na displeji) |

## Přehrávání a zvuk

| # | Požadavek | Stav | Doklad / kde |
|---|---|---|---|
| 9 | Ovládat ytdj z displeje: co hraje, play/pauza, hlasitost | ✅ | |
| 10 | Hlasitost propojená s kolečkem soundbaru, pamatuje si poslední hodnotu | ✅ | |
| 11 | Podržené −/+ na displeji mění hlasitost plynule | ✅ | |
| 12 | Bez zpoždění a „přepínání", začátek skladby bez divností, čas od nuly | ✅ | |
| 13 | Teploměr hlasitosti se kreslí správně | ✅ | |
| 14 | Rychle přeskakovat, i víc skladeb (10×) po sobě | ✅ nasazeno 16:58, měří se | připravená skladba ~0,6 s; postupná příprava 3 hned + po jedné až 10 (hloubka fronty 11); nepřipravená už nečeká na rozdělanou (druhé vlákno) |
| 15 | Žádné lupání | 🔄 nasazeno 16:58, měří se | výpadky vznikaly v tichu mezi skladbami, když se další otevírala > 1 s (doloženo: počet výpadků = (čekání − 1 s) / 42,7 ms); zásobník 2 s. ⚠️ Pi hlásí podpětí při startu (0x50000) |
| 16 | (nález) Restart bez dlouhého ticha | ✅ 5,8 s (ráno ~26 s) | skladba pokračuje od místa; přehrávač startuje první; zbývá zrychlit start mpv (3,6 s při souběhu) |

## DJ — přání

| # | Požadavek | Stav | Doklad / kde |
|---|---|---|---|
| 17 | „Musí hrát to, co mu user píše"; interpret = víc jeho písniček, ne jedna | ✅ | rychlá cesta bez modelu, režim interpreta |
| 18 | Rozumět i překlepům („z nouze cnost" → Znouzectnost, „Vojtano") | ✅ | |
| 19 | Přání přijmout hned, žádné „DJ ještě dokončuje…" | ✅ | přijetí 0,01–0,14 s |
| 20 | Lepší model, když je potřeba | ✅ | volné přání 11,7 s (Codex běží), ~25 s po pauze → v pracovní době zůstává zahřátý |
| 21 | Play bez přání = výběr podle času, dne, kanceláře, historie | ✅ | chytrý start |
| 22 | DJ nesmí slibovat, co neumí („nastavím střídání") | ✅ nasazeno 11:29 | odpověď se skládá z toho, co se opravdu zařadilo |
| 23 | Otázky a stížnosti („proč nehraje moje…", „to není demokracie") zodpovědět podle skutečnosti | ✅ | přání uvnitř stížnosti („nefér, chci Olympic") se zařadí (15:42) |
| 24 | „Chci oboje" / střídání dvou interpretů | ✅ nasazeno 11:29 | |
| 25 | „Ať to není ostuda": sprostá jména a texty se na displeji a webu neukážou | ✅ | `ytdj/display.py` |
| 46 | Volná přání přes model rychleji („zahraj mi něco veselého k práci" ~10–12 s, po pauze 23–33 s) | ☑️ v kódu, čeká na nasazení | rozbor Pi 26. 9.: studený start 4,8–12,4 s (9 z 18 tahů), nové vlákno +2,3 s, uvažování modelu 1,6–8,4 s, výpis odpovědi ~2,3 s, dohledání semínek ~1,4 s, rádio ~1,5 s, příprava první skladby ~7 s. Nahřátí při psaní přání a po startu (F-PROVOZ-08), náhradní vlákno (F-PROVOZ-09); úsilí modelu jen přepínač pro měření (`YTDJ_CODEX_EFFORT`) |

## Víc lidí v kanceláři

| # | Požadavek | Stav | Doklad / kde |
|---|---|---|---|
| 26 | Fronta přání pro víc lidí, volitelně „zařadit hned" | ✅ | „Hned po téhle skladbě" |
| 27 | Střídat se; „displej nesmí mít dlouhodobou přednost" | ✅ | rozpočet 4 skladby (displej 3), displej = 1 člověk; dvojité ťuknutí na Další už přání neukončí (15:42) |
| 28 | Nick při prvním otevření prohlížeče, pamatovat si ho | ✅ | |
| 29 | Obrazovka pro zadání přání na displeji | ✅ | klávesnice s diakritikou, rychlé volby |
| 30 | Síťová obrazovka: přihlášení k Wi-Fi, IP, jak se připojit | ✅ | + QR kód |
| 31 | Blacklist písně i interpreta, vždy vidět kdo přidal; vyřazení až hlasy víc lidí; oblíbené (whitelist) | ✅ | stránka Hlasování, 👍/👎 u hrající, fronty i odehraných |
| 32 | Hlasovat líbí/nelíbí i o celém interpretovi | ✅ | oblíbený interpret až od 2 lidí 👍 (práh v nastavení) |

## Displej a web

| # | Požadavek | Stav | Doklad / kde |
|---|---|---|---|
| 33 | Projít a vylepšit UI displeje i webu, s ohledem na 1 GB RAM | ✅ | obaly alb, QR „přání z mobilu", oznámení přání, hlasy; web gzip 110 → ~25 kB |
| 34 | Dotyk: „je utrpení na něco kliknout, trefit" | 🔄 nasazeno 16:08, měří se | ráno jen ~40 % dotyků → akce; nový filtr v ovladači, stisk podle dosednutí, mezery patří nejbližšímu tlačítku, orámování při dotyku, Kalibrace dotyku (síť → 5 křížků); vyhodnotí `python3 -m ytdj.panel.touchreport` |
| 35 | „Škoda, že není větší" | 📝 | UI je kreslené v PIL, větší HDMI displej (např. 7" 800×480) = jiný ovladač, ne přepis |

## Provoz a způsob práce

| # | Požadavek | Stav | Doklad / kde |
|---|---|---|---|
| 36 | Logovat vše podstatné pro pozdější ladění | ✅ | `python -m ytdj.telemetry report` |
| 37 | Používat aplikaci v praxi, být náročný, hledat chyby a opravovat | 🔄 průběžně | revize 26. 9. našla 10 chyb → opravy běží |
| 38 | Při testech neměnit hlasitost | ✅ pravidlo | |
| 39 | Tvrdit jen to, co doloží data | ✅ pravidlo | |
| 40 | Neposílat testovací přání do fronty, když se poslouchá; po testu uklidit | ✅ pravidlo (od 26. 9.) | |
| 41 | Bezpečnost a přihlášení vyřešit na konec a připomenout | 📋 | viz níž |
| 42 | „Aby to, co aplikace už umí, zůstávalo" — jasné funkce, žádný další požadavek je potichu nezmění | ✅ | `docs/FUNKCE.md`: 139 pravidel v 16 oblastech, každé s testem; `tests/test_funkce.py` hlídá, že žádné nezůstane bez testu; `CLAUDE.md`: změna jen se souhlasem vlastníka |
| 43 | Nápověda v poli přání ne „Olympic", ale např. „Zahraj mi něco veselého k práci" | ✅ | web i displej (15:42) |
| 44 | „Proč jen tři skladby? Jde to nastavit v nastavení?" | ✅ nasazeno 16:58 | nastavení jukeboxu: kolo 3 (2 když čekají jiní), rozpočet 4 (displej 3), interpret 12 — kvůli férovosti; výchozí hodnoty beze změny |
| 45 | „Co hraje dál a nemá to u sebe moje jméno?" | 🔄 web ✅ 16:58, displej s další verzí | „Rádio podle přání X · nálada" — podkres jde podle naposledy splněného přání |

## Na konec: bezpečnost a přihlášení (připomenout)

- Vlastní přihlášení YouTube na Pi (teď cookies z notebooku); možná osiřelá relace „Chrome on Linux" v Google účtu.
- Web bez hesla: kdokoli v síti ovládá hudbu; **nastavení a restart jukeboxu jsou otevřené** — před pilotem PIN nebo jen lokálně.
- Heslo Wi-Fi z displeje je krátce vidět v seznamu procesů.
- Panel běží jako root (kvůli /dev/mem).
- Na Pi chybí bubblewrap (sandbox Codexu).
- Na notebooku může běžet starý SSH tunel na web Pi.
