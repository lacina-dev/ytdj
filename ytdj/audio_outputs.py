"""Zvukový výstup jukeboxu: seznam výstupů, volba, návrat a pojistka hlasitosti.

Vlastník 7. 10. 2026 (POZADAVKY #75): „dej někam do nastavení možnost výběru
zvukového výstupu, tak aby si to pak vybraný výstup pamatovalo i po rebootu.
A aby to vidělo nový výstup, i když se připojí."

Jak se přepíná (F-ZVUK-29): přehrávače (mpv) se to vůbec netýká. Hraje do
výchozího výstupu PipeWire a přepnutí je přesun výchozího výstupu — zápis
`default.configured.audio.sink` do metadat „default", které WirePlumber
poslechne a proud mpv hned přepojí. Měřeno 7. 10. se skutečným mpv proti
soukromému PipeWire (dva prázdné výstupy, záznam z obou): ticho −21 až 85 ms
a skladba pokračuje, kde byla. Druhá možnost — vlastnost mpv `audio-device` —
otevírá výstup mpv znovu: 235–405 ms ticha a ~2 s skladby pryč (zásoba zvuku
se zahodí). Proto jen první cesta; filtr srovnání hlasitosti, ztlumení, posun
i zásoba 2 s zůstávají, jak jsou, protože mpv o přepnutí neví.

Odkud se bere stav: jeden trvale běžící `pw-dump -m` (PipeWire sám pošle
každou změnu: nový výstup, zmizelý výstup, přepojení proudu). Mezi změnami
nestojí nic (Pi: 45 s přehrávání = žádný výpis); opakované `pw-dump` by stálo
~0,1 s CPU pokaždé. Metadata se po změně čtou jedním `pw-metadata` — jejich
výpis v `pw-dump -m` se mezi verzemi PipeWire liší.

Volba patří jukeboxu (config `audio_output`, název uzlu PipeWire; "" =
automaticky podle priorit), ne stavu WirePlumberu: po startu a po každé změně
se porovná, co má WirePlumber připnuté, a co chce jukebox, a rozdíl se opraví.
Vybraný výstup, který není připojený, se nepřipíná (hraje náhradní podle
priorit); připne se, až je zpátky a vydrží RETURN_DEBOUNCE.

Pojistka hlasitosti (F-HLAS-10): jiný výstup = jiný zesilovač. Když se změní
výstup, na kterém jukebox hraje, hlasitost se stáhne na `audio_switch_volume`
(pokud je výš) — u plánovaného přepnutí ještě před ním.

Směšovač karty ani hlasitost výstupu v PipeWire se tu nikdy nemění, jen čtou.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from . import telemetry
from .config import DATA_DIR, save_values

log = logging.getLogger(__name__)

AUTO = ""  # hodnota nastavení: výstup vybírá WirePlumber podle priorit
AUTO_LABEL = "Automaticky (podle priority)"
# Název uzlu PipeWire. Jde jako argument příkazu (nikdy přes shell) a do JSONu
# — proto jen znaky, které ALSA uzly opravdu mají, žádné uvozovky a mezery.
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,199}$")
METADATA = "default"
KEY_PIN = "default.configured.audio.sink"  # připnutý výstup (wpctl set-default)
KEY_DEFAULT = "default.audio.sink"  # kam WirePlumber doopravdy posílá
SWITCH_VOLUME = 20  # výchozí strop hlasitosti po změně výstupu (config audio_switch_volume)
RETURN_DEBOUNCE = 5.0  # s — tak dlouho musí být vrácený výstup připojený, než se na něj přepne
STARTUP_GRACE = 20.0  # s — po startu se čeká, než se USB karty ohlásí (bez "náhradního výstupu")
COMMAND_TIMEOUT = 3.0  # s — pw-metadata
MONITOR_RETRY = 5.0  # s — PipeWire neběží / restartuje se
SETTLE = 0.05  # s — dávka změn grafu se zpracuje najednou
APPLY_WATCH = 3.0  # s — do kdy se čeká, že se proud po přepnutí opravdu přepojí
NOTICE_TTL = 15 * 60  # s — jak dlouho nastavení ukazuje "hlasitost stažena…"
KNOWN_MAX = 8  # kolik zapamatovaných výstupů držet
# pojistka proti přetahování s někým, kdo výstup přepíná jinudy (wpctl v cyklu)
WRITES_PER_MINUTE = 6
CONFLICT_PAUSE = 300.0

_KEEP = object()  # "připnutí teď neměnit"
_UPDATE = re.compile(r"key:'([^']+)' value:'(.*)' type:")
_PROFILE_SUFFIX = re.compile(
    r"\s+(Analog|Digital|Multichannel|Pro|Stereo|Mono|Surround|Duplex|Output|\(HDMI[^)]*\)|"
    r"\(IEC958[^)]*\)|[0-9.]+)\b.*$")


def _kind(obj: dict) -> str:
    return str(obj.get("type") or "").rsplit(":", 1)[-1]


def _props(obj: dict) -> dict:
    info = obj.get("info")
    props = info.get("props") if isinstance(info, dict) else None
    return props if isinstance(props, dict) else {}


def _name_of(value: Any) -> str | None:
    """Název uzlu z hodnoty metadat (`{"name": …}` jako objekt i jako text)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    name = value.get("name") if isinstance(value, dict) else None
    return name if isinstance(name, str) and NAME_OK.match(name) else None


