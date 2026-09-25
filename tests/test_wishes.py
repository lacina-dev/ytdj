"""Fronta přání pro víc lidí: spravedlnost, "zařadit hned", odebrání, podkres,
obnova po restartu, chytrý rozjezd — proti falešnému mpv (tests/fake_mpv.py)
a falešnému DJ (skriptované odpovědi místo Codexu), bez sítě:

    .venv/bin/python -m unittest tests.test_wishes -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-wishes-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mpv import FakeMpv  # noqa: E402
from ytdj import telemetry, wishes  # noqa: E402
from ytdj.agent.codex import CodexDJ, Plan, interleave  # noqa: E402
from ytdj.agent.intent import Intent, ListenerIntent, build_intent  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.player.base import PlayerEvent  # noqa: E402
from ytdj.player.mpv import MpvPlayer  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.wishes import BLOCK, Turns, Wish, WishQueue, fair_order, should_resume  # noqa: E402


def vid(name: str) -> str:
    return name.rjust(11, "x")[:11]


def T(name: str, artist: str = "A") -> Track:
    return Track(vid(name), f"song {name}", artist, duration=200)


KABAT = [T(f"kab{i}", "Kabát") for i in range(20)]
OLYMPIC = [T(f"oly{i}", "Olympic") for i in range(10)]
RADIO = [T(f"rad{i}", f"Radio {i}") for i in range(60)]
ARTISTS = {"kabát": KABAT, "olympic": OLYMPIC}


class Catalog:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.radio_base = 0

    async def search_song(self, artist: str, title: str):
        self.calls.append(("search_song", artist, title))
        if artist == "Nobody":
            return None
        return T(f"s-{title}"[:11], artist)

    async def artist_tracks(self, name: str, limit: int = 50):
        return list(ARTISTS.get(name.lower(), []))[:limit]

    async def find_artist_tracks(self, name: str, limit: int = 10):
        return await self.artist_tracks(name, limit)

    async def radio(self, video_id: str, limit: int = 50):
        self.calls.append(("radio", video_id))
        base = self.radio_base
        return [T(f"r{base + i}", f"Radio {i % 9}") for i in range(30)]


def decision(**kw) -> dict:
    base = {"action": "nothing", "seeds": [], "requested": [], "focus_artists": [],
            "after_current": False, "avoid": [], "mood": "", "volume": 0,
            "remember": "", "reply": ""}
    base.update(kw)
    return base


SCRIPT = {
    # "pusť Kabát" jde rychlou cestou (interpret), bez Codexu
    "pusť Kabát": ("fast", ["Kabát"]),
    "Olympic prosím": ("fast", ["Olympic"]),
    "Holky z naší školky": ("codex", decision(
        action="play_next", requested=[{"artist": "Olympic", "title": "Holky"}],
        reply="Zařazuju Holky z naší školky.")),
    "Jasná zpráva": ("codex", decision(
        action="play_next", requested=[{"artist": "Olympic", "title": "Jasna"}],
        reply="Zařazuju Jasnou zprávu.")),
    "Dancing Queen": ("codex", decision(
        action="play_next", requested=[{"artist": "ABBA", "title": "Dancing"}],
        reply="Zařazuju Dancing Queen.")),
    "něco klidnějšího": ("codex", decision(
        action="start_radio", mood="klidný pop",
        seeds=[{"artist": "Calm", "title": f"c{i}"} for i in range(3)],
        reply="Zklidním to.")),
    "Wonderwall od Nobody": ("codex", decision(
        action="play_next", requested=[{"artist": "Nobody", "title": "Wonderwall"}])),
}


class Rig:
    """Opravdový MpvPlayer nad falešným mpv + opravdová fronta přání."""

    def __init__(self, codex_delay: float = 0.05, jitter: float = 0.001) -> None:
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.fake = FakeMpv(self.dir / "mpv.sock", jitter=jitter)
        self.cfg = Config(**DEFAULTS)
        self.player = MpvPlayer(self.cfg)
        self.store = Store(self.dir / "state.db")
        self.catalog = Catalog()
        self.pools = RadioPools(self.catalog, self.store, self.cfg)
        self.dj = CodexDJ(self.cfg, self.catalog, self.pools, self.player, self.store)
        self.dj._wish_file = self.dir / "intent.json"
        self.dj.wish = ListenerIntent()
        self.codex_delay = codex_delay
        self.asked: list[tuple[str, bool]] = []
        self.events: list[tuple[str, dict]] = []
        self.wq: WishQueue | None = None

    async def fast_plan(self, text: str):
        kind, what = SCRIPT.get(text, ("codex", None))
        if kind != "fast":
            return None
        await asyncio.sleep(0.01)
        tracks = interleave([await self.catalog.artist_tracks(a) for a in what])
        return Plan(intent=Intent(kind="artist", text=text, artists=list(what),
                                  mood=", ".join(what)), artist_tracks=tracks)

    async def interpret(self, text: str, auto: bool = False):
        self.asked.append((text, auto))
        await asyncio.sleep(self.codex_delay)
        if auto:
            return build_intent(text, decision(
                action="start_radio", mood="ranní klid",
                seeds=[{"artist": "Start", "title": f"st{i}"} for i in range(3)],
                reply="Ráno klidně."), auto=True)
        said = text.split(wishes.CHANGE_HINT)[0]  # co napsal posluchač
        _, data = SCRIPT.get(said, ("codex", decision(reply="Nerozumím.")))
        return build_intent(text, data)

    async def __aenter__(self) -> "Rig":
        await self.fake.start()
        r, w = await asyncio.open_unix_connection(str(self.fake.path))

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
        self.wq = WishQueue(self.dj, self.player, self.pools, self.store, self.cfg,
                            state_file=self.dir / "session.json")
        self.player.on_event(self.wq.on_event)
        self.wq.start()
        return self

    async def __aexit__(self, *exc) -> None:
        self._tel.stop()
        await self.wq.stop()
        for t in asyncio.all_tasks() - {asyncio.current_task()}:
            t.cancel()
        await asyncio.sleep(0)
        if self.player.writer:
            self.player.writer.close()
        await self.fake.close()
        await asyncio.sleep(0)

    async def settle(self, t: float = 0.1) -> None:
        await asyncio.sleep(t)

    async def until(self, cond, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            if loop.time() > end:
                raise AssertionError("podmínka nenastala")
            await asyncio.sleep(0.02)

    async def background(self, n: int = 6) -> None:
        """Hraje podkres (rádio), jak ho nechává plnič."""
        await self.pools.set_seeds([T("seed0", "Seed")], mood="podkres")
        await self.player.enqueue(await self.dj.next_tracks(n))
        await self.settle()

    def upcoming(self) -> list[str]:
        return self.fake.upcoming()

    def owners(self) -> list[str]:
        """Kdo vlastní čekající položky: jméno, nebo "-" pro podkres."""
        out = []
        for v in self.upcoming():
            wid = self.wq.owner.get(v)
            w = self.wq.by_id(wid) if wid else None
            out.append(w.who if w else "-")
        return out

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


def run(coro):
    return asyncio.run(coro)


def W(who: str, n: int, mono: float, kind: str = "songs", **kw) -> Wish:
    w = Wish(id=f"{who}{mono}", token="t", who=who, source="web", text="x", created=mono,
             mono=mono, state="queued", kind=kind, **kw)
    w.tracks = [T(f"{who}{mono:.0f}-{i}") for i in range(n)]
    return w


def whos(order) -> str:
    return "".join(w.who for w, _ in order)


class FairOrder(unittest.TestCase):
    def test_round_robin_between_people(self):
        p1, p2 = W("P", 1, 1), W("P", 1, 2)
        j1, j2 = W("J", 1, 3), W("J", 1, 4)
        k1 = W("K", 1, 5)
        self.assertEqual(whos(fair_order([p1, p2, j1, j2, k1], Turns())), "PJKPJ")

    def test_artist_block_does_not_starve_others(self):
        petr = W("P", 20, 1, kind="artist")
        jana, karel = W("J", 1, 2), W("K", 1, 3)
        order = fair_order([petr, jana, karel], Turns(), limit=12)
        self.assertEqual(whos(order), "PPPJKPPPPPPP"[: len(order)])
        self.assertEqual(whos(order)[:BLOCK + 2], "P" * BLOCK + "JK")

    def test_started_block_finishes_before_newcomer(self):
        petr = W("P", 6, 1, kind="artist")
        turns = Turns()
        turns.start(petr)  # první skladba bloku právě hraje
        petr.current = petr.tracks[0].id
        jana = W("J", 1, 2)
        self.assertEqual(whos(fair_order([petr, jana], turns)), "PPJPPP")

    def test_least_recently_served_goes_first(self):
        turns = Turns()
        petr, jana = W("P", 3, 1), W("J", 3, 2)
        turns.start(jana)
        turns.block = None  # Jana hrála naposledy, blok skončil
        self.assertEqual(whos(fair_order([petr, jana], turns))[0], "P")

    def test_play_next_jumps_the_queue(self):
        petr = W("P", 3, 1)
        jana = W("J", 1, 2, play_next=True)
        order = fair_order([petr, jana], Turns())
        self.assertEqual(whos(order), "JPPP")

    def test_same_track_in_two_wishes_plays_once(self):
        a, b = W("P", 1, 1), W("J", 1, 2)
        b.tracks = list(a.tracks)
        order = fair_order([a, b], Turns())
        self.assertEqual(len(order), 1)

    def test_prefix_is_accounted(self):
        petr, jana = W("P", 4, 1), W("J", 2, 2)
        prefix = [(petr, petr.tracks[0]), (jana, jana.tracks[0])]
        order = fair_order([petr, jana], Turns(), prefix)
        self.assertNotIn(petr.tracks[0].id, [t.id for _, t in order])
        # Jana začala kolo (prefix) → dohraje blok, pak Petr
        self.assertEqual(whos(order), "JPPP")


class ResumeRule(unittest.TestCase):
    def test_rules(self):
        now = datetime(2026, 9, 25, 14, 0)
        wall = 1_000_000.0
        fresh = {"playing": True, "saved": wall - 60}
        self.assertEqual(should_resume(fresh, now, wall), (True, "fresh"))
        self.assertEqual(should_resume({**fresh, "playing": False}, now, wall)[1], "was_not_playing")
        self.assertEqual(should_resume({**fresh, "saved": wall - 3600}, now, wall)[1], "stale")
        self.assertEqual(should_resume(fresh, datetime(2026, 9, 25, 23, 30), wall)[1], "night")
        self.assertEqual(should_resume(fresh, datetime(2026, 9, 26, 5, 0), wall)[1], "night")
        self.assertEqual(should_resume(None, now, wall)[1], "no_state")


class Queue(unittest.TestCase):
    def test_three_people_concurrently_fair_and_nothing_lost(self):
        async def go():
            async with Rig(codex_delay=0.1) as rig:
                await rig.background()
                wq = rig.wq
                a = wq.submit("pusť Kabát", "Petr")
                b = wq.submit("Holky z naší školky", "Jana")
                c = wq.submit("Dancing Queen", "Karel")
                d = wq.submit("Jasná zpráva", "Jana")
                await rig.until(lambda: all(w.state in ("queued", "playing") for w in (a, b, c, d)))
                await rig.settle(0.2)
                owners = rig.owners()
                # Kabát utnul podkres (první na řadě) → hraje P1; P2 P3 dohrají blok,
                # pak Jana, Karel, Jana, a teprve pak zase Petr
                self.assertEqual(rig.fake.current_vid(), KABAT[0].id)
                # (Petr byl na řadě dřív než Jana → jeho další blok jde před její druhé přání)
                self.assertEqual(owners[:8], ["Petr", "Petr", "Jana", "Karel",
                                              "Petr", "Petr", "Petr", "Jana"], owners)
                # podkres až za přáními
                first_bg = owners.index("-")
                self.assertTrue(all(o == "-" for o in owners[first_bg:]), owners)
                # nic se neztratilo: všechny skladby přání jsou v playlistu mpv
                for w in (b, c, d):
                    self.assertIn(w.tracks[0].id, rig.upcoming())
                self.assertEqual(rig.player.upcoming_ids(), rig.upcoming())
                # odpovědi říkají, kdy to bude
                self.assertIn("Na řadě", b.reply)
                self.assertEqual(sorted(k for k in rig.kinds() if k == "request.queued"),
                                 ["request.queued"] * 4)

        run(go())

    def test_artist_block_interleaves_and_rest_goes_on(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state in ("queued", "playing"))
                j = wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued")
                await rig.settle(0.2)
                played = [rig.fake.current_vid()]
                for _ in range(6):
                    rig.fake.finish_current()
                    await rig.settle(0.15)
                    played.append(rig.fake.current_vid())
                names = ["J" if v == j.tracks[0].id else ("P" if "kab" in v else "-")
                         for v in played]
                self.assertEqual("".join(names[:5]), "PPPJP", played)
                # Jana je hotová; Petr sám → zbytek Kabátu pokračuje jako podkres
                self.assertEqual(j.state, "done")
                self.assertEqual(rig.pools.artist, "Kabát")
                self.assertTrue(p.handed)

        run(go())

    def test_play_next_is_right_after_current_and_capped(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                k = wq.submit("Dancing Queen", "Karel")
                await rig.until(lambda: k.state == "queued")
                cur = rig.fake.current_vid()
                j = wq.submit("Holky z naší školky", "Jana", play_next=True)
                await rig.until(lambda: j.state == "queued")
                await rig.settle(0.2)
                self.assertEqual(rig.fake.current_vid(), cur)  # nic se neutnulo
                self.assertEqual(rig.upcoming()[0], j.tracks[0].id)
                self.assertIn("hned po téhle", j.reply)
                # druhé "hned" od téže osoby jde normálně do fronty
                j2 = wq.submit("Jasná zpráva", "Jana", play_next=True)
                self.assertFalse(j2.play_next)
                self.assertIn("Jedno „hned“", j2.note)

        run(go())

    def test_explicit_cut_cuts_somebodys_wish(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.1)
                self.assertEqual(rig.fake.current_vid(), KABAT[0].id)
                SCRIPT["Dancing Queen hned teď"] = SCRIPT["Dancing Queen"]
                k = wq.submit("Dancing Queen hned teď", "Karel")
                self.assertTrue(k.play_next and k.cut)
                await rig.until(lambda: k.state == "playing")
                self.assertEqual(rig.fake.current_vid(), k.tracks[0].id)

        run(go())

    def test_wish_never_cuts_somebody_elses_wish(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                k = wq.submit("Dancing Queen", "Karel", play_next=True)
                await rig.until(lambda: k.state == "queued")
                await rig.settle(0.2)
                self.assertEqual(rig.fake.current_vid(), KABAT[0].id)

        run(go())

    def test_owner_token_removes_and_others_cannot(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                j = wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued" and p.state in ("queued", "playing"))
                await rig.settle(0.1)
                ok, _ = await wq.remove(j.id, p.token)
                self.assertFalse(ok)
                self.assertIn(j.tracks[0].id, rig.upcoming())
                ok, _ = await wq.remove(j.id, j.token)
                self.assertTrue(ok)
                await rig.settle(0.1)
                self.assertEqual(j.state, "removed")
                self.assertNotIn(j.tracks[0].id, rig.upcoming())
                self.assertIn("Petr", rig.owners())
                self.assertEqual(rig.player.upcoming_ids(), rig.upcoming())

        run(go())

    def test_remove_while_dj_thinks_discards_result(self):
        async def go():
            async with Rig(codex_delay=0.3) as rig:
                await rig.background()
                wq = rig.wq
                j = wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "thinking")
                ok, _ = await wq.remove(j.id, j.token)
                self.assertTrue(ok)
                await rig.settle(0.5)
                self.assertEqual(j.state, "removed")
                self.assertEqual(rig.owners().count("Jana"), 0)

        run(go())

    def test_auto_reseed_never_removes_requests(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                j = wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued" and p.state in ("queued", "playing"))
                await rig.settle(0.1)
                before = [v for v, o in zip(rig.upcoming(), rig.owners()) if o != "-"]
                rig.catalog.radio_base = 500  # nové rádio = jiné skladby
                await wq.background_turn("Posluchač přeskočil…")
                await rig.settle(0.1)
                after = [v for v, o in zip(rig.upcoming(), rig.owners()) if o != "-"]
                self.assertEqual(before, after)
                self.assertEqual(rig.pools.mood, "ranní klid")
                # podkres staré nálady je pryč (nový přijde za přání, až na něj dojde)
                bg = [v for v, o in zip(rig.upcoming(), rig.owners()) if o == "-"]
                self.assertFalse([v for v in bg if not v.startswith("xxxxxxxr5")], bg)

        run(go())

    def test_mood_wish_gets_a_block_and_sets_background(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state in ("queued", "playing"))
                rig.catalog.radio_base = 300
                k = wq.submit("něco klidnějšího", "Karel")
                await rig.until(lambda: k.state == "queued")
                await rig.settle(0.1)
                self.assertEqual(len(k.tracks), BLOCK)
                self.assertEqual(rig.owners().count("Karel"), BLOCK)
                self.assertEqual(rig.pools.mood, "klidný pop")
                self.assertEqual(wq.bg_reason["who"], "Karel")

        run(go())

    def test_not_found_is_honest(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Wonderwall od Nobody", "Jana")
                await rig.until(lambda: w.state == "notfound")
                self.assertIn("Nenašel", w.reply)
                self.assertEqual(rig.owners().count("Jana"), 0)

        run(go())

    def test_states_follow_playback(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                j = wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "playing")  # utnulo podkres
                pub = {x["id"]: x for x in wq.public()}
                self.assertEqual(pub[j.id]["state_cs"], "hraje")
                self.assertEqual(wq.reason_for(rig.fake.current_vid())["who"], "Jana")
                rig.fake.finish_current()
                await rig.until(lambda: j.state == "done")
                self.assertEqual(wq.reason_for(rig.fake.current_vid())["kind"], "radio")
                # čekání přání → první zvuk
                self.assertIn("request.done", rig.kinds())

        run(go())

    def test_first_sound_is_measured(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "playing")
                await rig.wq.on_event(PlayerEvent("sound", j.tracks[0]))
                ev = [f for k, f in rig.events if k == "request.started"]
                self.assertEqual(ev[0]["id"], j.id)
                self.assertGreaterEqual(ev[0]["wait_ms"], 0)

        run(go())

    def test_restart_persistence(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                j = wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued" and p.state in ("queued", "playing"))
                await wq.refresh_playing()
                wq.save()
                saved = wq.load_state()
            self.assertTrue(saved["playing"])
            self.assertEqual({w["who"] for w in saved["wishes"]}, {"Petr", "Jana"})
            async with Rig() as rig2:
                why = await rig2.wq.resume(saved, now=datetime(2026, 9, 25, 10, 0))
                self.assertEqual(why, "fresh")
                await rig2.settle(0.2)
                self.assertEqual({w.who for w in rig2.wq.active()}, {"Petr", "Jana"})
                self.assertIsNotNone(rig2.fake.current_vid())
                self.assertIn("Jana", rig2.owners() + [rig2.wq.reason_for(rig2.fake.current_vid()).get("who")])
                # a v noci ne
            async with Rig() as rig3:
                why = await rig3.wq.resume(saved, now=datetime(2026, 9, 25, 23, 0))
                self.assertEqual(why, "night")
                self.assertIsNone(rig3.fake.current_vid())

        run(go())

    def test_idle_start_uses_context(self):
        async def go():
            async with Rig() as rig:
                self.assertIsNone(rig.fake.current_vid())
                reply = await rig.wq.start_idle(now=datetime(2026, 9, 24, 8, 30))
                await rig.settle(0.1)
                text, auto = rig.asked[-1]
                self.assertTrue(auto)
                self.assertIn("Nic nehraje a nikdo si nic nevyžádal", text)
                self.assertIn("čtvrtek", text)
                self.assertEqual(reply, "Ráno klidně.")
                self.assertIsNotNone(rig.fake.current_vid())
                self.assertEqual(rig.wq.reason_for(rig.fake.current_vid())["kind"], "start")
                self.assertEqual(rig.wq.active(), [])  # není to přání posluchače

        run(go())

    def test_idle_start_falls_back_to_history(self):
        async def go():
            async with Rig() as rig:
                for i in range(5):
                    rig.store.record_start(vid(f"h{i}"), f"h{i}", f"Artist {i}", None)
                    rig.store.record_outcome(vid(f"h{i}"), "finished")

                async def broken(text, auto=False):
                    raise RuntimeError("Codex spadl")

                rig.dj.interpret = broken
                await rig.wq.start_idle()
                await rig.settle(0.1)
                self.assertIsNotNone(rig.fake.current_vid())
                self.assertEqual(rig.pools.mood, "jako minule")

        run(go())

    def test_local_commands_bypass_queue(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                cur = rig.fake.current_vid()
                reply = await rig.wq.try_local("další")
                await rig.settle(0.1)
                self.assertEqual(reply, "Přeskakuju.")
                self.assertNotEqual(rig.fake.current_vid(), cur)
                self.assertIsNone(await rig.wq.try_local("pusť Kabát"))

        run(go())

    def test_too_many_from_one_person(self):
        async def go():
            async with Rig(codex_delay=1.0) as rig:
                for _ in range(wishes.MAX_ACTIVE):
                    rig.wq.submit("Holky z naší školky", "Petr")
                with self.assertRaises(wishes.TooMany):
                    rig.wq.submit("Holky z naší školky", "Petr")

        run(go())




# --------------------------------------------------------------------------
# web API: 202 + stav pro všechny, staří klienti, odebrání, ▶ v tichu
# --------------------------------------------------------------------------

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from ytdj.__main__ import App  # noqa: E402
from ytdj.web import server as web  # noqa: E402


class WebApp:
    """To z ytdj.__main__.App, co web potřebuje — s opravdovou frontou přání."""

    play_or_start = App.play_or_start
    _start_idle = App._start_idle

    def __init__(self, rig: Rig) -> None:
        self.player = rig.player
        self.pools = rig.pools
        self.store = rig.store
        self.cfg = rig.cfg
        self.wishes = rig.wq
        self._start_task = None
        self.codex_busy = False


def req(body: dict | None = None, method: str = "POST",
        ua: str = "Mozilla/5.0 (X11; Linux) Chrome/130", **params):
    async def is_disconnected():
        return False

    async def read_body():
        return json.dumps(body).encode() if body is not None else b""

    return SimpleNamespace(client=SimpleNamespace(host="10.0.0.7"), headers={"user-agent": ua},
                           is_disconnected=is_disconnected, body=read_body,
                           url=SimpleNamespace(path="/x"), method=method, path_params=params)


async def sse(srv, seconds: float) -> list[str]:
    resp = await srv._events(req(method="GET"))
    out: list[str] = []

    async def pump():
        async for chunk in resp.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode())

    task = asyncio.create_task(pump())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return out


def body(resp) -> dict:
    return json.loads(resp.body)


class WebApi(unittest.TestCase):
    def test_prompt_is_accepted_at_once_and_progress_streams(self):
        async def go():
            async with Rig(codex_delay=0.4) as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                rig.wq.on_change = srv.poke
                watcher = asyncio.create_task(sse(srv, 2.0))
                await asyncio.sleep(0.1)
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                resp = await srv._prompt(req({"text": "Holky z naší školky", "who": "Jana",
                                              "source": "web"}))
                took = loop.time() - t0
                self.assertEqual(resp.status_code, 202)
                self.assertLess(took, 0.2)  # neblokuje po dobu tahu Codexu
                data = body(resp)
                self.assertTrue(data["id"] and data["token"])
                # druhé přání během tahu: žádné 409
                resp2 = await srv._prompt(req({"text": "Dancing Queen", "who": "Karel"}))
                self.assertEqual(resp2.status_code, 202)
                chunks = await watcher
                states = [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]
                seen = {(r["id"], r["state"]) for s in states for r in s.get("requests", [])}
                self.assertIn((data["id"], "thinking"), seen)
                self.assertTrue({(data["id"], "queued"), (data["id"], "playing")} & seen, seen)
                final = states[-1]
                mine = [r for r in final["requests"] if r["id"] == data["id"]][0]
                self.assertEqual(mine["who"], "Jana")
                self.assertNotIn(data["token"], json.dumps(final))  # token jen autorovi
                # přání od Jany hraje (utnulo podkres) → důvod u "hraje"
                self.assertEqual(final["current"]["reason"]["kind"], "wish")
                self.assertEqual(final["current"]["reason"]["who"], "Jana")
                tagged = [q for q in final["queue"] if q.get("req")]
                self.assertTrue(any(q["req"]["who"] == "Karel" for q in tagged), final["queue"])
                # kdo, odkud, čím — v provozním logu u každého přání
                created = [f for k, f in rig.events if k == "request.created"]
                self.assertEqual(created[0]["ip"], "10.0.0.7")
                self.assertEqual(created[0]["ua"], "Chrome/Linux")
                self.assertEqual(created[0]["who"], "Jana")

        run(go())

    def test_old_client_still_gets_the_reply(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                resp = await srv._prompt(req({"text": "Holky z naší školky", "source": "panel"},
                                             ua="ytdj-panel"))
                self.assertEqual(resp.status_code, 200)
                data = body(resp)
                self.assertIn("Holky", data["reply"])
                w = rig.wq.by_id(data["id"])
                self.assertEqual((w.who, w.source), ("displej", "panel"))

        run(go())

    def test_local_command_is_immediate(self):
        async def go():
            async with Rig(codex_delay=5) as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                resp = await srv._prompt(req({"text": "hlasitost 40", "who": "Jana"}))
                self.assertEqual((resp.status_code, body(resp)["reply"]), (200, "Hlasitost 40."))
                self.assertEqual(rig.wq.wishes, [])

        run(go())

    def test_remove_needs_the_owner_token(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                a = body(await srv._prompt(req({"text": "pusť Kabát", "who": "Petr"})))
                b = body(await srv._prompt(req({"text": "Holky z naší školky", "who": "Jana"})))
                await rig.until(lambda: rig.wq.by_id(b["id"]).state == "queued")
                bad = await srv._request_action(req({"action": "remove", "token": a["token"]},
                                                    rid=b["id"]))
                self.assertEqual(bad.status_code, 403)
                ok = await srv._request_action(req({"token": b["token"]}, method="DELETE",
                                                   rid=b["id"]))
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(rig.wq.by_id(b["id"]).state, "removed")

        run(go())

    def test_play_in_silence_starts_the_dj_with_context(self):
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                resp = await srv._control(req({"action": "play"}))
                self.assertEqual(body(resp), {"ok": True, "starting": True})
                await rig.until(lambda: rig.fake.current_vid() is not None)
                text, auto = rig.asked[-1]
                self.assertTrue(auto)
                self.assertIn("Nic nehraje", text)
                st = body(await srv._status(req(method="GET")))
                self.assertEqual(st["current"]["reason"]["kind"], "start")
                self.assertEqual(st["requests"], [])
                # podruhé ▶ = jen pokračovat, žádný další tah
                n = len(rig.asked)
                resp = await srv._control(req({"action": "play"}))
                self.assertEqual(body(resp), {"ok": True})
                self.assertEqual(len(rig.asked), n)

        run(go())

    def test_stop_never_removes_anybodys_wishes(self):
        """25. 9.: stará stránka v cache poslala "stop" a smazala přání tří lidí."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                b = body(await srv._prompt(req({"text": "Holky z naší školky", "who": "Jana"})))
                await rig.until(lambda: rig.wq.by_id(b["id"]).state in ("queued", "playing"))
                before = rig.upcoming()
                resp = await srv._control(req({"action": "stop"}))
                self.assertEqual(resp.status_code, 200)
                self.assertIn(rig.wq.by_id(b["id"]).state, ("queued", "playing"))
                self.assertEqual(rig.upcoming(), before)
                self.assertTrue(rig.player._paused)
                # ani povel napsaný DJovi
                reply = body(await srv._prompt(req({"text": "stop", "who": "Petr"})))["reply"]
                self.assertIn("zůstávají", reply)
                self.assertIn(rig.wq.by_id(b["id"]).state, ("queued", "playing"))

        run(go())

    def test_stale_page_start_prompt_is_an_idle_start(self):
        """▶ staré stránky poslalo rozjezd jako přání — teď je to rozjezd DJe."""
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                resp = await srv._prompt(req({"text": web.LEGACY_START_PROMPT, "source": "web"}))
                self.assertEqual(resp.status_code, 200)
                await rig.until(lambda: rig.fake.current_vid() is not None)
                self.assertEqual(rig.wq.wishes, [])  # žádné přání "hosta"
                self.assertTrue(rig.asked[-1][1])  # automatický tah
                st = body(await srv._status(req(method="GET")))
                self.assertEqual(st["current"]["reason"]["who"], "")

        run(go())

    def test_status_carries_build_and_index_is_not_cached(self):
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                st = body(await srv._status(req(method="GET")))
                self.assertEqual(st["build"], web.build_ids()["ui"])
                self.assertTrue(st["version"])
                resp = await srv._index(req(method="GET"))
                self.assertEqual(resp.headers["cache-control"], "no-cache")
                self.assertEqual(resp.headers["x-ytdj-build"], st["build"])

        run(go())
        html = (Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html").read_text()
        self.assertIn("location.reload()", html)  # stará stránka se sama obnoví
        self.assertNotIn('id="btnStop"', html)  # ■ pro všechny už na webu není

    def test_background_after_a_mood_wish_is_the_djs(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                k = rig.wq.submit("něco klidnějšího", "Karel")
                await rig.until(lambda: k.state in ("queued", "playing"))
                self.assertEqual(rig.wq.reason_for("not-a-wish")["who"], "Karel")
                for _ in range(10):  # blok nálady dohraje
                    if not k.active:
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.15)
                self.assertFalse(k.active)
                reason = rig.wq.reason_for(rig.fake.current_vid())
                self.assertEqual((reason["kind"], reason["who"]), ("radio", ""))
                self.assertEqual(reason["text"], "klidný pop")

        run(go())


class SongSteersBackground(unittest.TestCase):
    """ "X a pak podobné" přeladí podkres jen tomu, kdo je na přání sám."""

    def _go(self, crowded: bool):
        async def go():
            async with Rig() as rig:
                await rig.background()
                SCRIPT["Olympic a podobné"] = ("codex", decision(
                    action="start_radio", mood="Olympic a podobné",
                    requested=[{"artist": "Olympic", "title": "Holky"}],
                    seeds=[{"artist": "Olympic", "title": "Holky"}]))
                if crowded:
                    k = rig.wq.submit("Dancing Queen", "Karel")
                    await rig.until(lambda: k.state in ("queued", "playing"))
                w = rig.wq.submit("Olympic a podobné", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing"))
                return rig.pools.mood

        return run(go())

    def test_alone_steers(self):
        self.assertEqual(self._go(crowded=False), "Olympic a podobné")

    def test_others_waiting_keep_the_background(self):
        self.assertEqual(self._go(crowded=True), "podkres")


class ChangeOfDirection(unittest.TestCase):
    """25. 9.: "Překvap mě" model vyložil jako "dál Kabát" — změna směru musí
    z režimu interpreta vystoupit, "víc takového" v něm zůstat."""

    def test_surprise_breaks_out_of_artist_mode(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.handed and rig.pools.artist == "Kabát")
                # model (špatně) drží interpreta, seedy dá ale jiné
                SCRIPT["překvap mě něčím úplně jiným"] = ("codex", decision(
                    action="start_radio", focus_artists=["Kabát"], mood="Kabát",
                    seeds=[{"artist": "Kabát", "title": "Pohoda"},
                           {"artist": "Queen", "title": "Bohemian"}],
                    reply="Hraju dál Kabát."))
                rig.catalog.radio_base = 700
                w = rig.wq.submit("překvap mě něčím úplně jiným", "Jana")
                await rig.until(lambda: not w.active or w.state in ("queued", "playing"))
                self.assertEqual((w.kind, w.state in ("queued", "playing")), ("mood", True),
                                 (w.state, w.reply))
                self.assertEqual(rig.pools.artist, "")  # režim interpreta skončil
                self.assertNotIn("Kabát", w.reply)
                asked = rig.asked[-1][0]
                self.assertIn("jasnou změnu", asked)  # model dostal jednoznačné zadání
                self.assertTrue(any(k == "request.steer" for k in rig.kinds()))

        run(go())

    def test_more_like_this_stays(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                SCRIPT["víc takového"] = ("codex", decision(
                    action="start_radio", focus_artists=["Kabát"], mood="Kabát"))
                w = rig.wq.submit("víc takového", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing"))
                self.assertEqual(w.kind, "artist")
                self.assertNotIn("jasnou změnu", rig.asked[-1][0])

        run(go())

    def test_chip_texts_are_unambiguous(self):
        import ast

        from ytdj.wishes import wants_change

        # wishui potřebuje Pillow (není ve venv) — CHIPS stačí přečíst ze zdroje
        src = (Path(__file__).resolve().parents[1] / "ytdj/panel/wishui.py").read_text()
        tree = ast.parse(src)
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "CHIPS")
        chips = dict(ast.literal_eval(node.value)["cs"])
        self.assertTrue(wants_change(chips["Překvap mě"]))
        self.assertTrue(wants_change(chips["Něco jiného"]))
        self.assertFalse(wants_change(chips["Víc takového"]))
        html = (Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html").read_text()
        import re as _re
        for text in _re.findall(r'data-wish="([^"]+)"', html):
            if "jiné" in text or "překvap" in text.lower():
                self.assertTrue(wants_change(text), text)


if __name__ == "__main__":
    unittest.main()
