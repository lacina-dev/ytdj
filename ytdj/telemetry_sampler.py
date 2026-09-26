"""Vzorkování stroje pro provozní log: CPU, paměť, swap, teplota, výpadky zvuku.

Běží jako dvě úlohy v téže smyčce asyncio jako přehrávač (spouští ji
MpvPlayer.start):

  * každých ~10 s (YTDJ_SAMPLE_S) `sys.sample` — čte jen soubory z /proc a
    /sys (žádné podprocesy); `vcgencmd get_throttled` jednou za minutu a jen
    na Pi,
  * `pw-top -b` jako trvale běžící podproces (nice 10): PipeWire posílá data
    profileru a pw-top z nich každou vteřinu vypíše tabulku uzlů se sloupcem
    ERR (počet xrunů). Změna ERR = `audio.xrun` s kontextem (co hrálo, na čem
    pracoval resolver, jestli přemýšlel Codex, zatížení). Na Pi 3 to stojí
    ~0,2 % jednoho jádra; pw-dump ani pw-cli čítače xrunů nemají a opakované
    spouštění pw-top každých pár vteřin by bylo dražší a xruny mezi běhy by
    propadly.

Nic z toho nesmí přehrávání ohrozit: každá chyba se spolkne a vzorek se
prostě vynechá; bez pw-top / vcgencmd / /proc (jiný systém) se jen nic
nezapíše.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from typing import Any, Callable

from . import telemetry

log = logging.getLogger(__name__)

INTERVAL = float(os.environ.get("YTDJ_SAMPLE_S", "10") or 10)
THROTTLE_EVERY = 6  # vzorků — vcgencmd je podproces, stačí jednou za minutu
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
TEMP_FILE = "/sys/class/thermal/thermal_zone0/temp"

# get_throttled: bity 0–3 teď, 16–19 od startu
THROTTLE_BITS = {
    0: "podpětí", 1: "omezená frekvence", 2: "throttling", 3: "teplotní limit",
    16: "podpětí (od startu)", 17: "omezená frekvence (od startu)",
    18: "throttling (od startu)", 19: "teplotní limit (od startu)",
}


def throttle_flags(value: int) -> list[str]:
    return [name for bit, name in THROTTLE_BITS.items() if value & (1 << bit)]


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _cpu_times() -> tuple[int, int, int] | None:
    """(celkem, nečinnost vč. iowait, iowait) v tikách z /proc/stat."""
    raw = _read("/proc/stat")
    if not raw:
        return None
    parts = raw.split("\n", 1)[0].split()[1:]
    vals = [int(x) for x in parts[:8]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle, vals[4] if len(vals) > 4 else 0


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    raw = _read("/proc/meminfo") or ""
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty"):
            with contextlib.suppress(ValueError):
                out[key] = int(rest.split()[0])  # kB
    return out


def _vmstat() -> dict[str, int]:
    out: dict[str, int] = {}
    raw = _read("/proc/vmstat") or ""
    for line in raw.splitlines():
        if line.startswith(("pswpin ", "pswpout ", "pgmajfault ")):
            k, v = line.split()
            out[k] = int(v)
    return out


def _proc(pid: int) -> tuple[int, int] | None:
    """(tiky CPU vč. dokončených potomků, RSS v kB) procesu."""
    raw = _read(f"/proc/{pid}/stat")
    if not raw:
        return None
    try:
        fields = raw.rsplit(")", 1)[1].split()
        # po ")" začíná pole 3 (state): utime=14, stime=15, cutime=16, cstime=17, rss=24
        ticks = sum(int(fields[i]) for i in (11, 12, 13, 14))
        rss_kb = int(fields[21]) * PAGE // 1024
        return ticks, rss_kb
    except (IndexError, ValueError):
        return None


def pwtop_argv() -> list[str]:
    """pw-top do roury po řádcích. Bez stdbuf ho stdio bufferuje po ~4 kB:
    tabulky chodí po pěti (Pi 26. 9.: po ~5 s) a `audio.xrun` dostal čas
    i kontext (pozici, skladbu) o až 5 s pozdější — xruny v tichu mezi
    skladbami vypadaly jako „@0–2 s nové skladby"."""
    stdbuf = shutil.which("stdbuf")
    return [stdbuf, "-oL", "pw-top", "-b"] if stdbuf else ["pw-top", "-b"]


