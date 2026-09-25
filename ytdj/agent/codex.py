"""DJ powered by Codex via a subscription (not via an API key).

A ChatGPT/Codex subscription cannot be called like a regular API — auth is
OAuth tokens in ~/.codex/auth.json, so the only way in is the CLI and its
non-interactive mode `codex exec`.

We do NOT give it tools. The original attempt to expose them as an MCP server
on localhost did work, but in headless mode Codex cancels every tool call
("user cancelled MCP tool call") and the only thing that opens that gate is
`--dangerously-bypass-approvals-and-sandbox` — which also removes the sandbox
around the shell. Given that YouTube track titles (i.e. third-party input)
flow back into Codex's context, trading the sandbox for convenience makes no
sense.

Instead we get a structured JSON decision out of it via `--output-schema`
— which tracks to use as seeds — and do the search and radio ourselves.
Side effect: one pass instead of seven, so it is significantly faster.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR, Config
from ..music.catalog import Catalog, Track
from ..music.radio import RadioPools
from ..player.base import Player, queue_transaction
from ..state import Store
from .. import telemetry
from .appserver import (
    AppServer,
    AppServerFatal,
    app_server_enabled,
    default_binary,
    feature_args,
)
from .offline import (
    CALMER,
    CZECH,
    ERROR,
    LIMIT,
    LIVELIER,
    LOCAL_REPLY,
    LOGIN,
    MOOD_WORDS,
    MORE,
    OTHER,
    SURPRISE,
    Breaker,
    CodexOffline,
    classify,
    local_mood,
    looks_czech,
)
from .fastpath import FastResult, enforce_requested, find_artists, find_song
from .intent import Intent, ListenerIntent, Pair, build_intent, track_avoided
from .prompts import ROLE, render_state

log = logging.getLogger(__name__)

# Jeden tah trvá běžně do půl minuty. Bez stropu by vadná konfigurace
# (typicky model, na který je nainstalovaný Codex moc starý) držela zámek
# Codexu donekonečna a DJ by přestal reagovat úplně — hudba sice hraje dál,
# ale každý další dotaz dostane jen "Codex právě pracuje".
TURN_TIMEOUT = 240  # s
# Rozpočet celého tahu (app-server i případný exec) — pak jistič a DJ bez
# modelu. Posluchač nemá čekat minuty: 429 s opakováním držel tah až 240 s.
LISTENER_BUDGET = 25.0  # s
AUTO_BUDGET = 90.0  # s — přeseedování, o které nikdo nežádal, smí déle

# Response schema. Structured outputs require every property to be listed
# in `required` — unused ones are sent empty.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "start_radio",
                "play_next",
                "skip",
                "stop",
                "pause",
                "resume",
                "volume",
                "nothing",
            ],
        },
        "seeds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["artist", "title"],
                "additionalProperties": False,
            },
        },
        # Tracks the listener named out loud. Unlike seeds they get played,
        # and the "don't repeat for N days" rule does not apply to them.
        "requested": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    # prázdné = "cokoli od tohohle interpreta"; lepší než
                    # vymyšlený název, který trefí cizí kapelu
                    "title": {"type": "string"},
                },
                "required": ["artist", "title"],
                "additionalProperties": False,
            },
        },
        # Režim interpreta: "hraj X" = jen X, dokud posluchač neřekne jinak.
        "focus_artists": {"type": "array", "items": {"type": "string"}},
        # true = až po hrající skladbě; jinak vyžádané začne hned
        "after_current": {"type": "boolean"},
        # co posluchač výslovně nechce ("Kabát, ale ne Pohodu")
        "avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["artist", "title"],
                "additionalProperties": False,
            },
        },
        "mood": {"type": "string"},
        "volume": {"type": "integer"},
        "remember": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": [
        "action",
        "seeds",
        "requested",
        "focus_artists",
        "after_current",
        "avoid",
        "mood",
        "volume",
        "remember",
        "reply",
    ],
    "additionalProperties": False,
}


# Jak dlouho nechat hrát starou skladbu, než se nová vyřeší (pak se utne tak jako tak).
FIRST_TRACK_WAIT = 15.0  # s


class CodexUnavailable(RuntimeError):
    pass


def _stream_error(ev: dict) -> tuple[str, bool]:
    """(lidská zpráva, je definitivní?) z eventu `error` / `turn.failed`.

    Zpráva chodí jako JSON zabalený do JSONu — vybalit. Definitivní jsou
    chyby 4xx (kromě 429): server je vrací pořád stejně, takže čekat na
    interní retry Codexu znamená jen držet frontu o minuty déle.
    """
    payload = ev.get("message") or (ev.get("error") or {}).get("message")
    text = str(payload or "").strip()
    fatal = False
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        status = data.get("status")
        fatal = isinstance(status, int) and 400 <= status < 500 and status != 429
        inner = data.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            text = str(inner["message"])
        elif data.get("message"):
            text = str(data["message"])
    if "newer version of codex" in text.lower():
        text += " → npm install -g @openai/codex@latest"
    return text, fatal


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Zabije codex i s potomky.

    `codex` je node wrapper, který spouští vlastní binárku jako potomka se
    zděděnými rourami. proc.kill() by zabil jen wrapper: binárka žije dál,
    roury se nezavřou a proc.wait() by nikdy neskončil. Proto celá procesní
    skupina — proces se spouští se start_new_session=True, takže je jeho pgid
    naše, a nikoho cizího tím netrefíme.
    """
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _reap(proc: asyncio.subprocess.Process) -> None:  # [latency]
    """Nechá Codex po dokončeném tahu doběhnout; kdyby visel, zabije ho."""
    try:
        async with asyncio.timeout(60):
            await proc.wait()
    except (TimeoutError, Exception):
        _kill_tree(proc)


