# Všechno, co vlastník chtěl — kontrolní seznam

Úplný seznam požadavků vlastníka (25.–26. 9. 2026) s aktuálním stavem.
Aktualizuje se při každém nasazení. Podrobná pravidla a cíle jsou
v [PLAN.md](PLAN.md); tady je jen „na nic nezapomenout".

Stav: ✅ hotovo a ověřeno na Pi · ☑️ hotovo v kódu, čeká na nasazení ·
🔄 rozpracováno · 📋 čeká · ⚠️ známý problém

Stav k 27. 9. 2026 (na Pi běží 1710010).

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
| 14 | Rychle přeskakovat, i víc skladeb (10×) po sobě | ✅ | příprava skladby 7,4 → 3,5–4,2 s (22:03, Premium 774 zachován); postupná příprava 3 hned + po jedné až 10; nepřipravená nečeká na rozdělanou |
| 15 | Žádné lupání | 🔄 nasazeno 16:58, měří se | výpadky vznikaly v tichu mezi skladbami, když se další otevírala > 1 s (doloženo: počet výpadků = (čekání − 1 s) / 42,7 ms); zásobník 2 s. ⚠️ Pi hlásí podpětí při startu (0x50000) |
| 16 | (nález) Restart bez dlouhého ticha | ✅ 5,8 s (ráno ~26 s) | skladba pokračuje od místa; přehrávač startuje první; zbývá zrychlit start mpv (3,6 s při souběhu) |

## DJ — přání

