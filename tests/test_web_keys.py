"""Klávesové zkratky webu (F-WEB-11): mezerník ▶/⏸, n = další, / = přání —
jen na hlavní stránce přehrávače a nikdy, když klávesu potřebuje to, na čem je
fokus (pole, tlačítko, odkaz, posuvník…). Hlášení #36 (26. 10. 2026): mezerník
v záložce Hlasování pauzoval hudbu celé kanceláři.

Rozhodovací funkce `shortcutFor` se vyřízne přímo z index.html a spustí v Node
nad falešnými prvky stránky — zkouší se tedy ten kód, který běží v prohlížeči.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_web_keys -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-web-keys-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_votes import request  # noqa: E402
from test_web_votes import make  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"
NODE = shutil.which("node")


def js_block(name: str) -> str:
    """Kód mezi `/* <name>:start` a `/* <name>:end */` v index.html."""
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"/\* " + re.escape(name) + r":start.*?\*/(.*?)/\* " + re.escape(name) + r":end \*/",
                  html, re.S)
    if not m:
        raise AssertionError(f"blok {name} v index.html chybí")
    return m.group(1)


# Falešné prvky: tagName, role, contenteditable a rodič — víc rozhodnutí nepotřebuje.
KEYS_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const shortcutFor = new Function(src + "\nreturn shortcutFor;")();
function node(spec, parent) {
  if (!spec) return null;
  const n = { nodeType: 1, tagName: spec.tag || "DIV", isContentEditable: !!spec.editable,
              parentElement: parent || null,
              getAttribute: (a) => (a === "role" ? (spec.role || null) : null) };
  return n;
}
const out = cases.map((c) => {
  // c.path: od <body> dolů k prvku s fokusem
  let el = null;
  for (const spec of (c.path || [{ tag: "BODY" }])) el = node(spec, el);
  const ev = Object.assign({ key: " ", metaKey: false, ctrlKey: false, altKey: false,
                             repeat: false, defaultPrevented: false }, c.ev || {}, { target: el });
  return shortcutFor(ev, c.onPlayer !== false);
});
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "zkratky se zkouší v Node (není nainstalovaný)")
class KeyRule(unittest.TestCase):
    """F-WEB-11: kdy klávesa ovládá hudbu — přímo kód stránky."""

    @classmethod
    def setUpClass(cls):
        cls.harness = Path(_TMP) / "keys_harness.js"
        cls.harness.write_text(KEYS_HARNESS, encoding="utf-8")
        cls.src = Path(_TMP) / "keys.js"
        cls.src.write_text(js_block("klávesy"), encoding="utf-8")

    def decide(self, cases: list[dict]) -> list[str]:
        f = Path(_TMP) / "cases.json"
        f.write_text(json.dumps(cases), encoding="utf-8")
        r = subprocess.run([NODE, str(self.harness), str(self.src), str(f)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_space_on_the_player_page_plays_and_pauses(self):
        self.assertEqual(self.decide([{}, {"ev": {"key": "n"}}, {"ev": {"key": "/"}}]),
                         ["play", "next", "wish"])

    def test_nothing_on_another_page_or_with_a_window_open(self):
        # záložka Hlasování, list hlasu, přezdívka, nastavení: onPlayer = false (hlášení #36)
        for key in (" ", "n", "/"):
            with self.subTest(key=key):
                self.assertEqual(self.decide([{"onPlayer": False, "ev": {"key": key}}]), [""])

    def test_space_belongs_to_the_focused_control(self):
        body = {"tag": "BODY"}
        cases = [
            {"path": [body, {"tag": "INPUT"}]},                       # pole přání: napíše mezeru
            {"path": [body, {"tag": "TEXTAREA"}]},
            {"path": [body, {"tag": "SELECT"}]},
            {"path": [body, {"tag": "BUTTON"}]},                      # tlačítko: zmáčkne ho
            {"path": [body, {"tag": "BUTTON"}, {"tag": "SPAN"}]},     # i když míří do jeho ikony
            {"path": [body, {"tag": "A"}]},
            {"path": [body, {"tag": "SUMMARY"}]},
            {"path": [body, {"tag": "DIV", "editable": True}]},
            {"path": [body, {"tag": "DIV", "role": "button"}]},
            {"path": [body, {"tag": "DIV", "role": "slider"}]},
        ]
        self.assertEqual(self.decide(cases), [""] * len(cases))
        # „n" a „/" v poli jsou taky jen písmena
        self.assertEqual(self.decide([{"path": [body, {"tag": "INPUT"}], "ev": {"key": k}} for k in ("n", "/")]),
                         ["", ""])

    def test_held_or_combined_keys_do_nothing(self):
        cases = [{"ev": {"repeat": True}}, {"ev": {"ctrlKey": True}}, {"ev": {"metaKey": True}},
                 {"ev": {"altKey": True}}, {"ev": {"defaultPrevented": True}}, {"ev": {"key": "x"}}]
        self.assertEqual(self.decide(cases), [""] * len(cases))


class KeyWiring(unittest.TestCase):
    """Stránka rozhodnutí opravdu používá a posílá, že povel přišel z klávesy."""

    def test_handler_asks_the_rule_and_knows_the_player_page(self):
        html = INDEX.read_text(encoding="utf-8")
        m = re.search(r"function onPlayerView\(\) \{(.*?)\n  \}", html, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        for needle in ('location.hash !== "#hlasovani"', '$("#votesView").hasAttribute("hidden")',
                       'el.voteSheet.hasAttribute("hidden")', 'el.nickModal.hasAttribute("hidden")',
                       '!el.settings.classList.contains("show")'):
            self.assertIn(needle, body)
        self.assertIn("var act = shortcutFor(ev, onPlayerView());", html)
        # jediný globální posluchač kláves na hlavní stránce
        self.assertEqual(html.count('document.addEventListener("keydown"'), 1)
        self.assertIn('if (S.viaKey) body.via = "key";', html)
        # Nápověda zkratky popisuje i s tím, kde neplatí
        manual = (INDEX.parent / "napoveda.html").read_text(encoding="utf-8")
        self.assertIn('id="klavesy"', manual)
        self.assertIn("Na stránce Hlasování", manual)

    def test_control_from_a_key_is_logged_as_such(self):
        async def go():
            srv, app, h = make()
            paused = []

            async def toggle_pause(on):
                paused.append(on)

            app.player.toggle_pause = toggle_pause
            post = h[("/api/control", "POST")]
            events = []
            from ytdj.web import server as web
            with mock.patch.object(web.telemetry, "event", lambda kind, **f: events.append((kind, f))):
                r1 = await post(request({"action": "pause", "via": "key"}))
                r2 = await post(request({"action": "pause"}))
                r3 = await post(request({"action": "pause", "via": "něco jiného"}))
            self.assertEqual([r.status_code for r in (r1, r2, r3)], [200, 200, 200])
            ctl = [f for k, f in events if k == "web.control"]
            self.assertEqual([f.get("via") for f in ctl], ["key", None, None])
            self.assertEqual(paused, [True, True, True])

        tw.run(go())


if __name__ == "__main__":
    unittest.main()
