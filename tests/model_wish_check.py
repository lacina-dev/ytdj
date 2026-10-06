"""Does the real DJ model understand songs / albums / explicit wishes?

(FUNKCE F-PRANI-19 až F-PRANI-27, F-FRONTA-21, F-ZVUK-11.) It also counts how
often the model ASKS instead of deciding: never on the wishes with a clear
catalogue answer, and on the few genuinely ambiguous ones at the end. Not a unit test — a
measurement with the real model and the real catalogue, the way the app
decides: catalogue probe → prompt → model → build_intent → CodexDJ.resolve.
Nothing is played or queued: the player is a stub, state lives in a temp
directory, the Codex app-server is a throwaway one (effort like production,
a fresh thread per wish).

    .venv/bin/python tests/model_wish_check.py            # all cases, once
    .venv/bin/python tests/model_wish_check.py 2          # twice each
    WISH_OLD_ROLE=/path/to/old/prompts.py … = also time the decision with
        that older prompt (no catalogue lines) — latency before / after
    WISH_ONLY='["text", …]' = only these texts

Prints one line per wish (decision fields → what would be queued) and the
median decision latency; writes the rows as JSON next to the temp state.
"""
import asyncio
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-wishcheck-")
os.environ["XDG_DATA_HOME"] = _TMP  # state, dj-intent.json, codex workdir — not the real ones
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.agent.appserver import AppServer, effort_from_env, native_codex  # noqa: E402
from ytdj.agent.codex import DECISION_SCHEMA, CodexDJ, default_binary  # noqa: E402
from ytdj.agent.intent import build_intent, option_intent  # noqa: E402
from ytdj.agent.prompts import ROLE, render_state  # noqa: E402
from ytdj.config import Config  # noqa: E402
from ytdj.music.catalog import Catalog  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.player.base import PlayerStatus  # noqa: E402
from ytdj.state import Store  # noqa: E402

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 1

# (text, what must come out) — wish texts as they were typed in the office
CASES = [
    # A: a song the model does not know (a small Czech band)
    ("Zahraj nerdíky v neklidu, poté další věci od Pondělníků", {"plays_first": "Nerdíci v neklidu"}),
    ("nerdíci v neklidu", {"plays_first": "Nerdíci v neklidu"}),
    ("pondělnící - nerdíci v neklidu", {"plays_first": "Nerdíci v neklidu"}),
    ("Nerdici v neklidu od kapely Polednici", {"plays_first": "Nerdíci v neklidu"}),
    # B: albums
    ("sabaton - alba primo victoria a art of war", {"kind": "album", "albums": 2}),
    ("metallica - album load", {"kind": "album", "albums": 1}),
    ("rammstein live aus berlin 1998 remastered", {"kind": "album", "albums": 1}),
    ("pusť celé album Load", {"kind": "album", "albums": 1}),
    # C: explicitly vulgar
    ("Zahraj tu nejvulgárnější, nejsprostější prasárnu, co znáš", {"explicit_ok": True}),
    ("Zahraj něco vulgárního, nechutnýho a nekorektního, co pohorší všechny slušný lidi "
     "v místnosti. A ať je to česky.", {"explicit_ok": True}),
    # controls: behaviour must not change
    ("pusť Kabát", {"kind": "artist", "explicit_ok": False}),
    ("něco veselého k práci", {"kind": "mood", "explicit_ok": False}),
    ("pusť Jasnou zprávu od Olympicu", {"plays_first": "Jasná zpráva", "explicit_ok": False}),
    ("něco od Metallicy, klidně i z novějších alb", {"not_kind": "album", "explicit_ok": False}),
    ("zahraj nějaký pořádný český punk", {"kind": "mood", "explicit_ok": False}),
    ("něco klidného k práci", {"kind": "mood", "explicit_ok": False}),
    # words that describe a selection are not a title (the Pi played "Pecka" by another band)
    ("Zahraj nejvetsi pecky od Foo Fighters", {"kind": "artist", "artists": ["Foo Fighters"]}),
    ("to nejlepší od Kabátu", {"kind": "artist", "artists": ["Kabát"]}),
    ("hity od Queen", {"kind": "artist", "artists": ["Queen"]}),
    # the named artist does not have it → the existing version, with an honest note
    ("Holky z naší školky od Olympicu", {"plays_first": "Holky z naší školky", "note": "ji nemám"}),
    # genuinely ambiguous — the same words are a song and an album (or a band):
    # here, and only here, a question is the right answer
    ("Paranoid", {"ask": True}),
    ("Master of Puppets", {"ask": True}),
    ("All Is Violent, All Is Bright", {"ask": True}),
    ("Alice", {"ask": True}),
]
if os.environ.get("WISH_ONLY"):
    only = set(json.loads(os.environ["WISH_ONLY"]))
    CASES = [c for c in CASES if c[0] in only] or [(t, {}) for t in only]


