# Jak to funguje — úvody k oblastem

Z tohohle souboru a z `docs/FUNKCE.md` se skládá stránka webu
**Jak to funguje** (`/jak-to-funguje`). Pravidla samotná jsou jen ve
FUNKCE.md a na stránku se dostanou sama; tady je jen to, co k nim patří
navíc:

- **úvod ke každé oblasti** — nadpis `##` přesně jako ve FUNKCE.md, pod ním
  2–4 věty pro kolegu: *proč* to tak je. Nová oblast ve FUNKCE.md = nový
  úvod tady (hlídá `tests/test_manual.py`);
- **Automatiky, o kterých možná nevíš** — věci, které aplikace dělá sama od
  sebe; řádek `- **ID** krátký nadpis`, text pravidla se vezme z FUNKCE.md;
- **Nastavení u pravidel** — pravidlo, jehož čísla jdou změnit v Nastavení
  jukeboxu, ale klíč v jeho textu není (`- ID: klíč, klíč`). Klíče zapsané
  v textu pravidla (`` `wish_block` ``) se najdou samy.

Text před prvním `##` (tenhle) se na stránce neukazuje. Piš krátce,
přátelsky a pravdivě — jen co platí podle pravidel a testů.

## Přehrávání a zvuk

Hraje přehrávač mpv a fronta, kterou vidíš na webu i na displeji, je přesně
jeho playlist — žádná kopie, která by se mohla rozejít se skutečností.
Skladby se připravují dopředu, aby Další bylo rychlé a mezi skladbami nebylo
ticho ani lupání.

Když si o nic konkrétního neřekneš, hraje studiová verze, ne live, cover ani
výběrovka. A jiná kapela se stejně pojmenovanou písní nikdy — radši nic než
špatná skladba.

## Hlasitost

Jedna hlasitost pro všechno: web, displej, kolečko na repráku i přání
„hlasitěji". Strop je 100, aby repro nikdo omylem nepřepálil, a hodnota
přežije restart — služba se nevrátí na plný výkon.

## Přeskakování

Další má být okamžité, i když ho někdo zmáčkne desetkrát za sebou. Každé
přeskočení se připíše tomu, kdo ho zmáčkl: když tvé přání přeskakují
ostatní, kancelář tím říká, že se jí nelíbí, a přání skončí dřív. Tvoje
vlastní Další jen přejde na další skladbu tvého přání.

## Přání a jejich výklad (DJ)

