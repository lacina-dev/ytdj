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
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR, Config
from ..music.catalog import Catalog, Track
from ..music.radio import RadioPools
from ..player.base import Player
from ..state import Store
from .prompts import ROLE, render_state

log = logging.getLogger(__name__)

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
        "mood": {"type": "string"},
        "volume": {"type": "integer"},
        "remember": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": [
        "action",
        "seeds",
        "requested",
        "mood",
        "volume",
        "remember",
        "reply",
    ],
    "additionalProperties": False,
}


class CodexUnavailable(RuntimeError):
    pass


@dataclass
class Decision:
    action: str = "nothing"
    seeds: list[dict] = field(default_factory=list)
    requested: list[dict] = field(default_factory=list)
    mood: str = ""
    volume: int = 0
    remember: str = ""
    reply: str = ""


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

    # ---- calling Codex ----

    async def _build_prompt(self, user_input: str) -> str:
        st = await self.player.status()
        state = render_state(
            now_playing=st.current.label() if st.current else "",
            queue=[t.label() for t in st.queue],
            pools=self.pools.describe(),
            history=[
                f"{p.artist} — {p.title} [{p.outcome}]"
                for p in self.store.recent_history(25)
            ],
            taste=self.store.taste(),
            requested=[r.label() for r in self.store.top_requested(10)],
        )
        return f"{ROLE}\n\n{state}\n\nUživatel říká: {user_input}"

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
        args += [
            "--json",
            "--skip-git-repo-check",
            "--output-schema", str(self._schema),
            "-o", str(self._out),
        ]
        if self.cfg.codex_model:
            args += ["-m", self.cfg.codex_model]
        return args

    async def _run(self, args: list[str], prompt: str) -> Decision:
        self._out.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *args,
            prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        raw = ""
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "thread.started":
                self.thread_id = ev.get("thread_id") or self.thread_id
            elif ev.get("type") == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    raw = item.get("text") or raw

        await proc.wait()
        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            if "login" in err.lower() or "unauthor" in err.lower():
                raise CodexUnavailable(f"Codex není přihlášen: {err[:200]}")
            raise RuntimeError(err[:300] or f"codex skončil s kódem {proc.returncode}")

        # -o is more reliable than the last message from the stream
        if self._out.exists():
            raw = self._out.read_text() or raw
        if not raw.strip():
            raise RuntimeError("Codex nevrátil žádné rozhodnutí")

        data = json.loads(raw)
        return Decision(
            action=data.get("action", "nothing"),
            seeds=data.get("seeds") or [],
            requested=data.get("requested") or [],
            mood=data.get("mood", ""),
            volume=int(data.get("volume") or 0),
            remember=data.get("remember", ""),
            reply=data.get("reply", ""),
        )

    # ---- executing the decision ----

    async def _resolve_tracks(self, seeds: list[dict]) -> list[Track]:
        """Codex names the tracks; we look up the videoId ourselves."""
        out: list[Track] = []
        for seed in seeds[:5]:
            artist = (seed.get("artist") or "").strip()
            title = (seed.get("title") or "").strip()
            query = f"{artist} {title}".strip()
            if not query:
                continue
            try:
                hit = await self.catalog.search_song(artist, title)
            except Exception as exc:
                log.warning("hledání seedu %r selhalo: %s", query, exc)
                continue
            if hit:
                out.append(hit)
            else:
                log.info("seed %r se nenašel, přeskakuji", query)
        return out

    async def _requested_tracks(self, d: Decision) -> list[Track]:
        """Co si posluchač vyžádal jménem — dohledat a zapsat do evidence.

        Zapisuje se i to, co se pak nezahraje: chtěl to slyšet tak jako tak a
        z těch počtů se staví, co si lidi žádají nejčastěji.
        """
        tracks = await self._resolve_tracks(d.requested)
        for t in tracks:
            self.store.record_request(t.id, t.title, t.artist)
            # ať to pooly nenabídnou znovu za dvě skladby
            self.pools.session_seen.add(t.id)
        self.pools.remember_tracks(tracks)
        return tracks

    async def _apply(self, d: Decision) -> str:
        if d.remember.strip():
            self.store.remember(d.remember)

        if d.action == "start_radio":
            seeds = await self._resolve_tracks(d.seeds)
            if not seeds:
                return "Ani jednu z navržených skladeb se nepodařilo najít."
            requested = await self._requested_tracks(d)
            was_playing = (await self.player.status()).current is not None
            # Když si posluchač řekl o konkrétního interpreta, smí rádio sáhnout
            # i po delších kusech — u některých interpretů nic kratšího není.
            await self.pools.set_seeds(
                seeds, mood=d.mood, allow_long=bool(d.requested)
            )
            await self.player.clear_queue()
            # vyžádané jdou první a bez ohledu na to, kdy hrály naposledy —
            # do next_tracks, kde by je smetl filtr opakování, se vůbec nedostanou
            await self.player.enqueue(requested)
            fresh = await self.pools.next_tracks(self.cfg.queue_target)
            await self.player.enqueue(fresh)
            await self.player.toggle_pause(False)
            # clear_queue lets the currently playing track finish. But when the
            # user changes the mood, they want to hear different music right
            # away, not in three minutes.
            if was_playing:
                await self.player.skip()
        elif d.action == "play_next":
            requested = await self._requested_tracks(d)
            if not requested:
                return "Nenašel jsem, o co jsi si řekl."
            await self.player.enqueue_next(requested)
            await self.player.toggle_pause(False)
        elif d.action == "skip":
            await self.player.skip()
        elif d.action == "stop":
            await self.player.clear_queue()
            await self.player.toggle_pause(True)
        elif d.action == "pause":
            await self.player.toggle_pause(True)
        elif d.action == "resume":
            await self.player.toggle_pause(False)
        elif d.action == "volume" and d.volume:
            await self.player.set_volume(d.volume)

        return d.reply or "Hotovo."

    # ---- public API ----

    async def turn(self, user_input: str) -> str:
        prompt = await self._build_prompt(user_input)

        for resume in (True, False):
            if resume and not self.thread_id:
                continue
            try:
                decision = await self._run(self._args(resume), prompt)
            except CodexUnavailable:
                raise
            except Exception as exc:
                log.warning("codex exec selhal (resume=%s): %s", resume, exc)
                if resume:
                    self.thread_id = None  # session was lost, retry from scratch
                    continue
                return f"(Codex selhal: {exc}) — hudba hraje dál"
            log.info("rozhodnutí: %s, seedů=%d", decision.action, len(decision.seeds))
            return await self._apply(decision)

        return "(Codex neodpověděl) — hudba hraje dál"
