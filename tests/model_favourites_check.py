"""Does the real DJ model understand favourites requests? (FUNKCE F-HLASY-19)

Not a unit test — a measurement on the Pi. Read-only: state.db opened with
mode=ro, a throwaway Codex app-server (effort low like production, a fresh
thread per request), nothing played or queued. The result goes to
tests/fixtures/model_understanding.json (tests/test_manual.py checks that
Nápověda examples only the model understands are recorded there).

    scp -r ytdj tests/model_favourites_check.py pi:/tmp/ytdj-favcheck/
    ssh pi 'cd /tmp/ytdj-favcheck && nice -n 10 ~/ytdj/.venv/bin/python model_favourites_check.py 2'
    FAV_ONLY='[["text", ["favourites", "office", true]]]' … = only these texts
Clean up /tmp/ytdj-favcheck* on the Pi afterwards.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/tmp/ytdj-favcheck")

from ytdj.agent.appserver import AppServer, effort_from_env, native_codex  # noqa: E402
from ytdj.agent.codex import DECISION_SCHEMA, default_binary  # noqa: E402
from ytdj.agent.intent import build_intent  # noqa: E402
from ytdj.agent.prompts import ROLE, render_state  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.votes import VoteBook  # noqa: E402

DB = Path.home() / ".local/share/ytdj/state.db"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 2

# text → (action, scope, continuous) expected; alternate_artists expected true for favourites
CASES = [
    ("Hraj to co mame radi napreskacku", ("favourites", "office", True)),
    ("Hraj pisnicky co mame radi stridej interprety a hraj porad", ("favourites", "office", True)),
    ("pusť něco z toho, co tu posloucháme", ("favourites", "office", True)),
    ("dej naše srdcovky a nepřestávej", ("favourites", "office", True)),
    ("zahraj moje oblíbené, ale střídej kapely", ("favourites", "mine", True)),
    ("chci slyšet to, co mám v playlistu", ("favourites", "mine", True)),
    ("co máme rádi v kanceláři", ("favourites", "office", True)),
    ("hoď tam pár věcí, co se mi líbí", ("favourites", "mine", False)),
    ("hraj nám oblíbený věci celý den", ("favourites", "office", True)),
    ("pust to co jsme olajkovali", ("favourites", "office", True)),
    ("něco z našich oblíbených, ale jen chvilku", ("favourites", "office", False)),
    ("moje srdcovky dokola", ("favourites", "mine", True)),
    # nesmí být oblíbené
    ("pusť Kabát", ("artist", "", None)),
    ("něco veselého", ("mood", "", None)),
    ("hraj oblíbené od Kabátu", ("artist", "", None)),
]


if os.environ.get("FAV_ONLY"):  # jen tyhle: [[text, [action, scope, continuous]], …]
    CASES = [(t, tuple(w)) for t, w in json.loads(os.environ["FAV_ONLY"])]


def load_book() -> tuple[VoteBook, str, list[str]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("""SELECT target, key, voter, vote, COALESCE(who,''), COALESCE(video_id,''),
                                 COALESCE(artist,''), COALESCE(title,''), ts FROM votes ORDER BY ts""").fetchall()
    meta = con.execute("""SELECT id, client, COALESCE(who,''), playlist_id, COALESCE(title,''), created,
                                 fetched, COALESCE(total,0), COALESCE(skipped,0), COALESCE(cut,0)
                            FROM imports ORDER BY created""").fetchall()
    items = con.execute("""SELECT import_id, key, COALESCE(video_id,''), COALESCE(artist,''),
                                  COALESCE(title,''), added, COALESCE(pos,0)
                             FROM import_items ORDER BY import_id, pos""").fetchall()
    hist = con.execute("SELECT artist, title, COALESCE(outcome,'') FROM plays ORDER BY ts DESC LIMIT 25").fetchall()
    con.close()
    book = VoteBook(cfg=Config(**DEFAULTS)).load(rows).load_imports(meta, items)
    asker = meta[0][1] if meta else ""  # ten, kdo importoval (vlastník)
    return book, asker, [f"{a} — {t} [{o}]" for a, t, o in hist]


async def main() -> None:
    book, asker, history = load_book()
    office = book.describe(asker=asker)
    print("OFFICE LINE:\n" + office + "\n")
    binary = native_codex(default_binary()) or default_binary()
    work = tempfile.mkdtemp(prefix="ytdj-favcheck-")
    app = AppServer(binary, work, max_turns_per_thread=1, effort=effort_from_env())
    rows = []
    try:
        for text, want in CASES:
            for run in range(RUNS):
                state = render_state(
                    now_playing=history[0].rsplit(" [", 1)[0] if history else "",
                    queue=[], pools="oblíbené kanceláře a podobné (40)", history=history,
                    taste="", requested=[], intent="", focus="", office=office)
                prompt = f"{ROLE}\n\n{state}\n\nUživatel říká: {text}"
                t0 = time.monotonic()
                res = await app.turn(prompt, DECISION_SCHEMA, timeout=90)
                took = time.monotonic() - t0
                data = json.loads(res.text)
                intent = build_intent(text, data)
                got_action = "favourites" if intent.favourites else intent.kind
                ok = got_action == want[0]
                if ok and want[0] == "favourites":
                    ok = (intent.favourites == want[1] and intent.fav_continuous == want[2]
                          and intent.fav_alternate)
                if ok and want[0] == "artist":
                    ok = [a.lower() for a in intent.artists] == ["kabát"]
                rows.append((text, run + 1, got_action, intent.favourites, intent.fav_continuous,
                             intent.fav_alternate, intent.artists, ok, round(took, 1)))
                print(f"{'OK ' if ok else 'BAD'} {text!r} #{run + 1}: {got_action} scope={intent.favourites!r}"
                      f" cont={intent.fav_continuous} alt={intent.fav_alternate} artists={intent.artists}"
                      f" mood={intent.mood!r} {took:.1f}s", flush=True)
    finally:
        await app.close()
    good = sum(1 for r in rows if r[7])
    print(f"\n{good}/{len(rows)} correct")
    Path("/tmp/ytdj-favcheck/result.json").write_text(json.dumps(rows, ensure_ascii=False))


asyncio.run(main())
