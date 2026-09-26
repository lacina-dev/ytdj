"""Trvale běžící Codex (`codex app-server`, JSON-RPC po stdio) místo `codex exec`.

Proč: `codex exec` na Pi 3 platí při každém tahu start CLI (node obal + binárka,
2–10 s), načtení session a po odpovědi ještě úklid. Změřeno na Pi 25. 9.
(stejný skutečný prompt, gpt-5.6-luna):

  codex exec           request → odpověď  20–25 s (model sám 8–12 s)
  app-server, 1. tah   8.6 s   (start procesu 0.4–2.1 s jen jednou)
  app-server, 2. tah   5.3 s

Paměť: nativní binárka bez node obalu má v klidu ~165 MB RSS, při tahu ~180 MB.
Proces se spouští až při prvním přání. Mimo pracovní dobu se po IDLE_TTL bez
tahu ukončí; v pracovní době (`office_warm`) zůstává běžet, dokud má Pi dost
volné paměti (WARM_MIN_FREE_MB). Důvod — Pi 26. 9. 9:23: první přání po
pauze přetáhlo 25 s rozpočtu (request.done took_ms 25049) a skončilo chybou;
podle rozboru provozu (dj.turn) studený start ~9 s + model ~14 s. Volná paměť
na Pi podle telemetrie ze zadání: medián 590 MB, minimum 383 MB (nevím, zda
app-server v tu chvíli běžel) — proto hlídka paměti a při jejím nedostatku
ukončení jako dřív.

Codex dál nedostává žádné nástroje: sandbox read-only, schvalování `never`
a každý požadavek serveru (schválení příkazu, souboru…) se odmítne.
Když cokoli v protokolu selže, volající přejde na `codex exec` jako dřív.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .offline import LIMIT, LOGIN, classify

log = logging.getLogger(__name__)

# Co DJ nepotřebuje. Hlavně pluginy: při každém startu app-serveru se jinak
# stahuje a na SD kartu zapisuje 27 MB katalog pluginů (Pi 26. 9. 00:40:12,
# iowait 41–56 %, tah 26 s). Bez nástrojů shellu navíc model nemá jak cokoli
# spustit — na Pi chybí bubblewrap, takže sandbox read-only nejde vynutit.
DISABLED_FEATURES = [
    "plugins", "remote_plugin", "plugin_sharing", "apps", "browser_use",
    "browser_use_external", "computer_use", "image_generation", "shell_tool",
    "unified_exec", "shell_snapshot", "multi_agent", "skill_search", "tool_suggest",
    "goals", "hooks", "realtime_conversation", "view_image", "sleep_tool",
    "in_app_browser", "in_app_chat", "workspace_dependencies", "worktrees",
    "skill_mcp_dependency_install", "code_mode_host", "daemon_auto_start",
]


def feature_args() -> list[str]:
    """`-c features.X=false` pro každou vypnutou funkci (i pro `codex exec`)."""
    out: list[str] = []
    for f in DISABLED_FEATURES:
        out += ["-c", f"features.{f}=false"]
    return out


IDLE_TTL = 600.0  # s bez tahu → proces končí (uvolní ~165 MB), mimo pracovní dobu
WARM_HOURS = (7, 19)  # pracovní doba (Po–Pá): app-server drží teplý
WARM_MIN_FREE_MB = 250  # …ale jen když Pi i bez něj zbývá aspoň tolik paměti
START_TIMEOUT = 30.0  # s na initialize + thread/start (Pi pod zátěží)


# Mrtvé přihlášení hlásí Codex jen do stderr; tah pak 5× zkouší znovu a bez
# odpovědi vyčerpá celý rozpočet (26. 9. 01:56: 25 s, hláška "síť").
_AUTH_DEAD = re.compile(r"refresh_token_(invalidated|expired|reused)|invalidated oauth token", re.I)


class AppServerError(RuntimeError):
    pass


class AppServerFatal(AppServerError):
    """Přihlášení (401/403) nebo limit (429) — `codex exec` by dopadl stejně.

    `reason` je druh z offline.classify (login / limit).
    """

    def __init__(self, reason: str, text: str) -> None:
        self.reason = reason
        super().__init__(text)


AppServerAuthError = AppServerFatal  # starší jméno


def native_codex(wrapper: str | None) -> str | None:
    """Nativní binárka Codexu vedle npm obalu (`codex` je node skript).

    Obal na Pi 3 stojí ~2 s a ~50 MB navíc; binárka umí totéž. None, když
    ji nenajdeme (jiná instalace) — pak se použije obal.
    """
    if not wrapper:
        return None
    try:
        js = Path(wrapper).resolve()  # …/@openai/codex/bin/codex.js
    except OSError:
        return None
    root = js.parent.parent
    hits = sorted(glob.glob(str(root / "node_modules/@openai/codex-*/vendor/*/bin/codex")))
    return hits[0] if hits else None


@dataclass
class TurnResult:
    text: str
    startup_ms: int  # 0 = proces už běžel
    model_ms: int  # turn/start → turn/completed
    new_thread: bool


class AppServer:
    """Jeden dlouho běžící `codex app-server` a jedno vlákno v něm.

    Není vláknově bezpečné — volající (CodexDJ pod zámkem Codexu) posílá
    tahy po jednom.
    """

    def __init__(
        self,
        binary: str,
        cwd: str,
        model: str = "",
        max_turns_per_thread: int = 3,
        idle_ttl: float = IDLE_TTL,
        extra_args: list[str] | None = None,
        keep_warm: Any = None,
    ) -> None:
        self.binary = binary
        self.cwd = cwd
        self.model = model
        self.max_turns = max_turns_per_thread
        self.idle_ttl = idle_ttl
        self.extra_args = extra_args or []
        # () -> bool: True = po IDLE_TTL nečinnosti proces neukončovat (office_warm)
        self.keep_warm = keep_warm
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._idle: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self.thread_id: str | None = None
        self.thread_turns = 0
        self.stderr_tail: list[str] = []
        self._warm_lock = asyncio.Lock()
        self._prewarm: asyncio.Task | None = None
        self._fresh_thread = False  # vlákno ještě nemělo tah
        self.auth_dead: str | None = None  # řádek stderr: token zneplatněný

    # ---- proces ----

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def ready(self) -> bool:
        """Proces i vlákno běží — tah nezaplatí studený start."""
        return self.alive and self.thread_id is not None and self.thread_turns < self.max_turns

    async def _start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, "app-server", *feature_args(), *self.extra_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # vlastní pgid → close() zabije i potomky
            limit=16 * 1024 * 1024,  # řádky JSON-RPC bývají dlouhé
        )
        self._pending.clear()
        self._events = asyncio.Queue()
        self.auth_dead = None
        self.thread_id = None
        self.thread_turns = 0
        self._reader = asyncio.create_task(self._read_loop(self.proc))
        asyncio.create_task(self._read_stderr(self.proc))
        await self._request("initialize", {
            "clientInfo": {"name": "ytdj", "title": "ytdj DJ", "version": "1"},
        })
        await self._notify("initialized")

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        self.thread_id = None
        if self._idle and self._idle is not asyncio.current_task():
            self._idle.cancel()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)  # SIGTERM nechá binárku viset
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await proc.wait()
        if self._reader:
            self._reader.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AppServerError("app-server skončil"))
        self._pending.clear()

    def _touch(self) -> None:
        """Odloží ukončení pro nečinnost."""
        if self._idle:
            self._idle.cancel()
        if self.idle_ttl > 0:
            self._idle = asyncio.create_task(self._idle_close())

    async def _idle_close(self) -> None:
        while True:
            await asyncio.sleep(self.idle_ttl)
            keep = False
            if self.keep_warm is not None:
                with contextlib.suppress(Exception):
                    keep = bool(self.keep_warm())
            if not keep:
                break
        log.info("app-server %d s bez tahu — ukončuji (uvolní paměť)", int(self.idle_ttl))
        await self.close()

    # ---- JSON-RPC ----

    async def _send(self, obj: dict) -> None:
        if not self.alive or self.proc.stdin is None:
            raise AppServerError("app-server neběží")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _request(self, method: str, params: dict, timeout: float = START_TIMEOUT) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"id": rid, "method": method, "params": params})
        try:
            async with asyncio.timeout(timeout):
                return await fut
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and "method" in msg:
                    await self._refuse(msg)  # požadavek serveru na nás
                elif "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"] or {}
                            fut.set_exception(AppServerError(str(err.get("message") or err)))
                        else:
                            fut.set_result(msg.get("result"))
                elif "method" in msg:
                    self._events.put_nowait(msg)
        finally:
            self._events.put_nowait({"method": "_closed"})
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AppServerError("app-server skončil"))

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr_tail = (self.stderr_tail + [text])[-20:]
            if self.auth_dead is None and _AUTH_DEAD.search(text):
                self.auth_dead = text[-300:]
                self._events.put_nowait({"method": "_auth_dead"})

    async def _refuse(self, msg: dict) -> None:
        """Schválení příkazů, souborů, vstupu… — DJ nic z toho nedovoluje."""
        log.warning("app-server chce %s — odmítám", msg.get("method"))
        with contextlib.suppress(Exception):
            await self._send({
                "id": msg["id"],
                "error": {"code": -32601, "message": "ytdj: nástroje nejsou povolené"},
            })

    # ---- tah ----

    async def warm(self) -> bool:
        """Proces a vlákno připravené k tahu; True = vlákno je nové.

        Volá se i dopředu (při příchodu přání, souběžně s rychlou cestou),
        ať start procesu (na Pi 2–8 s) neplatí posluchač. Souběžná volání
        se počkají na jeden start.
        """
        async with self._warm_lock:
            if not self.alive:
                await self._start()
            if self.thread_id is not None and self.thread_turns < self.max_turns:
                return self._fresh_thread
            # nové vlákno: kontext nenarůstá (stav jde celý v každém zadání)
            params: dict[str, Any] = {
                "cwd": self.cwd,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "ephemeral": True,  # nepsat session na SD kartu
            }
            if self.model:
                params["model"] = self.model
            res = await self._request("thread/start", params)
            self.thread_id = ((res or {}).get("thread") or {}).get("id")
            if not self.thread_id:
                raise AppServerError(f"thread/start bez id: {str(res)[:200]}")
            self.thread_turns = 0
            self._fresh_thread = True
            self._touch()
            return True

    def prewarm(self) -> None:
        """Nastartuje na pozadí (bez čekání); chyby jen do logu."""
        if self.alive and self.thread_id is not None and self.thread_turns < self.max_turns:
            return
        if self._prewarm and not self._prewarm.done():
            return

        async def go() -> None:
            try:
                await self.warm()
            except Exception as exc:
                log.info("předstart app-serveru selhal: %s", exc)
                await self.close()

        self._prewarm = asyncio.create_task(go())

    async def turn(self, prompt: str, schema: dict, timeout: float) -> TurnResult:
        """Jeden tah; vrací text poslední zprávy agenta (JSON podle `schema`)."""
        t0 = time.monotonic()
        new_thread = await self.warm()
        startup_ms = int((time.monotonic() - t0) * 1000)  # ~0, když byl předstartovaný
        self._touch()

        # zbytky z minulého tahu (pozdní notifikace) zahodit
        while not self._events.empty():
            self._events.get_nowait()
        if self.auth_dead:  # nahlásil to už při startu
            raise AppServerFatal(LOGIN, "Codex není přihlášen (token zneplatněný)")
        t_turn = time.monotonic()
        res = await self._request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "outputSchema": schema,
        })
        turn_id = ((res or {}).get("turn") or {}).get("id")
        text = ""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    msg = await self._events.get()
                    method = msg.get("method")
                    params = msg.get("params") or {}
                    if method == "_closed":
                        raise AppServerError("app-server skončil uprostřed tahu")
                    if self.auth_dead:
                        raise AppServerFatal(LOGIN, "Codex není přihlášen (token zneplatněný)")
                    if params.get("threadId") not in (None, self.thread_id):
                        continue
                    if method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage":
                            text = item.get("text") or text
                    elif method == "error":
                        err = params.get("error") or {}
                        text = str(err.get("message") or err)[:300]
                        # 401/429 se opakovat nevyplatí (Codex by 5× zkoušel
                        # znovu, 60 s a víc) — ani když hlásí willRetry
                        kind = classify(text)
                        if kind in (LOGIN, LIMIT):
                            raise AppServerFatal(kind, text)
                        if not params.get("willRetry"):
                            raise AppServerError(text)
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        if turn_id and turn.get("id") not in (None, turn_id):
                            continue
                        if turn.get("status") not in (None, "completed"):
                            err = (turn.get("error") or {}).get("message") or turn.get("status")
                            raise AppServerError(f"tah skončil: {err}")
                        break
        except asyncio.CancelledError:
            # přednost dostal posluchač — tah zastavit, proces nechat žít
            with contextlib.suppress(Exception):
                await self._send({
                    "id": 10**9, "method": "turn/interrupt",
                    "params": {"threadId": self.thread_id, "turnId": turn_id},
                })
            raise
        self.thread_turns += 1
        self._fresh_thread = False
        self._touch()
        if not text.strip():
            raise AppServerError("tah bez odpovědi")
        return TurnResult(
            text=text,
            startup_ms=startup_ms,
            model_ms=int((time.monotonic() - t_turn) * 1000),
            new_thread=new_thread,
        )


def mem_available_mb() -> int | None:
    """MemAvailable z /proc/meminfo v MB; None, když nejde přečíst."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def office_warm(now: Any = None, free_mb: int | None = -1) -> bool:
    """Držet app-server teplý? Po–Pá v WARM_HOURS a jen s dost volnou pamětí.

    `now` (datetime) a `free_mb` dosadí testy; -1 = změřit.
    """
    from datetime import datetime

    now = now or datetime.now()
    if now.weekday() >= 5 or not (WARM_HOURS[0] <= now.hour < WARM_HOURS[1]):
        return False
    free = mem_available_mb() if free_mb == -1 else free_mb
    return free is None or free >= WARM_MIN_FREE_MB


def app_server_enabled() -> bool:
    """YTDJ_CODEX_APP_SERVER=0 vypne (návrat k `codex exec` u každého tahu)."""
    return os.environ.get("YTDJ_CODEX_APP_SERVER", "1").strip() not in ("0", "false", "no", "")


def default_binary() -> str | None:
    wrapper = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    return native_codex(wrapper) or (wrapper if os.path.exists(wrapper) else None)
