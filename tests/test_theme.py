"""Vzhled webu Auto / Den / Noc (F-WEB-09, F-WEB-10, POZADAVKY #61).

Hlídá, že přepínač je na všech stránkách stejný, že se zvolený vzhled nastaví
dřív, než se stránka vykreslí (skript v <head> před styly), že světlé barvy
jsou stejné pro „podle zařízení" i pro ruční volbu, a — skutečným během
theme.js v Node — že si volbu pamatuje, že lišta prohlížeče jde s ní a že bez
úložiště stránka dál funguje podle zařízení.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-theme-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "ytdj" / "web" / "static"
PAGES = ("index.html", "napoveda.html", "jak-to-funguje.html", "hlaseni.html")
THEME_JS = STATIC / "theme.js"
NODE = shutil.which("node")
DARK, LIGHT = "#0e1013", "#faf7f2"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def css_of(name: str) -> str:
    """Styl stránky: manual.css, u stránek s vlastním <style> i ten."""
    if name.endswith(".css"):
        return read(name)
    return "\n".join(re.findall(r"<style>(.*?)</style>", read(name), re.S))


def tokens(block: str) -> dict[str, str]:
    return {k: " ".join(v.split()) for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)}


def block_after(css: str, selector: str) -> str:
    """Tělo prvního pravidla `selector { … }` (bez vnořených závorek)."""
    m = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    if not m:
        raise AssertionError(f"chybí pravidlo {selector}")
    return m.group(1)


class ThemeOnEveryPage(unittest.TestCase):
    """F-WEB-09: jeden přepínač a jedny barvy na všech stránkách, bez probliknutí."""

    def test_script_runs_in_head_before_the_styles(self):
        for name in PAGES:
            with self.subTest(page=name):
                head = read(name).split("</head>")[0]
                tag = '<script src="/theme.js"></script>'  # bez defer/async: musí doběhnout před vykreslením
                self.assertEqual(head.count(tag), 1)
                styles = [i for i in (head.find("<style"), head.find('rel="stylesheet"')) if i >= 0]
                self.assertTrue(styles)
                self.assertLess(head.index(tag), min(styles))
                # lišta prohlížeče: dvě značky (tmavá, světlá), které skript přepisuje
                self.assertIn(f'<meta name="theme-color" content="{DARK}" media="(prefers-color-scheme: dark)">', head)
                self.assertIn(f'<meta name="theme-color" content="{LIGHT}" media="(prefers-color-scheme: light)">', head)
                self.assertLess(head.rindex('name="theme-color"'), head.index(tag))

    def test_the_same_three_way_switch_everywhere(self):
        boxes = set()
        for name in PAGES:
            with self.subTest(page=name):
                page = read(name)
                m = re.search(r'<div class="theme" data-theme-switch[^>]*>.*?</div>', page, re.S)
                self.assertIsNotNone(m)
                box = m.group(0)
                boxes.add(box)
                self.assertRegex(box.split(">")[0], r"\bhidden\b")  # bez skriptu se neukáže (F-WEB-10)
                self.assertEqual(re.findall(r'data-theme-set="([a-z]+)"[^>]*>([^<]+)</button>', box),
                                 [("auto", "Auto"), ("light", "Den"), ("dark", "Noc")])
                self.assertIn("Vzhled", box)
                # na hlavní stránce není ve footeru, který stránka Hlasování schovává
                foot = page[page.index('<footer class="foot">'): page.index("</footer>")]
                self.assertNotIn("data-theme-switch", foot)
        self.assertEqual(len(boxes), 1, "přepínač má být na všech stránkách stejný")

    def test_light_colours_are_the_same_for_device_and_for_choice(self):
        seen = {}
        for name in ("index.html", "manual.css"):
            with self.subTest(css=name):
                css = css_of(name)
                base = tokens(block_after(css, ":root"))
                by_device = tokens(block_after(css, ":root:not([data-theme])"))
                chosen = tokens(block_after(css, ':root[data-theme="light"]'))
                self.assertGreater(len(by_device), 15)
                self.assertEqual(by_device, chosen)
                self.assertEqual(base["--bg"], DARK)
                self.assertEqual(chosen["--bg"], LIGHT)
                self.assertIn("color-scheme: light", block_after(css, ':root[data-theme="light"]'))
                self.assertIn("color-scheme: dark", block_after(css, ':root[data-theme="dark"]'))
                seen[name] = (base, chosen)
        for i in (0, 1):  # hlavní stránka a ostatní mají stejné barvy
            idx, man = seen["index.html"][i], seen["manual.css"][i]
            self.assertEqual({k: v for k, v in man.items() if k in idx}, idx)

    def test_no_colour_rule_ignores_the_choice(self):
        """Každé pravidlo podle zařízení platí jen bez ruční volby a má dvojče pro volbu."""
        for name in (*PAGES, "manual.css"):
            css = css_of(name)
            for m in re.finditer(r"@media \(prefers-color-scheme: (light|dark)\)\s*\{\s*([^{]+)\{", css):
                scheme, selector = m.group(1), m.group(2).strip()
                with self.subTest(css=name, selector=selector):
                    self.assertTrue(selector.startswith(":root:not([data-theme])"), selector)
                    twin = selector.replace(":root:not([data-theme])", f':root[data-theme="{scheme}"]')
                    self.assertRegex(css, r"(?m)^\s*" + re.escape(twin) + r"\s*\{")

    def test_served_packed_with_etag_like_the_pages(self):
        from starlette.requests import Request

        from ytdj.config import DEFAULTS, Config
        from ytdj.web import server as web
        app = SimpleNamespace(cfg=Config(**DEFAULTS), player=None, wishes=None, store=None, pools=None, dj=None)
        srv = web.WebServer(app)
        h = {(r.path, m): r.endpoint for r in srv._starlette.routes for m in (getattr(r, "methods", None) or ())}

        def get(**headers):
            scope = {"type": "http", "method": "GET", "path": "/theme.js", "query_string": b"",
                     "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]}
            return asyncio.run(h[("/theme.js", "GET")](Request(scope)))

        plain = get()
        self.assertEqual(plain.status_code, 200)
        self.assertIn("javascript", plain.headers["content-type"])
        self.assertEqual(plain.body, THEME_JS.read_bytes())
        self.assertLess(len(plain.body), 4096)  # malý: načítá se před vykreslením každé stránky
        packed = get(accept_encoding="gzip")
        self.assertEqual(gzip.decompress(packed.body), plain.body)
        self.assertEqual(plain.headers["cache-control"], "no-cache")
        self.assertEqual(get(if_none_match=packed.headers["etag"]).status_code, 304)

    def test_help_page_says_where_the_switch_is(self):
        nap = read("napoveda.html")
        tip = nap[nap.index('id="vzhled"'):]
        tip = tip[: tip.index("</li>")]
        for word in ("Vzhled", "Auto", "Den", "Noc", "dole"):
            self.assertIn(word, tip)


# Malý falešný prohlížeč: jen to, na co theme.js sahá. Scénář se předá jako JSON.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const sc = JSON.parse(process.argv[3]);
function El(attrs) { this.attrs = Object.assign({}, attrs); }
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.closest = function (sel) { return sel === "[data-theme-set]" && "data-theme-set" in this.attrs ? this : null; };
const root = new El({});
const metas = [new El({ name: "theme-color", content: "#0e1013", media: "(prefers-color-scheme: dark)" }),
               new El({ name: "theme-color", content: "#faf7f2", media: "(prefers-color-scheme: light)" })];
const box = new El({ "data-theme-switch": "", hidden: "" });
const btns = ["auto", "light", "dark"].map(function (t) { return new El({ "data-theme-set": t, "aria-pressed": "x" }); });
let parsed = false;  // tělo stránky (přepínač) ještě není, když skript v <head> běží
const handlers = {};
const on = function (name, fn) { (handlers[name] = handlers[name] || []).push(fn); };
const fire = function (name, ev) { (handlers[name] || []).forEach(function (fn) { fn(ev || {}); }); };
const document = {
  documentElement: root,
  querySelectorAll: function (sel) {
    if (sel === 'meta[name="theme-color"]') return metas;
    if (!parsed) return [];
    if (sel === "[data-theme-switch]") return [box];
    if (sel === "[data-theme-set]") return btns;
    return [];
  },
  addEventListener: on,
};
const store = Object.assign({}, sc.store || {});
const broken = function () { throw new Error("SecurityError"); };
const localStorage = sc.broken ? { getItem: broken, setItem: broken, removeItem: broken } : {
  getItem: function (k) { return k in store ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; },
};
const window = { addEventListener: on };
const ctx = { document: document, window: window };
Object.defineProperty(ctx, "localStorage", sc.missing
  ? { get: function () { throw new Error("no storage"); } } : { value: localStorage });
const out = [];
const snap = function (step) {
  out.push({ step: step, theme: root.getAttribute("data-theme"), metas: metas.map(function (m) { return m.attrs.content; }),
             pressed: btns.map(function (b) { return b.attrs["aria-pressed"]; }), hidden: "hidden" in box.attrs,
             store: Object.assign({}, store) });
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
snap("head");
parsed = true;
fire("DOMContentLoaded");
snap("ready");
(sc.steps || []).forEach(function (s) {
  if (s.click) fire("click", { target: btns[["auto", "light", "dark"].indexOf(s.click)] });
  if (s.other_tab !== undefined) {
    if (s.other_tab) store["ytdj.theme"] = s.other_tab; else delete store["ytdj.theme"];
    fire("storage", { key: "ytdj.theme" });
  }
  if (s.back) fire("pageshow", { persisted: true });
  if (s.elsewhere) fire("click", { target: new El({}) });
  snap(JSON.stringify(s));
});
console.log(JSON.stringify(out));
"""


