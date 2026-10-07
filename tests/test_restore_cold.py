"""Obnova po vypnutí / výpadku proudu (POZADAVKY #59): po zapnutí Pi se vrátí
fronta, podkres i místo ve skladbě — potichu, jen z téhož dne, a rozbitý stav
start neshodí.

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python -m unittest tests.test_restore_cold
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-cold-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_player_queue import Harness, T, vid  # noqa: E402
from test_wishes import Rig, WebApp, run  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.wishes import cold_restore, is_cold, should_resume  # noqa: E402

DAY = datetime(2026, 10, 6, 10, 0)  # úterý dopoledne
STAMP = datetime(2026, 10, 6, 9, 40).timestamp()  # stav uložený týž den
YESTERDAY = datetime(2026, 10, 5, 17, 35).timestamp()


def cold(state: dict, **kw) -> dict:
    """Tentýž stav, jak ho najde ytdj po zapnutí Pi (jiný boot)."""
    return {**state, "boot": "predchozi-boot", "clock": True, "saved": STAMP, **kw}


def resumes(rig) -> list[dict]:
    return [f for k, f in rig.events if k == "request.resume"]


async def playing_wish(rig, pos: float = 42.0) -> tuple[dict, str, Path]:
    """Hraje přání nad podkresem; uložený stav, co hraje a kopie pozice na „kartě“."""
    await rig.background()
    j = rig.wq.submit("Holky z naší školky", "Jana")
    await rig.until(lambda: j.state == "playing")
    playing = rig.fake.current_vid()
    pb = rig.dir / "playback-disk.json"
    rig.player.playback_disk = pb
    rig.player._time_pos = pos
    rig.player._write_disk(*rig.player._disk_job(force=True))
    await rig.wq.refresh_playing()
    rig.wq.save()
    return rig.wq.load_state(), playing, pb


class ColdRule(unittest.TestCase):
    def test_same_day_only_and_only_with_a_trustworthy_clock(self):
        s = {"saved": STAMP, "clock": True}
        self.assertEqual(cold_restore(s, True, DAY), (True, "same_day"))
        # včerejší stav se nevrací
        self.assertEqual(cold_restore({**s, "saved": YESTERDAY}, True, DAY),
                         (False, "earlier_day"))
        # hodiny teď neseřízené: den nejde soudit → obnovit, nezahodit
        self.assertEqual(cold_restore({**s, "saved": YESTERDAY}, False, DAY),
                         (True, "age_unknown"))
        # čas uložení změřený neseřízenými hodinami (nebo starý zápis bez značky)
        self.assertEqual(cold_restore({"saved": YESTERDAY, "clock": False}, True, DAY),
                         (True, "age_unknown"))
        self.assertEqual(cold_restore({"saved": YESTERDAY}, True, DAY), (True, "age_unknown"))
        # stav „z budoucnosti“ a nesmyslný čas: hodinám nejde věřit
        self.assertEqual(cold_restore({**s, "saved": STAMP + 3 * 86400}, True, DAY),
                         (True, "age_unknown"))
        self.assertEqual(cold_restore({**s, "saved": "včera"}, True, DAY), (True, "age_unknown"))
        self.assertEqual(cold_restore(None, True, DAY), (False, "no_state"))
        self.assertEqual(cold_restore([], True, DAY), (False, "no_state"))

    def test_cold_boot_never_starts_music_by_itself(self):
        """F-RESTART-01 platí dál: po zapnutí Pi se hudba sama nerozjede."""
        s = {"saved": time.time() - 30, "playing": True, "boot": "predchozi-boot", "clock": True}
        self.assertEqual(should_resume(s, DAY, boot="novy-boot"), (False, "reboot"))
        self.assertTrue(is_cold(s, "novy-boot"))
        self.assertFalse(is_cold(s, "predchozi-boot"))
        self.assertFalse(is_cold(None, "novy-boot"))
        self.assertFalse(is_cold({"saved": 1}, "novy-boot"))  # starý zápis bez bootu


class ColdRestore(unittest.TestCase):
    def test_restores_wishes_background_and_position_but_stays_silent(self):
        async def go():
            async with Rig() as rig:
                saved, playing, pb = await playing_wish(rig)
            self.assertEqual(saved["now"], playing)
            async with Rig() as rig2:
                rig2.player.playback_disk = pb
                why = await rig2.wq.resume(cold(saved), now=DAY, synced=True)
                self.assertEqual(why, "same_day")
                await rig2.settle(0.3)
                # přání i podkres jsou zpátky, skladba čeká pozastavená na svém místě
                self.assertEqual({w.who for w in rig2.wq.active()}, {"Jana"})
                self.assertTrue(all(w.restored for w in rig2.wq.active()))
                self.assertTrue(rig2.pools.pools)
                self.assertTrue(rig2.player._paused)
                self.assertEqual(rig2.fake.started[0], playing)
                self.assertEqual(list(rig2.fake.loadfile_options.values()), [{"start": "40.0"}])
                self.assertEqual(rig2.fake.ids().count(playing), 1)
                # web i displej to řeknou
                self.assertIn("Po zapnutí", rig2.wq.last["reply"])
                self.assertIn("▶", rig2.wq.last["reply"])
                ev = resumes(rig2)[-1]
                self.assertEqual(
                    (ev["cold"], ev["restore"], ev["restore_reason"], ev["resume"], ev["reason"]),
                    (True, True, "same_day", False, "reboot"))
                self.assertEqual((ev["track"], ev["pos_s"], ev["paused"], ev["restored_wishes"]),
                                 (playing, 40.0, True, 1))
                # Hrát: pokračuje tatáž skladba, nic se nevybírá znovu
                await rig2.player.toggle_pause(False)
                await rig2.settle(0.1)
                self.assertFalse(rig2.player._paused)
                self.assertEqual(rig2.fake.current_vid(), playing)

        run(go())

    def test_yesterdays_state_is_not_restored(self):
        async def go():
            async with Rig() as rig:
                saved, _playing, pb = await playing_wish(rig)
            async with Rig() as rig2:
                rig2.player.playback_disk = pb
                why = await rig2.wq.resume(cold(saved, saved=YESTERDAY), now=DAY, synced=True)
                self.assertEqual(why, "earlier_day")
                await rig2.settle(0.2)
                self.assertEqual(rig2.wq.active(), [])
                self.assertFalse(rig2.fake.ids())
                self.assertFalse(rig2.pools.pools)
                ev = resumes(rig2)[-1]
                self.assertEqual((ev["restore"], ev["restore_reason"], ev["cold"]),
                                 (False, "earlier_day", True))
                self.assertGreater(ev["age_s"], 0)
                # jukebox jede dál načisto
                w = rig2.wq.submit("pusť Kabát", "Petr")
                await rig2.until(lambda: w.state in ("queued", "playing"))

        run(go())

    def test_unsynced_clock_restores_instead_of_throwing_away(self):
        async def go():
            async with Rig() as rig:
                saved, playing, pb = await playing_wish(rig)
            async with Rig() as rig2:
                rig2.player.playback_disk = pb
                # podle (špatných) hodin je stav včerejší — ale hodiny seřízené nejsou
                why = await rig2.wq.resume(cold(saved, saved=YESTERDAY), now=DAY, synced=False)
                self.assertEqual(why, "age_unknown")
                await rig2.settle(0.3)
                self.assertEqual({w.who for w in rig2.wq.active()}, {"Jana"})
                self.assertTrue(rig2.player._paused)
                self.assertEqual(rig2.fake.started[0], playing)
                ev = resumes(rig2)[-1]
                self.assertEqual((ev["restore_reason"], ev["clock_synced"]), ("age_unknown", False))
                self.assertIsNone(ev["age_s"])  # neznámé stáří se neuvádí

        run(go())

    def test_waits_for_the_clock_without_blocking_and_play_cuts_the_wait(self):
        async def go():
            flag = Path(tempfile.mkdtemp(dir=_TMP)) / "synchronized"
            async with Rig() as rig:
                saved, _playing, pb = await playing_wish(rig)
            with mock.patch.object(wishes, "CLOCK_SYNC_FLAG", flag), \
                    mock.patch.object(wishes, "CLOCK_WAIT", 5.0):
                # hodiny se seřídí za chvíli: obnova počká a pak soudí den
                async with Rig() as rig2:
                    wishes.CLOCK_WAIT = 5.0
                    rig2.player.playback_disk = pb
                    task = asyncio.create_task(rig2.wq.resume(cold(saved, saved=YESTERDAY)))
                    ticks = 0
                    for _ in range(10):  # smyčka mezitím běží (web, displej)
                        await asyncio.sleep(0.02)
                        ticks += 1
                    self.assertTrue(rig2.wq.restoring)
                    self.assertFalse(task.done())
                    flag.touch()
                    self.assertEqual(await asyncio.wait_for(task, 3), "earlier_day")
                    self.assertFalse(rig2.wq.restoring)
                    self.assertEqual(ticks, 10)
                    self.assertTrue(resumes(rig2)[-1]["clock_synced"])
                flag.unlink()
                # hodiny se neseřídí vůbec: po CLOCK_WAIT se obnoví s neznámým stářím
                async with Rig() as rig3:
                    wishes.CLOCK_WAIT = 0.2
                    rig3.player.playback_disk = pb
                    t0 = time.monotonic()
                    why = await rig3.wq.resume(cold(saved, saved=YESTERDAY))
                    self.assertEqual(why, "age_unknown")
                    self.assertLess(time.monotonic() - t0, 2.0)
                    self.assertGreaterEqual(resumes(rig3)[-1]["clock_wait_ms"], 150)
                # někdo stiskne Hrát dřív: nečeká se, obnoví se hned
                async with Rig() as rig4:
                    wishes.CLOCK_WAIT = 60.0
                    rig4.player.playback_disk = pb
                    task = asyncio.create_task(rig4.wq.resume(cold(saved, saved=YESTERDAY)))
                    await asyncio.sleep(0.05)
                    t0 = time.monotonic()
                    await rig4.wq.restore_now()
                    self.assertLess(time.monotonic() - t0, 3.0)
                    self.assertEqual(await task, "age_unknown")
                    self.assertEqual({w.who for w in rig4.wq.active()}, {"Jana"})
                    await rig4.wq.restore_now()  # podruhé už není na co čekat

        run(go())

    def test_play_during_the_wait_restores_at_once_and_continues(self):
        """Hrát (web, displej) na hodiny nečeká a nespustí nový výběr DJe:
        pokračuje skladba, která hrála, na svém místě."""
        async def go():
            flag = Path(tempfile.mkdtemp(dir=_TMP)) / "synchronized"
            async with Rig() as rig:
                saved, playing, pb = await playing_wish(rig)
            with mock.patch.object(wishes, "CLOCK_SYNC_FLAG", flag), \
                    mock.patch.object(wishes, "CLOCK_WAIT", 60.0):
                async with Rig() as rig2:
                    rig2.player.playback_disk = pb
                    app = WebApp(rig2)
                    task = asyncio.create_task(rig2.wq.resume(cold(saved), now=DAY))
                    await asyncio.sleep(0.05)
                    t0 = time.monotonic()
                    self.assertEqual(await app.play_or_start("panel"), "play")
                    self.assertLess(time.monotonic() - t0, 3.0)
                    self.assertEqual(await task, "age_unknown")
                    await rig2.settle(0.2)
                    self.assertFalse(rig2.player._paused)
                    self.assertEqual(rig2.fake.current_vid(), playing)
                    self.assertEqual(list(rig2.fake.loadfile_options.values()), [{"start": "40.0"}])
                    self.assertFalse(any(k == "dj.start" for k, _ in rig2.events))
                    self.assertEqual(rig2.asked, [])  # DJ nic nového nevybíral

        run(go())

    def test_what_somebody_started_while_waiting_is_not_overridden(self):
        async def go():
            flag = Path(tempfile.mkdtemp(dir=_TMP)) / "synchronized"
            async with Rig() as rig:
                saved, _playing, pb = await playing_wish(rig)
            with mock.patch.object(wishes, "CLOCK_SYNC_FLAG", flag), \
                    mock.patch.object(wishes, "CLOCK_WAIT", 5.0):
                async with Rig() as rig2:
                    rig2.player.playback_disk = pb
                    task = asyncio.create_task(rig2.wq.resume(cold(saved), now=DAY))
                    await asyncio.sleep(0.05)
                    w = rig2.wq.submit("pusť Kabát", "Petr")
                    await rig2.until(lambda: w.state == "playing")
                    flag.touch()
                    self.assertEqual(await asyncio.wait_for(task, 3), "superseded")
                    await rig2.settle(0.2)
                    self.assertFalse(rig2.player._paused)  # nové přání hraje dál
                    self.assertEqual({x.who for x in rig2.wq.active()}, {"Petr"})

        run(go())

    def test_position_of_another_older_track_is_not_used(self):
        """Stav přání říká „hraje Y“, kopie pozice na kartě je starší a patří X."""
        async def go():
            async with Rig() as rig:
                saved, playing, pb = await playing_wish(rig)
            snap = json.loads(pb.read_text())
            snap["saved"] = STAMP - 30
            pb.write_text(json.dumps(snap))
            async with Rig() as rig2:
                rig2.player.playback_disk = pb
                other = cold(saved, now="jinaSkladba")
                self.assertEqual(await rig2.wq.resume(other, now=DAY, synced=True), "same_day")
                await rig2.settle(0.3)
                self.assertFalse(rig2.fake.loadfile_options)  # nikam se neskáče
                self.assertEqual({w.who for w in rig2.wq.active()}, {"Jana"})
                self.assertTrue(rig2.player._paused)
                self.assertIsNone(resumes(rig2)[-1]["track"])
            # kopie novější než stav přání platí: skladba se změnila těsně před výpadkem
            snap["saved"] = STAMP + 3
            pb.write_text(json.dumps(snap))
            async with Rig() as rig3:
                rig3.player.playback_disk = pb
                await rig3.wq.resume(cold(saved, now="jinaSkladba"), now=DAY, synced=True)
                await rig3.settle(0.3)
                self.assertEqual(rig3.fake.started[0], playing)

        run(go())


async def top_up(rig, n: int = 8) -> list[str]:
    """Plnič podkresu, jak běží v aplikaci: doplní frontu z poolů. Vrací, co je v mpv."""
    await rig.player.enqueue(await rig.dj.next_tracks(n))
    await rig.settle(0.2)
    return rig.fake.ids()


async def playing_background(rig, pos: float = 152.0) -> tuple[dict, str, Path]:
    """Hraje jen podkres (rádio ze seedu), žádné přání — jako na Pi 6. 10."""
    await rig.background()
    playing = rig.fake.current_vid()
    pb = rig.dir / "playback-disk.json"
    rig.player.playback_disk = rig.player.playback_file = pb
    rig.player._time_pos = pos
    rig.player._save_playback()
    rig.player._write_disk(*rig.player._disk_job(force=True))
    await rig.wq.refresh_playing()
    rig.wq.save()
    return rig.wq.load_state(), playing, pb


class NoDuplicates(unittest.TestCase):
    """Navázaná skladba nesmí být ve frontě podruhé (Pi 6. 10. 18:48: po
    zapnutí byl v mpv „Jump" jako hrající i jako další a zahrál dvakrát)."""

    def test_cold_restore_of_background_does_not_queue_the_resumed_track_again(self):
        async def go():
            async with Rig() as rig:
                saved, playing, pb = await playing_background(rig)
            self.assertEqual(saved["bg"]["mode"], "seeds")
            async with Rig() as rig2:  # nová historie i paměť poolů, jako po zapnutí
                rig2.player.playback_disk = pb
                self.assertEqual(await rig2.wq.resume(cold(saved), now=DAY, synced=True),
                                 "same_day")
                await rig2.settle(0.2)
                self.assertEqual(rig2.fake.started[0], playing)
                ids = await top_up(rig2)
                self.assertGreater(len(ids), 4)
                self.assertEqual(ids.count(playing), 1, ids)
                self.assertEqual(len(ids), len(set(ids)), ids)
                # dohraje a jde se dál — ne znovu od začátku
                rig2.fake.finish_current()
                await rig2.settle(0.3)
                self.assertNotEqual(rig2.fake.current_vid(), playing)

        run(go())

    def test_no_video_id_twice_in_the_player_after_any_restore(self):
        """Studený i teplý start, hrálo se i stála pauza, s přáním i bez něj —
        a i kdyby si pooly nepamatovaly vůbec nic."""
        async def go():
            for with_wish in (False, True):
                for paused in (False, True):
                    async with Rig() as rig:
                        if with_wish:
                            saved, playing, pb = await playing_wish(rig)
                            rig.player.playback_file = pb
                        else:
                            saved, playing, pb = await playing_background(rig)
                        if paused:
                            await rig.player.toggle_pause(True)
                            rig.player._save_playback()
                            rig.player._write_disk(*rig.player._disk_job(force=True))
                            await rig.wq.refresh_playing()
                            rig.wq.save()
                            saved = rig.wq.load_state()
                    for is_cold in (False, True):
                        case = (with_wish, paused, is_cold)
                        async with Rig() as rig2:
                            rig2.player.playback_disk = rig2.player.playback_file = pb
                            state = cold(saved) if is_cold else saved
                            await rig2.wq.resume(state, now=DAY, synced=True)
                            await rig2.settle(0.3)
                            self.assertEqual(rig2.fake.started[0], playing, case)
                            ids = await top_up(rig2)
                            self.assertEqual(len(ids), len(set(ids)), (case, ids))
                            # pojistka v doplňování: ani se zapomnětlivými pooly
                            rig2.pools.session_seen.clear()
                            ids = await top_up(rig2, 12)
                            self.assertEqual(len(ids), len(set(ids)), (case, ids))
                            self.assertEqual(ids.count(playing), 1, case)

        run(go())

    def test_restored_wish_block_keeps_its_exact_order(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                playing = rig.fake.current_vid()
                before = [v for v in rig.upcoming() if rig.wq.owner.get(v) == p.id]
                self.assertGreaterEqual(len(before), 2)
                pb = rig.dir / "playback-disk.json"
                rig.player.playback_disk = pb
                rig.player._time_pos = 30.0
                rig.player._write_disk(*rig.player._disk_job(force=True))
                await rig.wq.refresh_playing()
                rig.wq.save()
                saved = rig.wq.load_state()
            async with Rig() as rig2:
                rig2.player.playback_disk = pb
                await rig2.wq.resume(cold(saved), now=DAY, synced=True)
                await rig2.settle(0.3)
                w = rig2.wq.by_id(p.id)
                self.assertEqual(rig2.fake.started[0], playing)
                after = [v for v in rig2.upcoming() if rig2.wq.owner.get(v) == w.id]
                self.assertEqual(after, before)
                ids = await top_up(rig2)
                self.assertEqual(len(ids), len(set(ids)), ids)
                # přání dál hraje před podkresem, ve stejném pořadí
                self.assertEqual(rig2.upcoming()[:len(before)], before)

        run(go())


class PowerCut(unittest.TestCase):
    """Výpadek proudu uprostřed zápisu: půlka souboru, nuly, nesmyslný obsah."""

    def _starts_clean(self, rig, why: str) -> None:
        self.assertIn(why, ("no_state", "error", "nothing_to_resume", "same_day", "age_unknown"))

    def test_truncated_session_file_starts_clean_and_queue_works(self):
        async def go():
            async with Rig() as rig:
                saved, _playing, _pb = await playing_wish(rig)
            text = json.dumps(cold(saved), ensure_ascii=False)
            for broken in (text[: len(text) // 2], "\0" * 4096, "", "[1, 2", "null", "42"):
                async with Rig() as rig2:
                    rig2.wq.state_file.write_text(broken)
                    why = await rig2.wq.resume(now=DAY, synced=True)
                    self.assertEqual(why, "no_state", broken[:20])
                    self.assertFalse(rig2.wq.restoring)
                    w = rig2.wq.submit("pusť Kabát", "Petr")
                    await rig2.until(lambda: w.state in ("queued", "playing"))

        run(go())

    def test_nonsense_content_never_crashes_or_wedges(self):
        async def go():
            async with Rig() as rig:
                saved, _playing, _pb = await playing_wish(rig)
            good = cold(saved)
            cases = [
                {**good, "wishes": 5},
                {**good, "wishes": [None, 3, {"id": 1}, {"id": "x", "token": "t", "tracks": 7}]},
                {**good, "bg": "podkres", "turns": []},
                {**good, "bg": {"mode": "artist", "tracks": [{"id": 5}, None], "left": "x"}},
                {**good, "bg": {"mode": "album", "tracks": "abc"}},
                {**good, "turns": {"last": {"a": "b"}, "turn_no": "x"}},
                {"boot": "predchozi-boot", "saved": [], "clock": True},
            ]
            for case in cases:
                async with Rig() as rig2:
                    why = await rig2.wq.resume(case, now=DAY, synced=True)
                    self.assertIsInstance(why, str)
                    self.assertFalse(rig2.wq.restoring)
                    self.assertTrue(resumes(rig2), case)  # vždy se zapíše, jak to dopadlo
                    # co se obnovilo napůl, ve frontě nestraší bez skladeb
                    self.assertTrue(all(w.tracks or w.state == "waiting"
                                        for w in rig2.wq.wishes), case)
                    w = rig2.wq.submit("pusť Kabát", "Petr")
                    await rig2.until(lambda: w.state in ("queued", "playing"), timeout=5)

        run(go())

    def test_half_written_position_copy_is_ignored(self):
        async def go():
            async with Rig() as rig:
                saved, _playing, pb = await playing_wish(rig)
            for broken in ('{"v": 1, "saved": 17', "\0" * 512, '{"track": {"id": "../etc"}}'):
                pb.write_text(broken)
                async with Rig() as rig2:
                    rig2.player.playback_disk = pb
                    self.assertEqual(await rig2.wq.resume(cold(saved), now=DAY, synced=True),
                                     "same_day")
                    await rig2.settle(0.2)
                    self.assertFalse(rig2.fake.loadfile_options)
                    self.assertEqual({w.who for w in rig2.wq.active()}, {"Jana"})

        run(go())


class Durable(unittest.TestCase):
    def test_important_changes_are_fsynced_off_the_loop_and_heartbeats_are_not(self):
        async def go():
            calls: list[int] = []
            real = os.fsync

            def fsync(fd):
                calls.append(threading.get_ident())
                return real(fd)

            async with Rig() as rig:
                await rig.background()
                loop_thread = threading.get_ident()
                with mock.patch.object(wishes.os, "fsync", fsync):
                    j = rig.wq.submit("Holky z naší školky", "Jana")
                    await rig.until(lambda: j.state == "playing")
                    await rig.wq.refresh_playing()
                    await rig.wq.save_async()
                    self.assertEqual(len(calls), 2)  # soubor a adresář (přejmenování)
                    self.assertNotIn(loop_thread, calls)  # event loop na kartu nečeká
                    self.assertTrue(any(k == "request.state_sync" for k, _ in rig.events))
                    # jen pozice / co hraje / stáří: zapíše se, ale bez fsync
                    rig.wq._now_id = "jinaSkladba"
                    before = rig.wq.state_file.read_text()
                    await rig.wq.save_async()
                    self.assertNotEqual(rig.wq.state_file.read_text(), before)
                    self.assertEqual(len(calls), 2)
                    # beze změny se nepíše vůbec
                    stamp = rig.wq.state_file.stat().st_mtime_ns
                    await rig.wq.save_async()
                    self.assertEqual(rig.wq.state_file.stat().st_mtime_ns, stamp)
                    # další důležitá změna (přání odebráno) → zase fsync
                    await rig.wq.remove(j.id, j.token)
                    await rig.settle(0.1)
                    await rig.wq.save_async()
                    self.assertEqual(len(calls), 4)
                saved = rig.wq.load_state()
                self.assertIn("clock", saved)
                self.assertFalse(list(rig.dir.glob("*.tmp")))  # dočasný soubor nezůstal

        run(go())


class PausedRestart(unittest.TestCase):
    def test_service_restart_keeps_the_paused_track_and_position(self):
        """Pauza → restart služby (nasazení) → Hrát pokračuje ve stejné skladbě."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "playing")
                playing = rig.fake.current_vid()
                await rig.player.toggle_pause(True)
                pb = rig.dir / "playback.json"
                rig.player.playback_file = pb
                rig.player._time_pos = 61.0
                rig.player._save_playback()
                await rig.wq.refresh_playing()
                rig.wq.save()
                saved = rig.wq.load_state()
            self.assertFalse(saved["playing"])
            self.assertTrue(json.loads(pb.read_text())["paused"])
            async with Rig() as rig2:
                rig2.player.playback_file = pb
                why = await rig2.wq.resume(saved, now=DAY)
                self.assertEqual(why, "was_not_playing")
                await rig2.settle(0.3)
                self.assertTrue(rig2.player._paused)
                self.assertEqual(rig2.fake.started[0], playing)
                self.assertEqual(list(rig2.fake.loadfile_options.values()), [{"start": "59.0"}])
                self.assertEqual(rig2.fake.ids().count(playing), 1)
                ev = resumes(rig2)[-1]
                self.assertEqual((ev["track"], ev["pos_s"], ev["paused"], ev["cold"]),
                                 (playing, 59.0, True, None))

        run(go())


class DiskSnapshot(unittest.TestCase):
    """MpvPlayer: kopie pozice na SD kartě — málo zápisů, ve vlákně, celý soubor."""

    def test_written_at_once_on_change_then_at_most_every_quarter_minute(self):
        pb = Path(tempfile.mkdtemp(dir=_TMP)) / "playback.json"

        async def go():
            async with Harness() as h:
                p = h.player
                p.playback_disk = pb
                self.assertIsNone(p._disk_job())  # nic nehraje, nic se nepíše
                await p.enqueue([T(1), T(2)])
                await h.settle(0.1)
                p._time_pos = 3.0
                job = p._disk_job()
                self.assertIsNotNone(job)  # nová skladba → hned
                p._write_disk(*job)
                self.assertEqual(json.loads(pb.read_text())["track"]["id"], vid(1))
                p._time_pos = 9.0
                self.assertIsNone(p._disk_job())  # jen pozice → až za DISK_EVERY
                p._disk_at -= mpvmod.DISK_EVERY
                job = p._disk_job()
                p._write_disk(*job)
                self.assertEqual(json.loads(pb.read_text())["pos"], 9.0)
                self.assertIsNone(p._disk_job())  # beze změny nic
                await p.toggle_pause(True)
                await h.settle(0.05)
                job = p._disk_job()
                self.assertIsNotNone(job)  # pauza → hned
                p._write_disk(*job)
                self.assertTrue(json.loads(pb.read_text())["paused"])
                self.assertFalse(list(pb.parent.glob("*.tmp")))
                # čtení: po zapnutí Pi se stáří nesoudí a pauza nevadí
                old = json.loads(pb.read_text())
                old["saved"] -= 5 * 3600
                pb.write_text(json.dumps(old))
                self.assertIsNone(p.saved_playback(900, disk=True))
                self.assertIsNone(p.saved_playback(None, disk=True))  # pauza bez povolení
                track, pos = p.saved_playback(None, True, True)
                self.assertEqual((track.id, pos), (vid(1), 7.0))
                # dohráno / zastaveno → kopie se jednou smaže
                p._current_id = None
                job = p._disk_job()
                self.assertEqual(job, (pb, None, None))
                p._write_disk(*job)
                self.assertFalse(pb.exists())
                self.assertIsNone(p._disk_job())
        run(go())

    def test_loop_writes_in_a_thread_and_reports_how_long_it_took(self):
        pb = Path(tempfile.mkdtemp(dir=_TMP)) / "playback.json"

        async def go():
            threads: list[int] = []
            async with Harness() as h:
                p = h.player
                p.playback_disk = pb
                real = p._write_disk

                def write(*a):
                    threads.append(threading.get_ident())
                    time.sleep(0.3)  # pomalá karta
                    return real(*a)

                p._write_disk = write
                await p.enqueue([T(1), T(2)])
                await h.settle(0.1)
                with mock.patch.object(mpvmod, "PLAYBACK_EVERY", 0.05), \
                        mock.patch.object(mpvmod, "DISK_SLOW_MS", 0):
                    task = asyncio.create_task(p._playback_loop())
                    t0, worst = time.monotonic(), 0.0
                    while time.monotonic() - t0 < 0.6:  # smyčka se o kartu nezasekne
                        t1 = time.monotonic()
                        await asyncio.sleep(0.01)
                        worst = max(worst, time.monotonic() - t1)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(threads)
                self.assertNotIn(threading.get_ident(), threads)
                self.assertLess(worst, 0.2)
                self.assertTrue(pb.exists())
                ev = [f for k, f in h.events if k == "player.snapshot_disk"]
                self.assertTrue(ev and ev[-1]["slow"] and ev[-1]["n"] >= 1, ev)
                self.assertIn("max_ms", ev[-1])
        run(go())


if __name__ == "__main__":
    unittest.main()