@dataclass
class Plan:
    """Vyložené přání převedené na konkrétní skladby — ještě nic nehraje.

    requested  vyžádané skladby (hrají první, bez pravidla o neopakování)
    artist_tracks  skladby vyžádaných interpretů, prostřídané (kind == "artist")
    seeds      z čeho postavit rádio (nálada, skladba)
    notes      pravdivé doplňky odpovědi ("Nenašel jsem: …")
    failed     proč se nedá nic zahrát; pak se přehrávání nemění
    """

    intent: Intent
    requested: list[Track] = field(default_factory=list)
    artist_tracks: list[Track] = field(default_factory=list)
    seeds: list[Track] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failed: str = ""


def parse_output(raw: str) -> dict:
    """Výstup `codex exec -o` → slovník podle DECISION_SCHEMA."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("rozhodnutí není objekt")
    return data


class CodexDJ:
    def __init__(
        self,
        cfg: Config,
        catalog: Catalog,
        pools: RadioPools,
        player: Player,
        store: Store,
    ) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.pools = pools
        self.player = player
        self.store = store
        self.thread_id: str | None = None
        self.codex = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
        # [app-server] trvale běžící Codex (viz appserver.py); None = jen exec
        self.app: AppServer | None = None
        self._binary: str | None = None  # dohledá se jednou, mimo smyčku
        # jistič: když mozek nejede, nezkoušet ho u každého přání (offline.py)
        self.breaker = Breaker()

        # Codex is primarily a coding agent. Running it in the project
        # directory would mean it starts digging through the sources; it gets
        # an empty directory instead. It must be PERSISTENT: Codex writes a
        # `[projects."..."] trust_level` entry to ~/.codex/config.toml for
        # every working directory, so a fresh temp directory on each run would
        # bloat the user's config indefinitely.
        self._dir = DATA_DIR / "codex-workdir"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._schema = self._dir / "schema.json"
        self._schema.write_text(json.dumps(DECISION_SCHEMA))
        self._out = self._dir / "decision.json"

        # Režim interpreta ("hraj X" = jen X, dokud neřekne jinak) drží pooly
        # (RadioPools.set_artist); tady jen jména, jak je posluchač řekl.
        self._focus_artists: list[str] = []
        # Vyžádané skladby, které čekají ve frontě a ještě nezačaly. Automatický
        # tah (přeseedování) je nesmí smést — 25. 9. tak zmizel vyžádaný Stypka.
        self.pending: dict[str, Track] = {}
        # co posluchač výslovně nechce — platí do dalšího jeho pokynu
        self.avoid: list[Pair] = []
        # Co posluchač naposledy výslovně chtěl — přežije i restart.
        self._wish_file = DATA_DIR / "dj-intent.json"
        self.wish = ListenerIntent.load(self._wish_file)
        # čekání na připravenost první nové skladby, než se utne stará
        self._switch_task: asyncio.Task | None = None

    # ---- calling Codex ----

    async def _build_prompt(self, user_input: str) -> str:
        st = await self.player.status()
        # [loop] čtení ze state.db a taste.md mimo event loop (SD karta pod zátěží)
        recent, taste, top = await asyncio.to_thread(
            lambda: (self.store.recent_history(25), self.store.taste(), self.store.top_requested(10))
        )
        state = render_state(
            now_playing=st.current.label() if st.current else "",
            queue=[t.label() for t in st.queue],
            pools=self.pools.describe(),
            history=[f"{p.artist} — {p.title} [{p.outcome}]" for p in recent],
            taste=taste,
            requested=[r.label() for r in top],
            intent=self.wish.describe(),
            focus=self.focus,
        )
        return f"{ROLE}\n\n{state}\n\nUživatel říká: {user_input}"

    # [latency] Obnovená session narůstá (na Pi 15k → 29k vstupních tokenů za
    # 5 tahů, model 7.7 → 12.3 s); stav se posílá celý v každém zadání, takže
    # stačí session po pár tazích začít znovu.
    MAX_RESUMED_TURNS = 3

    def _args(self, resume: bool) -> list[str]:
        args = [self.codex, "exec"]
        if resume and self.thread_id:
            # `resume` accepts neither -C nor -s; both are inherited from the
            # original session, so they must be omitted, not just have their
            # exit code ignored
            args += ["resume", self.thread_id]
        else:
            args += [
                "-C", str(self._dir),
                "-s", "read-only",  # the sandbox stays; Codex has nothing to run
            ]
        args += feature_args()  # [app-server] bez pluginů a shellu i u exec
        args += [
            "--json",
            "--skip-git-repo-check",
            "--output-schema", str(self._schema),
            "-o", str(self._out),
        ]
        if self.cfg.codex_model:
            args += ["-m", self.cfg.codex_model]
        return args

    async def _run(self, args: list[str], prompt: str) -> dict:
        self._out.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *args,
            prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # vlastní pgid kvůli _kill_tree
        )

        # Po _kill_tree se na proc.wait() nesmí čekat: waiter se po SIGKILL
        # umí neprobudit (pozorováno na 3.12), i když returncode dorazí.
        try:
            async with asyncio.timeout(TURN_TIMEOUT):
                raw, error, fatal = await self._read_events(proc)
                # [latency] po turn.completed Codex na Pi ještě 5–8 s končí
                # (zápis session, úklid) — na to se nečeká, uklidí se na pozadí
                if raw.strip() and self._turn_done and not fatal:
                    asyncio.create_task(_reap(proc))
                    return parse_output(raw)
                if not fatal:
                    await proc.wait()
        except TimeoutError:
            _kill_tree(proc)
            raise RuntimeError(f"Codex neodpověděl do {TURN_TIMEOUT} s, ukončen")
        except asyncio.CancelledError:
            # tah zrušil přednostní požadavek posluchače — codex nesmí dál
            # běžet na pozadí a zabírat paměť
            _kill_tree(proc)
            raise

        if fatal:
            raise RuntimeError(error)
        if proc.returncode != 0:
            stderr = (await proc.stderr.read()).decode(errors="replace").strip()
            if "login" in stderr.lower() or "unauthor" in stderr.lower():
                raise CodexUnavailable(f"Codex není přihlášen: {stderr[:200]}")
            raise RuntimeError(
                error or stderr[:300] or f"codex skončil s kódem {proc.returncode}"
            )

        # -o is more reliable than the last message from the stream
        if self._out.exists():
            raw = self._out.read_text() or raw
        if not raw.strip():
            # Na turn.failed umí codex skončit s kódem 0 — o chybě se ví
            # jen z eventů, návratový kód nestačí.
            raise RuntimeError(error or "Codex nevrátil žádné rozhodnutí")

        return parse_output(raw)

    async def _read_events(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[str, str, bool]:
        """Čte JSONL stream Codexu; vrací (zpráva agenta, chyba, zabito).

        Na definitivní chybu (4xx) se proces rovnou zabije: Codex by ji sám
        zkoušel znovu a znovu se stejným výsledkem.
        """
        raw = ""
        error = ""
        self._turn_done = False  # [latency]
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                return raw, error, False
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "thread.started":
                self.thread_id = ev.get("thread_id") or self.thread_id
            elif kind == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    raw = item.get("text") or raw
            elif kind == "turn.completed" and raw.strip():  # [latency]
                self._turn_done = True
                return raw, error, False
            elif kind in ("error", "turn.failed"):
                error, fatal = _stream_error(ev)
                if fatal:
                    _kill_tree(proc)
                    return raw, error, True

    # ---- resolve: přání → konkrétní skladby (katalog, bez přehrávače) ----

    async def _resolve_pairs(self, pairs: list[Pair]) -> tuple[list[Track], list[str]]:
        """Codex names the tracks; we look up the videoId ourselves.

        Vrací (nalezené, názvy nenalezených) — nenalezené se posluchači řeknou,
        místo aby odpověď tvrdila, že hrají.
        """
        out: list[Track] = []
        missing: list[str] = []
        pairs = [(a, t) for a, t in pairs[:5] if f"{a} {t}".strip()]

        async def one(artist: str, title: str) -> Track | None:
            try:
                return await self.catalog.search_song(artist, title)
            except Exception as exc:
                log.warning("hledání %r selhalo: %s", f"{artist} {title}", exc)
                return None

        # [latency] naráz, ne jedno po druhém — na Pi to bylo 10–12 s po tahu
        hits = await asyncio.gather(*(one(a, t) for a, t in pairs))
        for (artist, title), hit in zip(pairs, hits):
            query = f"{artist} {title}".strip()
            if hit is None:
                log.info("%r se nenašlo, přeskakuji", query)
                missing.append(f"{artist} — {title}" if artist and title else query)
            elif all(t.id != hit.id for t in out):
                out.append(hit)
        return out, missing

    async def _artist_tracks(self, name: str) -> list[Track]:
        """Skladby interpreta, nejznámější první (katalog hlídá, že jsou jeho)."""
        try:
            full = getattr(self.catalog, "artist_tracks", None)
            if full is not None:
                return await full(name, limit=100)
            return await self.catalog.find_artist_tracks(name, limit=30)
        except Exception as exc:
            log.warning("skladby interpreta %r selhaly: %s", name, exc)
            return []

    async def resolve(self, intent: Intent) -> Plan:
        """Dohledá v katalogu, co přání znamená. Přehrávače se nedotkne.

        Plan.failed je vyplněné, když z přání nejde nic zahrát — tehdy se
        přehrávání nemá měnit a posluchač má slyšet proč.
        """
        with telemetry.timer("dj.resolve", intent_kind=intent.kind) as ev:
            plan = await self._resolve(intent)
            ev.update(
                asked_tracks=[f"{a} — {t}" for a, t in intent.tracks] or None,
                resolved_tracks=[t.label() for t in plan.requested] or None,
                asked_artists=intent.artists or None,
                artist_tracks=len(plan.artist_tracks) if intent.kind == "artist" else None,
                seeds_asked=len(intent.seeds),
                seeds_resolved=len(plan.seeds),
                notes=plan.notes or None,
                failed=plan.failed or None,
            )
        return plan

    async def _resolve(self, intent: Intent) -> Plan:
        plan = Plan(intent=intent)
        if not intent.changes_music:
            return plan

        plan.requested, missing = await self._resolve_pairs(intent.tracks)
        if intent.seed_tracks and not intent.tracks:
            # DJ bez modelu už semínka má (historie, nálady YTM) — nehledat znovu
            plan.seeds = list(intent.seed_tracks)
            return plan
        # [truthful] katalog umí vrátit "aspoň něco od něj" — to není vyžádaná skladba
        # [truthful] a když posluchač jmenoval interpreta ("… od Olympicu"), tak od něj
        plan.requested, wrong, other = await enforce_requested(
            self.catalog, intent.text, intent.tracks, plan.requested
        )
        missing += wrong
        if other:  # [truthful] jiná verze, než jakou jmenoval — říct to, ne mlčet
            intent.reply = ""
            plan.notes.extend(other)
        if missing:
            plan.notes.append("Nenašel jsem: " + "; ".join(missing) + ".")
            if intent.tracks and not plan.requested:
                intent.reply = ""  # [truthful] odpověď modelu slibuje, co nehraje
                if intent.kind != "songs":
                    plan.notes.append("Místo toho hraju hudbu v tom duchu.")
        if intent.kind == "songs":
            if not plan.requested:
                plan.failed = "Nenašel jsem, o co sis řekl" + (
                    f" ({'; '.join(missing)})." if missing else "."
                )
            return plan

        if intent.kind == "artist":
            per_artist = [await self._artist_tracks(a) for a in intent.artists]
            unknown = [a for a, ts in zip(intent.artists, per_artist) if not ts]
            if unknown:
                plan.notes.append("Od " + ", ".join(unknown) + " jsem v katalogu nic nenašel.")
            plan.artist_tracks = interleave(per_artist)
            if not plan.artist_tracks:
                plan.failed = " ".join(plan.notes)
            return plan

        seeds, _ = await self._resolve_pairs(intent.seeds)
        if intent.more_like_current:
            # "víc takového" = od toho, co hraje teď, ne od čehokoli
            cur = (await self.player.status()).current
            if cur and all(t.id != cur.id for t in seeds):
                seeds = [cur] + seeds[:4]
        plan.seeds = seeds or list(plan.requested[:3])
        if not plan.seeds:
            if intent.tracks:
                plan.failed = " ".join(plan.notes) or (
                    "Nic z toho, o co sis řekl, jsem v katalogu nenašel."
                )
            else:
                plan.failed = "Ani jednu z navržených skladeb se nepodařilo najít."
        return plan

    # ---- fronta ----

    @property
    def focus(self) -> str:
        """Interpreti, jejichž režim právě běží ("" = žádný)."""
        if not getattr(self.pools, "artist", ""):
            return ""
        return ", ".join(self._focus_artists) or self.pools.artist

    def _allowed(self, tracks: list[Track]) -> list[Track]:
        if not self.avoid:
            return tracks
        out = [t for t in tracks if not track_avoided(t.artist, t.title, self.avoid)]
        for t in tracks:
            if t not in out:
                log.info("vynechávám (posluchač nechce): %s", t.label())
        return out

    async def next_tracks(self, count: int) -> list[Track]:
        """Další skladby do fronty (z poolů) — bez toho, co posluchač nechce."""
        out: list[Track] = []
        for _ in range(4):  # vyřazené nahradit, ale nezacyklit se
            batch = self._allowed(await self.pools.next_tracks(count - len(out)))
            out += batch
            if len(out) >= count or not self.avoid:
                break
        return out

    def note_started(self, video_id: str) -> None:
        """Přehrávač začal skladbu — vyžádaná už nečeká."""
        self.pending.pop(video_id, None)

    # ---- play: plán → přehrávač ----

    def _record_requests(self, tracks: list[Track]) -> None:
        """Co si posluchač vyžádal jménem — do evidence.

        Zapisuje se i to, co se pak nezahraje: chtěl to slyšet tak jako tak a
        z těch počtů se staví, co si lidi žádají nejčastěji.
        """
        for t in tracks:
            self.store.record_request(t.id, t.title, t.artist)
            # ať to pooly nenabídnou znovu za dvě skladby
            self.pools.session_seen.add(t.id)
            self.pending[t.id] = t
        self.pools.remember_tracks(tracks)

    async def play(self, plan: Plan, interrupt: bool = True) -> str:
        """Provede plán na přehrávači a vrátí pravdivou odpověď."""
        intent = plan.intent
        auto = intent.auto
        if intent.remember.strip() and not auto:
            # Automatický tah (série přeskočení) nesmí psát do vkusu: 25. 9. tak
            # vzniklo šest řádků "uživateli nesedí…", které nikdo neřekl.
            self.store.remember(intent.remember)
        if plan.failed:
            telemetry.event("dj.apply", intent_kind=intent.kind, applied=False, failed=plan.failed[:200])
            return plan.failed
        if auto and self.focus and intent.changes_music:
            # Režim interpreta mění jen posluchač. (Automatické tahy se v něm
            # ani nespouštějí — tohle je pojistka.)
            log.info("automatický tah v režimu interpreta %s ignoruji", self.focus)
            telemetry.event("dj.apply", intent_kind=intent.kind, applied=False, reason="artist_mode")
            return ""

        notes = list(plan.notes)
        if intent.kind in ("artist", "song", "mood"):
            await self._play_radio(plan, interrupt)
            if intent.kind == "artist":
                notes.append(f"Hraju {self.focus} — jen to, dokud neřekneš jinak.")
        elif intent.kind == "songs":
            self._record_requests(plan.requested)
            st = await self.player.status()
            await self.player.enqueue_next(plan.requested)
            await self.player.toggle_pause(False)
            # "pusť X" znamená teď, ne za čtyři minuty — 25. 9. vyžádaný Stypka
            # čekal za sedmiminutovou skladbou a nezahrál se vůbec
            now = interrupt and not intent.play_next
            skipped = st.current is not None and now
            if skipped:
                await self._switch_when_ready(plan.requested[0], st.current)
            log.info("tah: skladby %s (hned=%s)", [t.label() for t in plan.requested], now)
            telemetry.event(
                "dj.apply", intent_kind="songs", play_next=[t.label() for t in plan.requested],
                cut_current=skipped,
            )
        elif intent.kind == "control":
            if intent.control == "skip":
                await self.player.skip()
            elif intent.control == "stop":
                self.pending.clear()
                await self.player.clear_queue()
                await self.player.toggle_pause(True)
            elif intent.control == "pause":
                await self.player.toggle_pause(True)
            elif intent.control == "resume":
                await self.player.toggle_pause(False)
            elif intent.control == "volume" and intent.volume:
                await self.player.set_volume(intent.volume)
            telemetry.event("dj.apply", intent_kind="control", action=intent.control,
                            volume=intent.volume or None)

        return " ".join([intent.reply.strip(), *notes]).strip() or "Hotovo."

    async def _play_radio(self, plan: Plan, interrupt: bool) -> None:
        intent = plan.intent
        auto = intent.auto
        self._record_requests(plan.requested)

        # Vyžádané, které ještě čekají ve frontě: automatický tah je nechá,
        # nový pokyn posluchače je nahrazuje.
        st = await self.player.status()
        keep: list[Track] = []
        if auto:
            keep = [t for t in st.queue if t.id in self.pending]
        else:
            self.pending = {t.id: t for t in plan.requested}

        if intent.kind == "artist":
            label = ", ".join(intent.artists)
            await self.pools.set_artist(label, plan.artist_tracks, mood=intent.mood or label)
        else:
            await self.pools.set_seeds(
                plan.seeds, mood=intent.mood, allow_long=bool(plan.requested)
            )
        if not auto:
            # nový pokyn posluchače: režim interpreta a výjimky jen podle něj
            self._focus_artists = list(intent.artists) if intent.kind == "artist" else []
            self.avoid = list(intent.exclude)
        # vyčistit a naplnit naráz — plnič fronty se mezi to nevmísí
        async with queue_transaction(self.player):
            await self.player.clear_queue()
            # vyžádané jdou první a bez ohledu na to, kdy hrály naposledy —
            # do next_tracks, kde by je smetl filtr opakování, se vůbec nedostanou
            first = keep + [t for t in plan.requested if t not in keep]
            await self.player.enqueue(first)
            fresh = await self.next_tracks(max(1, self.cfg.queue_target - len(first)))
            await self.player.enqueue(fresh)
        await self.player.toggle_pause(False)
        # clear_queue lets the currently playing track finish. But when the
        # user changes the mood, they want to hear different music right
        # away, not in three minutes. Když náladu mění DJ sám (po sérii
        # přeskočení), hrající skladbu neutínáme — nová přijde po ní.
        cut = st.current is not None and interrupt and bool(first + fresh)
        if cut:
            await self._switch_when_ready((first + fresh)[0], st.current)
        upcoming = [t.label() for t in (first + fresh)[:5]]
        log.info(
            "tah (%s, %s): %s | interpret=%s | vyžádané=%s | dál=%s",
            "auto" if auto else "posluchač", intent.kind, intent.mood,
            self.focus or "-", [t.label() for t in plan.requested], upcoming,
        )
        telemetry.event(
            "dj.apply", intent_kind=intent.kind, auto=auto, artist_mode=self.focus or None,
            kept_requests=[t.label() for t in keep] or None,
            requested=[t.label() for t in plan.requested] or None,
            queued=upcoming, cut_current=cut,
            artist_pool=len(plan.artist_tracks) if intent.kind == "artist" else None,
        )

    async def _switch_when_ready(self, first: Track, old: Track) -> None:
        """Utnout hrající skladbu, až bude ta nová připravená k přehrání.

        Utnout hned znamená 5–10 s ticha, než yt-dlp na Pi novou skladbu
        vyřeší. Umí-li přehrávač počkat na připravenost (`wait_ready`), hraje
        stará skladba dál a přepne se až pak — na pozadí, ať tah (a zámek
        Codexu, který zdržuje předpřípravu) skončí hned. Neumí-li to, utne
        se hned jako dřív.
        """
        wait = getattr(self.player, "wait_ready", None)
        if wait is None:
            await self.player.skip(by_user=False)
            return
        if self._switch_task and not self._switch_task.done():
            self._switch_task.cancel()  # novější pokyn vyhrává

        async def switch() -> None:
            t0 = time.monotonic()
            try:
                ready = await wait(first.id, timeout=FIRST_TRACK_WAIT)
            except Exception:
                ready = False
            st = await self.player.status()
            # mezitím mohla stará dohrát nebo ji posluchač přeskočil sám
            still = st.current is not None and st.current.id == old.id
            ours = bool(st.queue) and st.queue[0].id == first.id
            if still and ours:
                await self.player.skip(by_user=False)
            telemetry.event(
                "dj.switch", first=first.label(), ready=bool(ready),
                switched=still and ours, took_ms=int((time.monotonic() - t0) * 1000),
            )

        self._switch_task = asyncio.create_task(switch())

    def _remember_wish(self, intent: Intent) -> None:
        """Poslední výslovné přání — jen tahy posluchače, které mění, co hraje."""
        if intent.auto or not intent.changes_music:
            return
        self.wish = self.wish.then(
            intent.text, time.time(), list(self._focus_artists) if self.focus else [],
            intent.mood,
        )
        self.wish.save(self._wish_file)

    # ---- public API ----

    async def fast_turn(self, text: str) -> str | None:
        """ "pusť Kabát" bez Codexu; None = nejisté, ať rozhodne model."""
        plan = await self.fast_plan(text)
        if plan is None:
            return None
        reply = await self.play(plan, interrupt=True)
        self._remember_wish(plan.intent)
        return reply

    async def fast_plan(self, text: str) -> Plan | None:
        """Rychlá cesta jako plán (nic nepřehrává) — pro frontu přání."""
        self.prewarm()  # [app-server] kdyby přání šlo k modelu, ať Codex už běží
        t0 = time.monotonic()
        try:
            res = await find_artists(self.catalog, text)
        except Exception as exc:  # katalog umí selhat na čemkoli
            res = FastResult(reason=f"error:{type(exc).__name__}")
        telemetry.event(
            "dj.fast_path", text=text[:300], accepted=not res.reason,
            artists=res.artists or None, reason=res.reason or None,
            lookups=res.lookups, took_ms=int((time.monotonic() - t0) * 1000),
        )
        if res.reason:
            log.info("rychlá cesta ne (%s): %s", res.reason, text)
            return await self._fast_song_plan(text)  # [fast-song] konkrétní skladba
        label = ", ".join(res.artists)
        intent = Intent(kind="artist", text=text, artists=res.artists, mood=label,
                        note="fast_path")
        log.info("rychlá cesta: %s → %s", text, label)
        telemetry.event("dj.intent", intent_kind="artist", auto=False,
                        artists=res.artists, repaired="fast_path")
        return Plan(intent=intent, artist_tracks=interleave(res.tracks))

    # [fast-song] — "pusť Jasnou zprávu od Olympicu" bez Codexu
    async def _fast_song_plan(self, text: str) -> Plan | None:
        """Skladba hned a pak rádio z ní ("… a podobné"); None = rozhodne model."""
        t0 = time.monotonic()
        song = await find_song(self.catalog, text)
        telemetry.event(
            "dj.fast_path", text=text[:300], what="song", accepted=song.track is not None,
            track=song.track.label() if song.track else None,
            reason=song.reason or None, lookups=song.lookups,
            took_ms=int((time.monotonic() - t0) * 1000),
        )
        if song.track is None:
            return None
        t = song.track
        intent = Intent(
            kind="song", text=text, tracks=[(t.artist, t.title)],
            mood=f"{t.artist} a podobné", note="fast_path_song",
            reply=f"{song.note} Pak podobné." if song.note  # [truthful] jiná verze
            else f"Hraju {t.label()}, pak podobné.",
        )
        log.info("rychlá cesta (skladba): %s → %s", text, t.label())
        return Plan(intent=intent, requested=[t], seeds=[t])

    # [app-server]
    async def _app(self) -> AppServer | None:
        if not app_server_enabled():
            return None
        if self.app is None:
            if self._binary is None:
                # glob po SD kartě trval na Pi 3.9 s a zasekl smyčku (26. 9. 00:35:50)
                self._binary = await asyncio.to_thread(default_binary) or ""
            if not self._binary:
                return None
            self.app = AppServer(
                self._binary, str(self._dir), model=self.cfg.codex_model,
                max_turns_per_thread=self.MAX_RESUMED_TURNS,
            )
        return self.app

    def prewarm(self) -> None:
        """Nastartuje Codex na pozadí, souběžně s rychlou cestou přání."""
        if not app_server_enabled() or not self.breaker.allow():
            return  # vypnuto, nebo mozek nejede — nezkoušet při každém přání

        async def go() -> None:
            if app := await self._app():
                app.prewarm()

        asyncio.create_task(go())

    async def _via_app_server(self, prompt: str, auto: bool) -> dict | None:
        """Tah přes trvale běžící Codex; None = selhalo, ať to vezme `codex exec`.

        Přihlášení / limit (AppServerFatal) se nepřevádí na exec — dopadl by
        stejně, jen by čekal. Vyhodí se dál a otevře jistič.
        """
        app = await self._app()
        if app is None:
            return None
        t0 = time.monotonic()
        try:
            res = await app.turn(prompt, DECISION_SCHEMA, timeout=TURN_TIMEOUT)
            data = parse_output(res.text)
        except (asyncio.CancelledError, TimeoutError):
            raise  # rozpočet tahu (interpret) nebo přednost posluchače
        except AppServerFatal as exc:
            telemetry.event("dj.turn", how="app_server", auto=auto, ok=False,
                            error=f"{exc.reason}: {exc}"[:300],
                            took_ms=int((time.monotonic() - t0) * 1000))
            if exc.reason == LOGIN:
                # po novém přihlášení musí proces načíst nový auth.json
                await app.close()
            raise
        except Exception as exc:
            log.warning("app-server selhal (%s) — beru codex exec", exc)
            telemetry.event(
                "dj.turn", how="app_server", auto=auto, ok=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
                stderr=" | ".join(app.stderr_tail[-3:])[:300] or None,
                took_ms=int((time.monotonic() - t0) * 1000),
            )
            await app.close()  # příště načisto
            return None
        telemetry.event(
            "dj.turn", how="app_server", auto=auto, ok=True, new_thread=res.new_thread,
            startup_ms=res.startup_ms, model_ms=res.model_ms,
            took_ms=int((time.monotonic() - t0) * 1000),
        )
        return data

    async def close(self) -> None:
        """Ukončí trvale běžící Codex (při vypínání ytdj)."""
        if self.app is not None:
            await self.app.close()

    def status(self) -> dict:
        """Stav DJ pro web / panel / frontu přání (malé stabilní API).

        {"brain": Breaker.status() — {"online", "reason", "since", "retry_at",
                   "retry_in_s", "message"},
         "model": str, "how": "app_server"|"exec",
         "artist_mode": str}
        `brain.online == False` → neukazovat "předplatné"; `brain.message` je
        vlídná česká zpráva pro posluchače.
        """
        return {
            "brain": self.breaker.status(),
            "model": self.cfg.codex_model or "výchozí",
            "how": "app_server" if app_server_enabled() else "exec",
            "artist_mode": self.focus,
        }

    @property
    def offline(self) -> bool:
        """True = mozek teď nejede (jistič otevřený)."""
        return self.breaker.offline

    async def _model_decision(self, prompt: str, auto: bool) -> dict:
        """Rozhodnutí modelu: app-server, při jeho chybě `codex exec`.

        Vyhazuje výjimku, kterou `interpret` roztřídí do jističe.
        """
        # [app-server] trvale běžící Codex; při jakékoli chybě `codex exec` jako dřív
        data = await self._via_app_server(prompt, auto)
        if data is not None:
            return data
        last_exc: Exception | None = None
        for resume in (True, False):
            if resume and not self.thread_id:
                continue
            if resume and getattr(self, "_thread_turns", 0) >= self.MAX_RESUMED_TURNS:
                self.thread_id = None  # [latency] session znovu, viz MAX_RESUMED_TURNS
                continue
            try:
                with telemetry.timer("dj.turn", how="exec", resume=resume, auto=auto) as ev:
                    data = await self._run(self._args(resume), prompt)
                    ev["ok"] = True
                self._thread_turns = getattr(self, "_thread_turns", 0) + 1 if resume else 1
                return data
            except CodexUnavailable:
                raise
            except Exception as exc:
                log.warning("codex exec selhal (resume=%s): %s", resume, exc)
                if classify(exc) in (LOGIN, LIMIT):
                    raise  # čerstvá session by dopadla stejně
                last_exc = exc
                if resume:
                    self.thread_id = None  # session was lost, retry from scratch
        raise RuntimeError(str(last_exc) if last_exc else "Codex neodpověděl")

    async def interpret(self, user_input: str, auto: bool = False) -> Intent:
        """Zeptá se modelu a vyloží odpověď. Nic nepřehrává.

        Když mozek nejede (jistič) nebo selže: jednoduchá přání vyřídí DJ sám
        (`offline.local_mood`), jinak vyhodí `CodexOffline` — `str()` je vlídná
        česká zpráva pro posluchače, `.reason` druh výpadku.
        Posluchačův tah má rozpočet LISTENER_BUDGET, automatický AUTO_BUDGET.
        """
        if not self.breaker.allow():
            return await self._offline_intent(user_input, auto, None)
        prompt = await self._build_prompt(user_input)
        budget = AUTO_BUDGET if auto else LISTENER_BUDGET
        t0 = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                data = await self._model_decision(prompt, auto)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                exc = TimeoutError(f"Codex neodpověděl do {budget} s")
            opened = self.breaker.failure(exc)
            telemetry.event(
                "dj.offline", opened=opened, reason=self.breaker.reason,
                error=f"{type(exc).__name__}: {exc}"[:300], auto=auto,
                retry_in_s=self.breaker.status()["retry_in_s"],
                took_ms=int((time.monotonic() - t0) * 1000),
            )
            log.warning("mozek DJ nejede (%s): %s", self.breaker.reason or "jednorázově", exc)
            return await self._offline_intent(user_input, auto, exc)
        if self.breaker.offline:
            log.info("mozek DJ zase jede")
            telemetry.event("dj.offline", opened=None, reason=None, recovered=True)
        self.breaker.success()
        return self._decision_intent(user_input, data, auto)

    def _decision_intent(self, user_input: str, data: dict, auto: bool) -> Intent:
        telemetry.event(
            "dj.decision",
            action=data.get("action"),
            seeds=_labels(data.get("seeds")),
            requested=_labels(data.get("requested")),
            focus_artists=data.get("focus_artists") or None,
            avoid=_labels(data.get("avoid")),
            after_current=bool(data.get("after_current")),
            mood=str(data.get("mood") or "")[:120],
            remember=str(data.get("remember") or "")[:200] or None,
        )
        intent = build_intent(user_input, data, auto=auto)
        if intent.note:
            log.info("oprava rozhodnutí: %s", intent.note)
        log.info(
            "přání (%s): %s | interpreti=%s | skladby=%s | bez=%s | %s",
            "auto" if auto else "posluchač", intent.kind, intent.artists or "-",
            intent.tracks or "-", intent.exclude or "-", intent.mood,
        )
        telemetry.event(
            "dj.intent", intent_kind=intent.kind, auto=auto,
            artists=intent.artists or None,
            tracks=[f"{a} — {t}" for a, t in intent.tracks] or None,
            seeds=[f"{a} — {t}" for a, t in intent.seeds] or None,
            exclude=[f"{a} — {t}" for a, t in intent.exclude] or None,
            play_next=intent.play_next, more_like_current=intent.more_like_current,
            control=intent.control or None, mood=intent.mood[:120],
            repaired=intent.note or None,
        )
        return intent

    # ---- bez modelu ----

    async def _offline_intent(self, text: str, auto: bool, exc: Exception | None) -> Intent:
        """Přání bez modelu, nebo CodexOffline s vlídnou zprávou."""
        key = OTHER if auto else local_mood(text)
        reason = self.breaker.reason or classify(exc) or ERROR
        if key:
            intent = await self.local_intent(key, text, auto=auto)
            if intent is not None:
                telemetry.event("dj.local", key=key, auto=auto, reason=reason,
                                seeds=[f"{a} — {t}" for a, t in intent.seeds][:5])
                return intent
        raise CodexOffline(reason) from exc

    async def local_intent(self, key: str, text: str = "", auto: bool = False) -> Intent | None:
        """Čip bez modelu (offline.CALMER / LIVELIER / CZECH / MORE / OTHER / SURPRISE).

        Semínka z toho, co hrálo a o co si lidi řekli, u klidnější/živější
        z nálad YouTube Music. None = nemám z čeho.
        """
        seeds = await self._local_seeds(key)
        if not seeds:
            return None
        return Intent(
            kind="mood", text=text,
            seeds=[(t.artist, t.title) for t in seeds[:5]],
            seed_tracks=seeds[:5],
            mood=LOCAL_REPLY[key].lower(),
            reply=f"{LOCAL_REPLY[key]} — zatím bez mozku DJ, podle toho, co tu hraje.",
            auto=auto, note="offline",
        )

    async def _local_seeds(self, key: str) -> list[Track]:
        st = await self.player.status()
        current = st.current
        history = [
            Track(id=p.video_id, title=p.title or "", artist=p.artist or "")
            for p in self.store.recent_history(200) if p.video_id
        ]
        finished = [
            Track(id=p.video_id, title=p.title or "", artist=p.artist or "")
            for p in self.store.recent_history(200) if p.outcome == "finished"
        ]
        requested = [
            Track(id=r.video_id, title=r.title or "", artist=r.artist or "")
            for r in self.store.top_requested(30)
        ]
        recent_artists = {t.artist for t in history[:15]}
        if current:
            recent_artists.add(current.artist)

        def pick(pool: list[Track], n: int = 4) -> list[Track]:
            seen: set[str] = set()
            out = []
            for t in pool:
                if t.id in seen or not t.title:
                    continue
                seen.add(t.id)
                out.append(t)
            random.shuffle(out)
            by_artist: dict[str, Track] = {}
            for t in out:
                by_artist.setdefault(t.artist, t)
            return list(by_artist.values())[:n]

        if key == MORE:
            base = [current] if current else []
            return base + pick([t for t in finished[:20] if not current or t.id != current.id], 3)
        if key in (OTHER, SURPRISE):
            pool = [t for t in requested + finished if t.artist not in recent_artists]
            return pick(pool, 4)
        if key == CZECH:
            pool = [t for t in requested + finished + history if looks_czech(t.artist, t.title)]
            seeds = pick(pool, 4)
            if len(seeds) < 2:
                try:
                    seeds += await self.catalog.search("české hity", limit=8)
                except Exception as exc:
                    log.info("české hity nenalezeny: %s", exc)
            return seeds[:5]
        if key in (CALMER, LIVELIER):
            seeds = await self._mood_playlist_seeds(key)
            return seeds or pick(finished, 4)
        return []

    async def _mood_playlist_seeds(self, key: str) -> list[Track]:
        """Pár skladeb z nálady YouTube Music ("Chill", "Energize"…)."""
        words = MOOD_WORDS[key]
        try:
            cats = await self.catalog.mood_categories()
            params = next(
                (item.get("params") for group in (cats or {}).values() for item in group
                 if any(w in (item.get("title") or "").lower() for w in words)),
                None,
            )
            if not params:
                return []
            lists = await self.catalog.mood_playlists(params)
            pid = next((p.get("playlistId") for p in lists or [] if p.get("playlistId")), None)
            if not pid:
                return []
            tracks = await self.catalog.playlist_tracks(pid, limit=25)
        except Exception as exc:
            log.info("nálada YouTube Music nedostupná: %s", exc)
            return []
        random.shuffle(tracks)
        return tracks[:4]

    async def turn(
        self, user_input: str, interrupt: bool = True, auto: bool = False
    ) -> str:
        """Jeden tah: vyložit → dohledat → zahrát. `auto` = zadání od aplikace."""
        try:
            intent = await self.interpret(user_input, auto=auto)
        except CodexOffline as exc:
            return str(exc)  # vlídně, bez syrové chyby
        except CodexUnavailable:
            raise
        except Exception as exc:
            log.warning("tah selhal: %s", exc)
            return "Tohle se mi teď nepovedlo — hudba hraje dál, zkus to prosím za chvíli."
        plan = await self.resolve(intent)
        reply = await self.play(plan, interrupt)
        if not plan.failed:
            self._remember_wish(intent)
        return reply


def _labels(items) -> list[str] | None:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            a = str(it.get("artist") or "").strip()
            t = str(it.get("title") or "").strip()
            out.append(f"{a} — {t}" if a and t else a or t)
    return [x for x in out if x] or None


def interleave(lists: list[list[Track]]) -> list[Track]:
    """Skladby více interpretů střídavě (Kabát, Škwor, Kabát, …), bez duplicit."""
    out: list[Track] = []
    seen: set[str] = set()
    queues = [list(x) for x in lists]
    while any(queues):
        for q in queues:
            while q:
                t = q.pop(0)
                if t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
                    break
    return out