class NoPlayer:
    """The player is never touched — resolve only asks what is playing."""

    queue_depth = 0

    async def status(self) -> PlayerStatus:
        return PlayerStatus(playing=False, paused=False, current=None, position=0.0,
                            duration=0.0, queue=[], volume=50)


def old_role() -> str | None:
    path = os.environ.get("WISH_OLD_ROLE")
    if not path:
        return None
    spec = importlib.util.spec_from_file_location("prompts_old", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ROLE + "\n\n" + mod.render_state(now_playing="", queue=[], pools="žádné aktivní seedy",
                                                history=[], taste="")


async def queued(dj: CodexDJ, pools: RadioPools, plan) -> tuple[list[str], dict]:
    """What the wish queue would take from the plan (labels) + pool statistics."""
    intent = plan.intent
    extra: dict = {}
    if plan.failed:
        return [], {"failed": plan.failed}
    if intent.kind == "album":
        extra["album"] = plan.album_label
        extra["album_tracks"] = len(plan.album_tracks)
        extra["then_background"] = [t.title for t in plan.album_tracks[4:7]]
        return [t.label() for t in plan.album_tracks[:4]], extra
    if intent.kind == "artist":
        first = list(plan.requested) + [t for t in plan.artist_tracks if t not in plan.requested]
        return [t.label() for t in first[:4]], extra
    if intent.kind == "songs":
        return [t.label() for t in plan.requested], extra
    if intent.kind == "song":
        return [t.label() for t in plan.requested], {"then_radio_from": [t.label() for t in plan.seeds[:3]]}
    if intent.kind == "mood":
        extra["seeds"] = [t.label() for t in plan.seeds]
        await pools.set_seeds(plan.seeds, mood=intent.mood, explicit_ok=intent.explicit_ok)
        block = await pools.next_tracks(3)
        extra["explicit_in_block"] = sum(1 for t in block if t.explicit)
        return [t.label() for t in block], extra
    return [], extra


def verdict(want: dict, intent, plan, first: list[str], asked: bool = False) -> bool:
    ok = asked == bool(want.get("ask", False))  # a question only where one is expected
    if asked:
        return ok
    if "kind" in want:
        ok &= intent.kind == want["kind"]
    if "not_kind" in want:
        ok &= intent.kind != want["not_kind"]
    if "albums" in want:
        ok &= len(intent.albums) == want["albums"] and bool(plan.album_tracks)
    if "explicit_ok" in want:
        ok &= bool(intent.explicit_ok) == want["explicit_ok"]
    if "artists" in want:
        ok &= [a.lower() for a in intent.artists] == [a.lower() for a in want["artists"]]
        ok &= bool(first) and all(want["artists"][0].lower() in f.lower() for f in first)
    if "note" in want:
        ok &= any(want["note"] in n for n in plan.notes)
    if "plays_first" in want:
        ok &= bool(first) and want["plays_first"].lower() in first[0].lower()
    return bool(ok)


async def main() -> None:
    cfg = Config.load()
    catalog = Catalog(cfg)
    store = Store(Path(_TMP) / "state.db")
    pools = RadioPools(catalog, store, cfg)
    dj = CodexDJ(cfg, catalog, pools, NoPlayer(), store)
    binary = native_codex(default_binary()) or default_binary()
    work = tempfile.mkdtemp(prefix="ytdj-wishcheck-codex-")
    app = AppServer(binary, work, model=cfg.codex_model, max_turns_per_thread=1,
                    effort=effort_from_env())
    old = old_role()
    rows, new_ms, old_ms, probe_ms = [], [], [], []
    try:
        await app.turn(f"{ROLE}\n\nUživatel říká: ahoj", DECISION_SCHEMA, timeout=120)  # warm-up
        for text, want in CASES:
            for run in range(RUNS):
                if old is not None:
                    t0 = time.monotonic()
                    await app.turn(f"{old}\n\nUživatel říká: {text}", DECISION_SCHEMA, timeout=120)
                    old_ms.append(int((time.monotonic() - t0) * 1000))
                probe = await catalog.probe(text)
                probe_ms.append(probe.took_ms)
                prompt = await dj._build_prompt(text, probe.lines())
                t0 = time.monotonic()
                res = await app.turn(prompt, DECISION_SCHEMA, timeout=120)
                took = int((time.monotonic() - t0) * 1000)
                new_ms.append(took)
                data = json.loads(res.text)
                intent = build_intent(text, data)
                asked = intent.kind == "ask"
                options = [o["label"] for o in intent.options]
                question = intent.question
                if asked:  # what the timeout would play: the first option
                    intent = option_intent(text, intent.options[0])
                plan = await dj.resolve(intent)
                intent = plan.intent
                first, extra = await queued(dj, pools, plan)
                if asked:
                    extra = {"asked": question, "options": options, **extra}
                ok = verdict(want, intent, plan, first, asked)
                fields = {
                    "action": data.get("action"), "kind": intent.kind,
                    "requested": [f"{a} — {t}" for a, t in intent.tracks],
                    "focus_artists": intent.artists,
                    "albums": [f"{a} — {t}" for a, t in intent.albums],
                    "explicit_ok": intent.explicit_ok, "mood": intent.mood,
                    "seeds": [f"{a} — {t}" for a, t in intent.seeds][:5],
                }
                rows.append({"text": text, "run": run + 1, "ok": ok, "asked": asked,
                             "should_ask": bool(want.get("ask")), "decision": fields,
                             "queued": first, "extra": extra, "notes": plan.notes,
                             "model_ms": took, "probe_ms": probe.took_ms,
                             "probe": probe.lines()})
                print(f"{'OK ' if ok else 'BAD'} {text!r} #{run + 1} ({took} ms, probe {probe.took_ms} ms)\n"
                      f"     decision: {json.dumps({k: v for k, v in fields.items() if v}, ensure_ascii=False)}\n"
                      f"     queued:   {first}\n"
                      f"     extra:    {json.dumps(extra, ensure_ascii=False)} notes={plan.notes}", flush=True)
    finally:
        await app.close()
    good = sum(1 for r in rows if r["ok"])
    print(f"\n{good}/{len(rows)} as expected")
    clear = [r for r in rows if not r["should_ask"]]
    vague = [r for r in rows if r["should_ask"]]
    print(f"asked on {sum(r['asked'] for r in clear)} of {len(clear)} wishes with a clear answer "
          f"(must be 0); on {sum(r['asked'] for r in vague)} of {len(vague)} ambiguous ones")
    print(f"decision latency (model turn), median: new prompt {statistics.median(new_ms):.0f} ms"
          + (f", old prompt {statistics.median(old_ms):.0f} ms" if old_ms else "")
          + f"; catalogue probe median {statistics.median(probe_ms):.0f} ms (runs alongside the fast path)")
    out = Path(_TMP) / "result.json"
    out.write_text(json.dumps({"rows": rows, "new_ms": new_ms, "old_ms": old_ms,
                               "probe_ms": probe_ms}, ensure_ascii=False, indent=1))
    print("rows:", out)


asyncio.run(main())
