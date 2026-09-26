"""Fronta = playlist mpv: pořadí, souběh plniče s tahem DJe, okno pro resolver, Další.

Proti falešnému mpv na unixovém socketu (tests/fake_mpv.py), bez sítě:

    python -m unittest tests.test_player_queue -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-queue-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mpv import FakeMpv  # noqa: E402
from ytdj import telemetry  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.player.base import queue_transaction  # noqa: E402
from ytdj.player.mpv import PREFETCH_AHEAD, MpvPlayer  # noqa: E402


def vid(n: int | str) -> str:
    return f"{n}".rjust(11, "x")[:11]


def T(n: int | str) -> Track:
    return Track(vid(n), f"T{n}", "A", duration=200)


class Harness:
    def __init__(self, old: bool = False, jitter: float = 0.002, seed: int = 1) -> None:
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.fake = FakeMpv(self.dir / "mpv.sock", old=old, jitter=jitter, seed=seed)
        self.player = MpvPlayer(Config(**DEFAULTS))
        self.resolver: list[dict] = []
        self.events: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "Harness":
        await self.fake.start()
        r, w = await asyncio.open_unix_connection(str(self.fake.path))

        async def resolver_call(req: dict) -> dict:
            self.resolver.append(req)
            return {"ok": True}

        self.player._resolver_call = resolver_call  # type: ignore[method-assign]
        self.player._resolver = object()  # type: ignore[assignment]  # "běží"
        self._tel = mock.patch.object(
            telemetry, "event", lambda kind, **f: self.events.append((kind, f))
        )
        self._tel.start()
        await self.player.attach(r, w)
        return self

    async def __aexit__(self, *exc) -> None:
        self._tel.stop()
        for t in asyncio.all_tasks() - {asyncio.current_task()}:
            t.cancel()
        await asyncio.sleep(0)
        if self.player.writer:
            self.player.writer.close()
        await self.fake.close()
        await asyncio.sleep(0)

    async def settle(self, t: float = 0.05) -> None:
        await asyncio.sleep(t)

    def aheads(self) -> list[list[str]]:
        return [r["ids"] for r in self.resolver if r.get("op") == "ahead"]

    async def queue_ids(self) -> list[str]:
        return [t.id for t in (await self.player.status()).queue]


def run(coro):
    return asyncio.run(coro)


class TrueOrder(unittest.TestCase):
    def test_status_queue_is_mpv_playlist(self) -> None:
        async def go():
            async with Harness() as h:
                await h.player.enqueue([T(i) for i in range(5)])
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(0))
                self.assertEqual(await h.queue_ids(), h.fake.upcoming())
                self.assertEqual(h.player.queue_depth, 4)

        run(go())

    def _enqueue_next_races(self, old: bool) -> None:
        async def go():
            async with Harness(old=old) as h:
                await h.player.enqueue([T("A"), T("B"), T("C")])
                await h.settle()
                # plnič přidává na konec, zároveň posluchač chce X, Y hned
                await asyncio.gather(
                    h.player.enqueue([T("D"), T("E")]),
                    h.player.enqueue_next([T("X"), T("Y")]),
                    h.player.enqueue([T("F")]),
                )
                await h.settle()
                up = h.fake.upcoming()
                self.assertEqual(up[:2], [vid("X"), vid("Y")], h.fake.ids())
                self.assertEqual(sorted(up), sorted(vid(c) for c in "BCDEFXY"))
                self.assertEqual(h.player.upcoming_ids(), up)
                self.assertEqual(await h.queue_ids(), up)

        run(go())

    def test_enqueue_next_with_interleaved_enqueue(self) -> None:
        self._enqueue_next_races(old=False)

    def test_enqueue_next_fallback_old_mpv(self) -> None:
        self._enqueue_next_races(old=True)

    def test_enqueue_next_when_idle_plays_now(self) -> None:
        async def go():
            async with Harness() as h:
                await h.player.enqueue_next([T("X"), T("Y")])
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid("X"))
                self.assertEqual(await h.queue_ids(), [vid("Y")])

        run(go())

    def test_dj_turn_and_filler_do_not_interleave(self) -> None:
        """Pi 25. 9.: clear_queue tahu DJe + enqueue plniče → fronta ytdj
        o dvě položky vedle mpv; teď se sled DJe nesmí rozdělit a náš obraz
        je vždycky ten mpv."""

        async def go():
            for seed in range(8):
                async with Harness(jitter=0.003, seed=seed) as h:
                    p = h.player
                    await p.enqueue([T("cur")])
                    await h.settle()
                    counter = iter(range(1000, 9999))
                    rng = random.Random(seed)

                    async def filler():
                        for _ in range(25):
                            gen = p.generation
                            await asyncio.sleep(rng.random() * 0.004)  # next_tracks
                            tracks = [T(next(counter)) for _ in range(rng.randint(1, 3))]
                            async with queue_transaction(p):
                                if p.generation != gen:
                                    continue
                                await p.enqueue(tracks)

                    async def dj(k: int):
                        await asyncio.sleep(rng.random() * 0.03)
                        first = [T(f"r{k}")]
                        async with queue_transaction(p):
                            await p.clear_queue()
                            await p.enqueue(first)
                            await asyncio.sleep(0.005)  # next_tracks
                            fresh = [T(f"f{k}a"), T(f"f{k}b")]
                            await p.enqueue(fresh)
                            # hned za hrající přesně to, co tah zařadil — plnič se
                            # nevmísil (vyžádané přes enqueue_next smí být před tím)
                            up = [v for v in h.fake.upcoming() if not v.startswith("xxxxxxxxxn")]
                            self.assertEqual(
                                up, [vid(f"r{k}"), vid(f"f{k}a"), vid(f"f{k}b")],
                                (seed, h.fake.ids()),
                            )
                            self.assertEqual(p.upcoming_ids(), h.fake.upcoming())

                    async def listener():
                        for k in range(3):
                            await asyncio.sleep(rng.random() * 0.02)
                            await p.enqueue_next([T(f"n{k}")])

                    await asyncio.gather(filler(), dj(1), dj(2), listener())
                    await h.settle()
                    self.assertEqual(p.upcoming_ids(), h.fake.upcoming())
                    self.assertEqual(await h.queue_ids(), h.fake.upcoming())

        run(go())

    def test_random_mutations_keep_mirror_true(self) -> None:
        async def go():
            for seed in range(6):
                async with Harness(jitter=0.002, seed=seed) as h:
                    p = h.player
                    rng = random.Random(seed)
                    n = iter(range(100, 999))

                    async def actor():
                        for _ in range(15):
                            op = rng.choice(["enq", "enq", "next", "clear", "skip", "eof"])
                            if op == "enq":
                                await p.enqueue([T(next(n)) for _ in range(rng.randint(1, 3))])
                            elif op == "next":
                                await p.enqueue_next([T(next(n))])
                            elif op == "clear":
                                await p.clear_queue()
                            elif op == "skip":
                                await p.skip()
                            elif h.fake.cur is not None:
                                h.fake.finish_current()
                            await asyncio.sleep(rng.random() * 0.003)

                    await asyncio.gather(actor(), actor(), actor())
                    await h.settle()
                    self.assertEqual(await h.queue_ids(), h.fake.upcoming(), seed)
                    self.assertEqual(p.upcoming_ids(), h.fake.upcoming(), seed)

        run(go())

    def test_history_is_pruned(self) -> None:
        async def go():
            async with Harness(jitter=0) as h:
                await h.player.enqueue([T(i) for i in range(20)])
                for _ in range(12):
                    h.fake.finish_current()
                    await h.settle(0.02)
                await h.settle(0.1)
                self.assertEqual(h.fake.current_vid(), vid(12))
                self.assertLessEqual(h.fake.cur, mpvmod.KEEP_HISTORY)
                self.assertEqual(await h.queue_ids(), [vid(i) for i in range(13, 20)])

        run(go())


class Prefetch(unittest.TestCase):
    def test_ahead_is_next_n_in_mpv_order(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(12)])
                await p.enqueue_next([T("X")])
                await h.settle(0.4)
                self.assertEqual(h.aheads()[-1], h.fake.upcoming()[:PREFETCH_AHEAD])
                self.assertEqual(h.aheads()[-1][0], vid("X"))
                self.assertEqual(p.prefetch_ids(), h.fake.upcoming()[:PREFETCH_AHEAD])

        run(go())

    def test_skip_resends_window_immediately(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(12)])
                await h.settle(0.4)
                before = len(h.aheads())
                await p.skip()
                await h.settle(0.05)  # < PREFETCH_SETTLE: po Další se nečeká
                self.assertGreater(len(h.aheads()), before)
                self.assertEqual(h.aheads()[-1], h.fake.upcoming()[:PREFETCH_AHEAD])
                self.assertEqual(h.aheads()[-1][0], vid(2))
                # skladbu, na kterou se právě přeskočilo, resolver nesmí zahodit
                last = [r for r in h.resolver if r.get("op") == "ahead"][-1]
                self.assertEqual(last["keep"], [vid(1)])

        run(go())

    def test_codex_hold_keeps_list_and_first(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                p.busy_check = lambda: True
                await p.enqueue([T(i) for i in range(4)])
                await h.settle(0.4)
                req = [r for r in h.resolver if r.get("op") == "ahead"][-1]
                self.assertTrue(req["hold"])
                self.assertEqual(req["ids"], h.fake.upcoming()[:PREFETCH_AHEAD])
                # dvě nejbližší se řeší i během tahu Codexu (Další hned po přání)
                self.assertEqual(req["first"], [vid(1), vid(2)])

                # wait_ready: přednostně i během hold, True, jakmile je hotová
                async def resolved_later():
                    await asyncio.sleep(0.3)
                    p._on_resolver_event("_state", {"ready": [vid(3)], "busy": None, "urgent": []})

                asyncio.create_task(resolved_later())
                task = asyncio.create_task(p.wait_ready(vid(3), timeout=2))
                await h.settle(0.1)
                req = [r for r in h.resolver if r.get("op") == "ahead"][-1]
                self.assertEqual(req["first"], [vid(3), vid(1), vid(2)])
                self.assertTrue(await task)
                self.assertFalse(await p.wait_ready(vid(2), timeout=0.2))

        run(go())


class SmartSkip(unittest.TestCase):
    """Další, když ta hned další ještě není vyřešená, ale pozdější podkres ano."""

    async def _setup(self, h, n=8, ready=(), protected=()):
        p = h.player
        await p.enqueue([T(i) for i in range(n)])
        await h.settle()
        p.is_protected = lambda v: v in {vid(x) for x in protected}
        p._on_resolver_event("_state", {"ready": [vid(x) for x in ready], "busy": None,
                                        "urgent": []})
        return p

    def _reqs(self, h):
        return [f for k, f in h.events if k == "track.request" and f["why"] == "skip"]

    def test_jumps_to_prepared_background_track(self) -> None:
        async def go():
            async with Harness() as h:
                p = await self._setup(h, ready=(4, 5))
                await p.skip()
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(4))
                # přeskočené nezmizely — jsou hned za ní, v původním pořadí
                self.assertEqual(h.fake.upcoming()[:4], [vid(1), vid(2), vid(3), vid(5)])
                req = self._reqs(h)[-1]
                self.assertTrue(req["smart_skip"])
                self.assertEqual(req["bypassed"], [vid(1), vid(2), vid(3)])
                self.assertEqual(req["next_id"], vid(4))
                self.assertEqual(await h.queue_ids(), h.fake.upcoming())

        run(go())

    def test_never_jumps_over_a_request(self) -> None:
        async def go():
            async with Harness() as h:
                p = await self._setup(h, ready=(4,), protected=(2,))
                await p.skip()
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertNotIn("smart_skip", self._reqs(h)[-1])

        run(go())

    def test_never_moves_a_request_forward(self) -> None:
        async def go():
            async with Harness() as h:
                p = await self._setup(h, ready=(3,), protected=(3,))
                await p.skip()
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(1))

        run(go())

    def test_plain_next_when_next_is_ready(self) -> None:
        async def go():
            async with Harness() as h:
                p = await self._setup(h, ready=(1, 4))
                await p.skip()
                await h.settle()
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertNotIn("smart_skip", self._reqs(h)[-1])

        run(go())

    def test_burst_plays_only_prepared_while_any_exists(self) -> None:
        async def go():
            async with Harness() as h:
                p = await self._setup(h, n=12, ready=(3, 5, 6))
                played = []
                for _ in range(3):
                    await p.skip()
                    await h.settle()
                    played.append(h.fake.current_vid())
                self.assertEqual(played, [vid(3), vid(5), vid(6)])
                self.assertEqual(h.fake.upcoming()[:4], [vid(1), vid(2), vid(4), vid(7)])

        run(go())


class ProgressivePrefetch(unittest.TestCase):
    """F-ZVUK-05: nejdřív 3 nejbližší, pak po jedné až 10, nikdy na úkor naléhavé."""

    def resolver(self):
        return ResolverQueue.resolver(self)  # type: ignore[arg-type]

    @staticmethod
    def drain(r, res, limit: int = 30) -> list[str]:
        """Co by hlavní vlákno postupně řešilo (každá skladba hned hotová)."""
        order = []
        for _ in range(limit):
            v = res._next()
            if v is None:
                break
            order.append(v)
            res.ready[v] = (r.time.time(), "{}")
        return order

    def test_player_sends_window_of_ten_with_near_three(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(15)])
                await h.settle(0.4)
                req = [r for r in h.resolver if r.get("op") == "ahead"][-1]
                self.assertEqual(req["ids"], h.fake.upcoming()[:10])
                self.assertEqual(req["near"], 3)
                self.assertEqual(p.prefetch_window, 10)
                self.assertEqual(p.prefetch_depth, 6)  # plnič fronty beze změny
                # nastavení za běhu (web): menší okno, jiný počet hned
                p.cfg.prefetch_max, p.cfg.prefetch_first = 5, 2
                await p.enqueue([T("Z")])
                await h.settle(0.4)
                req = [r for r in h.resolver if r.get("op") == "ahead"][-1]
                self.assertEqual((len(req["ids"]), req["near"]), (5, 2))

        run(go())

    def test_nearest_three_first_then_extends_to_ten_when_idle(self) -> None:
        r, res = self.resolver()
        ids = [vid(i) for i in range(1, 11)]
        with redirect_stderr(io.StringIO()):
            res.t_get = r.time.monotonic()  # skladba právě startuje
            res.set_ahead(ids, near=3)
            self.assertEqual(self.drain(r, res), ids[:3])  # nejbližší hned
            self.assertIsNotNone(res._wake_at)  # rozšiřování počká na klid
            res.t_get -= r.EXTEND_QUIET
            self.assertEqual(self.drain(r, res), ids[3:])  # pak po jedné až 10
        self.assertEqual(set(res.ready), set(ids))

    def test_urgent_and_wish_preempt_extension(self) -> None:
        r, res = self.resolver()
        ids = [vid(i) for i in range(1, 11)]
        with redirect_stderr(io.StringIO()):
            res.set_ahead(ids, near=3)
            self.assertEqual(self.drain(r, res, 5), ids[:5])  # rozšiřuje se
            res.urgent.append(vid("U"))  # mpv čeká (Další na nepřipravenou)
            self.assertEqual(res._next(), vid("U"))
            res.urgent.clear()
            res.set_ahead(ids, near=3, first=[vid("W")])  # první skladba nového přání
            self.assertEqual(res._next(), vid("W"))
            res.set_ahead(ids, near=3, hold=True)  # Codex: rozšiřování stojí
            self.assertIsNone(res._next())
            # druhé (urgentní) vlákno rozšiřování nikdy nedělá
            res.set_ahead(ids, near=3)
            res.busy = vid("B")
            self.assertIsNone(res._next(urgent_only=True))

    def test_window_reshuffles_when_a_wish_is_inserted(self) -> None:
        r, res = self.resolver()
        ids = [vid(i) for i in range(1, 11)]
        with redirect_stderr(io.StringIO()):
            res.set_ahead(ids, near=3)
            self.drain(r, res)
            wish = vid("X")
            res.set_ahead([wish] + ids[:9], near=3)  # přání hned za hrající
            self.assertNotIn(ids[9], res.ready)  # vypadla z okna → z paměti
            self.assertEqual(res._next(), wish)  # přání je mezi nejbližšími
            self.assertEqual(self.drain(r, res), [wish])
            res.set_ahead(ids[:4], near=3)  # fronta se zkrátila
            self.assertEqual(set(res.ready), set(ids[:4]))

    def test_ten_skips_in_a_row_all_land_on_prepared(self) -> None:
        async def go():
            async with Harness(jitter=0.001) as h:
                p = h.player
                await p.enqueue([T(i) for i in range(15)])
                await h.settle(0.4)
                p._on_resolver_event("_state", {"ready": p.prefetch_ids(), "busy": None,
                                                "urgent": []})
                for _ in range(10):
                    await p.skip()
                    await asyncio.sleep(0.01)
                await h.settle()
                reqs = [f for k, f in h.events if k == "track.request" and f["why"] == "skip"]
                self.assertEqual(len(reqs), 10)
                self.assertTrue(all(r["next_ready"] for r in reqs), reqs)
                self.assertFalse([r for r in reqs if r.get("smart_skip")])  # nic se nepřeskládalo
                self.assertEqual(h.fake.current_vid(), vid(10))

        run(go())


class SkipBurst(unittest.TestCase):
    def test_burst_requests_name_what_mpv_plays(self) -> None:
        async def go():
            async with Harness(jitter=0.001) as h:
                p = h.player
                await p.enqueue([T(i) for i in range(15)])
                await h.settle()
                started = []
                for _ in range(10):
                    await p.skip()
                    await asyncio.sleep(0.01)
                    started.append(h.fake.current_vid())
                await h.settle()
                reqs = [f for k, f in h.events if k == "track.request" and f["why"] == "skip"]
                self.assertEqual(len(reqs), 10)  # každé Další je požadavek
                self.assertEqual([r["next_id"] for r in reqs], started)
                self.assertEqual(await h.queue_ids(), h.fake.upcoming())
                self.assertEqual(h.aheads()[-1], h.fake.upcoming()[:PREFETCH_AHEAD])

        run(go())

    def test_rapid_skips_without_waiting(self) -> None:
        async def go():
            async with Harness(jitter=0.002) as h:
                p = h.player
                await p.enqueue([T(i) for i in range(15)])
                await h.settle()
                await asyncio.gather(*(p.skip() for _ in range(5)))
                await h.settle(0.1)
                self.assertEqual(h.fake.current_vid(), vid(5))
                self.assertEqual(await h.queue_ids(), h.fake.upcoming())

        run(go())

    def test_skip_while_loading_cancels_and_is_not_an_error(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(i) for i in range(5)])
                await h.settle()
                self.assertIsNotNone(p._load)
                self.assertIsNone(p._load["t_play"])  # zvuk ještě neteče
                p._load["vid"] = vid(0)
                got = []

                async def handler(ev):
                    got.append((ev.kind, ev.track.id if ev.track else None))

                p.on_event(handler)
                # mpv opuštěnou položku, jejíž ytdl_hook skončil chybou "zrušeno",
                # může ohlásit jako error — nesmí to být "nepřehratelné"
                h.fake.next_reason = "error"
                await p.skip()
                await h.settle()
                self.assertIn({"op": "cancel", "ids": [vid(0)]}, h.resolver)
                self.assertIn(("skipped", vid(0)), got)
                self.assertNotIn("error", [k for k, _ in got])

        run(go())

    def test_skip_with_nothing_playing_is_not_a_request(self) -> None:
        async def go():
            async with Harness() as h:
                await h.player.skip()
                await h.settle()
                self.assertFalse([k for k, _ in h.events if k == "track.request"])

        run(go())


class ResolverQueue(unittest.TestCase):
    """ytdl_resolver: cache se po zásahu drží, zrušení pustí mpv dál, first i s hold."""

    def resolver(self):
        stub = types.ModuleType("yt_dlp")
        stub.YoutubeDL = object  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"yt_dlp": stub}):
            sys.modules.pop("ytdj.player.ytdl_resolver", None)
            from ytdj.player import ytdl_resolver as r
        res = r.Resolver()
        res.template = ["-J", "--"]
        res.ydl = object()  # type: ignore[assignment]
        res.loaded = True  # yt-dlp "naimportované"
        return r, res

    def argv(self, v: str) -> list[str]:
        return ["-J", "--", f"https://music.youtube.com/watch?v={v}"]

    def test_hit_keeps_entry_until_dropped(self) -> None:
        r, res = self.resolver()
        res.ready[vid(1)] = (r.time.time(), "{}")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(res.get(self.argv(vid(1)))[0], "{}")
            self.assertEqual(res.get(self.argv(vid(1)))[0], "{}")  # mpv prefetch + load
            res.set_ahead([vid(2)])
        self.assertNotIn(vid(1), res.ready)

    def test_keep_protects_track_being_loaded(self) -> None:
        r, res = self.resolver()
        res.ready[vid(1)] = (r.time.time(), "{}")
        with redirect_stderr(io.StringIO()):
            res.set_ahead([vid(2), vid(3)], keep=[vid(1)])  # okno po Další
            self.assertEqual(res.get(self.argv(vid(1)))[0], "{}")  # pak přijde mpv
        self.assertNotEqual(res._next(), vid(1))

    def test_cancel_releases_waiting_get(self) -> None:
        import threading

        r, res = self.resolver()
        out = {}
        with redirect_stderr(io.StringIO()):
            t = threading.Thread(target=lambda: out.update(r=res.get(self.argv(vid(1)), 5)))
            t.start()
            for _ in range(100):
                if vid(1) in res.waiting:
                    break
                r.time.sleep(0.01)
            res.cancel([vid(1)])
            t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(out["r"], (None, "zrušeno"))
        self.assertNotIn(vid(1), res.urgent)
        self.assertFalse(res.cancelled)

    def test_foreign_template_drops_cache(self) -> None:
        """Ruční dotaz shimem bez voleb mpv přepnul formát na video HLS a
        hotové JSONy s ním pak dostávalo mpv (Pi 25. 9.: načtení 25 s)."""
        r, res = self.resolver()
        mpv_argv = ["--format=774/251", "-J", "--"]
        res.template = mpv_argv
        res.ready[vid(1)] = (r.time.time(), "{}")
        with redirect_stderr(io.StringIO()):
            res.set_template(mpv_argv + ["url"])  # stejné volby — nic
            self.assertIn(vid(1), res.ready)
            res.set_template(["-J", "--", "url"])  # cizí volby
        self.assertFalse(res.ready)

    def _lanes(self):
        """Resolver s oběma vlákny; hlavní YoutubeDL drží skladbu, dokud se
        nepustí `gate` (rozdělaná skladba dopředu, ~7 s na Pi)."""
        import threading

        r, res = self.resolver()
        gate = threading.Event()

        class Ydl:
            def __init__(self, block: bool) -> None:
                self.calls: list[str] = []
                self.block = block

            def extract_info(self, url, download=False):
                v = url.rsplit("v=", 1)[-1]
                self.calls.append(v)
                if self.block:
                    gate.wait(5)
                return {"id": v}

            def sanitize_info(self, i):
                return i

        main, second = Ydl(True), Ydl(False)
        res.ydl, res.ydl_for = main, res.template
        res.ydl_urgent, res.ydl_urgent_for = second, res.template
        res.lanes = 2
        for target in (res.worker, res.urgent_worker):
            threading.Thread(target=target, daemon=True).start()
        return r, res, gate, main, second

    def test_urgent_does_not_wait_for_track_being_prepared(self) -> None:
        """F-PRESKOK-09: Další za připravené skladby nečeká na rozdělanou."""
        err = io.StringIO()
        with redirect_stderr(err):
            r, res, gate, main, second = self._lanes()
            try:
                res.set_ahead([vid(1)])
                for _ in range(200):  # hlavní vlákno se pustilo do skladby dopředu
                    if res.busy == vid(1):
                        break
                    r.time.sleep(0.01)
                self.assertEqual(res.busy, vid(1))
                t0 = r.time.monotonic()
                data, error = res.get(self.argv(vid(2)), 3)
                took = r.time.monotonic() - t0
            finally:
                gate.set()
                for _ in range(200):  # doběhne, ať nepíše do stderr dalších testů
                    if res.busy is None:
                        break
                    r.time.sleep(0.01)
        self.assertIsNone(error)
        self.assertIn(vid(2), data)
        self.assertLess(took, 1.0)  # ne až po rozdělané (gate by čekal 5 s)
        self.assertEqual(second.calls, [vid(2)])  # druhé vlákno: jen urgentní
        self.assertEqual(main.calls, [vid(1)])
        get = [json.loads(x[6:]) for x in err.getvalue().splitlines()
               if x.startswith("EVENT ") and '"resolver.get"' in x][-1]
        self.assertIsNone(get["blocked_by"])
        self.assertEqual(get["how"], "miss")

    def test_second_lane_never_prepares_ahead(self) -> None:
        """Dopředu jen jedno vlákno (dva node při hrající hudbě = lupání);
        je-li hlavní volné, urgentní skladbu vezme ono samo."""
        with redirect_stderr(io.StringIO()):
            r, res, gate, main, second = self._lanes()
            gate.set()  # hlavní nic nedrží
            data, error = res.get(self.argv(vid(4)), 3)  # hlavní je volné
            self.assertIsNone(res._next(urgent_only=True))
            res.set_ahead([vid(1), vid(2), vid(3)])
            for _ in range(300):
                if all(v in res.ready for v in (vid(1), vid(2), vid(3))):
                    break
                r.time.sleep(0.01)
        self.assertIsNone(error)
        self.assertEqual(second.calls, [])
        self.assertEqual(sorted(main.calls), sorted([vid(1), vid(2), vid(3), vid(4)]))

    def test_hold_and_first(self) -> None:
        r, res = self.resolver()
        with redirect_stderr(io.StringIO()):
            res.set_ahead([vid(1), vid(2)], hold=True)
            self.assertIsNone(res._next())
            res.set_ahead([vid(1), vid(2)], hold=True, first=[vid(2)])
            self.assertEqual(res._next(), vid(2))
            res.set_ahead([vid(1), vid(2)])
            self.assertEqual(res._next(), vid(1))


if __name__ == "__main__":
    unittest.main()