Hlavní smysl jukeboxu: zahrát přesně to, co si kdo přeje. Přání se přijme
hned. Jednoduchá přání — interpret, písnička, odkaz z YouTube, povel — vyřídí
aplikace sama bez modelu; volný text („něco klidného na odpoledne") domýšlí
DJ, tedy model Codex, a to trvá pár vteřin.

Co se nenajde, DJ nezahraje a přizná to. Potvrzení („Zařadil jsem: …") skládá
aplikace z toho, co se opravdu zařadilo, takže DJ nemůže slíbit něco, co
neumí.

## Fronta a férovost

V kanceláři je víc lidí a jeden repro, takže se přání střídají: na řadě je
ten, kdo nejdéle nic svého neslyšel, a každý dostane kolo pár skladeb.
Dlouhé přání (třeba celý interpret) ostatní nezablokuje — hraje po kouscích
mezi jejich přáními a dohraje, až nikdo nečeká.

Když si nikdo nic nepřeje, hraje podkres (rádio), který navazuje na poslední
přání. Cizí přání nikdo neutne a odebrat ho může jen jeho autor — ani
automatika ne.

## Přezdívky a identita

Přezdívka je jen popisek, aby bylo vidět, čí písnička hraje. Člověka pozná
aplikace podle prohlížeče (nebo relace na displeji), ne podle jména ani
adresy — dva kolegové za jednou sítí jsou dva lidé a nikdo nepřevezme cizí
přání tím, že si dá stejné jméno.

## Hlasování

👍 a 👎 říkají DJovi, co kancelář ráda a co ne. Jeden hlas nic nezakáže:
vyřazení potřebuje víc lidí, aby jeden nespokojený kolega neumlčel cizí
oblíbenou písničku. Hlasy řídí podkres; výslovné přání se splní vždycky,
jen s poznámkou, kdo skladbu vyřadil.

## Displej

Dotykový displej u repráku je pro toho, kdo zrovna stojí u něj: co hraje,
Hrát/Pauza, Další, hlasitost a přání z klávesnice. Celý displej se v pořadí
počítá jako jeden člověk, aby neměl přednost před lidmi u počítačů. Když
dlouho nic nehraje, ztlumí se a ukáže QR kód na web.

## Web

Web vidí všichni stejně a živě — co hraje, co bude dál a čí přání čekají.
Do telefonu jde zabalený, a když se jukebox aktualizuje, stará otevřená
stránka se obnoví sama. Tlačítko Stop na webu není, jen pauza, aby nikdo
omylem nesmazal ostatním přání.

## Síť a Wi-Fi

Jukebox jde připojit k síti bez klávesnice a monitoru: na displeji se vybere
Wi-Fi a zadá heslo. Přehled sítě ukáže adresu webu a QR kód, i když samotná
hudební služba zrovna neběží.

## Výpadky (YouTube, Codex, síť)

Na YouTube, síť ani model DJe se nedá spolehnout vždycky. Když něco vypadne,
nic se nemaže: fronta i přání počkají, aplikace zkouší spojení znovu a po
obnově pokračuje. Bez modelu dál fungují přání interpreta a písničky
a tlačítka nálad; posluchač nikdy neuvidí surovou chybovou hlášku.

## Restart a obnova

Po restartu služby (aktualizace, chyba) hudba pokračuje tam, kde byla — od
stejného místa skladby a se stejnou frontou přání. V noci (22–7 h) nebo po
zapnutí celého Pi se ale sama nerozehraje, aby jukebox nezačal hrát
v prázdné kanceláři.

## Chytrý start

Když nic nehraje a někdo zmáčkne Hrát, DJ vybere hudbu sám: podle času, dne
v týdnu, českých svátků a toho, co tu v podobnou dobu hrálo dřív. Je to jen
rozjezd — první přání posluchače ho hned nahradí.

## Kancelářská slušnost (filtr slov)

Displej visí na zdi a web vidí všichni, takže sprostá slova v přezdívkách
a textech přání se nezobrazí. DJ přitom dostane přání tak, jak bylo napsané
— filtr jen skrývá, co se ukazuje.

## Provoz (logování, report, paměť)

Aby šlo zjistit, proč se něco stalo (lupnutí, pomalé přání), zapisuje
aplikace provozní log a měří časy. DJ (Codex) se drží „zahřátý" v pracovní
době a po přáních, aby odpovídal rychle, ale jen dokud má Pi dost paměti.
Pomalá SD karta nesmí zastavit hudbu ani web.

## Bezpečnost (současný stav — k rozhodnutí)

Heslo k Wi-Fi a přihlašovací tokeny se nikde neukazují ani nelogují. Přání
může odebrat jen jeho autor. DJ (model) nemá žádné nástroje — nemůže
spouštět příkazy ani sahat na soubory.

## Automatiky, o kterých možná nevíš

- **F-RESTART-01** V noci a po zapnutí Pi se hudba sama nerozehraje
- **F-RESTART-03** Po restartu služby skladba pokračuje od místa, kde byla
- **F-RESTART-02** Přání přežijí restart služby
- **F-HLAS-05** Hlasitost si jukebox pamatuje i přes restart
- **F-FRONTA-02** Dlouhé přání má rozpočet skladeb, pak pustí ostatní
- **F-FRONTA-11** Kdo dlouho nic neslyšel a ozve se, jde hned na řadu
- **F-FRONTA-13** Podkres navazuje na poslední splněné přání
- **F-FRONTA-14** Interpret v podkresu po přání jen omezeně dlouho
- **F-FRONTA-10** Tatáž skladba ve dvou přáních zazní jen jednou
- **F-PRESKOK-04** Když tvé přání přeskakují ostatní, skončí dřív
- **F-HLASY-06** Vyřazená skladba hned zmizí z podkresu
- **F-ZVUK-11** Vulgární skladby se samy do podkresu nedostanou
- **F-ZVUK-12** Nepřehratelná skladba se na týden vynechá
- **F-ZVUK-15** Automatika nikdy sama nepřeskočí skladbu bez důvodu
- **F-SLUSNOST-01** Sprostá slova se na displeji a webu nezobrazí
- **F-VYPADEK-01** Při výpadku YouTube nebo sítě se čeká, nic se nemaže
- **F-VYPADEK-04** Když nejede DJ, jednoduchá přání jedou dál
- **F-PROVOZ-05** DJ se drží zahřátý, aby odpovídal rychle
- **F-PROVOZ-08** DJ se nahřívá už ve chvíli, kdy začneš psát přání
- **F-DISPLEJ-06** Displej se po 5 minutách ticha ztlumí a ukáže QR
- **F-START-04** Úmyslnou pauzu nové přání samo nezruší
- **F-WEB-03** Stará otevřená stránka se po aktualizaci obnoví sama

## Nastavení u pravidel

- F-FRONTA-01: wish_block, wish_shared_block
- F-FRONTA-02: wish_budget, wish_budget_panel
- F-HLASY-03: ban_song_votes
- F-HLASY-04: ban_artist_votes, favourite_artist_votes
- F-HLASY-05: repeat_days
- F-SLUSNOST-01: display_filter
