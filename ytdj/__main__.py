"""Entry point: wires the components together and runs three tasks in one loop.

  repl    reads commands from the user
  filler  watches queue depth and tops it up from the pools
  mpv     sends events (start / finished / skipped)

One asyncio loop, no locks, no cross-thread handoffs.
"""

from __future__ import annotations

import os
import time


def _proc_age_s() -> float | None:
    """Jak dlouho už běží tenhle proces (od exec run.sh), podle /proc."""
    try:
        with open("/proc/self/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])  # pole 22: starttime
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        return up - ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


class StartClock:
    """Časy fází startu od spuštění procesu, pro událost `app.start`.

    Po restartu služby je ticho od konce starého mpv po první zvuk; nový proces
    startuje hned po konci starého, takže `sound = track.start − proc_start`.
    """

    def __init__(self) -> None:
        age = _proc_age_s()
        now = time.monotonic()
        # execv (restart z webu mimo systemd) si nechává PID i čas startu
        self.exact = age is not None and 0 <= age < 120
        self.t0 = now - (age if self.exact else 0.0)
        self.wall0 = time.time() - (now - self.t0)
        self.marks: dict[str, int] = {}

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, round((time.monotonic() - self.t0) * 1000))

    def fields(self) -> dict:
        return {**{f"{k}_ms": v for k, v in self.marks.items()},
                "proc_start": round(self.wall0, 3), "exact": self.exact}


CLOCK = StartClock()
CLOCK.mark("py")  # interpret + site, do prvního řádku ytdj

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import importlib  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
from contextlib import suppress  # noqa: E402

# Jen to, co potřebuje přehrávač: mpv a resolver startují dřív, než se načte
# zbytek aplikace (DJ, fronta přání, web, ytmusicapi) — ten se importuje ve
# vlákně souběžně se startem mpv (F-RESTART-08, _import_app).
from . import telemetry  # noqa: E402
from .config import (  # noqa: E402
    DATA_DIR, Config, load_secrets, write_default_config, write_env_template,
)
from .diagnose import check_audio, yt_dlp_warning  # noqa: E402
from .loopwatch import LoopWatch  # noqa: E402
from .player import MpvPlayer  # noqa: E402
from .player.base import PlayerEvent, queue_transaction  # noqa: E402

CLOCK.mark("imports")

# Zbytek aplikace; na Pi ~0,2 s importu (bez webu, REPL a ytmusicapi).
APP_MODULES = ("ytdj.state", "ytdj.music", "ytdj.agent", "ytdj.wishes", "ytdj.votes")
WEB_MODULE = "ytdj.web.server"
REPL_MODULE = "ytdj.ui.repl"