class _InNode:
    """theme.js opravdu spuštěný v Node nad falešným prohlížečem."""

    @classmethod
    def setUpClass(cls):
        cls.harness = Path(_TMP) / "theme_harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")

    def run_js(self, **scenario) -> list[dict]:
        r = subprocess.run([NODE, str(self.harness), str(THEME_JS), json.dumps(scenario)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


@unittest.skipUnless(NODE, "theme.js se zkouší v Node (není nainstalovaný)")
class ThemeBehaviour(_InNode, unittest.TestCase):
    """F-WEB-09: volba, paměť zařízení, lišta prohlížeče."""

    def test_saved_choice_is_applied_before_first_paint(self):
        for theme, colour in (("dark", DARK), ("light", LIGHT)):
            with self.subTest(theme=theme):
                head, ready = self.run_js(store={"ytdj.theme": theme})
                # už v <head>: atribut i barva lišty, dřív než existuje tělo stránky
                self.assertEqual(head["theme"], theme)
                self.assertEqual(head["metas"], [colour, colour])
                self.assertEqual(ready["theme"], theme)
                self.assertEqual(ready["pressed"], [str(t == theme).lower() for t in ("", "light", "dark")])

    def test_choice_is_remembered_and_auto_forgets_it(self):
        steps = self.run_js(steps=[{"click": "light"}, {"click": "dark"}, {"click": "auto"}])
        ready, light, dark, auto = steps[1:]
        self.assertIsNone(ready["theme"])
        self.assertEqual(ready["pressed"], ["true", "false", "false"])  # výchozí je Auto
        self.assertFalse(ready["hidden"])
        self.assertEqual((light["theme"], light["store"], light["metas"]),
                         ("light", {"ytdj.theme": "light"}, [LIGHT, LIGHT]))
        self.assertEqual(light["pressed"], ["false", "true", "false"])
        self.assertEqual((dark["theme"], dark["store"], dark["metas"]),
                         ("dark", {"ytdj.theme": "dark"}, [DARK, DARK]))
        self.assertEqual(dark["pressed"], ["false", "false", "true"])
        # Auto: žádný atribut, nic uloženého, lišta zase podle zařízení
        self.assertEqual((auto["theme"], auto["store"], auto["metas"]), (None, {}, [DARK, LIGHT]))
        self.assertEqual(auto["pressed"], ["true", "false", "false"])

    def test_other_clicks_change_nothing(self):
        steps = self.run_js(store={"ytdj.theme": "dark"}, steps=[{"elsewhere": True}])
        self.assertEqual(steps[-1]["theme"], "dark")
        self.assertEqual(steps[-1]["store"], {"ytdj.theme": "dark"})

    def test_follows_a_choice_made_in_another_tab_or_page(self):
        steps = self.run_js(steps=[{"other_tab": "dark"}, {"other_tab": ""}, {"other_tab": "light"}, {"back": True}])
        self.assertEqual([s["theme"] for s in steps[2:]], ["dark", None, "light", "light"])
        self.assertEqual(steps[2]["metas"], [DARK, DARK])
        self.assertEqual(steps[3]["metas"], [DARK, LIGHT])


@unittest.skipUnless(NODE, "theme.js se zkouší v Node (není nainstalovaný)")
class ThemeWithoutStorage(_InNode, unittest.TestCase):
    """F-WEB-10: bez volby a bez úložiště jede stránka podle zařízení a nespadne."""

    def test_nothing_saved_follows_the_device(self):
        head, ready = self.run_js()
        for s in (head, ready):
            self.assertIsNone(s["theme"])
            self.assertEqual(s["metas"], [DARK, LIGHT])
        self.assertEqual(ready["store"], {})  # samo nic neukládá

    def test_unknown_saved_value_is_auto(self):
        head, ready = self.run_js(store={"ytdj.theme": "ruzova"})
        self.assertIsNone(head["theme"])
        self.assertEqual(ready["pressed"], ["true", "false", "false"])

    def test_broken_storage_does_not_break_the_page(self):
        for kind in ("broken", "missing"):  # úložiště hází chybu / není vůbec (soukromé okno, zákaz)
            with self.subTest(storage=kind):
                steps = self.run_js(**{kind: True}, steps=[{"click": "dark"}, {"back": True}, {"click": "auto"}])
                self.assertIsNone(steps[0]["theme"])
                self.assertFalse(steps[1]["hidden"])
                # volba platí aspoň do zavření stránky
                self.assertEqual([s["theme"] for s in steps[2:]], ["dark", "dark", None])
                self.assertEqual(steps[2]["metas"], [DARK, DARK])

    def test_switch_is_hidden_until_the_script_runs(self):
        head, ready = self.run_js()
        self.assertTrue(head["hidden"])   # jen HTML: přepínač skrytý, stránka podle zařízení
        self.assertFalse(ready["hidden"])
        for name in ("index.html", "manual.css"):
            self.assertIn("[hidden] { display: none !important; }", css_of(name))


if __name__ == "__main__":
    unittest.main()