def parse_pwtop_frame(lines: list[str]) -> dict[str, int]:
    """Řádky jedné tabulky `pw-top -b` → {"<id> <jméno>": ERR}.

    Sloupce: S ID QUANT RATE WAIT BUSY W/Q B/Q ERR [FORMAT…] NAME. Následníci
    (streamy, např. mpv) mají před jménem "+ ".
    """
    out: dict[str, int] = {}
    for line in lines:
        f = line.split()
        if len(f) < 10 or f[0] == "S" or not f[1].isdigit():
            continue
        try:
            err = int(f[8])
        except ValueError:
            continue
        name = f[-1]
        out[f"{f[1]} {name}"] = err
    return out


class SystemSampler:
    """`context()` vrací, co se právě děje (skladba, resolver, Codex);
    `pids()` jména → pid procesů, jejichž CPU a paměť se mají sledovat."""

    def __init__(
        self,
        context: Callable[[], dict[str, Any]],
        pids: Callable[[], dict[str, int]],
        interval: float = INTERVAL,
    ) -> None:
        self.context = context
        self.pids = pids
        self.interval = interval
        self._tasks: list[asyncio.Task] = []
        self._pwtop: asyncio.subprocess.Process | None = None
        self._prev_cpu: tuple[int, int, int] | None = None
        self._prev_vm: dict[str, int] = {}
        self._prev_proc: dict[str, tuple[int, float]] = {}
        self._prev_t = 0.0
        self._n = 0
        self._throttled: int | None = None
        self._vcgencmd = shutil.which("vcgencmd")
        self.last: dict[str, Any] = {}  # poslední vzorek — kontext pro audio.xrun
        self.xruns = 0

    # ---- životní cyklus ----

    def start(self) -> None:
        if not telemetry.ENABLED:
            return
        self._tasks = [asyncio.create_task(self._sample_loop(), name="ytdj-sampler")]
        if shutil.which("pw-top"):
            self._tasks.append(asyncio.create_task(self._pwtop_loop(), name="ytdj-pwtop"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks = []
        await self._kill_pwtop()

    async def _kill_pwtop(self) -> None:
        proc, self._pwtop = self._pwtop, None
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)

    # ---- periodický vzorek ----

    async def _sample_loop(self) -> None:
        self.sample(emit=False)  # základ pro rozdíly
        while True:
            await asyncio.sleep(self.interval)
            try:
                if self._vcgencmd and self._n % THROTTLE_EVERY == 0:
                    await self._read_throttled()
                self.sample()
            except Exception:
                log.debug("vzorek systému selhal", exc_info=True)

    async def _read_throttled(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._vcgencmd, "get_throttled",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
            value = int(out.decode().strip().split("=", 1)[1], 16)
        except Exception:
            return
        if value != self._throttled:
            if self._throttled is not None or value:
                telemetry.event("sys.throttle", value=hex(value), flags=throttle_flags(value),
                                was=hex(self._throttled) if self._throttled is not None else None)
            self._throttled = value

    def sample(self, emit: bool = True, reason: str | None = None) -> dict[str, Any]:
        """Jeden vzorek; `reason` pro vzorek na vyžádání mimo pravidelný takt."""
        t_cpu0 = time.process_time()
        now = time.monotonic()
        dt = now - self._prev_t if self._prev_t else 0.0
        rec: dict[str, Any] = {}

        cpu = _cpu_times()
        if cpu and self._prev_cpu:
            total = cpu[0] - self._prev_cpu[0]
            if total > 0:
                rec["cpu"] = round(100 * (1 - (cpu[1] - self._prev_cpu[1]) / total), 1)
                rec["iowait"] = round(100 * (cpu[2] - self._prev_cpu[2]) / total, 1)
        self._prev_cpu = cpu or self._prev_cpu

        load = _read("/proc/loadavg")
        if load:
            rec["load1"] = float(load.split()[0])

        mem = _meminfo()
        if "MemAvailable" in mem:
            rec["mem_avail_mb"] = mem["MemAvailable"] // 1024
        if "SwapTotal" in mem:
            rec["swap_used_mb"] = (mem["SwapTotal"] - mem.get("SwapFree", 0)) // 1024
        if "Dirty" in mem:
            rec["dirty_mb"] = round(mem["Dirty"] / 1024, 1)

        vm = _vmstat()
        if vm and self._prev_vm and dt > 0:
            rec["swapin_ps"] = round((vm.get("pswpin", 0) - self._prev_vm.get("pswpin", 0)) / dt, 1)
            rec["swapout_ps"] = round((vm.get("pswpout", 0) - self._prev_vm.get("pswpout", 0)) / dt, 1)
            rec["majflt_ps"] = round(
                (vm.get("pgmajfault", 0) - self._prev_vm.get("pgmajfault", 0)) / dt, 1)
        self._prev_vm = vm or self._prev_vm

        temp = _read(TEMP_FILE)
        if temp:
            with contextlib.suppress(ValueError):
                rec["temp_c"] = round(int(temp) / 1000, 1)
        if self._throttled:
            rec["throttled"] = hex(self._throttled)

        # CPU a RSS vlastních procesů (ytdj, mpv, resolver vč. node, pw-top)
        pids = {"ytdj": os.getpid()}
        with contextlib.suppress(Exception):
            pids.update({k: v for k, v in self.pids().items() if v})
        if self._pwtop and self._pwtop.returncode is None:
            pids["pwtop"] = self._pwtop.pid
        seen = {}
        with_rss = self._n % THROTTLE_EVERY == 0  # paměť procesů se mění pomalu: jednou za minutu
        for name, pid in pids.items():
            st = _proc(pid)
            if not st:
                continue
            ticks, rss = st
            if with_rss or reason:
                rec[f"rss_{name}_mb"] = rss // 1024
            prev = self._prev_proc.get(name)
            if prev and prev[0] == pid and dt > 0:
                rec[f"cpu_{name}"] = round(100 * (ticks - prev[1]) / CLK_TCK / dt, 1)
            seen[name] = (pid, ticks)
        self._prev_proc = seen  # type: ignore[assignment]

        with contextlib.suppress(Exception):
            # False/None se nepíšou — vzorek je nejčastější řádek logu
            rec.update({k: v for k, v in self.context().items() if v is not None and v is not False})
        st = telemetry.stats()
        if st["dropped"]:
            rec["log_dropped"] = st["dropped"]
        rec["xruns"] = self.xruns
        if reason:
            rec["reason"] = reason
        # vlastní cena vzorku (CPU procesu, ne zeď) — ať je vidět, co stojí měření
        rec["self_us"] = int((time.process_time() - t_cpu0) * 1_000_000)

        self._prev_t = now
        self._n += 1
        self.last = rec
        if emit:
            telemetry.event("sys.sample", **rec)
        return rec

    # ---- xruny z pw-top ----

    async def _pwtop_loop(self) -> None:
        backoff = 5.0
        while True:
            started = time.monotonic()
            try:
                await self._run_pwtop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("pw-top selhal", exc_info=True)
            await self._kill_pwtop()
            # PipeWire se restartoval / pw-top spadl: zkusit znovu, ne v kole
            backoff = 5.0 if time.monotonic() - started > 60 else min(backoff * 2, 300)
            await asyncio.sleep(backoff)

    async def _run_pwtop(self) -> None:
        self._pwtop = await asyncio.create_subprocess_exec(
            *pwtop_argv(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=lambda: os.nice(10),
        )
        assert self._pwtop.stdout
        base: dict[str, int] | None = None
        frame: list[str] = []
        async for raw in self._pwtop.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line.lstrip().startswith("S ") and " ID " in line:
                if frame:
                    counts = parse_pwtop_frame(frame)
                    if base is None:
                        base = counts  # čítače driveru jsou od startu PipeWire
                    else:
                        self._compare(base, counts)
                        base = counts
                frame = []
            else:
                frame.append(line)

    def _compare(self, prev: dict[str, int], cur: dict[str, int]) -> None:
        nodes = []
        total = 0
        for key, err in cur.items():
            before = prev.get(key, 0)
            if err > before:
                nodes.append(f"{key.split(' ', 1)[1]}+{err - before}")
                total += err - before
        if not total:
            return
        self.xruns += total
        ctx: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            ctx.update(self.context())
        last = self.last
        for k in ("cpu", "iowait", "load1", "mem_avail_mb", "swap_used_mb", "temp_c",
                  "cpu_mpv", "cpu_resolver"):
            if k in last:
                ctx[f"last_{k}"] = last[k]
        telemetry.event("audio.xrun", delta=total, nodes=nodes, total=self.xruns, **ctx)
