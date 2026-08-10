"""Vstupní bod: poskládá komponenty a rozjede tři úlohy v jedné smyčce.

  repl    čte povely od uživatele
  filler  hlídá hloubku fronty a dolévá z poolů
  mpv     posílá události (start / dohráno / přeskočeno)

Jedna asyncio smyčka, žádné zámky, žádné předávání mezi vlákny.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys

from .agent import CodexDJ
from .config import Config, load_secrets, write_default_config
from .music import Catalog, RadioPools
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


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store()
        self.catalog = Catalog(cfg)
        self.player = MpvPlayer(cfg)
        self.pools = RadioPools(self.catalog, self.store, cfg)
        self.dj = CodexDJ(cfg, self.catalog, self.pools, self.player, self.store)
        # REPL se staví až v run(); prompt_toolkit si při konstrukci sáhne na
        # stdin, což bez terminálu (režim --web-only) vypíše zbytečné varování
        self.repl: Repl | None = None
        self.web = WebServer(self, cfg.web_host, cfg.web_port) if cfg.web_enabled else None
        self._reseeding = False
        self._last_reseed = 0.0
        self._skips_at_last_check = 0
        # Jediný zámek pro všechny tahy Codexu — REPL, web i automatické
        # přeseedování. Dva souběžné tahy by si navzájem přepsaly pooly.
        self._codex_lock = asyncio.Lock()

    def _set_status(self, text: str) -> None:
        """Spodní lišta REPL — v režimu jen s webem žádná není."""
        if self.repl:
            self.repl.set_status(text)

    @property
    def codex_busy(self) -> bool:
        return self._codex_lock.locked()

    async def ask(self, text: str) -> str:
        """Jediný vstup k Codexu. Používá ho REPL i web."""
        async with self._codex_lock:
            return await self.dj.turn(text)

    # ---- události přehrávače ----

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
            # nedostupné (věkově omezené, regionálně blokované, jen pro Premium)
            self.store.record_outcome(ev.track.id, "error")
            self.store.blacklist(ev.track.id, "nepřehratelné")
            log.info("přeskakuji nepřehratelné: %s", ev.track.label())

    # ---- plnič fronty ----

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
                elif self.pools.pools:
                    # pooly došly a rádio už nic nového nedává
                    await self._reseed(
                        "Pooly se vyčerpaly. Zůstaň u stejné nálady, ale postav "
                        "ji na jiných interpretech než dosud."
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("plnič fronty selhal, pokračuji")

    async def _check_skip_burst(self) -> None:
        """Tři skipy za deset minut znamenají, že se nálada netrefila."""
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
        """Vyžádaný zásah modelu bez toho, aby o něj uživatel žádal.

        Drží se odstup, ať se z toho nestane smyčka, která pálí tokeny.
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
        # Codex startuje vlastní session, takže tah trvá jednotky až desítky
        # sekund. Přehrávání to nebrzdí — běží ve stejné smyčce, ale nezávisle.
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

    # ---- běh ----

    async def run(self, repl: bool = True) -> None:
        self.player.on_event(self._on_event)
        await self.player.start()

        if self.web:
            try:
                await self.web.start()
            except OSError as exc:
                print(f"web se nepodařilo spustit ({exc}) — pokračuji bez něj")
                self.web = None

        auth = "přihlášen" if self.catalog.authenticated else "anonymně"
        cookies = self.cfg.cookies_browser or "bez cookies"
        model = self.cfg.codex_model or "výchozí"
        print(
            f"ytdj — ytmusicapi {auth}, yt-dlp cookies: {cookies}\n"
            f"       mozek: codex ({model}), předplatné"
        )
        print(f"       web:  {self.web.url}\n" if self.web else "       web:  vypnutý\n")

        filler = asyncio.create_task(self._filler())
        try:
            if repl:
                self.repl = Repl(self.player, self._on_prompt)
                await self.repl.run()
            elif self.web:
                print("Běžím jen s webem. Ukončit: Ctrl+C\n")
                await asyncio.Event().wait()  # dokud nepřijde signál
            else:
                print("Bez REPL i bez webu není co obsluhovat — končím.")
        except asyncio.CancelledError:
            pass
        finally:
            filler.cancel()
            await asyncio.gather(filler, return_exceptions=True)
            # web musí dolů dřív než store — SSE by jinak sáhlo na zavřenou SQLite
            if self.web:
                await self.web.stop()
            await self.player.stop()
            self.store.close()


USAGE = """\
ytdj — AI DJ pro YouTube Music

  ytdj              terminál + web
  ytdj --web-only   jen web (bez terminálového REPL, pro běh na pozadí)
  ytdj --no-web     jen terminál
  ytdj --help       tahle nápověda
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
    load_secrets()
    cfg = Config.load()
    cfg.ensure_dirs()
    if no_web:
        cfg.web_enabled = False

    if problems := preflight(cfg):
        print("Než to půjde spustit:\n")
        for p in problems:
            print(f"  • {p}")
        return 1

    app = App(cfg)
    await app.run(repl=not web_only)
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
