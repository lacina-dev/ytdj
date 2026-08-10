"""DJ poháněný Codexem přes předplatné (ne přes API klíč).

ChatGPT/Codex předplatné se nedá volat jako běžné API — auth jsou OAuth tokeny
v ~/.codex/auth.json, takže jedinou cestou je CLI a jeho neinteraktivní režim
`codex exec`.

Nástroje mu NEDÁVÁME. Původní pokus vystavit je jako MCP server na localhostu
sice fungoval, ale v headless režimu Codex každé volání nástroje zruší
("user cancelled MCP tool call") a jediné, co tu bránu otevře, je
`--dangerously-bypass-approvals-and-sandbox` — což zároveň sundá sandbox kolem
shellu. Vzhledem k tomu, že se Codexovi do kontextu vracejí názvy skladeb
z YouTube (tedy cizí vstup), vyměnit sandbox za pohodlí nedává smysl.

Místo toho z něj přes `--output-schema` dostaneme strukturované JSON rozhodnutí
— které skladby použít jako seedy — a vyhledání i rádio si obstaráme sami.
Vedlejší efekt: jeden průchod místo sedmi, tedy výrazně rychleji.
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

# Schéma odpovědi. Strukturované výstupy vyžadují, aby byly všechny
# vlastnosti v `required` — nepoužité se posílají prázdné.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "start_radio",
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
        "mood": {"type": "string"},
        "volume": {"type": "integer"},
        "remember": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "seeds", "mood", "volume", "remember", "reply"],
    "additionalProperties": False,
}


class CodexUnavailable(RuntimeError):
    pass


@dataclass
class Decision:
    action: str = "nothing"
    seeds: list[dict] = field(default_factory=list)
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

        # Codex je primárně kódovací agent. Pustit ho v adresáři projektu by
        # znamenalo, že se začne hrabat ve zdrojácích; dostane prázdný adresář.
        # Musí být STÁLÝ: Codex si ke každému pracovnímu adresáři zapíše
        # `[projects."..."] trust_level` do ~/.codex/config.toml, takže nový
        # dočasný adresář na každý běh by uživateli config donekonečna nafukoval.
        self._dir = DATA_DIR / "codex-workdir"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._schema = self._dir / "schema.json"
        self._schema.write_text(json.dumps(DECISION_SCHEMA))
        self._out = self._dir / "decision.json"

    # ---- volání Codexu ----

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
        )
        return f"{ROLE}\n\n{state}\n\nUživatel říká: {user_input}"

    def _args(self, resume: bool) -> list[str]:
        args = [self.codex, "exec"]
        if resume and self.thread_id:
            # `resume` nebere -C ani -s; obojí si dědí z původní session,
            # takže je nutné je vynechat, ne jen ignorovat návratový kód
            args += ["resume", self.thread_id]
        else:
            args += [
                "-C", str(self._dir),
                "-s", "read-only",  # sandbox zůstává; Codex nic spouštět nepotřebuje
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

        # -o je spolehlivější než poslední zpráva ze streamu
        if self._out.exists():
            raw = self._out.read_text() or raw
        if not raw.strip():
            raise RuntimeError("Codex nevrátil žádné rozhodnutí")

        data = json.loads(raw)
        return Decision(
            action=data.get("action", "nothing"),
            seeds=data.get("seeds") or [],
            mood=data.get("mood", ""),
            volume=int(data.get("volume") or 0),
            remember=data.get("remember", ""),
            reply=data.get("reply", ""),
        )

    # ---- provedení rozhodnutí ----

    async def _resolve_seeds(self, seeds: list[dict]) -> list[Track]:
        """Codex jmenuje skladby, videoId si dohledáme sami."""
        out: list[Track] = []
        for seed in seeds[:5]:
            artist = (seed.get("artist") or "").strip()
            title = (seed.get("title") or "").strip()
            query = f"{artist} {title}".strip()
            if not query:
                continue
            try:
                hits = await self.catalog.search(query, limit=1)
            except Exception as exc:
                log.warning("hledání seedu %r selhalo: %s", query, exc)
                continue
            if hits:
                out.append(hits[0])
            else:
                log.info("seed %r se nenašel, přeskakuji", query)
        return out

    async def _apply(self, d: Decision) -> str:
        if d.remember.strip():
            self.store.remember(d.remember)

        if d.action == "start_radio":
            seeds = await self._resolve_seeds(d.seeds)
            if not seeds:
                return "Ani jednu z navržených skladeb se nepodařilo najít."
            was_playing = (await self.player.status()).current is not None
            await self.pools.set_seeds(seeds, mood=d.mood)
            await self.player.clear_queue()
            fresh = await self.pools.next_tracks(self.cfg.queue_target)
            await self.player.enqueue(fresh)
            await self.player.toggle_pause(False)
            # clear_queue nechává právě hrající skladbu doznít. Když ale uživatel
            # mění náladu, chce slyšet jinou hudbu hned, ne až za tři minuty.
            if was_playing:
                await self.player.skip()
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

    # ---- veřejné API ----

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
                    self.thread_id = None  # session se ztratila, zkus nanovo
                    continue
                return f"(Codex selhal: {exc}) — hudba hraje dál"
            log.info("rozhodnutí: %s, seedů=%d", decision.action, len(decision.seeds))
            return await self._apply(decision)

        return "(Codex neodpověděl) — hudba hraje dál"
