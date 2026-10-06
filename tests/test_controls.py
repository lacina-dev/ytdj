"""Posun ve skladbě, „Zahrát znovu" a klik do fronty (přání kolegů 6. 10. 2026,
POZADAVKY #54, #55, #56) — proti falešnému mpv, proti frontě přání a webu,
a tytéž věci na skutečném mpv s místním zvukem (bez sítě, výstup --ao=null):

    .venv/bin/python -m unittest tests.test_controls -v
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-controls-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402  (Rig: opravdový MpvPlayer + fronta přání nad falešným mpv)
from test_player_queue import Harness, T, vid  # noqa: E402
from ytdj import votes as V  # noqa: E402
from ytdj import telemetry  # noqa: E402
from ytdj.__main__ import App  # noqa: E402
from ytdj.agent.intent import SkipWatch  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.nicks import tag_of  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.player.mpv import AUDIO_BUFFER, SEEK_END_GUARD  # noqa: E402
from ytdj.votes import SONG  # noqa: E402
from ytdj.web import server as web  # noqa: E402
from ytdj.wishes import MAX_ACTIVE, Refused, TooMany  # noqa: E402

run = tw.run
PETR, JANA, KAREL = "client-petr", "client-jana", "client-karel"
INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"
OLD = Track("zzzzzzznovu", "Stará známá", "Kapela", duration=180)


async def sounds(x, duration: float = 200.0, pos: float | None = None) -> None:
    """Hrající položka falešného mpv opravdu hraje a má délku."""
    x.fake.duration = duration
    if pos is not None:
        x.fake.time_pos = pos
    x.fake.sound()
    for _ in range(200):
        if (x.player._load or {}).get("t_play") is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("skladba se nerozehrála")


def watch(player) -> list[tuple[str, str | None]]:
    """Co přehrávač ohlásil (druh události, videoId)."""
    seen: list[tuple[str, str | None]] = []

    async def handler(ev):
        seen.append((ev.kind, ev.track.id if ev.track else None))

    player.on_event(handler)
    return seen


def since_start(seen: list[tuple[str, str | None]], first: str) -> list[tuple[str, str | None]]:
    """Události od chvíle, kdy začala první skladba.

    Skutečné mpv spuštěné s --idle ohlásí při startu jednou "idle" (vstup do
    klidu, ještě před první skladbou). Dostaneme ho jen tehdy, když se k jeho
    socketu připojíme dřív, než do klidu vstoupí — záleží na zatížení stroje
    (měřeno: 6 z 36 běhů při šesti souběžných, vždy v čase 0,000–0,001 s a
    vždy před prvním start-file; po začátku první skladby ani jednou). Testy
    proto hodnotí události od začátku první skladby — "idle" kdykoli potom
    (po posunu, na konci skladby, po kliku) by byla chyba a test ji chytí.
    """
    return seen[seen.index(("start", first)):]


def as_app(rig) -> SimpleNamespace:
    """Historie a série přeskočení jako v aplikaci (App._on_event nad Rigem)."""
    ns = SimpleNamespace(store=rig.store, pools=rig.pools, dj=rig.dj, wishes=rig.wq,
                         skips=SkipWatch(), _set_status=lambda text: None, _now=lambda: 0.0)

    async def handler(ev):
        with mock.patch("builtins.print"):
            await App._on_event(ns, ev)  # type: ignore[arg-type]

    rig.player.on_event(handler)
    return ns


def outcomes(rig) -> dict[str, str]:
    rig.store.flush()
    return {r.video_id: r.outcome for r in reversed(rig.store.recent_history(40))}


# --------------------------------------------------------------------------
# přehrávač: posun (falešné mpv)
# --------------------------------------------------------------------------


class PlayerSeek(unittest.TestCase):
    """F-ZVUK-26: absolutní skok v téže skladbě, oříznutý, nic jiného se nemění."""

    def test_absolute_seek_is_clamped_to_the_track(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                seen = watch(p)
                await p.enqueue([T(i) for i in range(4)])
                await h.settle()
                await sounds(h, 200.0, 50.0)
                before = (h.fake.current_vid(), h.fake.upcoming())
                self.assertEqual(SEEK_END_GUARD, AUDIO_BUFFER + 1.0)  # zásoba zvuku + 1 s
                res = await p.seek(position=120)
                self.assertEqual((res["from"], res["to"], res["duration"], res["video_id"]),
                                 (50.0, 120.0, 200.0, vid(0)))
                self.assertEqual((await p.seek(position=1000))["to"], 200.0 - SEEK_END_GUARD)
                self.assertEqual((await p.seek(position=199.5))["to"], 197.0)  # ne do konce skladby
                self.assertEqual((await p.seek(position=-30))["to"], 0.0)
                h.fake.time_pos = 60.0
                self.assertEqual((await p.seek(delta=10))["to"], 70.0)
                self.assertEqual((await p.seek(delta=-10))["to"], 60.0)
                self.assertEqual((await p.seek(delta=-500))["to"], 0.0)
                self.assertEqual(h.fake.seeks, [120.0, 197.0, 197.0, 0.0, 70.0, 60.0, 0.0])
                seeks = [c for c in h.fake.log if isinstance(c, list) and c[0] == "seek"]
                self.assertTrue(all(c[2] == "absolute" for c in seeks))  # nikdy relativně
                await h.settle()
                # jen posun: táž skladba, tatáž fronta, žádný konec skladby
                self.assertEqual((h.fake.current_vid(), h.fake.upcoming()), before)
                self.assertEqual([k for k, _ in seen if k not in ("start", "sound")], [])
                self.assertEqual([k for k, _ in h.events if k in ("track.end", "track.request")][1:], [])

        run(go())

    def test_no_seek_when_there_is_nothing_to_seek_in(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                self.assertIsNone(await p.seek(position=10))  # nic nehraje
                await p.enqueue([T(i) for i in range(3)])
                await h.settle()
                h.fake.duration = 200.0
                self.assertIsNone(await p.seek(position=10))  # ještě se načítá (zvuk neteče)
                await sounds(h, 0.0)
                self.assertIsNone(await p.seek(position=10))  # proud bez délky
                h.fake.duration = SEEK_END_GUARD + 1
                self.assertIsNone(await p.seek(position=1))  # tak krátké, že není kam
                h.fake.duration = 200.0
                self.assertIsNone(await p.seek(position=float("nan")))
                self.assertIsNone(await p.seek(position="deset"))  # type: ignore[arg-type]
                p._outage = {"reason": "network", "since": 0, "detail": ""}
                self.assertIsNone(await p.seek(position=10))  # výpadek: stojí se
                p._outage = None
                self.assertEqual(h.fake.seeks, [])
                self.assertEqual((await p.seek(position=10))["to"], 10.0)

        run(go())

    def test_no_seek_while_only_the_end_of_the_track_sounds(self) -> None:
        """Dohrávající konec (mpv už je u další skladby): posun by trefil tu další."""
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(3)])
                await h.settle()
                await sounds(h, 200.0)
                p._handle_event({"event": "property-change", "name": "duration", "data": 200.0})
                p._handle_event({"event": "property-change", "name": "time-pos", "data": 197.5})
                h.fake.finish_current()
                await h.settle(0.05)
                self.assertEqual((await p.status()).current.id, vid(0))  # navenek pořád ona
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertIsNone(await p.seek(position=30))
                self.assertEqual(h.fake.seeks, [])

        run(go())

    def test_status_shows_the_new_position_at_once(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(2)])
                await h.settle()
                await sounds(h, 200.0, 12.0)
                await p.seek(position=150)
                # mpv ještě hledá ve streamu a hlásí staré místo — navenek už platí nové
                h.fake.time_pos = 12.3
                st = await p.status()
                self.assertAlmostEqual(st.position, 150.0, delta=0.01)
                self.assertEqual(st.current.id, vid(0))
                # jakmile mpv hlásí nové místo, platí jeho čas
                h.fake.time_pos = 150.4
                self.assertAlmostEqual((await p.status()).position, 150.4, delta=0.01)
                # a po usazení se na staré místo nikdy nevrátí samo od sebe
                p._seek["t0"] -= mpvmod.SEEK_SETTLE + 0.1
                h.fake.time_pos = 152.0
                self.assertAlmostEqual((await p.status()).position, 152.0, delta=0.01)

        run(go())

    def test_refill_after_a_seek_is_not_a_stream_stall(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player

                async def idle_for(seconds: float) -> None:
                    h.fake._send({"event": "property-change", "name": "core-idle", "data": True})
                    await asyncio.sleep(seconds)
                    h.fake._send({"event": "property-change", "name": "core-idle", "data": False})
                    await h.settle(0.05)

                await p.enqueue([T(i) for i in range(2)])
                await h.settle()
                await sounds(h, 200.0)
                await p.seek(position=90)
                await idle_for(0.3)  # mpv plní zásobu od nového místa
                self.assertEqual([k for k, _ in h.events if k == "track.stall"], [])
                self.assertEqual(p._load["stalls"], 0)
                p._seek["t0"] -= mpvmod.SEEK_SETTLE + 0.1  # bez posunu je to výpadek jako dřív
                await idle_for(0.3)
                self.assertEqual(len([k for k, _ in h.events if k == "track.stall"]), 1)

        run(go())


# --------------------------------------------------------------------------
# přehrávač: skladba z fronty hned (falešné mpv)
# --------------------------------------------------------------------------


class PlayerJump(unittest.TestCase):
    """F-FRONTA-23: skladba z fronty hned za hrající, ostatní zůstanou v pořadí."""

    def _now(self, old: bool) -> None:
        async def go():
            async with Harness(old=old) as h:
                p = h.player
                seen = watch(p)
                await p.enqueue([T(i) for i in range(6)])
                await h.settle()
                self.assertEqual(await p.play_from_queue(vid(3), True), "now")
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(3))
                # přeskočené zůstaly hned za ní, ve svém pořadí — nic nezmizelo
                self.assertEqual(h.fake.upcoming(), [vid(1), vid(2), vid(4), vid(5)])
                self.assertEqual(p.upcoming_ids(), h.fake.upcoming())  # pravdivá fronta
                self.assertEqual(await h.queue_ids(), h.fake.upcoming())
                # hrající odsunul klik, ne Další: "replaced", žádné "skipped"
                ends = [(k, v) for k, v in seen if k not in ("start", "sound")]
                self.assertEqual(ends, [("replaced", vid(0))])
                self.assertEqual(h.fake.started, [vid(0), vid(3)])  # 1 a 2 nezačaly hrát
                req = [f for k, f in h.events if k == "track.request"][-1]
                self.assertEqual(req["why"], "replace")

        run(go())

    def test_clicked_track_plays_now_and_the_jumped_ones_keep_their_order(self) -> None:
        self._now(old=False)

    def test_the_same_on_mpv_without_insert(self) -> None:
        self._now(old=True)

    def test_as_next_keeps_what_plays(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                seen = watch(p)
                await p.enqueue([T(i) for i in range(6)])
                await h.settle()
                self.assertEqual(await p.play_from_queue(vid(4), False), "next")
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(0))  # nic se neutnulo
                self.assertEqual(h.fake.upcoming(), [vid(4), vid(1), vid(2), vid(3), vid(5)])
                self.assertEqual(await h.queue_ids(), h.fake.upcoming())
                self.assertEqual([k for k, _ in seen if k not in ("start", "sound")], [])
                # ta hned další: nic se nehýbe
                n = len(h.fake.log)
                self.assertEqual(await p.play_from_queue(vid(4), False), "next")
                self.assertEqual([c for c in h.fake.log[n:] if c[0] == "playlist-move"], [])
                # resolver dostane nové okno (klik = chystat tuhle první)
                await h.settle(0.4)
                self.assertEqual(h.aheads()[-1][0], vid(4))

        run(go())

    def test_what_is_not_in_the_queue_changes_nothing(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                self.assertIsNone(await p.play_from_queue(vid(1), True))  # nic nehraje
                await p.enqueue([T(i) for i in range(4)])
                await h.settle()
                before = (h.fake.current_vid(), h.fake.upcoming())
                self.assertIsNone(await p.play_from_queue(vid(9), True))  # není ve frontě
                self.assertIsNone(await p.play_from_queue(vid(0), True))  # ta hrající
                p._outage = {"reason": "network", "since": 0, "detail": "", "resume_entry": None}
                self.assertIsNone(await p.play_from_queue(vid(2), True))  # výpadek
                p._outage = None
                self.assertEqual((h.fake.current_vid(), h.fake.upcoming()), before)

        run(go())

    def test_during_the_audible_end_nothing_unheard_is_cut(self) -> None:
        """Zní konec skladby ze zásoby a mpv už hraje další: ta se klikem neutne
        (zmizela by z fronty, aniž ji kdo slyšel) — klik znamená „jako další"."""
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(5)])
                await h.settle()
                p._handle_event({"event": "property-change", "name": "duration", "data": 200.0})
                p._handle_event({"event": "property-change", "name": "time-pos", "data": 197.5})
                h.fake.finish_current()
                await h.settle(0.05)
                self.assertEqual((await p.status()).current.id, vid(0))
                self.assertEqual(await p.play_from_queue(vid(3), True), "next")
                await h.settle(0.05)
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertEqual(h.fake.upcoming(), [vid(3), vid(2), vid(4)])
                # klik na tu, která už nabíhá, jen utne doznívání
                self.assertEqual(await p.play_from_queue(vid(1), True), "now")
                await h.settle(0.1)
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertEqual((await p.status()).current.id, vid(1))

        run(go())