| # | Požadavek | Stav | Doklad / kde |
|---|---|---|---|
| 17 | „Musí hrát to, co mu user píše"; interpret = víc jeho písniček, ne jedna | ✅ | rychlá cesta bez modelu, režim interpreta |
| 18 | Rozumět i překlepům („z nouze cnost" → Znouzectnost, „Vojtano") | ✅ | |
| 19 | Přání přijmout hned, žádné „DJ ještě dokončuje…" | ✅ | přijetí 0,01–0,14 s |
| 20 | Lepší model, když je potřeba | ✅ | úsilí low (A/B 16 skutečných vět: stejný výklad, medián 7,2 → 5,8 s); zahřátý 2 h po přání; 5 tahů na vlákno |
| 21 | Play bez přání = výběr podle času, dne, kanceláře, historie | ✅ | chytrý start |
| 22 | DJ nesmí slibovat, co neumí („nastavím střídání") | ✅ nasazeno 11:29 | odpověď se skládá z toho, co se opravdu zařadilo |
| 23 | Otázky a stížnosti („proč nehraje moje…", „to není demokracie") zodpovědět podle skutečnosti | ✅ | přání uvnitř stížnosti („nefér, chci Olympic") se zařadí (15:42) |
| 24 | „Chci oboje" / střídání dvou interpretů | ✅ nasazeno 11:29 | |
| 25 | „Ať to není ostuda": sprostá jména a texty se na displeji a webu neukážou | ✅ | `ytdj/display.py` |
| 46 | Volná přání přes model rychleji („zahraj mi něco veselého k práci" ~10–12 s, po pauze 23–33 s) | ☑️ v kódu, čeká na nasazení | rozbor Pi 26. 9.: studený start 4,8–12,4 s (9 z 18 tahů), nové vlákno +2,3 s, uvažování modelu 1,6–8,4 s, výpis odpovědi ~2,3 s, dohledání semínek ~1,4 s, rádio ~1,5 s, příprava první skladby ~7 s. Nahřátí při psaní přání a po startu (F-PROVOZ-08), náhradní vlákno (F-PROVOZ-09); volba vlastníka 26. 9.: zahřátý 2 h po každém přání, 5 tahů na vlákno, úsilí „low" (A/B na Pi: stejné porozumění, model medián 7,2 → 5,8 s), displej nahřívá při otevření přání |
| 47 | Na webu stránka s rychlým názorným návodem pro běžného uživatele + dokumentace „jak to funguje" se všemi automatikami, vhodně vyvážená, dohledatelná, pravidelně aktuální | ✅ 23:05 | `/napoveda` (2 min, příklady ověřené testem) a `/jak-to-funguje` (živě z FUNKCE.md: 159 pravidel, hledání, „Automatiky, o kterých možná nevíš"); odkaz „?" v hlavičce webu |
| 48 | Import playlistů jako oblíbené kanceláře: každý předá své preference odkazem na playlist, bez vypisování; písničky patří k oblíbeným kanceláře | ✅ 27. 9. | Hlasování → Playlisty v oblíbených; import = 👍 pod přezdívkou, nic nepouští; bez limitu hlasů (pojistka 1000 písniček); „pusť oblíbené" střídá lidi; Obnovit/Odebrat |
| 49 | „Hraj to, co máme rádi, napřeskáčku interprety… a hraj pořád" — DJ má hrát z oblíbených (je tam hromada umělců), střídat interprety a nepřestat po pár skladbách | 🔄 hotovo v kódu, čeká na nasazení | 27. 9. 00:59 DJ nepoznal oblíbené a hrál 2 interprety z historie → DJ (model) chápe přání oblíbených podle smyslu (akce oblíbené: čí, pořád, napřeskáčku; ne seznam frází — vlastník: „chci, aby chápal, co mu user napíše“) + režim oblíbených v podkresu do dalšího přání; FUNKCE F-FRONTA-20, F-HLASY-19/20 |

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
| 34 | Dotyk: „je utrpení na něco kliknout, trefit" | ✅ 27. 9. 03:15 — vlastník: „šlo to teď naposled docela dobře všude… fungovalo to přijatelně" | tlačítko podle místa, kde prst ležel; kalibrace s mřížkou; Test dotyku a Test prstem; 27. 9. čtení podle datasheetu XPT2046 (ustálení 100 µs, 1 MHz, medián 7): chyba u okraje ~3× menší, rozptyl ve stisku ~10× menší, vlastník: „teď je ten dotyk celkem dobrej"; 02:48 robustní kalibrace (okrajové křížky 2×, oprava okraje max 15 px) a „Zpět" dole velké; 03:08 kalibrace prstem + Test prstem: uprostřed 3–14 px, horní okraj čte ~+19 px níž (mez opravy okraje 15 px zahodila opakovatelně naměřených ~20 px; přepočet bez meze by dal ~+2 px) — vlastník změnu meze nechtěl, knoflíky nahoře se trefují |
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
| 45 | „Co hraje dál a nemá to u sebe moje jméno?" | ✅ | „Rádio podle přání X · nálada" na webu i displeji |
| 50 | „Nechci, aby se učil konkrétní fráze a podle nich pak něco dělal. Chci, aby chápal, co mu user napíše, a choval se podle toho.“ | ✅ pravidlo (27. 9.) | `docs/PLAN.md` Vize 5; oblíbené vykládá model (F-HLASY-19), bez modelu jen jednoznačné povely a jména |
| 51 | „A jak to vidíš s tou bezpečností" → „Oprav všechny body, které můžeš" (27. 9.) — web, Wi-Fi, sudo, sandbox | ✅ nasazeno 27. 9. 04:45 | nebezpečné klíče (mpv_extra_args, cookies, web_host…) jen v config.toml; nastavení a restart s PINem správce (displej: Síť), brzda 5 špatných / 5 min — F-BEZP-08 až F-BEZP-11, `tests/test_admin.py`; heslo Wi-Fi přes stdin (F-BEZP-06); ytdj s NoNewPrivileges — sudo z ytdj/Codexu na Pi ověřeně nejde, mpv dál nice −11; bubblewrap na Pi, sandbox Codexu ověřen (zápis → Read-only file system), přání přes model 9,0 s (F-BEZP-07); z LAN ověřeno: bez PINu 401, špatný 403, web_host i s PINem 400 |
| 52 | „Udělat tam issues, aby kolegové mohli ty věci reportovat a navrhovat, aniž bych to musel přepisovat… všechno tam dej a pak k tomu piš, jak to bylo vyřešeno" (vlastník) | ✅ nasazeno 6. 10. 18:44 | stránka „Chyby a nápady“ (`/hlaseni`, 💡 v hlavičce): položka, komentář, +1 bez přihlášení; stav a „Jak to bylo vyřešeno“ s PINem správce; #52–#62 v `ytdj/data/issues_seed.json`; `python -m ytdj.issues` — F-HLASENI-01 až 10, `tests/test_issues.py`; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 53 | Kolegovi to nenašlo píseň „Nerdící v neklidu" od kapely Poledníci — „to je problém" | ✅ nasazeno 6. 10. 18:44 | v kódu, čeká na večerní nasazení — model teď dostává podklad z katalogu a název, který není kapela, ale skladba, zahraje jako skladbu (F-PRANI-19, F-PRANI-20); skutečný model změřit `tests/model_wish_check.py`; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 54 | Možnost posouvat se v písničce, tam a zpět | ✅ nasazeno 6. 10. 18:44 | Web: lišta průběhu je posuvník a vedle ní −10 / +10 s; v rádiu posouvá kdokoli, v přání jen jeho autor (F-ZVUK-26, F-ZVUK-27). Na displej u repráku se tlačítka posunu bezpečně nevešla (vedle Další) — návrh čeká na rozhodnutí vlastníka; vlastník 6. 10.: „Posun stačí jen na webu, na displeji není nutný" — na dotykovém displeji se nedělá; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 55 | Mít možnost nechat zahrát to, co bylo přehráno | ✅ nasazeno 6. 10. 18:44 | Web: u každé skladby v Odehráno tlačítko „Zahrát znovu" — přání přesně té skladby, do fronty jako každé jiné; vyřazenou hlasováním nezařadí (F-PRANI-28, F-PRANI-29); na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 56 | Možnost kliknutím přehrát z playlistu i písničku, co teprve bude | ✅ nasazeno 6. 10. 18:44 | Volba vlastníka „Hned, ale férově": ťuknutí na obal v Hraje dál pustí skladbu hned, když se tím nikdo nepředběhne (vlastní přání, rádio); přeskočené zůstávají ve frontě, cizí přání se neutne (F-FRONTA-22 až F-FRONTA-24); na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 57 | Když uživatel zadá, že chce hrát jen jedno album, DJ se toho nedrží a hraje i jiná alba | ✅ nasazeno 6. 10. 18:44 | v kódu, čeká na večerní nasazení — album jako podkres: první 4 skladby jako přání, zbytek v pořadí alba (F-PRANI-21, F-FRONTA-21); na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 58 | Přehrávat i videa z YT Music na telce připojené přes HDMI; jinak by tam ukazovalo písničku, co hraje | 🔄 nasazeno 6. 10. 18:44, na telce zatím není obraz | Etapa 1 (volba vlastníka „začít obrazovkou právě hraje"): samostatná obrazovka na telce — obal, název, od koho, co dál, QR na web (F-TV-01 až F-TV-06, služba `ytdj-tv`, bez změny nastavení Pi). Video až po změření na Pi, pokud vůbec: dnes má Pi úsporný firmware bez dekodéru (gpu_mem=16), hraje hlavně písničky bez klipu (86 % z 80 vzorků) a klip bývá jiná nahrávka než písnička. Na Pi večer ověřit: skutečný obraz na telce, telka zapnutá až po startu Pi, konzole na téže obrazovce; služba ytdj-tv kreslí 720×480, ale firmware při startu telku neviděl a zvolil kompozitní výstup (dispmanx display:3) — nutný restart Pi se zapnutou telkou nebo hdmi_force_hotplug |
| 59 | Pamatovat si aktuální stav, aby to i po vypnutí a opětovném startu pokračovalo tam, kde to bylo | 🔄 nasazeno 6. 10. 18:44, čeká na zkoušku s restartem celého Pi | Volba vlastníka „Obnovit, ale mlčet" a „Jen týž den": po vypnutí / výpadku proudu se vrátí přání, podkres i místo ve skladbě (~15–25 s) a čeká se na ▶ (F-RESTART-02, -03, -11 až -14). Z Pi: od 28. 9. tři zapnutí, všechna po tvrdém výpadku proudu, pokaždé se zahodilo všechno (1. 10. po 3 minutách bez proudu přání i režim interpreta). Na Pi večer ověřit: `request.resume` po `sudo reboot` uprostřed skladby, ▶ naváže na místo; restart služby navázal na stejnou skladbu (pozice 275,6 s), frontu i podkres |
| 60 | PRIORITA vlastníka: „potřebuju, aby to srovnávalo hlasitost písniček na stejnou úroveň" — každá písnička je vytvořená s jinou hlasitostí | ✅ nasazeno 6. 10. 18:44 | Každá skladba dostane jeden pevný zisk na −14 LUFS podle hlasitosti, kterou o ní má YouTube (na 18 skladbách se od měření ffmpeg ebur128 liší nejvýš o 0,06 dB); dynamika se nemění, hlasitost z kolečka/webu/displeje taky ne (F-ZVUK-24, F-ZVUK-25; nastavení „Srovnávat hlasitost skladeb" a „Cílová hlasitost"). Změřeno 6. 10. na notebooku: 18 skladeb (Metallica −3,5 LUFS … Satie −17,9 LUFS) rozptyl 14,4 LU → 0,3 LU; celý řetěz přehrávače (mpv do souboru, Premium formát, 4 skladby): −3,5 / −17,9 / −8,0 / −16,0 LUFS → −14,0 / −14,3 / −14,0 / −14,1. Na Pi ověřit po nasazení: `track.gain` u každé skladby, čas startu a výpadky (xruny) beze změny; na Pi ověřeno: první nově načtená skladba −9,1 LUFS → −4,9 dB (via json, state ok), žádná chyba filtru, start skladby 769 ms, od restartu bez xrunů |
| 61 | Ruční přepínání vizuálu v módu den/noc | ✅ nasazeno 6. 10. 18:44 | Web: přepínač „Vzhled" dole na každé stránce (Auto / Den / Noc), pamatuje si ho zařízení (F-WEB-09, F-WEB-10); displej u repráku denní/noční vzhled nemá — nic se na něm neměnilo; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 62 | Kolega chtěl „nejvulgárnější a nejsprostší prasárnu, co DJ zná" a pustilo mu to Mötley Crüe — čekal spíš Záviše a píseň s vulgárními texty; „prověř to" | ✅ nasazeno 6. 10. 18:44 | v kódu, čeká na večerní nasazení — filtr explicitních se zvedne jen pro výslovně vulgární přání a jeho podkres (F-ZVUK-11, pole `explicit_ok`); u textových přání DJ sahá nejdřív po českých interpretech; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 63 | „Minimálně v UI bych to přejmenoval na Jukebox a ne ytdj — i když DJ bych tam nechal jako roli dál" (vlastník) | ✅ nasazeno 6. 10. 18:44 | Na webu, displeji a ve znění pravidel „Jukebox" místo „ytdj"; DJ zůstal jako role; technická jména (adresa ytdj.local, služba, příkazy) se neměnila; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 64 | „Nebylo by od věci, kdyby se DJ, pokud si není jistý, co user myslí (viz Nerdíci v neklidu), mohl usera zeptat nebo ho to nechat upřesnit, třeba jestli myslí kapelu nebo písničku" (vlastník) | ✅ nasazeno 6. 10. 18:44 | v kódu, čeká na večerní nasazení — otázka s 2–3 možnostmi na jedno ťuknutí (web i displej), do 25 s jinak DJ vezme nejpravděpodobnější a řekne to; nic nezdrží, nejvýš jedna otázka (F-PRANI-22 až F-PRANI-27); jak často se ptá skutečný model, změřit `tests/model_wish_check.py`; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 65 | „ytdj.local je ok, ale bylo by fajn, kdyby bylo i jukebox.local" (vlastník) | ❌ nasazeno 6. 10. a vypnuto | služba `ytdj-mdns-alias` vyhlásí jukebox.local a při změně adresy ho obnoví (F-SIT-05, `tests/test_mdns_alias.py`); na Pi doinstalovat avahi-utils; služba ytdj-mdns-alias jméno nevyhlásila (avahi-publish: D-Bus chyba pod DynamicUser) a po jejím spuštění se z notebooku přestalo překládat i ytdj.local; po vypnutí se překlad do 10 s vrátil — udělat jinak |
| 66 | „Teď jsem dal přání ‚Zahraj největší pecky od Foo Fighters' a zahrálo to Pecka od Divokej Bill. To není to, co jsem si přál. Špatně to vyhodnotil." (vlastník) | ✅ nasazeno 6. 10. 18:44 | v kódu, čeká na večerní nasazení — rychlá cesta přijme jen skladbu, kterou katalog potvrdí u jmenovaného interpreta; když ji nemá, nic nedosazuje a přání jde k DJovi (modelu) s kandidáty z katalogu (F-PRANI-07 upřesněno); odmítnutí stojí 4 dotazy místo 11; na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |
| 67 | „Když dám přehrát hudbu oblíbenou v kanceláři, tak by ji to chtělo asi náhodně promíchat, protože takhle ji to hraje pokaždé ve stejném pořadí." (vlastník) | ✅ nasazeno 6. 10. 18:44 | na hlasech z Pi: první skladba stejná ve 30 z 30 spuštění (šly napřed ty s nejvíc 👍); nově náhodné pořadí při zachování stejného dílu na člověka — 26 různých prvních skladeb z 30, žádná v první osmičce víc než 10× (F-HLASY-17); na Pi po nasazení: web a stránky odpovídají (titulek Jukebox, 16 hlášení), chování přání v provozu se ukáže používáním |

## Na konec: bezpečnost a přihlášení (připomenout)

- Vlastní přihlášení YouTube na Pi (teď cookies z notebooku); možná osiřelá relace „Chrome on Linux" v Google účtu.
- Web bez hesla: kdokoli v síti ovládá hudbu (záměr); nastavení a restart s PINem správce — ✅ #51. Zbývá: web bez HTTPS (PIN jde sítí čitelně); PIN si přečte i proces na Pi pod uživatelem lacina.
- ✅ Heslo Wi-Fi z displeje už není v seznamu procesů (#51).
- Panel běží jako root (kvůli /dev/mem) — ponecháno, omezený sandboxem systemd.
- ✅ bubblewrap na Pi, sandbox Codexu ověřen; ✅ ytdj bez sudo (NoNewPrivileges) (#51).
- ✅ Starý SSH tunel na notebooku neběží (ověřeno 27. 9.).
