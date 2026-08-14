"""Entry point: wires the components together and runs three tasks in one loop.

  repl    reads commands from the user
  filler  watches queue depth and tops it up from the pools
  mpv     sends events (start / finished / skipped)

One asyncio loop, no locks, no cross-thread handoffs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
from contextlib import suppress

from .agent import CodexDJ
from .config import Config, load_secrets, write_default_config, write_env_template
from .diagnose import check_audio
from .music import Catalog, RadioPools
from .music.catalog import RE_URL
from .player import MpvPlayer
from .player.base import PlayerEvent
from .state import Store
from .ui import Repl
from .web import WebServer

log = logging.getLogger("ytdj")


def preflight(cfg: Config) -> list[str]:
    problems = []
    if not shutil.which("mpv"):
        problems.append("chybí mpv  →  sudo apt install mpv ffmpeg")
    if not os.path.exists(cfg.yt_dlp_path) and not shutil.which("yt-dlp"):
        problems.append("chybí yt-dlp  →  uv tool install yt-dlp --with secretstorage")

    codex = shutil.which("codex")
    if not codex:
        problems.append("chybí codex CLI  →  https://github.com/openai/codex")
    elif not (os.path.expanduser("~/.codex/auth.json") and
              os.path.exists(os.path.expanduser("~/.codex/auth.json"))):
        problems.append("Codex není přihlášený  →  codex login")
    return problems


def cookie_warning(cfg: Config) -> str | None:
    """Cookies z prohlížeče bez přihlášené plochy nefungují.

    Chrome má jar šifrovaný klíčem z klíčenky, a ta se odemyká až přihlášením.
    Jako služba na headless stroji tak yt-dlp cookies tiše zahodí a hraje
    128 kb/s — což se pozná až podle bitrate. Proto to řekneme rovnou.
    """
    if cfg.cookies_file or not cfg.cookies_browser or cfg.cookies_browser == "none":
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if os.path.exists(os.path.join(runtime, "keyring", "control")):
        return None
    return (
        "cookies z prohlížeče se nepodaří rozšifrovat — v téhle session neběží "
        "klíčenka.\n       Vyexportuj je do souboru a nastav cookies_file "
        "(viz README), jinak hraje 128 kb/s."
    )


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store()
        self.catalog = Catalog(cfg)
        self.player = MpvPlayer(cfg)
        self.pools = RadioPools(self.catalog, self.store, cfg)
        self.dj = CodexDJ(cfg, self.catalog, self.pools, self.player, self.store)
        # The REPL is built only in run(); prompt_toolkit touches stdin during
        # construction, which prints a pointless warning when there is no
        # terminal (--web-only mode)
        self.repl: Repl | None = None
        self.web = WebServer(self, cfg.web_host, cfg.web_port) if cfg.web_enabled else None
        self._reseeding = False
        self._last_reseed = 0.0
        self._skips_at_last_check = 0
        # A single lock for all Codex turns — REPL, web, and automatic
        # reseeding. Two concurrent turns would overwrite each other's pools.
        self._codex_lock = asyncio.Lock()
        # Restart z webu: ukončíme se, systemd nás nastartuje znovu. Je to
        # jediná cesta, jak z běžícího procesu načíst nastavení, která platí
        # až od startu (formáty, cookies, jazyk).
        self.restart_requested = asyncio.Event()

    def _set_status(self, text: str) -> None:
        """Bottom REPL status bar — there is none in web-only mode."""
        if self.repl:
            self.repl.set_status(text)

    @property
    def codex_busy(self) -> bool:
        return self._codex_lock.locked()

    async def ask(self, text: str) -> str:
        """The single entry point to Codex. Used by both the REPL and the web."""
        if reply := await self._try_link(text):
            return reply
        async with self._codex_lock:
            return await self.dj.turn(text)

    async def _try_link(self, text: str) -> str | None:
        """Odkaz na YouTube obslouží rovnou, bez modelu.

        Je to jednoznačné zadání — u odkazu není co domýšlet, a Codex by na
        něm strávil dvacet vteřin, aby došel ke stejnému závěru. Vrací None,
        když v textu odkaz není nebo se ho nepodařilo rozluštit; pak to jde
        obvyklou cestou.
        """
        match = RE_URL.search(text)
        if not match:
            return None
        try:
            target = await self.catalog.resolve_link(match.group(0))
        except Exception:
            log.exception("odkaz se nepodařilo zpracovat")
            return None
        if not target:
            return "Tenhle odkaz jsem nerozluštil — zkus název skladby nebo interpreta."

        if target.kind == "track":
            track = target.tracks[0]
            self.store.record_request(track.id, track.title, track.artist)
            self.pools.remember_tracks(target.tracks)
            self.pools.session_seen.add(track.id)
            await self.player.enqueue_next(target.tracks)
            await self.player.toggle_pause(False)
            return f"Zařazuju {track.label()}."

        # playlist i kanál: postavit z toho rádio, ať to po dohrání pokračuje
        seeds = target.tracks[:4]
        # odkaz je jednoznačné přání, takže i delší kusy, když kratší nejsou
        await self.pools.set_seeds(seeds, mood=target.label, allow_long=True)
        was_playing = (await self.player.status()).current is not None
        await self.player.clear_queue()
        if target.kind == "playlist":
            # u playlistu chce uživatel slyšet ten playlist, ne jen jeho náladu
            from_list = target.tracks[: self.cfg.queue_target]
            self.pools.remember_tracks(from_list)
            self.pools.session_seen.update(t.id for t in from_list)
            await self.player.enqueue(from_list)
        else:
            await self.player.enqueue(await self.pools.next_tracks(self.cfg.queue_target))
        await self.player.toggle_pause(False)
        if was_playing:
            await self.player.skip()
        what = "playlist" if target.kind == "playlist" else target.label
        return f"Jedu podle odkazu — {what}."

    # ---- player events ----

    async def _on_event(self, ev: PlayerEvent) -> None:
        if ev.kind == "start" and ev.track:
            self.store.record_start(
                ev.track.id, ev.track.title, ev.track.artist, self.pools.mood or None
            )
            print(f"▶ {ev.track.label()}")
            self._set_status(f"▶ {ev.track.label()}")

        elif ev.kind == "finished" and ev.track:
            self.store.record_outcome(ev.track.id, "finished")
            self.pools.mark_finished(ev.track.id)

        elif ev.kind == "skipped" and ev.track:
            self.store.record_outcome(ev.track.id, "skipped")

        elif ev.kind == "error" and ev.track:
            # unavailable (age-restricted, region-blocked, Premium-only)
            self.store.record_outcome(ev.track.id, "error")
            self.store.blacklist(ev.track.id, "nepřehratelné")
            log.info("přeskakuji nepřehratelné: %s", ev.track.label())

    # ---- queue filler ----

    async def _filler(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                await self._check_skip_burst()

                depth = self.player.queue_depth
                if depth >= self.cfg.queue_low:
                    continue
                need = self.cfg.queue_target - depth
                if need <= 0:
                    continue

                tracks = await self.pools.next_tracks(need)
                if tracks:
                    await self.player.enqueue(tracks)
                elif not self.pools.pools:
                    # Žádné pooly: došlo na vyžádanou skladbu bez rádia
                    # (odkaz, play_next). Až dohraje, bylo by ticho — tak z ní
                    # rádio postavíme. Bez modelu, hned.
                    await self._seed_from_current()
                elif self.pools.pools:
                    # the pools ran dry and the radio yields nothing new anymore
                    await self._reseed(
                        "Pooly se vyčerpaly. Zůstaň u stejné nálady, ale postav "
                        "ji na jiných interpretech než dosud."
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("plnič fronty selhal, pokračuji")

    async def _seed_from_current(self) -> None:
        """Rozjede rádio z toho, co zrovna hraje."""
        st = await self.player.status()
        if not st.current:
            return
        log.info("bez poolů — stavím rádio z %s", st.current.label())
        # Sem se dojde jen po vyžádané skladbě nebo odkazu, takže je to pořád
        # ten interpret, o kterého si posluchač řekl.
        await self.pools.set_seeds(
            [st.current], mood=st.current.artist or "", allow_long=True
        )

    async def _check_skip_burst(self) -> None:
        """Three skips within ten minutes mean the mood missed the mark."""
        skips = self.store.skip_burst(minutes=10)
        if skips < 3 or skips == self._skips_at_last_check:
            return
        self._skips_at_last_check = skips
        skipped = [
            f"{p.artist} — {p.title}"
            for p in self.store.recent_history(15)
            if p.outcome == "skipped"
        ][:6]
        await self._reseed(
            "Uživatel právě přeskočil několik skladeb po sobě: "
            + "; ".join(skipped)
            + ". Nálada se netrefila — postav ji znovu, jiným směrem. "
            "Uživateli stačí jedna věta o tom, kam jsi to posunul."
        )

    async def _reseed(self, instruction: str) -> None:
        """A model intervention triggered without the user asking for it.

        Keeps its distance so this doesn't turn into a loop that burns tokens.
        """
        now = asyncio.get_running_loop().time()
        if self._reseeding or self.codex_busy or now - self._last_reseed < 120:
            return
        self._reseeding = True
        self._last_reseed = now
        try:
            reply = await self.ask(instruction)
            if reply:
                print(f"\n{reply}")
        except Exception:
            log.exception("přeseedování selhalo")
        finally:
            self._reseeding = False

    # ---- LLM ----

    async def _on_prompt(self, text: str) -> str:
        # Codex spins up its own session, so a turn takes seconds to tens of
        # seconds. Playback isn't held up — it runs in the same loop, but
        # independently.
        self._set_status("⏳ ptám se Codexu…")
        if self.codex_busy:
            return "Codex právě pracuje (asi z webu) — zkus to za chvíli."
        try:
            return await self.ask(text)
        finally:
            st = await self.player.status()
            self._set_status(
                f"▶ {st.current.label()}" if st.current else "nic nehraje"
            )

    # ---- run ----

    async def run(self, repl: bool = True) -> int:
        self.player.on_event(self._on_event)
        await self.player.start()

        if self.web:
            try:
                await self.web.start()
            except OSError as exc:
                print(f"web se nepodařilo spustit ({exc}) — pokračuji bez něj")
                self.web = None

        auth = "přihlášen" if self.catalog.authenticated else "anonymně"
        model = self.cfg.codex_model or "výchozí"
        print(
            f"ytdj — ytmusicapi {auth}, yt-dlp cookies: {self.cfg.cookie_source()}\n"
            f"       mozek: codex ({model}), předplatné"
        )
        print(f"       web:  {self.web.url}\n" if self.web else "       web:  vypnutý\n")
        if warning := cookie_warning(self.cfg):
            print(f"POZOR: {warning}\n")

        rc = 0
        filler = asyncio.create_task(self._filler())
        try:
            if repl:
                self.repl = Repl(self.player, self._on_prompt)
                await self.repl.run()
            elif self.web:
                print("Běžím jen s webem. Ukončit: Ctrl+C\n")
                # Konec přijde buď smrtí mpv, nebo restartem z webu; signál
                # dorazí jako zrušení úlohy.
                waits = [
                    asyncio.create_task(self.player.died.wait()),
                    asyncio.create_task(self.restart_requested.wait()),
                ]
                try:
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waits:
                        task.cancel()
                if self.restart_requested.is_set():
                    print("Restart na vyžádání z webu.")
                    self.player.expect_exit()
                else:
                    print("mpv skončil — ukončuji, ať se to nastartuje načisto.")
                rc = 1
            else:
                print("Bez REPL i bez webu není co obsluhovat — končím.")
                rc = 1
        except asyncio.CancelledError:
            pass
        finally:
            filler.cancel()
            await asyncio.gather(filler, return_exceptions=True)
            # the web must go down before the store — SSE would otherwise touch
            # a closed SQLite
            if self.web:
                await self.web.stop()
            await self.player.stop()
            self.store.close()
        return rc


USAGE = """\
ytdj — AI DJ pro YouTube Music

  ytdj                terminál + web
  ytdj --web-only     jen web (bez terminálového REPL, pro běh na pozadí)
  ytdj --no-web       jen terminál
  ytdj --check-audio  co se nabízí za kvalitu a co jí případně chybí
  ytdj --help         tahle nápověda
"""


async def _amain() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    web_only = "--web-only" in argv
    no_web = "--no-web" in argv

    logging.basicConfig(
        level=os.environ.get("YTDJ_LOG", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    write_default_config()
    write_env_template()
    load_secrets()
    cfg = Config.load()
    cfg.ensure_dirs()
    if fix := cfg.fix_cookie_profile():
        print(f"POZOR: {fix}")
    if no_web:
        cfg.web_enabled = False

    if "--check-audio" in argv:
        print(await check_audio(cfg))
        return 0

    if problems := preflight(cfg):
        print("Než to půjde spustit:\n")
        for p in problems:
            print(f"  • {p}")
        return 1

    app = App(cfg)
    task = asyncio.create_task(app.run(repl=not web_only))
    # systemd stops a service with SIGTERM; without this the default handler
    # would kill us mid-flight and leave mpv and the SQLite behind unclosed.
    def terminate() -> None:
        app.player.expect_exit()
        task.cancel()

    with suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, terminate)
    try:
        return await task
    except asyncio.CancelledError:
        return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