def parse_metadata(text: str) -> dict[str, str | None]:
    """Výpis `pw-metadata -n default` → {klíč: název uzlu}."""
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        m = _UPDATE.search(line)
        if m and m.group(1) in (KEY_PIN, KEY_DEFAULT):
            out[m.group(1)] = _name_of(m.group(2))
    return out


def human_label(name: str, props: dict, device: dict | None) -> str:
    """Jméno výstupu pro člověka („USB Audio Device", „HDMI (telka)")."""
    card = str(props.get("alsa.card_name") or (device or {}).get("alsa.card_name") or "")
    low = name.lower()
    if card.startswith("vc4-hdmi") or (".hdmi" in low and "platform-" in low):
        return "HDMI (telka)"
    if re.match(r"bcm2835.*headphones", card, re.I):
        return "Sluchátkový výstup 3,5 mm"
    text = str((device or {}).get("device.description") or "").strip()
    if not text:
        text = _PROFILE_SUFFIX.sub("", str(props.get("node.description") or "")).strip()
    return text or str(props.get("node.nick") or name)


class Graph:
    """Obraz grafu PipeWire skládaný z výpisů `pw-dump -m` (jen co je potřeba)."""

    KINDS = ("Node", "Link", "Device", "Client", "Metadata")

    def __init__(self) -> None:
        self.objs: dict[int, dict] = {}

    def apply(self, chunk: Any) -> set[str]:
        """Zapracuje jeden výpis (pole objektů). Vrací druhy, které se změnily."""
        changed: set[str] = set()
        for obj in chunk if isinstance(chunk, list) else ():
            if not isinstance(obj, dict) or not isinstance(obj.get("id"), int):
                continue
            oid = obj["id"]
            old = self.objs.get(oid)
            kind = _kind(obj)
            if kind not in self.KINDS or (obj.get("info") is None and kind != "Metadata"):
                # zmizel ({"id": N, "info": null}) nebo je to něco, co nesledujeme
                if old is not None:
                    changed.add(_kind(old))
                    del self.objs[oid]
                continue
            if kind == "Node" and "Audio" not in str(_props(obj).get("media.class") or ""):
                self.objs.pop(oid, None)
                continue
            if old is not None and kind == _kind(old) and isinstance(obj.get("info"), dict):
                # změnový výpis nemusí nést parametry znovu — nechat poslední známé
                old_params = (old.get("info") or {}).get("params")
                if old_params and not obj["info"].get("params"):
                    obj["info"]["params"] = old_params
            self.objs[oid] = obj
            changed.add(kind)
        return changed

    def _of(self, kind: str) -> list[dict]:
        return [o for o in self.objs.values() if _kind(o) == kind]

    def default_metadata_seen(self, chunk: Any) -> bool:
        return any(isinstance(o, dict) and _kind(o) == "Metadata"
                   and (o.get("props") or {}).get("metadata.name") == METADATA
                   for o in chunk if isinstance(chunk, list))

    def sinks(self) -> list[dict]:
        """Výstupy: [{name, id, label, muted, volume, priority, hw_muted}]."""
        devices = {o["id"]: o for o in self._of("Device")}
        out = []
        for node in self._of("Node"):
            props = _props(node)
            name = props.get("node.name")
            if props.get("media.class") != "Audio/Sink" or not isinstance(name, str):
                continue
            if not NAME_OK.match(name) or name.startswith("auto_null"):
                continue  # "Dummy Output" není výstup, na který by šlo přepnout
            device = devices.get(props.get("device.id"))
            dprops = _props(device) if device else None
            par = ((node.get("info") or {}).get("params") or {}).get("Props") or [{}]
            par = par[0] if isinstance(par[0], dict) else {}
            vols = [v for v in (par.get("channelVolumes") or []) if isinstance(v, (int, float))]
            volume = max(vols) if vols else par.get("volume")
            out.append({
                "name": name, "id": node["id"], "label": human_label(name, props, dprops),
                "description": str(props.get("node.description") or ""),
                "muted": bool(par.get("mute")),
                "volume": float(volume) if isinstance(volume, (int, float)) else None,
                "priority": props.get("priority.session"),
                "hw_muted": self._route_silent(device, props),
            })
        # stejné jméno dvakrát (dva výstupy jedné karty) → celý popis uzlu
        seen: dict[str, int] = {}
        for s in out:
            seen[s["label"]] = seen.get(s["label"], 0) + 1
        for s in out:
            if seen[s["label"]] > 1 and s["description"]:
                s["label"] = s["description"]
        out.sort(key=lambda s: (-(s["priority"] if isinstance(s["priority"], int) else 0), s["label"]))
        return out

    @staticmethod
    def _route_silent(device: dict | None, node_props: dict) -> bool:
        """Je výstupní cesta karty (její směšovač) ztlumená nebo na nule?"""
        routes = ((device or {}).get("info") or {}).get("params", {}).get("Route") or []
        outs = [r for r in routes if isinstance(r, dict) and r.get("direction") == "Output"]
        want = node_props.get("card.profile.device")
        match = [r for r in outs if want is not None and r.get("device") == want]
        if not match and len(outs) == 1:
            match = outs
        for r in match:
            p = r.get("props") if isinstance(r.get("props"), dict) else {}
            vols = [v for v in (p.get("channelVolumes") or []) if isinstance(v, (int, float))]
            if p.get("mute") or (vols and max(vols) <= 0):
                return True
        return False

    def stream_sink(self, pid: int | None, app: str = "mpv") -> tuple[bool, str | None]:
        """(proud přehrávače existuje, název výstupu, do kterého je zapojený)."""
        clients = {o["id"]: _props(o) for o in self._of("Client")}
        streams = []
        for node in self._of("Node"):
            props = _props(node)
            if props.get("media.class") != "Stream/Output/Audio":
                continue
            cpid = props.get("application.process.id",
                             clients.get(props.get("client.id"), {}).get("application.process.id"))
            mine = pid is not None and cpid is not None and str(cpid) == str(pid)
            if mine or props.get("application.name") == app:
                streams.append((0 if mine else 1, node["id"]))
        if not streams:
            return False, None
        sid = sorted(streams)[0][1]
        names = {o["id"]: _props(o).get("node.name") for o in self._of("Node")
                 if _props(o).get("media.class") == "Audio/Sink"}
        for link in self._of("Link"):
            info = link.get("info") or {}
            if info.get("output-node-id") == sid and info.get("input-node-id") in names:
                return True, names[info["input-node-id"]]
        return True, None


