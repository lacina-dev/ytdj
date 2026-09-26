"""Meta-test závazného popisu funkcí (docs/FUNKCE.md).

Každé pravidlo `F-OBLAST-NN` musí mít aspoň jeden hlídací test ve tvaru
`tests/soubor.py::Třída::test_metoda` a každý uvedený test musí existovat.
Výjimka jen `(bez testu: <důvod>)` — hardware, který unit test neprověří.

Testy se hledají ve zdrojácích (nic se neimportuje), takže to funguje i pro
testy panelu, které potřebují Pillow mimo venv:

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_funkce -v
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNKCE = ROOT / "docs" / "FUNKCE.md"

RULE = re.compile(r"^- \*\*(F-[A-Z]+-\d{2})\*\*")
ID_FORM = re.compile(r"^F-[A-Z]+-\d{2}$")
REF = re.compile(r"`(tests/[\w/]+\.py)::(\w+)::(\w+)`")
NO_TEST = re.compile(r"\(bez testu: [^)]{8,}\)")
# jen hardware: kdo chce výjimku pro další pravidlo, musí ho zapsat sem
HARDWARE = {"F-ZVUK-14", "F-DISPLEJ-13"}


def parse(text: str) -> dict[str, str]:
    """ID pravidla → jeho text (řádek s ID a odsazené řádky pod ním)."""
    rules: dict[str, str] = {}
    dup: list[str] = []
    cur: str | None = None
    for line in text.splitlines():
        m = RULE.match(line)
        if m:
            cur = m.group(1)
            if cur in rules:
                dup.append(cur)
            rules[cur] = line
        elif cur and line.startswith("  "):
            rules[cur] += "\n" + line
        else:
            cur = None
    if dup:
        raise AssertionError("Pravidla se stejným ID: " + ", ".join(dup))
    return rules


def _methods(path: Path) -> dict[str, set[str]]:
    """Třída → metody test_* (zdroják se jen rozparsuje, nic se nespouští)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        node.name: {f.name for f in node.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and f.name.startswith("test_")}
        for node in tree.body if isinstance(node, ast.ClassDef)
    }


def missing_refs(rules: dict[str, str]) -> tuple[list[str], list[str]]:
    """(pravidla bez testu, odkazy na neexistující testy)."""
    no_test, broken = [], []
    cache: dict[str, dict[str, set[str]] | None] = {}
    for rid, body in rules.items():
        refs = REF.findall(body)
        if not refs:
            if not (NO_TEST.search(body) and rid in HARDWARE):
                no_test.append(rid)
            continue
        for file, cls, meth in refs:
            if file not in cache:
                p = ROOT / file
                cache[file] = _methods(p) if p.is_file() else None
            found = cache[file]
            if found is None:
                broken.append(f"{rid}: soubor {file} neexistuje")
            elif cls not in found:
                broken.append(f"{rid}: {file} nemá třídu {cls}")
            elif meth not in found[cls]:
                broken.append(f"{rid}: {file}::{cls} nemá test {meth}")
    return no_test, broken


class FunkceContract(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = parse(FUNKCE.read_text())

    def test_there_are_rules(self) -> None:
        self.assertGreater(len(self.rules), 100, "docs/FUNKCE.md: pravidla se nenačetla")
        bad = [r for r in self.rules if not ID_FORM.match(r)]
        self.assertFalse(bad, f"Špatný tvar ID: {bad}")

    def test_every_rule_has_an_existing_test(self) -> None:
        no_test, broken = missing_refs(self.rules)
        msg = []
        if no_test:
            msg.append("Pravidla bez hlídacího testu (doplň test, nebo — jen u hardwaru — "
                       "'(bez testu: důvod)' a ID do HARDWARE): " + ", ".join(no_test))
        if broken:
            msg.append("Odkazy na testy, které neexistují (přejmenovaný/smazaný test "
                       "mění závazné pravidlo — jen se souhlasem vlastníka):\n  "
                       + "\n  ".join(broken))
        self.assertFalse(msg, "\n".join(msg))

    def test_hardware_exceptions_are_listed_and_marked(self) -> None:
        for rid in HARDWARE:
            self.assertIn(rid, self.rules, f"{rid} v HARDWARE, ale ne ve FUNKCE.md")
            self.assertTrue(NO_TEST.search(self.rules[rid]),
                            f"{rid}: chybí '(bez testu: důvod)'")

    def test_checker_catches_problems(self) -> None:
        """Kontrola sama: pravidlo bez testu a odkaz na neexistující test neprojdou."""
        fake = parse("- **F-X-01** A.\n  Testy: `tests/test_funkce.py::FunkceContract::test_nope`\n"
                     "- **F-X-02** B.\n"
                     "- **F-X-03** C.\n  (bez testu: protože se mi nechce psát)\n"
                     "- **F-X-04** D.\n  Testy: `tests/test_funkce.py::FunkceContract::test_there_are_rules`\n")
        no_test, broken = missing_refs(fake)
        self.assertEqual(no_test, ["F-X-02", "F-X-03"])
        self.assertEqual(len(broken), 1)
        self.assertIn("test_nope", broken[0])
        with self.assertRaises(AssertionError):
            parse("- **F-X-01** A.\n- **F-X-01** B.\n")


if __name__ == "__main__":
    unittest.main()