# --------------------------------------------------------------------------
# fronta přání: kdo smí posouvat
# --------------------------------------------------------------------------


class SeekRule(unittest.TestCase):
    """F-ZVUK-27: v podkresu posouvá kdokoli, v přání jen jeho autor."""

    def test_anybody_seeks_in_the_background(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                await sounds(rig, 200.0, 30.0)
                ok, msg, info = await rig.wq.seek(position=100, by="Karel", client=KAREL)
                self.assertEqual((ok, msg, info["to"]), (True, "", 100.0))
                ok, _, info = await rig.wq.seek(delta=-10, by="někdo")  # i bez id (starý klient)
                self.assertEqual((ok, info["to"]), (True, 90.0))
                ev = [f for k, f in rig.events if k == "player.seek"]
                self.assertEqual([(f["by"], f["from_s"], f["to_s"]) for f in ev],
                                 [("Karel", 30.0, 100.0), ("někdo", 100.0, 90.0)])
                self.assertEqual(ev[0]["cid"], KAREL[-6:])
                self.assertEqual(ev[0]["video_id"], rig.fake.current_vid())

        run(go())

    def test_only_the_author_seeks_in_a_wish(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.2)
                await sounds(rig, 200.0, 20.0)
                ok, msg, info = await rig.wq.seek(position=80, by="Jana", client=JANA)
                self.assertFalse(ok)
                self.assertEqual(msg, "Teď hraje, co si přeje Petr — v cizím přání se neposouvá.")
                self.assertEqual(info, {"why": "foreign_wish"})
                ok, _, _ = await rig.wq.seek(delta=10, by="někdo")  # bez id není autor nikdo
                self.assertFalse(ok)
                self.assertEqual(rig.fake.seeks, [])
                refused = [f for k, f in rig.events if k == "player.seek_refused"]
                self.assertEqual([f["why"] for f in refused], ["foreign_wish", "foreign_wish"])
                ok, _, info = await rig.wq.seek(position=80, by="Petr", client=PETR)
                self.assertEqual((ok, info["to"], rig.fake.seeks), (True, 80.0, [80.0]))
                # jméno nestačí: kdo je kdo, určuje prohlížeč (jako u Další)
                ok, _, _ = await rig.wq.seek(position=10, by="Petr", client=KAREL)
                self.assertFalse(ok)

        run(go())

    def test_display_is_the_author_of_display_wishes(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                d = rig.wq.submit("Holky z naší školky", "", source="panel",
                                  client={"id": "panel-0001"})
                await rig.until(lambda: d.state == "playing")
                await rig.settle(0.2)
                await sounds(rig, 200.0, 5.0)
                ok, _, _ = await rig.wq.seek(delta=10, by="Jana", client=JANA)
                self.assertFalse(ok)
                ok, _, info = await rig.wq.seek(delta=10, by="displej")
                self.assertEqual((ok, info["to"]), (True, 15.0))

        run(go())

    def test_seeking_is_not_a_skip_a_vote_or_a_play(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                app = as_app(rig)
                book, _ = _wire(rig)
                seen = watch(rig.player)
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.2)
                await sounds(rig, 200.0, 20.0)
                cur, queue = rig.fake.current_vid(), rig.upcoming()
                state = (p.state, p.played, set(p.done_ids), p.current, p.skips_others, p.skipped_by)
                turns = (rig.wq.turns.turn_no, rig.wq.turns.block)
                n_seen, n_started = len(seen), len(rig.fake.started)
                for want in (150, 197, 0, 60):
                    ok, _, _ = await rig.wq.seek(position=want, by="Petr", client=PETR)
                    self.assertTrue(ok)
                await rig.settle(0.3)
                self.assertEqual((rig.fake.current_vid(), rig.upcoming()), (cur, queue))
                self.assertEqual(seen[n_seen:], [])  # přehrávač neohlásil nic
                self.assertEqual(len(rig.fake.started), n_started)
                self.assertEqual((p.state, p.played, set(p.done_ids), p.current, p.skips_others,
                                  p.skipped_by), state)
                self.assertEqual((rig.wq.turns.turn_no, rig.wq.turns.block), turns)
                self.assertEqual(app.skips._skips, [])  # série přeskočení (rádio) nic neví
                self.assertEqual(outcomes(rig)[cur], "started")  # v historii pořád jen "hraje"
                self.assertEqual(book.items, {})  # žádný hlas
                kinds = rig.kinds()
                for bad in ("request.skipped", "request.skip_repeat", "request.done", "vote.cast"):
                    self.assertNotIn(bad, kinds)
                self.assertEqual(kinds.count("player.seek"), 4)

        run(go())

    def test_honest_no_when_it_cannot_be_done(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                ok, msg, info = await rig.wq.seek(position=10, by="Karel", client=KAREL)
                self.assertFalse(ok)  # ticho
                self.assertIn("posunout nejde", msg)
                self.assertEqual(info, {"why": "not_seekable"})
                await rig.background()
                ok, msg, _ = await rig.wq.seek(position=10, by="Karel", client=KAREL)
                self.assertFalse(ok)  # skladba se teprve načítá
                self.assertIn("načítá", msg)

        run(go())


def _wire(rig):
    app = SimpleNamespace(store=rig.store, cfg=rig.cfg, wishes=rig.wq, pools=rig.pools,
                          dj=rig.dj, player=rig.player)
    return V.wire(app), app


# --------------------------------------------------------------------------
# fronta přání: klik do „Hraje dál"
# --------------------------------------------------------------------------


class ClickToPlay(unittest.TestCase):
    """F-FRONTA-22 až 24: hned, ale férově."""

    def test_own_wish_song_plays_now_and_the_jumped_ones_follow_in_order(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                app = as_app(rig)
                seen = watch(rig.player)
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.3)
                up = rig.upcoming()
                self.assertEqual(rig.owners()[:3], ["Petr"] * 3)
                cut = rig.fake.current_vid()
                rig.player._res_ready.update(up)
                ok, msg, info = await rig.wq.play_queued(up[2], by="Petr", client=PETR)
                self.assertEqual((ok, msg, info), (True, "Hraje hned.", {"mode": "now", "video_id": up[2]}))
                await rig.until(lambda: rig.fake.current_vid() == up[2] and p.current == up[2])
                await rig.settle(0.3)
                self.assertEqual(rig.upcoming()[:2], up[:2])  # přeskočené hned za ní, v pořadí
                self.assertEqual(rig.owners()[:2], ["Petr", "Petr"])
                self.assertEqual(rig.player.upcoming_ids(), rig.upcoming())
                self.assertEqual([t.id for t in p.pending()][:2], up[:2])
                self.assertEqual(p.state, "playing")
                # přeskočené nezačaly hrát, nejsou odepsané a nikdo je "nepřeskočil"
                self.assertFalse(set(up[:2]) & set(rig.fake.started))
                self.assertFalse(set(up[:2]) & p.done_ids)
                self.assertEqual((p.skips_others, p.skipped_by), (0, ""))
                self.assertNotIn("skipped", [k for k, _ in seen])
                self.assertEqual(app.skips._skips, [])
                hist = outcomes(rig)
                self.assertEqual(hist[cut], "replaced")  # utnutá: nahrazeno, ne přeskočeno
                self.assertEqual(hist[up[2]], "started")
                self.assertFalse(set(up[:2]) & set(hist))  # do historie nepřišly
                self.assertNotIn("request.skipped", rig.kinds())
                jump = [f for k, f in rig.events if k == "queue.jump"][-1]
                self.assertEqual((jump["by"], jump["mode"], jump["jumped"], jump["cut_id"],
                                  jump["own_wish"]), ("Petr", "now", 2, cut, p.id))

        run(go())

    def test_background_song_plays_now_when_no_wish_is_in_the_way(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                app = as_app(rig)
                await rig.background()
                up = rig.upcoming()
                cut = rig.fake.current_vid()
                rig.player._res_ready.update(up)
                ok, msg, info = await rig.wq.play_queued(up[3], by="Karel", client=KAREL)
                self.assertEqual((ok, info["mode"]), (True, "now"))
                await rig.until(lambda: rig.fake.current_vid() == up[3])
                await rig.settle(0.2)
                self.assertEqual(rig.upcoming(), up[:3] + up[4:])
                self.assertEqual(outcomes(rig)[cut], "replaced")
                self.assertEqual(app.skips._skips, [])  # rádio se kvůli kliku nepřeladí
                # i bez id klienta (starý prohlížeč): podkres není ničí
                ok, _, info = await rig.wq.play_queued(up[1], by="někdo")
                self.assertEqual((ok, info["mode"]), (True, "now"))

        run(go())

    def test_nothing_of_somebody_elses_wish_is_ever_jumped(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued")
                await rig.settle(0.3)
                up, owners = rig.upcoming(), rig.owners()
                rig.player._res_ready.update(up)
                jana = owners.index("Jana")
                self.assertGreater(jana, 0)  # před Janou ještě dohrává Petrovo kolo
                later = next(i for i, o in enumerate(owners) if o == "Petr" and i > jana)
                radio = owners.index("-")
                cur = rig.fake.current_vid()
                # Petr: jeho pozdější skladba je až za Janinou → ne
                ok, msg, info = await rig.wq.play_queued(up[later], by="Petr", client=PETR)
                self.assertEqual((ok, msg, info), (False, "Nejdřív dohraje přání od Jana.",
                                                   {"why": "jumps_wish"}))
                # Jana: její skladba je za Petrovou → taky ne; cizí skladbu si nepustí nikdo
                ok, msg, _ = await rig.wq.play_queued(up[jana], by="Jana", client=JANA)
                self.assertEqual((ok, msg), (False, "Nejdřív dohraje přání od Petr."))
                ok, msg, info = await rig.wq.play_queued(up[0], by="Jana", client=JANA)
                self.assertFalse(ok)
                self.assertEqual(info, {"why": "foreign_track"})
                self.assertIn("přání od Petr", msg)
                # Karel bez přání: podkres je až za přáními obou
                ok, msg, _ = await rig.wq.play_queued(up[radio], by="Karel", client=KAREL)
                self.assertEqual((ok, msg), (False, "Nejdřív dohraje přání od Petr."))
                ok, _, _ = await rig.wq.play_queued(up[radio], by="někdo")
                self.assertFalse(ok)
                await rig.settle(0.2)
                self.assertEqual((rig.fake.current_vid(), rig.upcoming()), (cur, up))  # nic se nehnulo
                self.assertNotIn("playlist-move", [c[0] for c in rig.fake.log[-8:] if isinstance(c, list)])
                refused = [f["why"] for k, f in rig.events if k == "queue.jump_refused"]
                self.assertEqual(refused, ["jumps_wish", "jumps_wish", "foreign_track", "jumps_wish",
                                           "jumps_wish"])
                # Petr smí svou hned další (před Janou nikdo není): hraje hned
                ok, _, info = await rig.wq.play_queued(up[0], by="Petr", client=PETR)
                self.assertEqual((ok, info["mode"]), (True, "now"))
                await rig.until(lambda: rig.fake.current_vid() == up[0])
                await rig.settle(0.2)
                self.assertEqual(rig.upcoming()[0], up[jana])  # Jana je na řadě jako předtím

        run(go())

    def test_somebody_elses_wish_playing_means_play_as_next(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                seen = watch(rig.player)
                await rig.background()
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "playing")
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "queued")
                await rig.settle(0.3)
                up = rig.upcoming()
                self.assertEqual(rig.owners()[:3], ["Petr"] * 3)
                rig.player._res_ready.update(up)
                holky = rig.fake.current_vid()
                ok, msg, info = await rig.wq.play_queued(up[2], by="Petr", client=PETR)
                self.assertEqual((ok, info["mode"]), (True, "next"))
                self.assertIn("Teď hraje, co si přeje Jana — to se nepřerušuje.", msg)
                self.assertIn("zahraje hned potom", msg)
                await rig.settle(0.4)  # i po přeplánování
                self.assertEqual(rig.fake.current_vid(), holky)  # Janino hraje dál
                self.assertEqual(j.state, "playing")
                self.assertEqual(rig.upcoming()[:3], [up[2], up[0], up[1]])
                self.assertEqual([k for k, _ in seen if k in ("skipped", "replaced")][1:], [])
                self.assertEqual([f["why"] for k, f in rig.events if k == "queue.jump"],
                                 ["foreign_current"])
                # Jana ve svém přání smí: klik na Petrovu skladbu ale ne
                ok, _, info = await rig.wq.play_queued(up[2], by="Jana", client=JANA)
                self.assertEqual((ok, info["why"]), (False, "foreign_track"))

        run(go())

    def test_not_prepared_yet_means_play_as_next(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                up = rig.upcoming()
                cur = rig.fake.current_vid()
                ok, msg, info = await rig.wq.play_queued(up[2], by="Karel", client=KAREL)
                self.assertEqual((ok, info["mode"]), (True, "next"))
                self.assertIn("se ještě připravuje — zahraje hned po téhle skladbě", msg)
                await rig.settle(0.2)
                self.assertEqual(rig.fake.current_vid(), cur)  # žádné ticho při čekání na skladbu
                self.assertEqual(rig.upcoming(), [up[2], up[0], up[1]] + up[3:])
                self.assertEqual([f["why"] for k, f in rig.events if k == "queue.jump"], ["not_ready"])

        run(go())

    def test_background_as_next_stays_ahead_of_own_wishes_only(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "playing")
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "queued")
                await rig.settle(0.3)
                up, owners = rig.upcoming(), rig.owners()
                rig.player._res_ready.update(up)
                radio = up[owners.index("-")]
                ok, _, info = await rig.wq.play_queued(radio, by="Petr", client=PETR)
                self.assertEqual((ok, info["mode"]), (True, "next"))  # Janino se neutne
                await rig.settle(0.4)
                self.assertEqual(rig.upcoming()[:2], [radio, up[0]])  # před jeho vlastními drží
                # přijde přání někoho dalšího: podkres před cizí přání nepatří
                tw.SCRIPT.setdefault("Dancing Queen", tw.SCRIPT["Dancing Queen"])
                k = rig.wq.submit("Dancing Queen", "Karel")
                await rig.until(lambda: k.state == "queued")
                await rig.settle(0.4)
                now_owners = rig.owners()
                self.assertGreater(rig.upcoming().index(radio), now_owners.index("Karel"))
                self.assertNotEqual(now_owners[0], "-")

        run(go())

    def test_click_on_a_song_that_is_gone_or_in_an_outage_is_said(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                ok, msg, _ = await rig.wq.play_queued(vid("nic"), by="Karel", client=KAREL)
                self.assertEqual((ok, msg), (False, "Tahle skladba už ve frontě není."))
                await rig.background()
                ok, msg, _ = await rig.wq.play_queued("x" * 11, by="Karel", client=KAREL)
                self.assertEqual((ok, msg), (False, "Tahle skladba už ve frontě není."))
                rig.player._outage = {"reason": "network", "since": 0, "detail": "",
                                      "resume_entry": None}
                ok, msg, info = await rig.wq.play_queued(rig.upcoming()[0], by="Karel", client=KAREL)
                self.assertEqual((ok, info["why"]), (False, "outage"))
                self.assertIn("YouTube teď nejede", msg)
                rig.player._outage = None

        run(go())

    def test_deliberate_pause_is_respected(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                up = rig.upcoming()
                rig.player._res_ready.update(up)
                rig.wq.note_pause(True)
                await rig.player.toggle_pause(True)
                ok, msg, _ = await rig.wq.play_queued(up[1], by="Karel", client=KAREL)
                self.assertTrue(ok)
                self.assertIn("Hudba je pozastavená", msg)
                self.assertTrue(rig.player._paused)

        run(go())


# --------------------------------------------------------------------------
# „Zahrát znovu"
# --------------------------------------------------------------------------


class Replay(unittest.TestCase):
    """F-PRANI-28, F-PRANI-29: přání přesně té skladby, do fronty jako každé jiné."""

    def test_it_is_that_persons_wish_for_exactly_that_track_without_the_dj(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                w = rig.wq.replay(OLD, "Petr", client={"id": PETR, "ip": "10.0.0.7"})
                self.assertEqual((w.who, w.cid, w.state, w.text), ("Petr", PETR, "waiting",
                                                                    "Znovu: Kapela — Stará známá"))
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                self.assertEqual([t.id for t in w.tracks], [OLD.id])  # přesně ta, žádná jiná verze
                self.assertEqual((w.via, w.kind), ("replay", "songs"))
                self.assertEqual(rig.asked, [])  # model se neptal
                self.assertEqual([c for c in rig.catalog.calls if c[0] == "search_song"], [])
                self.assertIn("Zařadil jsem: Stará známá (Kapela)", w.reply)
                await rig.until(lambda: rig.fake.current_vid() == OLD.id)  # první na řadě utne podkres
                self.assertEqual(rig.wq.reason_for(OLD.id)["who"], "Petr")
                done = [f for k, f in rig.events if k == "request.created"][-1]
                self.assertEqual(done["replay"], OLD.id)
                # evidence vyžádaných: počítá se jako každé jiné přání skladby
                rig.store.flush()
                self.assertIn(OLD.id, [r.video_id for r in rig.store.top_requested(5)])

        run(go())

    def test_the_label_is_never_read_as_a_command_or_a_correction(self) -> None:
        async def go():
            async with tw.Rig(codex_delay=0.5) as rig:
                await rig.background()
                first = rig.wq.submit("Dancing Queen", "Petr")  # čeká na DJe
                trap = Track("zzzzzzzpast", "Ne, radši hned teď něco jiného (zruš to)", "Další", 200)
                w = rig.wq.replay(trap, "Petr", client={"id": PETR})
                self.assertEqual((w.cut, w.play_next, w.note), (False, False, ""))
                self.assertEqual(first.state in ("waiting", "thinking"), True)  # nic nenahradilo
                vol = Track("zzzzzzzzvol", "hlasitost 100", "", 200)
                v = rig.wq.replay(vol, "Petr", client={"id": PETR})
                await rig.until(lambda: v.state in ("queued", "playing") and w.state in ("queued", "playing"))
                self.assertEqual((v.kind, [t.id for t in v.tracks]), ("songs", [vol.id]))  # ne povel
                await rig.until(lambda: first.state in ("queued", "playing"))
                self.assertEqual([x.state for x in (first, w, v) if x.state == "replaced"], [])

        run(go())

    def test_it_takes_turns_with_colleagues_like_any_wish(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.2)
                cur = rig.fake.current_vid()
                again = [Track(f"zzzzzzzzzz{i}", f"Znovu {i}", "Kapela", 200) for i in range(3)]
                mine = [rig.wq.replay(t, "Jana", client={"id": JANA}) for t in again]
                await rig.until(lambda: all(w.state == "queued" for w in mine))
                await rig.settle(0.3)
                self.assertEqual(rig.fake.current_vid(), cur)  # cizí přání se neutne
                owners = rig.owners()
                # tři „znovu" od jednoho člověka nejsou tři místa před kolegou: střídá se po kolech
                first_six = owners[:6]
                self.assertIn("Petr", first_six)
                self.assertLess(first_six.count("Jana"), 4)
                jana = [i for i, o in enumerate(owners) if o == "Jana"]
                petr_between = [i for i, o in enumerate(owners) if o == "Petr" and i < jana[-1]]
                self.assertTrue(petr_between, owners)

        run(go())

    def test_a_song_voted_out_is_refused_with_the_reason(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                book, _ = _wire(rig)
                await rig.background()
                book.cast(SONG, PETR, -1, "Petr", track=OLD)
                book.cast(SONG, JANA, -1, "Jana", track=OLD)
                self.assertEqual(book.blocked(OLD), SONG)
                with self.assertRaises(Refused) as ctx:
                    rig.wq.replay(OLD, "Karel", client={"id": KAREL})
                self.assertEqual(str(ctx.exception),
                                 "Tohle znovu nezařadím — vyřazená hlasováním — Petr, Jana.")
                self.assertEqual(rig.wq.wishes, [])  # žádné přání nevzniklo
                self.assertEqual([f["why"] for k, f in rig.events if k == "request.replay_refused"],
                                 ["banned"])
                # jeden 👎 nestačí (práh je 2 lidi): pak to jde
                book.cast(SONG, JANA, 0, "Jana", track=OLD)
                self.assertIsNone(book.blocked(OLD))
                self.assertEqual(rig.wq.replay(OLD, "Karel", client={"id": KAREL}).replay, OLD)

        run(go())

    def test_it_counts_against_the_persons_limit_of_open_wishes(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                tracks = [Track(f"zzzzzzzzzl{i}", f"Limit {i}", "Kapela", 200) for i in range(MAX_ACTIVE + 1)]
                for t in tracks[:MAX_ACTIVE]:
                    rig.wq.replay(t, "Jana", client={"id": JANA})
                with self.assertRaises(TooMany):
                    rig.wq.replay(tracks[MAX_ACTIVE], "Jana", client={"id": JANA})
                # strop je na člověka: kolega může dál
                self.assertEqual(rig.wq.replay(tracks[MAX_ACTIVE], "Karel", client={"id": KAREL}).who,
                                 "Karel")

        run(go())

    def test_the_same_song_is_not_queued_twice_for_one_person(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                a = rig.wq.replay(OLD, "Jana", client={"id": JANA})
                with self.assertRaises(Refused) as ctx:  # dvojí ťuknutí
                    rig.wq.replay(OLD, "Jana", client={"id": JANA})
                self.assertEqual(str(ctx.exception), "„Stará známá“ už ve frontě máš.")
                await rig.until(lambda: a.state == "queued")
                with self.assertRaises(Refused):
                    rig.wq.replay(OLD, "Jana", client={"id": JANA})
                # kolega si ji přát smí — zazní jednou (F-FRONTA-10)
                b = rig.wq.replay(OLD, "Karel", client={"id": KAREL})
                await rig.until(lambda: b.state == "queued")
                await rig.settle(0.2)
                self.assertEqual(rig.upcoming().count(OLD.id), 1)

        run(go())

    def test_a_waiting_replay_survives_a_restart_as_that_track(self) -> None:
        async def go():
            async with tw.Rig(codex_delay=0.5) as rig:
                w = rig.wq.replay(OLD, "Petr", client={"id": PETR})
                back = tw.Wish.from_json(json.loads(json.dumps(w.to_json())))
                self.assertEqual((back.replay.id, back.replay.title), (OLD.id, OLD.title))

        run(go())


# --------------------------------------------------------------------------
# web
# --------------------------------------------------------------------------


def post(body: dict, **kw):
    return tw.req(body, **kw)


class WebApi(unittest.TestCase):
    def test_seek_endpoint(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                await sounds(rig, 200.0, 30.0)
                r = await srv._control(post({"action": "seek", "value": 120, "client": KAREL}))
                self.assertEqual((r.status_code, tw.body(r)),
                                 (200, {"ok": True, "position": 120.0, "duration": 200.0}))
                r = await srv._control(post({"action": "seek", "delta": -10, "value": None,
                                             "client": KAREL}))
                self.assertEqual((r.status_code, tw.body(r)["position"]), (200, 110.0))
                r = await srv._control(post({"action": "seek", "value": 5000, "client": KAREL}))
                self.assertEqual(tw.body(r)["position"], 197.0)  # nikdy do posledních 3 s
                for bad in ({"value": "x"}, {"value": True}, {"value": -4}, {}, {"delta": "2"},
                            {"value": None, "delta": None}):
                    r = await srv._control(post({"action": "seek", "client": JANA, **bad}))
                    self.assertEqual(r.status_code, 400, bad)
                self.assertEqual(rig.fake.seeks, [120.0, 110.0, 197.0])
                # stav hned ukáže nové místo
                self.assertEqual((await srv._snapshot())["position"], 197.0)
                ctl = [f for k, f in rig.events if k == "web.control" and f.get("action") == "seek"]
                self.assertEqual((ctl[0]["status"], ctl[0]["to"]), (200, 120.0))

        run(go())

    def test_seek_is_refused_in_somebody_elses_wish_and_when_impossible(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                r = await srv._control(post({"action": "seek", "value": 10, "client": KAREL}))
                self.assertEqual(r.status_code, 409)  # ještě se načítá
                self.assertIn("posunout nejde", tw.body(r)["error"])
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.2)
                await sounds(rig, 200.0, 20.0)
                await asyncio.sleep(web.SEEK_WINDOW)
                r = await srv._control(post({"action": "seek", "value": 10, "client": JANA,
                                             "who": "Petr"}))
                self.assertEqual(r.status_code, 403)
                self.assertEqual(tw.body(r)["error"],
                                 "Teď hraje, co si přeje Petr — v cizím přání se neposouvá.")
                r = await srv._control(post({"action": "seek", "value": 10, "client": PETR}))
                self.assertEqual(r.status_code, 200)
                self.assertEqual(rig.fake.seeks, [10.0])

        run(go())

    def test_seek_is_rate_limited_per_client(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                await sounds(rig, 200.0, 30.0)
                codes = []
                for i in range(8):  # tažení po liště: osm povelů naráz
                    r = await srv._control(post({"action": "seek", "value": 40 + i, "client": KAREL}))
                    codes.append(r.status_code)
                self.assertEqual(codes, [200] * web.SEEK_RATE + [429] * (8 - web.SEEK_RATE))
                self.assertEqual(len(rig.fake.seeks), web.SEEK_RATE)  # mpv dostalo jen tři
                self.assertEqual(web.SEEK_RATE, 3)
                # jiný člověk tím omezený není; a po vteřině to jde zase
                r = await srv._control(post({"action": "seek", "value": 50, "client": JANA}))
                self.assertEqual(r.status_code, 200)
                await asyncio.sleep(web.SEEK_WINDOW + 0.05)
                r = await srv._control(post({"action": "seek", "value": 60, "client": KAREL}))
                self.assertEqual(r.status_code, 200)
                # bez id klienta se počítá adresa
                codes = [(await srv._control(post({"action": "seek", "value": 70}))).status_code
                         for _ in range(5)]
                self.assertEqual(codes.count(200), web.SEEK_RATE)

        run(go())

    def test_everybody_sees_the_new_position_at_once(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                rig.wq.on_change = srv.poke
                await sounds(rig, 200.0, 30.0)
                watcher = asyncio.create_task(tw.sse(srv, 0.6))
                await asyncio.sleep(0.25)
                t0 = time.monotonic()
                await srv._control(post({"action": "seek", "value": 150, "client": KAREL}))
                chunks = await watcher
                took = time.monotonic() - t0
                self.assertLess(took, web.TICK)  # nečekalo se na další tik
                self.assertIn("event: pos\ndata: [150.0]\n\n", chunks)

        run(go())

    def test_jump_endpoint(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                up = rig.upcoming()
                rig.player._res_ready.update(up)
                for bad in ({}, {"video_id": 7}, {"video_id": "krátké"}):
                    r = await srv._control(post({"action": "jump", **bad}))
                    self.assertEqual(r.status_code, 400)
                r = await srv._control(post({"action": "jump", "video_id": "x" * 11, "client": KAREL}))
                self.assertEqual((r.status_code, tw.body(r)["error"]),
                                 (409, "Tahle skladba už ve frontě není."))
                r = await srv._control(post({"action": "jump", "video_id": up[2], "client": KAREL}))
                self.assertEqual((r.status_code, tw.body(r)),
                                 (200, {"ok": True, "mode": "now", "message": "Hraje hned."}))
                await rig.until(lambda: rig.fake.current_vid() == up[2])
                # cizí přání ve frontě: 403 s větou, kdo je na řadě
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued")
                await rig.settle(0.3)
                snap = await srv._snapshot()
                radio = next(q["id"] for q in snap["queue"] if not q.get("req"))
                r = await srv._control(post({"action": "jump", "video_id": radio, "client": KAREL}))
                self.assertEqual(r.status_code, 403)
                self.assertRegex(tw.body(r)["error"], r"^Nejdřív dohraje přání od (Petr|Jana)\.$")
                # stav nese, čí která skladba je (značka, ne id klienta) — stránka podle toho kreslí
                tags = [q["req"]["who_key"] for q in snap["queue"] if q.get("req")]
                self.assertEqual(set(tags), {tag_of(PETR), tag_of(JANA)})
                self.assertNotIn(PETR, json.dumps(snap))

        run(go())

    def test_web_cannot_pose_as_the_display(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                d = rig.wq.submit("Holky z naší školky", "", source="panel",
                                  client={"id": "panel-0001"})
                await rig.until(lambda: d.state == "playing")
                await rig.settle(0.2)
                await sounds(rig, 200.0, 5.0)
                r = await srv._control(post({"action": "seek", "value": 50, "who": "displej"}))
                self.assertEqual(r.status_code, 403)
                r = await srv._control(post({"action": "seek", "value": 50}, ua="ytdj-panel"))
                self.assertEqual(r.status_code, 200)

        run(go())

    def test_replay_endpoint_takes_the_track_from_what_was_played(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                rig.store.record_start(OLD.id, OLD.title, OLD.artist, None)
                rig.store.record_outcome(OLD.id, "finished")
                rig.store.flush()
                r = await srv._prompt(post({"replay": OLD.id, "who": "Petr", "client": PETR,
                                            "wait": False, "title": "Podvržený název"}))
                self.assertEqual(r.status_code, 202)
                data = tw.body(r)
                self.assertTrue(data["id"] and data["token"])
                w = rig.wq.by_id(data["id"])
                # název a interpreta bere server ze své historie, ne od klienta
                self.assertEqual((w.replay.id, w.replay.title, w.replay.artist),
                                 (OLD.id, OLD.title, OLD.artist))
                self.assertEqual(data["request"]["text"], "Znovu: Kapela — Stará známá")
                await rig.until(lambda: w.state in ("queued", "playing"))
                # co v Odehráno není, zahrát znovu nejde; nesmysl taky ne
                r = await srv._prompt(post({"replay": "y" * 11, "who": "Petr", "client": PETR}))
                self.assertEqual((r.status_code, tw.body(r)["error"]),
                                 (404, "Tahle skladba už v Odehráno není."))
                for bad in (17, "", "../../etc", {"id": OLD.id}):
                    r = await srv._prompt(post({"replay": bad, "who": "Petr", "client": PETR}))
                    self.assertEqual(r.status_code, 400, bad)
                # skladba, která se nepovedla přehrát, se znovu nezařadí
                rig.store.record_start("zzzzzzzvada", "Vadná", "Kapela", None)
                rig.store.record_outcome("zzzzzzzvada", "error")
                rig.store.flush()
                srv._hist = None
                r = await srv._prompt(post({"replay": "zzzzzzzvada", "who": "Petr", "client": PETR}))
                self.assertEqual(r.status_code, 409)
                self.assertIn("nepodařilo přehrát", tw.body(r)["error"])
                # podruhé totéž od téhož: už ji má
                r = await srv._prompt(post({"replay": OLD.id, "who": "Petr", "client": PETR}))
                self.assertEqual(r.status_code, 409)
                self.assertIn("už ve frontě máš", tw.body(r)["error"])

        run(go())

    def test_replay_endpoint_refuses_voted_out_and_too_many(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                book, _ = _wire(rig)
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                tracks = [Track(f"zzzzzzzzzw{i}", f"Web {i}", "Kapela", 200) for i in range(MAX_ACTIVE + 1)]
                for t in [OLD, *tracks]:
                    rig.store.record_start(t.id, t.title, t.artist, None)
                    rig.store.record_outcome(t.id, "finished")
                rig.store.flush()
                book.cast(SONG, PETR, -1, "Petr", track=OLD)
                book.cast(SONG, KAREL, -1, "Karel", track=OLD)
                r = await srv._prompt(post({"replay": OLD.id, "who": "Jana", "client": JANA}))
                self.assertEqual(r.status_code, 409)
                self.assertEqual(tw.body(r)["error"],
                                 "Tohle znovu nezařadím — vyřazená hlasováním — Petr, Karel.")
                codes = []
                for t in tracks:
                    r = await srv._prompt(post({"replay": t.id, "who": "Jana", "client": JANA}))
                    codes.append(r.status_code)
                self.assertEqual(codes, [202] * MAX_ACTIVE + [429])
                self.assertIn("rozpracovaných", tw.body(r)["error"])

        run(go())


# --------------------------------------------------------------------------
# stránka
# --------------------------------------------------------------------------


def render_queue(queue: list[dict], tag: str, requests: list[dict] | None = None) -> list[dict]:
    """Spustí renderQueue z index.html v node; vrací tlačítka obalů po řádcích."""
    src = INDEX.read_text()

    def fn(name: str) -> str:
        return re.search(r"\n  function %s\(.*?\n  \}\n" % name, src, re.S).group(0)

    icons = "\n".join(re.findall(r"\n  var ICO_(?:PLAY|AGAIN) = '.*?';", src))
    script = """
var S = {tag: %s, sig: {}, connected: true, tokens: {}};
var el = {queue: {innerHTML: ""}, queueCount: {}, queueEmpty: {}};
function setHidden() {}
function hue() { return 0; }
function voteMarks() { return ""; }
function rowVoteBtn() { return ""; }
function fmtTime(s) { return String(s); }
function whoChip(name, you) { return you ? "tvoje" : name; }
%s
%s
%s
%s
%s
%s
renderQueue(%s, %s);
process.stdout.write(el.queue.innerHTML);
""" % (json.dumps(tag), icons, fn("esc"), fn("coverUrl"), fn("isMine"), fn("thumb"), fn("renderQueue"),
       json.dumps(queue, ensure_ascii=False), json.dumps(requests or [], ensure_ascii=False))
    html = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True,
                          timeout=20).stdout
    rows = []
    for li in html.split("<li")[1:]:
        m = re.search(r'<button class="thumb qplay( blocked)?" type="button" data-jump="(\d+)"[^>]*'
                      r'aria-label="([^"]*)"', li)
        rows.append({"blocked": bool(m.group(1)), "row": int(m.group(2)), "label": m.group(3)} if m else {})
    return rows


class Page(unittest.TestCase):
    def setUp(self) -> None:
        self.html = INDEX.read_text()

    def test_progress_bar_is_a_keyboard_accessible_range_with_ten_second_buttons(self) -> None:
        html = self.html
        self.assertRegex(html, r'<input type="range" id="seek" min="0" max="0" step="1" value="0" '
                               r'aria-label="Pozice ve skladbě">')
        self.assertIn('id="btnBack" type="button" aria-label="O 10 vteřin zpět"', html)
        self.assertIn('id="btnFwd" type="button" aria-label="O 10 vteřin dopředu"', html)
        self.assertIn('seekTo(predictPos() - 10)', html)
        self.assertIn('seekTo(predictPos() + 10)', html)
        self.assertIn('setAttribute("aria-valuetext"', html)
        self.assertNotIn('role="progressbar"', html)  # posuvník, ne jen ukazatel
        # server: absolutní místo; při tažení se neposílá nic, až po puštění
        self.assertIn('{ action: "seek", value: Math.round(want * 10) / 10', html)
        drag = re.search(r'el\.seek\.addEventListener\("input", function \(\) \{(.*?)\}\);', html, re.S).group(1)
        self.assertNotIn("seekTo", drag)
        self.assertNotIn("postJSON", drag)
        # nejvýš jeden povel za 350 ms (server dovolí 3 za vteřinu)
        every = int(re.search(r"var SEEK_EVERY = (\d+);", html).group(1))
        self.assertGreaterEqual(every * web.SEEK_RATE, 1000 * web.SEEK_WINDOW)
        self.assertEqual(float(re.search(r"var SEEK_END = (\d+);", html).group(1)), SEEK_END_GUARD)
        # cizí přání: poctivá věta, ne tichý nezdar
        self.assertIn("v cizím přání se neposouvá", html)

    def test_touch_targets_are_finger_sized(self) -> None:
        html = self.html
        sk = re.search(r"\n  \.sk \{(.*?)\}", html, re.S).group(1)
        self.assertIn("width: 44px; height: 44px", sk)
        self.assertIn("@media (pointer: coarse) { button.thumb { width: 44px; height: 44px; } }", html)
        self.assertIn("@media (pointer: coarse) { .rv { width: 44px; height: 44px; } }", html)
        coarse = re.search(r"@media \(pointer: coarse\) \{\s*input\[type=range\] \{ height: (\d+)px", html)
        self.assertEqual(int(coarse.group(1)), 44)

    def test_new_styles_use_only_theme_tokens(self) -> None:
        css = self.html[self.html.index("/* posun ve skladbě:"):self.html.index("  .controls {")]
        css += self.html[self.html.index("/* obal ve frontě je tlačítko"):self.html.index("  .empty {")]
        self.assertGreater(len(css), 800)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\((?!var)", css), [])
        self.assertIn("var(--accent)", css)

    @unittest.skipUnless(shutil.which("node"), "node není")
    def test_queue_shows_which_songs_the_viewer_may_play_now(self) -> None:
        def q(vid_: str, who: str = "", key: str = "") -> dict:
            d = {"id": vid_, "title": "T " + vid_[-1], "artist": "A", "duration": 200}
            if who:
                d["req"] = {"id": "w-" + key, "who": who, "who_key": key}
            return d

        queue = [q("aaaaaaaaaa1", "Petr", "tp"), q("aaaaaaaaaa2", "Petr", "tp"),
                 q("aaaaaaaaaa3", "Jana", "tj"), q("aaaaaaaaaa4", "Petr", "tp"), q("aaaaaaaaaa5")]
        petr = render_queue(queue, "tp")
        self.assertEqual([r["blocked"] for r in petr], [False, False, True, True, True])
        self.assertEqual([r["row"] for r in petr], [0, 1, 2, 3, 4])
        self.assertTrue(petr[0]["label"].startswith("Přehrát hned: A — T 1"))
        self.assertIn("čeká přání někoho jiného", petr[3]["label"])
        # Jana a kolega bez přání: hned první řádek je cizí přání
        self.assertEqual([r["blocked"] for r in render_queue(queue, "tj")], [True] * 5)
        self.assertEqual([r["blocked"] for r in render_queue(queue, "")], [True] * 5)
        # jen podkres: hrát hned jde cokoli
        self.assertEqual([r["blocked"] for r in render_queue([q("aaaaaaaaaa5"), q("aaaaaaaaaa6")], "")],
                         [False, False])

    def test_queue_and_history_buttons_call_the_api(self) -> None:
        html = self.html
        self.assertIn('{ action: "jump", video_id: t.id, who: S.nick, client: S.client }', html)
        self.assertIn('{ replay: t.id, who: S.nick, source: "web", wait: false, client: S.client }', html)
        self.assertIn('title="Zahrát znovu"', html)
        self.assertIn('aria-label="Zahrát znovu: ', html)
        self.assertIn('function canReplay(t) { return !!coverUrl(t.id) && t.outcome !== "error"; }', html)
        # odpověď serveru (i odmítnutí) se ukáže, ne zahodí
        jump = html[html.index('ev.target.closest("[data-jump]")'):html.index('ev.target.closest("[data-replay]")')]
        self.assertIn("toast(d.message", jump)
        self.assertIn("toast(e.message, true)", jump)


# --------------------------------------------------------------------------
# skutečné mpv (místní zvuk, --ao=null, bez sítě)
# --------------------------------------------------------------------------


def _sine(path: Path, secs: float, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(rate * secs))))


def RT(n: int) -> Track:
    """Skladba pro skutečné mpv: délka jako místní soubor (30 s)."""
    return Track(vid(n), f"T{n}", "A", duration=30)


class RealRig(tw.Rig):
    """Rig, ve kterém místo falešného mpv běží skutečné: tytéž volby zvuku jako
    v provozu (zásoba 2 s, gapless, cache), jen bez yt-dlp a s výstupem null.
    Každé videoId je místní soubor `watch?v=<id>` (30 s tónu)."""

    SECS = 30.0

    async def __aenter__(self) -> "RealRig":
        base = self.dir / "tone.wav"
        _sine(base, self.SECS)
        ids = [t.id for t in tw.KABAT + tw.OLYMPIC] + [tw.vid(f"r{i}") for i in range(60)]
        ids += [tw.vid("seed0"), tw.vid("s-Holky"), tw.vid("s-Dancing"), tw.vid("s-Jasna"), OLD.id]
        ids += [vid(i) for i in range(8)]
        for v in dict.fromkeys(ids):
            os.link(base, self.dir / f"watch?v={v}")
        self._url = mock.patch.object(mpvmod, "WATCH_URL", str(self.dir / "watch?v={}"))
        self._url.start()
        sock = self.dir / "real.sock"
        self.proc = subprocess.Popen(
            ["mpv", "--no-config", "--idle=yes", "--no-video", "--no-terminal", "--ytdl=no",
             f"--input-ipc-server={sock}", "--prefetch-playlist=no", "--gapless-audio=weak",
             "--cache=yes", f"--audio-buffer={AUDIO_BUFFER:g}", "--cache-pause-initial=yes",
             "--cache-pause-wait=2", "--keep-open=no", "--volume=100", "--ao=null"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.monotonic()
        while not sock.exists():
            if time.monotonic() - t0 > 15:
                raise AssertionError("mpv nenastartovalo")
            await asyncio.sleep(0.01)
        r, w = await asyncio.open_unix_connection(str(sock))

        async def resolver_call(req: dict) -> dict:
            return {"ok": True}

        async def wait_ready(video_id: str, timeout: float) -> bool:
            return True

        self.player._resolver_call = resolver_call  # type: ignore[method-assign]
        self.player._resolver = object()  # type: ignore[assignment]
        self.player.wait_ready = wait_ready  # type: ignore[method-assign]
        self._tel = mock.patch.object(telemetry, "event",
                                      lambda kind, **f: self.events.append((kind, f)))
        self._tel.start()
        await self.player.attach(r, w)
        self.dj.fast_plan = self.fast_plan  # type: ignore[method-assign]
        self.dj.interpret = self.interpret  # type: ignore[method-assign]
        self.wq = tw.WishQueue(self.dj, self.player, self.pools, self.store, self.cfg,
                               state_file=self.dir / "session.json")
        self.player.on_event(self.wq.on_event)
        self.wq.start()
        return self

    async def __aexit__(self, *exc) -> None:
        self._tel.stop()
        self._url.stop()
        await self.wq.stop()
        for t in asyncio.all_tasks() - {asyncio.current_task()}:
            t.cancel()
        await asyncio.sleep(0)
        if self.player.writer:
            self.player.writer.close()
        self.proc.kill()
        self.proc.wait(10)
        await asyncio.sleep(0)

    async def mpv_playlist(self) -> tuple[str | None, list[str]]:
        """Co má v playlistu skutečné mpv: (hrající, všechny položky v pořadí)."""
        data = await self.player._get("playlist")
        ids = [mpvmod.video_id_of(e["filename"]) for e in data]
        cur = next((mpvmod.video_id_of(e["filename"]) for e in data if e.get("current")), None)
        return cur, ids

    async def mpv_pos(self) -> float:
        return float(await self.player._get("time-pos") or 0.0)

    async def plays(self, video_id: str | None = None) -> None:
        def ok() -> bool:
            p = self.player
            return bool((p._load or {}).get("t_play")) and (video_id is None or p._current_id == video_id)

        await self.until(ok, timeout=10)


@unittest.skipUnless(shutil.which("mpv"), "mpv na tomhle stroji není")
class RealMpv(unittest.TestCase):
    """Posun a klik do fronty na skutečném mpv — pozice po posunu a pořadí
    playlistu po skoku jsou čtené z mpv, ne z našeho obrazu."""

    def test_seek_lands_where_asked_and_changes_nothing_else(self) -> None:
        async def go():
            async with RealRig() as rig:
                p = rig.player
                seen = watch(p)
                await p.enqueue([RT(i) for i in range(4)])
                await rig.plays(vid(0))
                await asyncio.sleep(0.5)
                before = await rig.mpv_playlist()
                t0 = time.monotonic()
                res = await p.seek(position=20)
                took = time.monotonic() - t0
                self.assertEqual((res["to"], res["duration"]), (20.0, 30.0))
                self.assertLess(res["from"], 2.0)
                self.assertLess(took, 0.5)
                st = await p.status()
                self.assertTrue(20.0 <= st.position < 20.6, st.position)  # stav hned na novém místě
                await asyncio.sleep(1.0)
                pos = await rig.mpv_pos()
                self.assertTrue(20.8 <= pos <= 21.8, pos)  # a hraje odtamtud dál
                # zpět, relativně, a pod nulu
                self.assertAlmostEqual((await p.seek(delta=-10))["to"], pos - 10, delta=0.3)
                await asyncio.sleep(0.3)
                self.assertTrue(pos - 10 <= await rig.mpv_pos() <= pos - 9, await rig.mpv_pos())
                self.assertEqual((await p.seek(position=-5))["to"], 0.0)
                await asyncio.sleep(0.3)
                self.assertLess(await rig.mpv_pos(), 1.0)
                self.assertEqual(await rig.mpv_playlist(), before)  # playlist beze změny
                self.assertEqual(since_start(seen, vid(0)), [("start", vid(0)), ("sound", vid(0))])
                self.assertEqual([k for k, _ in rig.events if k in ("track.end", "track.stall")], [])

        run(go())

    def test_seek_to_the_end_is_clamped_and_the_track_ends_normally(self) -> None:
        async def go():
            async with RealRig() as rig:
                p = rig.player
                seen = watch(p)
                await p.enqueue([RT(i) for i in range(3)])
                await rig.plays(vid(0))
                await asyncio.sleep(0.3)
                res = await p.seek(position=10_000)
                self.assertEqual(res["to"], rig.SECS - SEEK_END_GUARD)  # 27 s ze 30
                t0 = time.monotonic()
                await asyncio.sleep(0.5)
                pos = await rig.mpv_pos()
                self.assertTrue(27.3 <= pos <= 28.2, pos)  # po skoku ještě normálně hraje
                self.assertEqual((await rig.mpv_playlist())[0], vid(0))
                await rig.until(lambda: ("start", vid(1)) in seen, timeout=10)
                took = time.monotonic() - t0
                # skladba dohrála sama (eof), další navázala: žádné přeskočení ani chyba —
                # a mpv mezi nimi nespadlo do klidu (žádné "idle", žádné "nic nehraje")
                await rig.until(lambda: ("sound", vid(1)) in seen, timeout=10)
                self.assertEqual(since_start(seen, vid(0)),
                                 [("start", vid(0)), ("sound", vid(0)), ("finished", vid(0)),
                                  ("start", vid(1)), ("sound", vid(1))])
                st = await p.status()
                self.assertEqual((st.current.id, st.playing), (vid(1), True))
                self.assertTrue(2.0 <= took <= 4.5, took)  # ~3 s do konce, ne hned
                end = [f for k, f in rig.events if k == "track.end"][0]
                self.assertEqual((end["reason"], end["mpv_reason"]), ("finished", "eof"))
                self.assertFalse(end.get("premature"))

        run(go())

    def test_click_plays_now_and_mpv_keeps_the_jumped_songs_in_order(self) -> None:
        async def go():
            async with RealRig() as rig:
                p = rig.player
                seen = watch(p)
                await p.enqueue([RT(i) for i in range(6)])
                await rig.plays(vid(0))
                await asyncio.sleep(0.3)
                t0 = time.monotonic()
                self.assertEqual(await p.play_from_queue(vid(3), True), "now")
                await rig.plays(vid(3))
                took = time.monotonic() - t0
                self.assertLess(took, 1.5)
                cur, ids = await rig.mpv_playlist()
                self.assertEqual(cur, vid(3))
                self.assertEqual(ids, [vid(0), vid(3), vid(1), vid(2), vid(4), vid(5)])
                self.assertEqual(p.upcoming_ids(), [vid(1), vid(2), vid(4), vid(5)])
                self.assertEqual([e for e in since_start(seen, vid(0)) if e[0] not in ("start", "sound")],
                                 [("replaced", vid(0))])
                self.assertLess(await rig.mpv_pos(), 1.5)  # od začátku
                # „jako další": hrající hraje dál, v mpv je vybraná hned za ní
                self.assertEqual(await p.play_from_queue(vid(5), False), "next")
                cur, ids = await rig.mpv_playlist()
                self.assertEqual((cur, ids), (vid(3), [vid(0), vid(3), vid(5), vid(1), vid(2), vid(4)]))
                # a opravdu pak zahraje: posun ke konci, skladba dohraje, naváže vybraná
                await p.seek(position=10_000)
                await rig.until(lambda: ("start", vid(5)) in seen, timeout=10)
                self.assertIn(("finished", vid(3)), seen)
                self.assertNotIn(("idle", None), since_start(seen, vid(0)))
                self.assertEqual((await rig.mpv_playlist())[1][3:], [vid(1), vid(2), vid(4)])

        run(go())

    def test_whole_path_wishes_web_and_real_mpv(self) -> None:
        """Web → fronta přání → skutečné mpv: klik ve vlastním přání, odmítnutí
        přes cizí přání, posun autora a odmítnutý posun kolegy."""
        async def go():
            async with RealRig() as rig:
                app = as_app(rig)
                srv = web.WebServer(tw.WebApp(rig))
                await rig.background()
                pw = rig.wq.submit("pusť Kabát", "Petr", client={"id": PETR})
                await rig.until(lambda: pw.state == "playing", timeout=10)
                await rig.plays(tw.KABAT[0].id)
                jw = rig.wq.submit("Holky z naší školky", "Jana", client={"id": JANA})
                await rig.until(lambda: jw.state == "queued", timeout=10)
                await rig.settle(0.5)
                snap = await srv._snapshot()
                queue = [q["id"] for q in snap["queue"]]
                who = [(q.get("req") or {}).get("who", "-") for q in snap["queue"]]
                cur, ids = await rig.mpv_playlist()
                self.assertEqual(ids[ids.index(cur) + 1:], queue)  # fronta webu = playlist mpv
                jana = who.index("Jana")
                later = next(i for i, o in enumerate(who) if o == "Petr" and i > jana)
                rig.player._res_ready.update(queue)
                # posun: kolegyně v Petrově přání ne, Petr ano — a mpv je opravdu tam
                r = await srv._control(post({"action": "seek", "value": 15, "client": JANA}))
                self.assertEqual(r.status_code, 403)
                self.assertLess(await rig.mpv_pos(), 5.0)
                r = await srv._control(post({"action": "seek", "value": 15, "client": PETR}))
                self.assertEqual((r.status_code, tw.body(r)["position"]), (200, 15.0))
                await asyncio.sleep(0.4)
                self.assertTrue(15.0 <= await rig.mpv_pos() <= 16.2)
                self.assertEqual((await srv._snapshot())["current"]["id"], tw.KABAT[0].id)
                # klik: přes Janino přání ne (mpv beze změny), svou hned další ano
                r = await srv._control(post({"action": "jump", "video_id": queue[later], "client": PETR}))
                self.assertEqual((r.status_code, tw.body(r)["error"]), (403, "Nejdřív dohraje přání od Jana."))
                self.assertEqual(await rig.mpv_playlist(), (cur, ids))
                r = await srv._control(post({"action": "jump", "video_id": queue[0], "client": PETR}))
                self.assertEqual((r.status_code, tw.body(r)["mode"]), (200, "now"))
                await rig.plays(queue[0])
                await rig.settle(0.5)
                cur2, ids2 = await rig.mpv_playlist()
                self.assertEqual(cur2, queue[0])
                after = ids2[ids2.index(cur2) + 1:]
                self.assertEqual(after[0], queue[jana])  # Jana je další, nikdo ji nepředběhl
                self.assertEqual([q["id"] for q in (await srv._snapshot())["queue"]], after)
                self.assertEqual(sorted(after), sorted(queue[1:]))  # nic z fronty nezmizelo
                hist = outcomes(rig)
                self.assertEqual(hist[tw.KABAT[0].id], "replaced")
                self.assertNotIn("skipped", hist.values())
                self.assertEqual(app.skips._skips, [])
                self.assertEqual((pw.skips_others, jw.state), (0, "queued"))

        run(go())


if __name__ == "__main__":
    unittest.main()