class AudioOutputs:
    """Hlídá výstupy, drží volbu jukeboxu a stará se o pojistku hlasitosti."""

    def __init__(self, cfg: Any, player: Any = None, *, state_file: Path | None = None,
                 pw_dump: str = "pw-dump", pw_metadata: str = "pw-metadata",
                 clock: Callable[[], float] = time.monotonic,
                 on_change: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.player = player
        self.state_file = state_file if state_file is not None else DATA_DIR / "audio-outputs.json"
        self.pw_dump, self.pw_metadata = pw_dump, pw_metadata
        self.clock = clock
        self.on_change = on_change
        self.graph = Graph()
        self.available: bool | None = None  # None = ještě nevíme
        self.reason = ""
        # {"adopted": bool, "last": výstup, na kterém se hrálo, "known": {název: {"label", "seen"}}}
        self.state: dict[str, Any] = {"v": 1, "adopted": False, "last": None, "known": {}}
        self._state_sig: str | None = None
        self.pin: str | None = None  # co má připnuté WirePlumber
        self.default: str | None = None  # kam WirePlumber posílá
        self.fallback = False  # vybraný výstup není připojený — hraje náhradní
        self._meta_dirty = True
        self._names: set[str] | None = None  # výstupy při minulém pohledu
        self._present_since: float | None = None  # od kdy je vybraný výstup připojený
        self._ever_present = False
        self._away_since: float | None = None
        self._t0 = clock()
        self._lock = asyncio.Lock()
        self._kick_handle: asyncio.TimerHandle | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._timer_at = 0.0
        self._again = False
        self._task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, Any] | None = None  # rozdělané přepnutí (do telemetrie)
        self._writes: list[float] = []
        self._paused_until = 0.0
        self._notices: list[tuple[float, str]] = []
        self._stopping = False

    # ---------- život ----------

    async def start(self) -> None:
        """Nic nečeká: sledování běží na pozadí, hudbu to nezdrží."""
        self._t0 = self.clock()
        missing = [b for b in (self.pw_dump, self.pw_metadata) if not shutil.which(b)]
        if missing:
            self.available, self.reason = False, "na tomhle stroji není PipeWire"
            telemetry.event("audio.monitor", ok=False, missing=missing)
            return
        await asyncio.to_thread(self._load_state)
        self._monitor_task = asyncio.create_task(self._monitor(), name="ytdj-audio-outputs")

    async def stop(self) -> None:
        self._stopping = True
        for handle in (self._kick_handle, self._timer):
            if handle is not None:
                handle.cancel()
        for task in (self._monitor_task, self._task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), 2)

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        known = data.get("known") if isinstance(data.get("known"), dict) else {}
        self.state["known"] = {
            n: {"label": str(v.get("label") or n)[:80], "seen": float(v.get("seen") or 0)}
            for n, v in known.items()
            if isinstance(n, str) and NAME_OK.match(n) and isinstance(v, dict)}
        self.state["adopted"] = bool(data.get("adopted"))
        last = data.get("last")
        self.state["last"] = last if isinstance(last, str) and NAME_OK.match(last) else None
        self._state_sig = json.dumps(self.state, sort_keys=True)

    async def _save_state(self) -> None:
        sig = json.dumps(self.state, sort_keys=True)
        if sig == self._state_sig:
            return
        self._state_sig = sig
        text = json.dumps(self.state, ensure_ascii=False, indent=1)

        def write() -> None:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_file.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(self.state_file)
            except OSError:
                log.debug("stav zvukových výstupů nejde uložit", exc_info=True)

        await asyncio.to_thread(write)

    # ---------- sledování grafu ----------

    async def _monitor(self) -> None:
        while not self._stopping:
            try:
                await self._watch()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("sledování zvukových výstupů selhalo", exc_info=True)
            await self._kill()
            if self._stopping:
                return
            if self.available is not False:
                telemetry.event("audio.monitor", ok=False, reason="pw-dump skončil")
            self.available, self.reason = False, "PipeWire neodpovídá"
            self.graph = Graph()
            self._meta_dirty = True
            self._poke()
            await asyncio.sleep(MONITOR_RETRY)

    async def _watch(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.pw_dump, "-m", "-N", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 20)
        assert self._proc.stdout
        buf: list[bytes] = []
        async for raw in self._proc.stdout:
            buf.append(raw)
            if raw.rstrip() != b"]":
                continue
            data = b"".join(buf)
            buf = []
            try:
                # první výpis má stovky kB (Pi: 311 kB) — rozebrat mimo smyčku
                chunk = (await asyncio.to_thread(json.loads, data) if len(data) > 32_000
                         else json.loads(data))
            except ValueError:
                continue
            self.feed(chunk)

    def feed(self, chunk: Any) -> None:
        """Jeden výpis `pw-dump -m` (i z testů)."""
        changed = self.graph.apply(chunk)
        if self.graph.default_metadata_seen(chunk):
            self._meta_dirty = True
        if self.available is not True:
            self.available, self.reason = True, ""
            telemetry.event("audio.monitor", ok=True)
            changed.add("Node")
        if changed & {"Node", "Link", "Device", "Metadata"} or self._meta_dirty:
            self.kick()

    def kick(self, delay: float = SETTLE) -> None:
        """Naplánuje srovnání stavu (dávka změn se zpracuje najednou)."""
        if self._stopping or self._kick_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._kick_handle = loop.call_later(delay, self._run_reconcile)

    def _run_reconcile(self) -> None:
        self._kick_handle = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._guarded())
        else:
            self._again = True

    async def _guarded(self) -> None:
        self._again = True
        while self._again and not self._stopping:
            self._again = False
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("srovnání zvukového výstupu selhalo")

    def _later(self, delay: float) -> None:
        """Srovnat znovu za `delay` s. Platí nejbližší termín — každé srovnání
        si své další potřeby (ustálení, lhůta po startu) spočítá znovu."""
        delay = max(0.05, delay)
        at = self.clock() + delay
        if self._timer is not None and not self._timer.cancelled() and self._timer_at <= at \
                and self._timer_at > self.clock():
            return
        if self._timer is not None:
            self._timer.cancel()
        with suppress(RuntimeError):
            self._timer = asyncio.get_running_loop().call_later(delay, self._fire)
            self._timer_at = at

    def _fire(self) -> None:
        self._timer = None
        self.kick(0.0)

    def _poke(self) -> None:
        if self.on_change is not None:
            with suppress(Exception):
                self.on_change()

    # ---------- příkazy ----------

    async def _command(self, *args: str) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.pw_metadata, *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            return False, str(exc)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            return False, "timeout"
        return proc.returncode == 0, out.decode(errors="replace")

    async def _read_metadata(self) -> dict[str, str | None] | None:
        ok, out = await self._command("-n", METADATA, "0")
        return parse_metadata(out) if ok else None

    async def _write_pin(self, name: str | None) -> bool:
        if name is None:
            ok, _ = await self._command("-n", METADATA, "-d", "0", KEY_PIN)
            return ok
        if not NAME_OK.match(name):
            return False
        # typ je nutný: bez "Spa:String:JSON" WirePlumber hodnotu nepřevezme
        ok, _ = await self._command("-n", METADATA, "0", KEY_PIN,
                                    json.dumps({"name": name}), "Spa:String:JSON")
        return ok

    # ---------- pohledy ----------

    def chosen(self) -> str:
        value = getattr(self.cfg, "audio_output", AUTO)
        return value if isinstance(value, str) and NAME_OK.match(value) else AUTO

    def _pid(self) -> int | None:
        proc = getattr(self.player, "proc", None)
        return getattr(proc, "pid", None) if proc is not None else None

    def current(self, sinks: list[dict] | None = None) -> tuple[str | None, bool]:
        """(výstup, na kterém jukebox hraje / bude hrát; proud mpv existuje)."""
        sinks = self.graph.sinks() if sinks is None else sinks
        names = {s["name"] for s in sinks}
        has_stream, linked = self.graph.stream_sink(self._pid())
        if linked in names:
            return linked, True
        return (self.default if self.default in names else None), has_stream

    def output_name(self) -> str | None:
        """Pro provozní log (audio.xrun): na čem se právě hraje."""
        if not self.available:
            return None
        return self.current()[0]

    def label(self, name: str | None, sinks: list[dict] | None = None) -> str:
        if not name:
            return "—"
        for s in self.graph.sinks() if sinks is None else sinks:
            if s["name"] == name:
                return s["label"]
        return str((self.state["known"].get(name) or {}).get("label") or name)

    def known_names(self) -> set[str]:
        """Co smí přijít z webu: připojené výstupy a ty zapamatované."""
        return {s["name"] for s in self.graph.sinks()} | set(self.state["known"]) | {self.chosen()}

    def _notes(self, sinks: list[dict], playing: str | None) -> list[str]:
        notes: list[str] = []
        chosen = self.chosen()
        names = {s["name"] for s in sinks}
        if chosen != AUTO and chosen not in names:
            if playing:
                notes.append(f"Vybraný výstup „{self.label(chosen, sinks)}“ není připojený — hraje "
                             f"náhradní „{self.label(playing, sinks)}“. Až se připojí, jukebox se "
                             "na něj sám vrátí.")
            else:
                notes.append(f"Vybraný výstup „{self.label(chosen, sinks)}“ není připojený.")
        elif chosen != AUTO and playing and playing != chosen:
            notes.append(f"Přepíná se na „{self.label(chosen, sinks)}“…")
        for s in sinks:
            if s["name"] != playing:
                continue
            if s["muted"] or s["volume"] == 0:
                notes.append(f"Výstup „{s['label']}“ je v systému "
                             f"{'ztlumený' if s['muted'] else 'stažený na nulu'} — jukebox hraje, ale "
                             "není slyšet. Jukebox hlasitost výstupu v systému nemění; oprav ji na Pi "
                             "(wpctl set-mute / set-volume).")
            elif s["hw_muted"]:
                notes.append(f"Zvuková karta výstupu „{s['label']}“ má ztlumený nebo nulový směšovač "
                             "— jukebox hraje, ale není slyšet. Jukebox směšovač karty nemění; oprav "
                             "ho na Pi (alsamixer).")
        now = self.clock()
        self._notices = [(t, text) for t, text in self._notices if now - t < NOTICE_TTL]
        notes += [text for _, text in self._notices[-2:]]
        return notes

    def snapshot(self) -> dict[str, Any]:
        """Stav pro nastavení na webu. Jen čte paměť — nic nespouští."""
        chosen = self.chosen()
        if not self.available:
            text = self.reason or "zjišťuji…"
            return {"available": False, "chosen": chosen, "playing": None, "fallback": False,
                    "choices": [{"value": chosen, "label": f"Není k dispozici ({text})"}],
                    "notes": [f"Výběr zvukového výstupu není k dispozici: {text}."]}
        sinks = self.graph.sinks()
        playing, _ = self.current(sinks)
        names = {s["name"] for s in sinks}
        choices = [{"value": AUTO, "label": AUTO_LABEL, "connected": True}]
        for s in sinks:
            choices.append({"value": s["name"], "connected": True, "playing": s["name"] == playing,
                            "label": s["label"] + (" — hraje teď" if s["name"] == playing else "")})
        absent = [n for n in self.state["known"] if n not in names]
        if chosen != AUTO and chosen not in names and chosen not in absent:
            absent.append(chosen)
        absent.sort(key=lambda n: (n != chosen, -float((self.state["known"].get(n) or {}).get("seen") or 0)))
        for n in absent:
            choices.append({"value": n, "connected": False,
                            "label": f"{self.label(n, sinks)} (není připojený)"})
        notes = [f"Teď hraje: {self.label(playing, sinks)}."] if playing else []
        return {"available": True, "chosen": chosen, "playing": playing,
                "playing_label": self.label(playing, sinks) if playing else None,
                "fallback": self.fallback, "choices": choices,
                "notes": notes + self._notes(sinks, playing)}

    # ---------- srovnání: co chce jukebox × co platí ----------

    async def reconcile(self) -> None:
        async with self._lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        if not self.available:
            return
        now = self.clock()
        if self._meta_dirty:
            self._meta_dirty = False
            meta = await self._read_metadata()
            if meta is None:
                self._meta_dirty = True
                self._later(MONITOR_RETRY)
            else:
                self.pin, self.default = meta.get(KEY_PIN), meta.get(KEY_DEFAULT)
        sinks = self.graph.sinks()
        names = {s["name"] for s in sinks}
        self._note_outputs(sinks, names)

        if not self.state["adopted"]:
            # První běh s výběrem výstupu: co je ve WirePlumberu připnuté ručně
            # (wpctl set-default), se převezme jako volba jukeboxu — při
            # nasazení se tak nic slyšitelně nezmění.
            self.state["adopted"] = True
            if self.chosen() == AUTO and self.pin:
                self.cfg.audio_output = self.pin
                with suppress(OSError):
                    await asyncio.to_thread(save_values, {"audio_output": self.pin})
                telemetry.event("audio.output_adopt", output=self.pin,
                                label=self.label(self.pin, sinks))
                log.info("zvukový výstup: převzata ruční volba %s", self.pin)

        chosen = self.chosen()
        want: Any = _KEEP
        if chosen == AUTO:
            want = None
            self._present_since = self._away_since = None
            self._set_fallback(False, chosen, sinks)
        elif chosen in names:
            if self._present_since is None:
                # poprvé od startu / po změně volby hned; po návratu až vydrží
                self._present_since = now if self._away_since is not None else float("-inf")
            self._ever_present = True
            left = self._present_since + RETURN_DEBOUNCE - now
            if left <= 0:
                want = chosen
            else:
                self._later(left)
        else:
            self._present_since = None
            grace = self._t0 + STARTUP_GRACE - now
            if grace > 0 and not self._ever_present and not self.fallback:
                self._later(grace)  # po startu: USB karta se možná teprve ohlásí
            else:
                if self._away_since is None:
                    self._away_since = now
                want = None  # nepřipínat nepřipojený výstup: návrat řídí jukebox
                self._set_fallback(True, chosen, sinks)

        if want is not _KEEP and want != self.pin and now >= self._paused_until:
            await self._apply(want, sinks, now)
        elif want == chosen and chosen != AUTO:
            self._set_fallback(False, chosen, sinks)
        await self._observe(sinks, names)
        await self._save_state()
        self._poke()

    def _note_outputs(self, sinks: list[dict], names: set[str]) -> None:
        now = time.time()
        known = self.state["known"]
        for s in sinks:
            known[s["name"]] = {"label": s["label"], "seen": float(int(now))
                                if s["name"] not in known or now - known[s["name"]]["seen"] > 3600
                                else known[s["name"]]["seen"]}
        chosen = self.chosen()
        if len(known) > KNOWN_MAX:
            for n in sorted((n for n in known if n not in names and n != chosen),
                            key=lambda n: known[n]["seen"])[:len(known) - KNOWN_MAX]:
                del known[n]
        if self._names is None:
            telemetry.event("audio.outputs", present=sorted(names), first=True,
                            labels={s["name"]: s["label"] for s in sinks})
        elif names != self._names:
            appeared, removed = sorted(names - self._names), sorted(self._names - names)
            telemetry.event("audio.outputs", present=sorted(names), appeared=appeared or None,
                            removed=removed or None,
                            labels={n: self.label(n, sinks) for n in appeared + removed})
            log.info("zvukové výstupy: přibyl %s, zmizel %s", appeared or "—", removed or "—")
        self._names = set(names)

    def _set_fallback(self, on: bool, chosen: str, sinks: list[dict]) -> None:
        if on == self.fallback:
            return
        self.fallback = on
        playing = self.current(sinks)[0]
        if on:
            telemetry.event("audio.output_fallback", chosen=chosen, playing=playing,
                            label=self.label(chosen, sinks))
            log.warning("zvukový výstup %s není připojený — hraje náhradní %s", chosen, playing)
        else:
            away = self.clock() - self._away_since if self._away_since is not None else None
            self._away_since = None
            if chosen != AUTO:
                telemetry.event("audio.output_return", chosen=chosen,
                                away_s=round(away, 1) if away is not None else None)
                log.info("zvukový výstup %s je zpátky", chosen)

    async def _apply(self, want: str | None, sinks: list[dict], now: float) -> None:
        self._writes = [t for t in self._writes if now - t < 60] + [now]
        if len(self._writes) > WRITES_PER_MINUTE:
            # někdo jiný výstup přepíná pořád zpátky — nepřetahovat se o zvuk
            self._paused_until = now + CONFLICT_PAUSE
            telemetry.event("audio.output_conflict", want=want, pin=self.pin,
                            pause_s=int(CONFLICT_PAUSE))
            log.warning("zvukový výstup se přepíná i odjinud — %d s to nechávám být",
                        int(CONFLICT_PAUSE))
            self._later(CONFLICT_PAUSE)
            return
        before = self.current(sinks)[0]
        reason = "return" if (self.fallback and want is not None) else (
            "fallback" if self.fallback else "choice")
        if want is not None and want != before:
            # známý cíl: hlasitost stáhnout dřív, než na něj zvuk dojde
            await self._cap(want, sinks, reason)
        t0 = self.clock()
        ok = await self._write_pin(want)
        self._pending = {"want": want, "t0": t0, "ok": ok, "from": before, "reason": reason,
                         "cmd_ms": int((self.clock() - t0) * 1000)}
        self._meta_dirty = True
        if ok:
            self.pin = want
            if want is not None:
                self._set_fallback(False, want, sinks)
            self._later(APPLY_WATCH)
        else:
            self._later(MONITOR_RETRY * 2)
        self.kick()

    async def _cap(self, to: str | None, sinks: list[dict], reason: str) -> None:
        """Pojistka hlasitosti při změně výstupu (F-HLAS-10)."""
        try:
            limit = int(getattr(self.cfg, "audio_switch_volume", SWITCH_VOLUME))
        except (TypeError, ValueError):
            limit = SWITCH_VOLUME
        lower = getattr(self.player, "lower_volume", None)
        if limit <= 0 or not callable(lower):
            return
        try:
            res = await lower(max(0, min(100, limit)))
        except Exception:
            log.exception("stažení hlasitosti při změně výstupu selhalo")
            return
        if res:
            old, new = res
            self._notices.append((self.clock(), f"Hlasitost stažena na {new} (bylo {old}) — výstup "
                                  f"„{self.label(to, sinks)}“ může hrát hlasitěji. Přidej si podle "
                                  "potřeby."))
            telemetry.event("audio.volume_cap", output=to, reason=reason, volume_from=old,
                            volume_to=new)
            log.info("změna výstupu (%s): hlasitost %d → %d", reason, old, new)

    async def _observe(self, sinks: list[dict], names: set[str]) -> None:
        """Na čem se doopravdy hraje: výsledek přepnutí a změna výstupu."""
        now = self.clock()
        current, has_stream = self.current(sinks)
        pend = self._pending
        if pend is not None:
            arrived = pend["want"] is not None and current == pend["want"]
            if not pend["ok"] or arrived or pend["want"] is None or now - pend["t0"] >= APPLY_WATCH:
                self._pending = None
                telemetry.event(
                    "audio.output_apply", target=pend["want"], ok=bool(pend["ok"]),
                    reason=pend["reason"], cmd_ms=pend["cmd_ms"], playing=current,
                    was=pend["from"], stream=has_stream,
                    # od zápisu po přepojení proudu přehrávače na cílový výstup
                    moved_ms=int((now - pend["t0"]) * 1000) if arrived else None)
            else:
                self._later(pend["t0"] + APPLY_WATCH - now)
        last = self.state.get("last")
        if current is None or current == last:
            return
        chosen = self.chosen()
        waiting = (now - self._t0 < STARTUP_GRACE and not has_stream and chosen != AUTO
                   and chosen not in names)
        if waiting:
            return  # po startu se karty teprve hlásí a nic nehraje — nehodnotit
        self.state["last"] = current
        if last is None:
            return  # první běh: jen zapamatovat, nic neměnit
        reason = (pend or {}).get("reason") or ("fallback" if self.fallback else "system")
        await self._cap(current, sinks, reason)
        telemetry.event("audio.output_change", was=last, now=current, reason=reason,
                        label=self.label(current, sinks), stream=has_stream)
        log.info("zvukový výstup: %s → %s (%s)", last, current, reason)

    # ---------- volba z nastavení ----------

    async def config_changed(self, previous: str | None = None,
                             who: dict[str, Any] | None = None, wait: float = 1.5) -> list[str]:
        """Nastavení se změnilo (web). Vrací věty pro člověka (co se stalo)."""
        chosen = self.chosen()
        if previous is not None and previous != chosen:
            telemetry.event("audio.output_choice", was=previous or None, now=chosen or None,
                            label=self.label(chosen) if chosen else AUTO_LABEL, **(who or {}))
            # vědomá volba platí hned (čekání na ustálení je jen pro návrat)
            self._present_since = self._away_since = None
            self._ever_present = False
            self._paused_until = 0.0
            self._writes.clear()
        if not self.available:
            return [f"Výběr zvukového výstupu není k dispozici: {self.reason or 'zjišťuji…'}."]
        mark = len(self._notices)
        await self.reconcile()
        deadline = self.clock() + wait
        while self._pending is not None and self.clock() < deadline:
            await asyncio.sleep(0.05)  # ať odpověď rovnou řekne, kde se hraje
        notes = [text for _, text in self._notices[mark:]]
        sinks = self.graph.sinks()
        names = {s["name"] for s in sinks}
        if chosen != AUTO and chosen not in names:
            notes.insert(0, f"Výstup „{self.label(chosen, sinks)}“ teď není připojený — přepne se "
                            "na něj, až se připojí.")
        elif previous is not None and previous != chosen:
            playing = self.current(sinks)[0]
            notes.insert(0, f"Hraje: {self.label(playing, sinks)}.")
        return notes