def _import_app() -> None:
    for name in APP_MODULES:
        importlib.import_module(name)


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
    def __init__(self, cfg: Config, player: MpvPlayer | None = None,
                 player_start: asyncio.Future | None = None,
                 clock: StartClock | None = None) -> None:
        from .agent import CodexDJ
        from .agent.intent import SkipWatch
        from .music import Catalog, RadioPools
        from .state import Store
        from .votes import wire as wire_votes
        from .wishes import WishQueue

        self.cfg = cfg
        # zápisy ve vlastním vlákně: event loop nikdy nečeká na SD kartu
        self.store = Store(background=True)
        self.catalog = Catalog(cfg)
        # Přehrávač může startovat už před sestavením aplikace (start_app):
        # player_start je jeho běžící start(), run() na něj jen počká.
        self.player = player if player is not None else MpvPlayer(cfg)
        self._player_start = player_start
        self.clock = clock
        # když Codex přemýšlí, resolver nic nechystá dopředu — oba naráz se do RAM nevejdou
        # …a ani když DJ rozhoduje o přání: resolver pak bude hned volný pro
        # jeho první skladbu (yt-dlp běží po jednom a nedá se přerušit)
        self.player.busy_check = lambda: self.codex_busy or self.wishes.busy
        self.pools = RadioPools(self.catalog, self.store, cfg)
        self.dj = CodexDJ(cfg, self.catalog, self.pools, self.player, self.store)
        # The REPL is built only in run(); prompt_toolkit touches stdin during
        # construction, which prints a pointless warning when there is no
        # terminal (--web-only mode)
        self.repl = None
        # Web (starlette/uvicorn, ~0,4 s importu na Pi) se sestaví až v run(),
        # po rozjezdu navázané skladby; do té doby je None (_poke_web nic nedělá).
        self.web = None
        self._reseeding = False
        self._reseed_task: asyncio.Task | None = None
        self._last_reseed = float("-inf")
        # jen přeskočení od posledního tahu DJ, v paměti — viz SkipWatch
        self.skips = SkipWatch()
        # A single lock for all Codex turns — REPL, web, and automatic
        # reseeding. Two concurrent turns would overwrite each other's pools.
        self._codex_lock = asyncio.Lock()
        # Restart z webu: ukončíme se, systemd nás nastartuje znovu. Je to
        # jediná cesta, jak z běžícího procesu načíst nastavení, která platí
        # až od startu (formáty, cookies, jazyk).
        self.restart_requested = asyncio.Event()
        # ---- fronta přání pro víc lidí (fáze 2, ytdj/wishes.py) ----
        # Přání z webu, displeje i terminálu; DJ je vyřizuje jedno po druhém a
        # skladby střídá spravedlivě mezi lidmi. Podkres (pooly) jen za nimi.
        self.wishes = WishQueue(
            self.dj, self.player, self.pools, self.store, cfg,
            lock=self._codex_lock,
            state_file=DATA_DIR / "session.json",
            on_change=self._poke_web,
            on_listener=self._listener_spoke,
            catalog=self.catalog,
        )
        # ---- hlasování kanceláře (PLAN H, ytdj/votes.py) ----
        # 👍/👎 skladbám a 👎 interpretům; podkres a DJ je respektují, výslovné
        # přání se splní vždy. Hlasy se načtou v run() mimo event loop.
        self.votes = wire_votes(self)
        self._start_task: asyncio.Task | None = None

    def _poke_web(self) -> None:
        if self.web:
            self.web.poke()

    async def _listener_spoke(self) -> None:
        """Přání posluchače má přednost před automatickým přeseedováním."""
        self.skips.turn(self._now(), by_user=True)
        if self._start_task and not self._start_task.done():
            # Rozjezd (▶ v tichu) drží zámek Codexu — přání posluchače má
            # přednost; hudbu pak určí to přání.
            log.info("ruším rozjezd kvůli přání posluchače")
            telemetry.event("dj.start", phase="cancelled_by_user")
            self._start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._start_task
        if self._reseed_task and not self._reseed_task.done():
            log.info("ruším automatické přeseedování kvůli požadavku posluchače")
            telemetry.event("dj.reseed", phase="cancelled_by_user")
            self._reseed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reseed_task

    async def play_or_start(self, source: str = "web") -> str:
        """▶ na webu / Hrát na displeji / povel play.

        Něco hraje nebo čeká → jen odpauzovat. Nic nehraje ani nečeká →
        chytrý rozjezd (C7) podle času, dne, kanceláře a historie — jako
        tah DJe, ne jako přání posluchače. Vrací "play" | "starting".
        """
        st = await self.player.status()
        if st.current is not None or st.queue or self.wishes.has_requests():
            await self.player.toggle_pause(False)
            return "play"
        if self.wishes.starting or (self._start_task and not self._start_task.done()):
            return "starting"
        telemetry.event("dj.start", source=source)
        self._start_task = asyncio.create_task(self._start_idle(), name="ytdj-start")
        return "starting"

    async def _start_idle(self) -> None:
        try:
            reply = await self.wishes.start_idle()
            if reply:
                print(f"\n{reply}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rozjezd selhal")
            # ať to vidí web i displej, ne jen log
            self.wishes.note_last(text="▶ rozjezd podle času a dne", source="start", ok=False,
                                  reply="Rozjezd se nepovedl — napiš DJovi, co chceš slyšet.")
            self._poke_web()

    def _set_status(self, text: str) -> None:
        """Bottom REPL status bar — there is none in web-only mode."""
        if self.repl:
            self.repl.set_status(text)

    @property
    def codex_busy(self) -> bool:
        return self._codex_lock.locked()

    async def ask(
        self, text: str, interrupt: bool = True, source: str = "web", requester: str = ""
    ) -> str:
        """Přání posluchače (terminál, starší volající) nebo tah aplikace.

        Posluchač jde frontou přání (ytdj/wishes.py): počká se, až ho DJ
        vyřídí, a vrátí se odpověď. `interrupt=False` = zásah, o který nikdo
        nežádal (přeseedování) — sahá jen na podkres, přání nechá být.
        """
        if not interrupt:
            telemetry.event("dj.request", source="auto-reseed", text=text[:300])
            try:
                reply = await self.wishes.background_turn(text)
            finally:
                self.skips.turn(self._now(), by_user=False)
            telemetry.event("dj.reply", source="auto-reseed", text=(reply or "")[:300])
            return reply
        if (reply := await self.wishes.try_local(text)) is not None:
            return reply
        w = self.wishes.submit(text, requester, source)
        await self.wishes.wait(w)
        telemetry.event("dj.reply", source=source, requester=w.who, id=w.id,
                        text=(w.reply or "")[:300])
        return w.reply

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    # ---- player events ----

    async def _on_event(self, ev: PlayerEvent) -> None:
        if ev.kind == "start" and ev.track:
            self.store.record_start(
                ev.track.id, ev.track.title, ev.track.artist, self.pools.mood or None
            )
            self.dj.note_started(ev.track.id)
            print(f"▶ {ev.track.label()}")
            self._set_status(f"▶ {ev.track.label()}")

        elif ev.kind == "finished" and ev.track:
            self.store.record_outcome(ev.track.id, "finished")
            self.pools.mark_finished(ev.track.id)

        elif ev.kind == "skipped" and ev.track:
            self.store.record_outcome(ev.track.id, "skipped")
            # Přeskočené přání někoho jiného není výtka podkresu — do série
            # přeskočení (a automatického přeseedování) se počítá jen podkres.
            if not self.wishes.is_request_track(ev.track.id):
                self.skips.skipped(ev.track.label(), self._now())

        elif ev.kind == "replaced" and ev.track:
            # Odsunula ji nová nálada nebo vyžádaný odkaz, ne posluchač. Jako
            # "skipped" by se počítala do série přeskočení a ta spouští další
            # přeseedování, které zase odsune skladbu — smyčka, kdy DJ každé
            # dvě minuty sám mění náladu a nic nedohraje.
            self.store.record_outcome(ev.track.id, "replaced")

        elif ev.kind == "error" and ev.track:
            # [player/outage] Na černou listinu jen vlastnost videa (soukromé,
            # smazané, věk, region) — a na čas; natrvalo jen smazané. Chyby
            # z výpadku sítě/YouTube chodí jako "unavailable" a sem nepatří.
            self.store.record_outcome(ev.track.id, "error")
            cls = (ev.detail or "").split("|", 1)[0]
            if cls in ("content", "removed"):
                self.store.blacklist(ev.track.id, f"nepřehratelné ({cls})",
                                     days=None if cls == "removed" else 7)
                log.info("přeskakuji nepřehratelné (%s): %s", cls, ev.track.label())

    # ---- queue filler ----

    async def _filler_after(self, resume: asyncio.Task) -> None:
        """Plnič až po obnově stavu (nejdéle 15 s): dřív ho od navázání
        oddělovala sonda yt-dlp (~2 s na Pi), teď by jinak stavěl rádio
        z navázané skladby dřív, než obnova vrátí přání a podkres."""
        await asyncio.wait({resume}, timeout=15)
        await self._filler()

    async def _filler(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                await self._check_skip_burst()
                if self.wishes.needs_top_up():
                    # přání se zhmotňují po blocích — další, když ubývají
                    self.wishes.kick()

                # Aspoň tolik, kolik přehrávač chystá dopředu (+1): jinak by
                # klouzavé okno připravených skladeb bylo kratší, než je třeba
                # na sérii rychlých "Další".
                ahead = getattr(self.player, "prefetch_depth", 0) or 0
                low = max(self.cfg.queue_low, ahead)
                target = max(self.cfg.queue_target, ahead + 1 if ahead else 0)
                depth = self.player.queue_depth
                if depth >= low:
                    continue
                need = target - depth
                if need <= 0:
                    continue

                # v režimu interpreta od něj, jinak z poolů
                gen = getattr(self.player, "generation", 0)
                tracks = await self.dj.next_tracks(need)
                if tracks:
                    async with queue_transaction(self.player):
                        # Mezitím tah DJe vyčistil frontu a naplnil ji podle
                        # nového přání — tyhle skladby patří ke staré náladě
                        # a přidat je by znamenalo vmísit je do nové fronty.
                        if getattr(self.player, "generation", 0) != gen:
                            self.pools.give_back(tracks)
                            continue
                        room = max(0, target - self.player.queue_depth)
                        self.pools.give_back(tracks[room:])  # nevešly se
                        await self.player.enqueue(tracks[:room])
                elif not self.pools.pools:
                    # Žádné pooly: došlo na vyžádanou skladbu bez rádia
                    # (odkaz, play_next). Až dohraje, bylo by ticho — tak z ní
                    # rádio postavíme. Bez modelu, hned. Ne ale, dokud na
                    # zpracování čeká další přání — to podkres určí samo
                    # (25. 9.: z "pusť Kabát" se stavělo rádio z Malé dámy,
                    # zatímco Jana a Karel teprve čekali).
                    if self.wishes.can_seed_background():
                        await self._seed_from_current()
                elif self.pools.pools and not self.dj.focus and not self.pools.retrying():
                    # (při výpadku rádia nevolat Codex každé 2 minuty — pooly to zkusí samy)
                    # (v režimu interpreta pooly jedou dokola samy; prázdná
                    # dávka znamená jen, že všechno už čeká ve frontě)
                    # the pools ran dry and the radio yields nothing new anymore
                    await self._reseed(
                        "Pooly se vyčerpaly. Zůstaň u stejné nálady a u "
                        "posledního výslovného přání posluchače, ale postav "
                        "ji na jiných skladbách než dosud."
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
        """Three skips since the last DJ turn mean the picks missed the mark.

        Nový směr ale jen uvnitř toho, co posluchač chtěl: 25. 9. na "něco
        pohodového, akustického" a tři přeskočení DJ sám přešel na "energickou
        taneční" hudbu — pravý opak zadání. A v režimu interpreta se nic
        nemění vůbec: přeskočená skladba Stypky neznamená "už ne Stypku".
        """
        if self.dj.focus:
            return
        skipped = self.skips.due(self._now())
        if not skipped:
            return
        wish = self.dj.wish.describe()
        scope = (
            f"Posluchač naposledy výslovně chtěl: {wish}. Drž se toho — změň "
            "interprety a skladby, ne žánr, jazyk ani interpreta, o které si řekl."
            if wish else
            "Zkus jiný směr."
        )
        await self._reseed(
            "Posluchač přeskočil několik skladeb po sobě: "
            + "; ".join(skipped)
            + ". Výběr se netrefil. " + scope
            + " Přeskočení nejsou vyjádření vkusu — `remember` nech prázdné. "
            "Posluchači stačí jedna věta o tom, kam jsi to posunul."
        )

    async def _reseed(self, instruction: str) -> None:
        """A model intervention triggered without the user asking for it.

        Keeps its distance so this doesn't turn into a loop that burns tokens.
        """
        now = self._now()
        if self._reseeding or self.codex_busy or now - self._last_reseed < 120:
            return
        if self.wishes.has_requests() or self.wishes.starting:
            # podkres se mění, jen když nikdo nečeká na své přání (D6)
            return
        log.info("automatický tah: %s", instruction[:200])
        telemetry.event("dj.reseed", phase="start", trigger=instruction[:300])
        self._reseeding = True
        self._last_reseed = now
        # Vlastní úloha: zrušit se smí jen tenhle tah, ne ten, kdo ho spustil
        # (třeba zpracování událostí přehrávače).
        self._reseed_task = asyncio.create_task(self.ask(instruction, interrupt=False))
        try:
            reply = await self._reseed_task
            if reply:
                print(f"\n{reply}")
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # ruší se nás samotné (vypínání), ne jen tah
            # jinak přednost dostal požadavek posluchače
        except Exception:
            log.exception("přeseedování selhalo")
        finally:
            self._reseeding = False
            self._reseed_task = None

    # ---- LLM ----

    async def _on_prompt(self, text: str) -> str:
        # Codex spins up its own session, so a turn takes seconds to tens of
        # seconds. Playback isn't held up — it runs in the same loop, but
        # independently.
        self._set_status("⏳ DJ vybírá…")
        try:
            # i z terminálu je to přání ve frontě — nic se neodmítá
            return await self.ask(text, source="repl", requester="terminál")
        finally:
            st = await self.player.status()
            self._set_status(
                f"▶ {st.current.label()}" if st.current else "nic nehraje"
            )

    # ---- run ----

    def _mark(self, name: str) -> None:
        if self.clock is not None:
            self.clock.mark(name)

    async def _after_resume(self, resume: asyncio.Task) -> None:
        """Co při startu nespěchá, až po navázání skladby (nejdéle 15 s), ať
        nebere CPU mpv a resolveru, zatímco otevírají první skladbu: sonda
        yt-dlp (vlastní proces s importem yt-dlp, ~2 s CPU na Pi) a YTMusic
        (import ~0,6 s)."""
        await asyncio.wait({resume}, timeout=15)
        if warning := await yt_dlp_warning(self.cfg):
            print(f"POZOR: {warning}\n")
        warm = getattr(self.catalog, "warm", None)
        if warm is not None:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(warm)
                log.info("katalog připraven za %d ms", (time.monotonic() - t0) * 1000)
            except Exception:
                log.warning("katalog (ytmusicapi) se nepodařilo připravit", exc_info=True)
        # [latency] v pracovní době Codex nahřát hned po startu, ne až s prvním
        # přáním (Pi 26. 9.: první přání po restartu čekalo 8,8–12,4 s)
        warm_dj = getattr(self.dj, "warm_ahead", None)
        if warm_dj is not None:
            try:
                warm_dj("start")
            except Exception:
                log.warning("nahřátí Codexu po startu selhalo", exc_info=True)


    async def run(self, repl: bool = True) -> int:
        self.player.on_event(self._on_event)
        self.player.on_event(self.wishes.on_event)
        # chytré Další přeskakuje jen podkres, přání nikdy
        if hasattr(self.player, "is_protected"):
            self.player.is_protected = self.wishes.is_request_track
        from .votes import aload_quietly

        await aload_quietly(self.votes)  # state.db ve vlákně, ne v event loopu
        if self._player_start is not None:
            await self._player_start  # start() běží od _amain, souběžně s importy
        else:
            await self.player.start()
        self._mark("player")
        self.wishes.start()
        # hlídač zaseknutého event loopu (sys.loop_lag se zásobníkem)
        self.loopwatch = LoopWatch()
        self.loopwatch.start()
        # Po restartu služby navázat (jen čerstvý stav, ne v noci — viz
        # wishes.should_resume); na pozadí, ať web naběhne hned.
        resume = asyncio.create_task(self.wishes.resume(), name="ytdj-resume")
        self._mark("resume")

        if self.cfg.web_enabled:
            try:
                # import webu ve vlákně: event loop mezitím rozjíždí navázanou skladbu
                await asyncio.to_thread(importlib.import_module, WEB_MODULE)
                from .web import WebServer

                self.web = WebServer(self, self.cfg.web_host, self.cfg.web_port)
                await self.web.start()
            except OSError as exc:
                print(f"web se nepodařilo spustit ({exc}) — pokračuji bez něj")
                self.web = None
        self._mark("web")
        telemetry.event("app.start", **(self.clock.fields() if self.clock else {}),
                        web=self.web is not None, repl=repl)
        # sonda yt-dlp a ytmusicapi až po navázání; kdo katalog potřebuje dřív,
        # vytvoří si YTMusic sám (LazyYT)
        later = asyncio.create_task(self._after_resume(resume), name="ytdj-after-resume")

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
        filler = asyncio.create_task(self._filler_after(resume))
        try:
            if repl:
                await asyncio.to_thread(importlib.import_module, REPL_MODULE)
                from .ui import Repl

                self.repl = Repl(self.player, self._on_prompt)
                # Restart z webu musí umět ukončit i REPL, jinak by tlačítko
                # fungovalo jen v režimu --web-only.
                repl_task = asyncio.create_task(self.repl.run())
                restart_task = asyncio.create_task(self.restart_requested.wait())
                await asyncio.wait(
                    {repl_task, restart_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if self.restart_requested.is_set():
                    print("Restart na vyžádání z webu.")
                    self.player.expect_exit()
                repl_task.cancel()
                restart_task.cancel()
                await asyncio.gather(repl_task, restart_task, return_exceptions=True)
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
            resume.cancel()
            later.cancel()
            await asyncio.gather(filler, resume, later, return_exceptions=True)
            # co hrálo a kdo na co čeká — pro navázání po restartu
            if not self.player.died.is_set():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.wishes.refresh_playing(), 2)
            await self.wishes.stop()
            await self.loopwatch.stop()
            # the web must go down before the store — SSE would otherwise touch
            # a closed SQLite
            if self.web:
                await self.web.stop()
            await self.dj.close()  # [app-server] trvale běžící Codex
            await self.player.stop()  # playback.json: co hraje a kde (naposledy)
            # Přání až po přehrávači: session.json pak odpovídá playback.json —
            # skladba, která mezitím dohrála, je v přání hotová a po restartu
            # nezazní znovu (dřív se přání ukládala o celé ukončení dřív).
            self.wishes.save()
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


async def start_app(cfg: Config, clock: StartClock | None = None,
                    import_app=_import_app) -> App:
    """Přehrávač první, zbytek aplikace souběžně (F-RESTART-08).

    mpv (~1,4 s na Pi) a resolver se spustí hned po načtení konfigurace; DJ,
    fronta přání a hlasování se mezitím importují ve vlákně. Navázání
    přerušené skladby (App.run → wishes.resume) tak nečeká na import celé
    aplikace — dřív startoval přehrávač až ~2,7 s po spuštění procesu.
    Obsluhy událostí se zaregistrují v App.run; do té doby v mpv nic nehraje.
    """
    player = MpvPlayer(cfg)
    started = asyncio.create_task(player.start(), name="ytdj-player-start")
    if clock is not None:
        clock.mark("player_call")
        started.add_done_callback(lambda _t: clock.mark("player"))
    await asyncio.sleep(0)  # start() se rozběhne (spustí mpv) dřív než importy
    try:
        await asyncio.to_thread(import_app)
        if clock is not None:
            clock.mark("app_imports")
        app = App(cfg, player=player, player_start=started, clock=clock)
    except BaseException:
        started.cancel()
        with suppress(BaseException):
            await started
        with suppress(Exception):
            await player.stop()
        raise
    if clock is not None:
        clock.mark("app")
    return app


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

    CLOCK.mark("config")
    app = await start_app(cfg, CLOCK)
    task = asyncio.create_task(app.run(repl=not web_only))
    # systemd stops a service with SIGTERM; without this the default handler
    # would kill us mid-flight and leave mpv and the SQLite behind unclosed.
    def terminate() -> None:
        app.player.expect_exit()
        task.cancel()

    with suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, terminate)
    try:
        rc = await task
    except asyncio.CancelledError:
        return 0
    if app.restart_requested.is_set() and not os.environ.get("INVOCATION_ID"):
        # Pod systemd stačí skončit a o nové spuštění se postará on. Bez něj
        # (běh z terminálu) se proces vymění sám: úklid už proběhl v App.run,
        # takže execv jen nahradí obraz procesu čerstvým startem.
        os.execv(sys.executable, [sys.executable, "-u", "-m", "ytdj", *argv])
    return rc


def main() -> None:
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
