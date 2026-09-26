"""Nápověda a Jak to funguje (F-WEB-07, F-WEB-08, POZADAVKY #47).

Hlídá, aby obě stránky zůstaly pravdivé a aktuální: každá oblast FUNKCE.md
má úvod a je na stránce, odkazy na testy se neukazují, souhlas vlastníka
ano, čísla odpovídají nastavení, a příklady přání v Nápovědě aplikace
opravdu takhle chápe.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-manual-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ytdj import manual  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.agent import fastpath  # noqa: E402
from ytdj.agent.intent import favourites_request, local_command, meta_kind, norm  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import RE_URL  # noqa: E402

INDEX = ROOT / "ytdj/web/static/index.html"


def funkce() -> manual.Funkce:
    return manual.parse_funkce(manual.FUNKCE_MD.read_text(encoding="utf-8"))


def intro() -> manual.Intro:
    return manual.parse_intro(manual.INTRO_MD.read_text(encoding="utf-8"))


def docs_page(values: dict | None = None, labels: dict | None = None) -> str:
    return manual.render_docs(manual.DOCS_HTML.read_text(encoding="utf-8"), funkce(), intro(),
                              values, labels)


def examples() -> list[tuple[dict, str]]:
    src = manual.NAPOVEDA_HTML.read_text(encoding="utf-8")
    out = []
    # <li data-example=…><q>text</q>…</li> → text z <q>; <q data-example=…>text</q> → text
    for m in re.finditer(r"<(li|q)\s([^>]*data-example=[^>]*)>(.*?)</\1>", src, re.S):
        attrs = dict(re.findall(r'(data-[a-z]+)="([^"]*)"', m.group(2)))
        body = m.group(3)
        q = re.search(r"<q>(.*?)</q>", body, re.S)
        text = re.sub(r"<[^>]+>", "", q.group(1) if q else body).strip()
        out.append((attrs, attrs.get("data-text") or text))
    return out


class DocsFromFunkce(unittest.TestCase):
    """F-WEB-08: stránka Jak to funguje je FUNKCE.md, jen čitelně."""

    def test_every_area_has_an_intro_and_is_on_the_page(self):
        f, i = funkce(), intro()
        self.assertGreaterEqual(len(f.areas), 16)
        missing = [a.title for a in f.areas if len(i.areas.get(a.title, "")) < 80]
        self.assertFalse(missing, "Oblasti bez úvodu v docs/JAK-TO-FUNGUJE.md: " + ", ".join(missing))
        stale = sorted(set(i.areas) - {a.title for a in f.areas})
        self.assertFalse(stale, f"Úvody k oblastem, které ve FUNKCE.md nejsou: {stale}")
        page = docs_page()
        for a in f.areas:
            self.assertIn(f'id="{a.slug}"', page, a.title)
            self.assertIn(f'<span class="count">{len(a.rules)}</span>', page, a.title)
        for rid in (r.id for a in f.areas for r in a.rules):
            self.assertIn(f'id="{rid}"', page, rid)
        self.assertNotIn("<!--YTDJ:", page.replace("<!--YTDJ:CHYBI-->", ""))

    def test_test_references_are_hidden_numbers_and_consent_kept(self):
        page = docs_page()
        self.assertNotIn("tests/", page)
        self.assertNotIn("Testy:", page)
        self.assertNotIn("bez testu", page)
        # čísla z pravidel zůstávají
        self.assertIn("22–7 h", page)
        self.assertIn("nejvýš 15 min", page)
        # poznámky o souhlasu vlastníka i tabulka změn
        f = funkce()
        consents = [n for a in f.areas for r in a.rules for n in r.notes if "souhlasem vlastníka" in n]
        self.assertGreater(len(consents), 5)
        self.assertIn('class="note consent"', page)
        self.assertEqual(page.count("<tr><td>"), len(f.changes))
        self.assertGreater(len(f.changes), 5)
        self.assertIn('id="zmeny"', page)

    def test_parser_on_a_small_file(self):
        f = manual.parse_funkce(
            "# X\n\n## Jak s tím zacházet\n\n- **Pravidla** platí.\n\n## Oblast A\n\n"
            "- **F-A-01** První `wish_block` věta.\n"
            "  Změněno 1. 1. 2026 se souhlasem vlastníka: proto.\n"
            "  Upřesněno 3. 1. 2026 po revizi:\n"
            "    zalomeno.\n"
            "  Testy: `tests/x.py::A::test_a`\n"
            "- **F-A-02** Druhá (Přidáno 2. 1. 2026 na přání vlastníka: „ať“)\n"
            "  (bez testu: hardware)\n\n"
            "## Bez testu (hardware)\n\n- F-A-02 — displej.\n\n"
            "## Změny se souhlasem vlastníka\n\n| Datum | Pravidlo | Co |\n|---|---|---|\n"
            "| 1. 1. 2026 | F-A-01 | Proto. |\n")
        self.assertEqual([a.title for a in f.areas], ["Oblast A"])
        a1, a2 = f.areas[0].rules
        self.assertEqual(a1.text, "První `wish_block` věta.")
        self.assertEqual(a1.notes, ["Změněno 1. 1. 2026 se souhlasem vlastníka: proto.",
                                    "Upřesněno 3. 1. 2026 po revizi: zalomeno."])
        self.assertEqual(a1.keys, ["wish_block"])
        self.assertEqual(a2.text, "Druhá")
        self.assertEqual(a2.notes, ["Přidáno 2. 1. 2026 na přání vlastníka: „ať“"])
        self.assertEqual(f.changes, [("1. 1. 2026", "F-A-01", "Proto.")])

    def test_automatic_behaviours_section(self):
        f, i = funkce(), intro()
        self.assertGreaterEqual(len(i.auto), 10)
        unknown = [rid for rid, _ in i.auto if f.rule(rid) is None]
        self.assertFalse(unknown, f"Automatiky odkazují na neexistující pravidla: {unknown}")
        page = docs_page()
        self.assertIn("Automatiky, o kterých možná nevíš", page)
        # vlastníkův příklad: v noci se po restartu samo nerozehraje
        night = next(h for rid, h in i.auto if rid == "F-RESTART-01")
        self.assertIn("noci", night)
        self.assertEqual(page.count('<li class="auto">'), len(i.auto))

    def test_settings_show_the_current_value(self):
        f, i = funkce(), intro()
        for rid, keys in i.settings.items():
            self.assertIsNotNone(f.rule(rid), rid)
            for k in keys:
                self.assertIn(k, manual.SHOWN_KEYS, f"{rid}: {k} není číselné nastavení")
        labels = {"wish_block": "Přání: skladeb v jednom kole"}
        page = docs_page()
        self.assertIn("Přání", docs_page(labels=labels))
        self.assertIn("wish_block", page)  # klíč z textu F-FRONTA-18 i z F-FRONTA-01
        changed = docs_page({"wish_block": 5, "ban_song_votes": 3}, labels)
        li = re.search(r'<li class="rule" id="F-FRONTA-01".*?</li>', changed, re.S).group(0)
        self.assertIn("Přání: skladeb v jednom kole: <b>5</b> <small>(výchozí 3)</small>", li)
        li = re.search(r'<li class="rule" id="F-HLASY-03".*?</li>', changed, re.S).group(0)
        self.assertIn("<b>3</b> <small>(výchozí 2)</small>", li)
        self.assertIn("Nastavení jukeboxu", li)
        # texty a seznamy (cesty, sprostá slova navíc) se neukazují
        self.assertNotIn("display_blocklist", manual.SHOWN_KEYS)
        self.assertNotIn("cookies_file", manual.SHOWN_KEYS)

    def test_page_follows_funkce_without_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            fk, it = Path(d) / "FUNKCE.md", Path(d) / "JAK.md"
            fk.write_text("## Oblast\n\n- **F-O-01** Staré znění.\n  Testy: `x`\n", encoding="utf-8")
            it.write_text("## Oblast\n\nÚvod.\n", encoding="utf-8")
            pages = manual.Pages(fk, it)
            first = pages.get("jak-to-funguje")
            self.assertIn("Staré znění.", first.raw.decode())
            self.assertIs(pages.get("jak-to-funguje"), first)  # beze změny z paměti
            fk.write_text("## Oblast\n\n- **F-O-01** Nové znění.\n  Testy: `x`\n", encoding="utf-8")
            st = fk.stat()
            os.utime(fk, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            second = pages.get("jak-to-funguje")
            self.assertIn("Nové znění.", second.raw.decode())
            self.assertNotEqual(first.etag, second.etag)
            self.assertEqual(gzip.decompress(second.packed), second.raw)
            # jiné nastavení = jiná stránka
            self.assertIsNot(pages.get("napoveda", {"wish_block": 4}), pages.get("napoveda", {}))

    def test_missing_funkce_is_said_not_crashed(self):
        with tempfile.TemporaryDirectory() as d:
            page = manual.Pages(Path(d) / "nic.md", Path(d) / "nic2.md").get("jak-to-funguje")
            self.assertIn("docs/FUNKCE.md", page.raw.decode())
            self.assertIn("chybí", page.raw.decode())

    def test_search_box_and_collapsible_areas(self):
        src = manual.DOCS_HTML.read_text(encoding="utf-8")
        self.assertIn('type="search"', src)
        self.assertIn('normalize("NFD")', src)  # bez ohledu na diakritiku
        page = docs_page()
        self.assertIn('<details class="area card"', page)

    def test_deploy_ships_docs(self):
        deploy = (ROOT / "packaging/rpi/deploy.sh").read_text(encoding="utf-8")
        rsync = deploy[deploy.index("rsync -az"): deploy.index('"$PI:$DEST/"')]
        self.assertNotIn("docs", rsync, "deploy.sh nesmí vynechat docs/ — stránka Jak to funguje je z nich")
        self.assertEqual(manual.FUNKCE_MD, ROOT / "docs" / "FUNKCE.md")


class NapovedaIsTrue(unittest.TestCase):
    """F-WEB-07: příklady v Nápovědě aplikace opravdu takhle chápe."""

    def artist_names(self, text: str) -> list[list[str]]:
        """Jména, která zkouší rychlá cesta (find_artists), bez katalogu."""
        p = fastpath.parse(text)
        if p is None:
            return []
        return [p.whole] + p.groups + ([[p.lead] + p.whole] if p.lead else [])

    def test_examples_are_understood(self):
        found = examples()
        kinds = {a["data-example"] for a, _ in found}
        self.assertTrue({"artist", "song", "next", "then", "fix", "command", "favourites",
                         "question", "link", "model"} <= kinds, kinds)
        for a, text in found:
            kind = a["data-example"]
            with self.subTest(kind=kind, text=text):
                if kind == "artist":
                    self.assertTrue(any(fastpath.name_matches(n, a["data-artist"])
                                        for n in self.artist_names(text)))
                elif kind == "song":
                    sp = fastpath.parse_song(text)
                    self.assertIsNotNone(sp)
                    self.assertTrue(any(norm(t) == norm(a["data-title"])
                                        and fastpath.name_matches(norm(ar).split(), a["data-artist"])
                                        for ar, t in sp.pairs), sp.pairs)
                elif kind == "next":
                    self.assertTrue(wishes._NEXT.search(norm(text)))
                elif kind == "then":
                    self.assertTrue(wishes.additive(text))
                    self.assertFalse(wishes.replaces(text))
                elif kind == "fix":
                    self.assertTrue(wishes.replaces(text))
                elif kind == "command":
                    self.assertEqual(local_command(text)[0], a["data-command"])
                elif kind == "favourites":
                    self.assertEqual(favourites_request(text), a["data-which"])
                elif kind == "question":
                    self.assertIsNotNone(meta_kind(text))
                elif kind == "link":
                    self.assertTrue(RE_URL.search(text))
                elif kind == "model":
                    # jde k DJovi (modelu): nic z rychlých cest si ho nepřivlastní
                    self.assertIsNone(local_command(text))
                    self.assertIsNone(meta_kind(text))
                    self.assertIsNone(favourites_request(text))
                    self.assertIsNone(fastpath.parse_song(text))
                    self.assertFalse(RE_URL.search(text))
                else:
                    self.fail(f"neznámý druh příkladu {kind}")

    def test_model_understood_examples_are_measured(self):
        """Příklad, kterému rozumí jen model (data-understood="favourites:office"),
        musí mít záznam skutečného modelu na Pi (tests/model_favourites_check.py
        → tests/fixtures/model_understanding.json): každé měření správně."""
        rec = json.loads((ROOT / "tests" / "fixtures" / "model_understanding.json").read_text())
        by_text = {r["text"]: r for r in rec["results"]}
        found = [(a, t) for a, t in examples() if a.get("data-understood")]
        self.assertTrue(found)
        for a, text in found:
            with self.subTest(text=text):
                action, _, scope = a["data-understood"].partition(":")
                self.assertIn(text, by_text, "změř ho: tests/model_favourites_check.py")
                runs = by_text[text]["runs"]
                self.assertGreaterEqual(len(runs), 2)
                for r in runs:
                    self.assertTrue(r["ok"])
                    self.assertEqual((r["action"], r.get("scope", "")), (action, scope))

    def test_owner_example_pust_kabat_is_an_artist(self):
        self.assertIn(({"data-example": "artist", "data-artist": "Kabát"}, "pusť Kabát"), examples())

    def test_the_chips_it_names_are_on_the_page(self):
        idx = INDEX.read_text(encoding="utf-8")
        src = manual.NAPOVEDA_HTML.read_text(encoding="utf-8")
        for chip in re.findall(r'class="chip"[^>]*>([^<]+)</button>', idx):
            self.assertIn(f"<b>{chip}</b>", src, chip)

    def test_numbers_follow_the_settings(self):
        pages = manual.Pages()
        default = pages.get("napoveda", {k: DEFAULTS[k] for k in manual.SHOWN_KEYS}).raw.decode()
        self.assertIn('data-forms="skladbě|skladbách">2\u00a0skladbách</span>', default)
        self.assertIn('data-forms="skladbu|skladby|skladeb">4\u00a0skladby</span>', default)
        self.assertIn('data-forms="člověk|lidé|lidí">2\u00a0lidé</span>', default)
        changed = pages.get("napoveda", {"wish_budget": 5, "wish_shared_block": 1,
                                         "ban_song_votes": 1}).raw.decode()
        self.assertIn(">5\u00a0skladeb</span>", changed)
        self.assertIn(">1\u00a0skladbě</span>", changed)
        self.assertIn(">1\u00a0člověk</span>", changed)
        for key in re.findall(r'data-cfg="([a-z_]+)"', manual.NAPOVEDA_HTML.read_text(encoding="utf-8")):
            self.assertIn(key, manual.SHOWN_KEYS)

    def test_night_rule_matches_funkce(self):
        """Nápověda říká 22:00–7:00 — stejně jako F-RESTART-01."""
        self.assertIn("22–7 h", funkce().rule("F-RESTART-01").text)
        self.assertIn("22:00–7:00", manual.NAPOVEDA_HTML.read_text(encoding="utf-8"))

    def test_reachable_from_the_header_and_the_nick_card(self):
        idx = INDEX.read_text(encoding="utf-8")
        header = idx[idx.index('<header class="top">'): idx.index("</header>")]
        self.assertIn('href="/napoveda"', header)
        card = idx[idx.index('id="nickModal"'): idx.index('id="voteSheet"')]
        self.assertRegex(card, r'href="/napoveda"[^>]*>Jak to funguje\?')
        nap = manual.NAPOVEDA_HTML.read_text(encoding="utf-8")
        self.assertIn('href="/jak-to-funguje"', nap)
        self.assertIn('href="/napoveda"', manual.DOCS_HTML.read_text(encoding="utf-8"))


class Serving(unittest.TestCase):
    """Obě stránky jdou zabalené jako index (F-WEB-05), 304 beze změny, mimo smyčku."""

    def make(self):
        from starlette.requests import Request

        from ytdj.web import server as web
        app = SimpleNamespace(cfg=Config(**{**DEFAULTS, "wish_block": 4, "wish_shared_block": 1}), player=None, wishes=None,
                              store=None, pools=None, dj=None)
        srv = web.WebServer(app)
        h = {(r.path, m): r.endpoint for r in srv._starlette.routes
             for m in (getattr(r, "methods", None) or ())}

        def get(path, **headers):
            scope = {"type": "http", "method": "GET", "path": path, "query_string": b"",
                     "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]}
            return asyncio.run(h[(path, "GET")](Request(scope)))
        return get

    def test_pages_gzip_etag_and_304(self):
        get = self.make()
        for path, needle in (("/napoveda", "Nápověda"), ("/jak-to-funguje", "Jak to funguje"),
                             ("/manual.css", "--accent")):
            with self.subTest(path=path):
                plain = get(path)
                self.assertEqual(plain.status_code, 200)
                self.assertIn(needle, plain.body.decode())
                packed = get(path, accept_encoding="gzip")
                self.assertEqual(packed.headers["content-encoding"], "gzip")
                self.assertEqual(gzip.decompress(packed.body), plain.body)
                self.assertEqual(get(path, if_none_match=packed.headers["etag"]).status_code, 304)
        docs = get("/jak-to-funguje").body.decode()
        self.assertIn("Přání: skladeb v jednom kole: <b>4</b>", docs)  # živé nastavení
        self.assertIn(">1\u00a0skladbě</span>", get("/napoveda").body.decode())

    def test_build_runs_off_the_event_loop(self):
        from ytdj.web import server as web
        src = Path(web.__file__).read_text(encoding="utf-8")
        body = src[src.index("async def _manual"): src.index("async def _static_fallback")]
        self.assertIn("asyncio.to_thread(self._pages.get", body)

    def test_whole_page_is_quick(self):
        t0 = time.perf_counter()
        manual.Pages().get("jak-to-funguje")
        self.assertLess(time.perf_counter() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
