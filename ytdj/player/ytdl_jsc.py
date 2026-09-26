"""Trvale běžící node pro JS výzvy YouTube (n/sig) — pro resolver.

Běží pod interpretem yt-dlp spolu s ytdl_resolver.py, takže nesmí nic
importovat z ytdj (resolver ho načte jako sousední soubor).

Proč: yt-dlp (EJS) pro každou skladbu spustí nový `node --permission -`
a pošle mu na stdin knihovnu řešitele + ~4,3 MB předzpracovaného přehrávače
YouTube. Node to celé znovu zparsuje a přeloží — Pi 3 26. 9.: 2,0–2,4 s
z ~7 s na skladbu. Samotné řešení výzvy v už přeloženém přehrávači trvá
15–40 ms (tentýž výsledek, ověřeno). Tady proto běží jeden node trvale:
knihovnu načte jednou, přeložený přehrávač si drží (podle otisku kódu)
a na každou skladbu dostane jen výzvy.

Bezpečnost jako u yt-dlp: `node --permission` (bez souborů, sítě, podprocesů).
Paměť: ~100–120 MB, dokud node žije — po IDLE_S bez práce se ukončí a další
skladba ho spustí znovu (jednou zaplatí plné načtení jako dřív).
Cokoli selže (node chybí, spadne, odpověď nepřijde do TIMEOUT_S) → ta
skladba se vyřeší postaru (jednorázový node od yt-dlp), nic se neztratí.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import threading
import time

IDLE_S = 600.0  # s bez výzvy → node skončí (paměť zpět)
# s na jednu odpověď: řešení ~70 ms, přeložení přehrávače z cache ~2 s na
# Pi. Dokud se čeká, drží se zámek (i urgentní vlákno) — radši brzy vzdát a
# vyřešit postaru. Nová verze přehrávače (celé předzpracování) má víc.
TIMEOUT_S = 10.0
TIMEOUT_RAW_S = 30.0
KEEP_PLAYERS = 2  # kolik přeložených přehrávačů node drží (nová verze YouTube)

# Řádek JSON dovnitř → řádek JSON ven. "init" načte knihovnu + jádro
# řešitele (stejný kód, jaký by yt-dlp poslal na stdin), "solve" dělá totéž
# co main() jádra, jen s přeloženým přehrávačem z paměti.
SERVER_JS = r"""
'use strict';
const players = new Map();
let ytdjMain = null;
function compiled(key, code) {
  if (players.has(key)) {
    const s = players.get(key);
    players.delete(key);
    players.set(key, s);
    return s;
  }
  if (code == null) return null;
  const s = { n: null, sig: null };
  Function('_result', code)(s);
  players.set(key, s);
  while (players.size > KEEP) players.delete(players.keys().next().value);
  return s;
}
function solve(solvers, requests) {
  return requests.map((r) => {
    if (r.type !== 'n' && r.type !== 'sig') return { type: 'error', error: `Unknown request type: ${r.type}` };
    const f = solvers[r.type];
    if (!f) return { type: 'error', error: `Failed to extract ${r.type} function` };
    try {
      return { type: 'result', data: Object.fromEntries(r.challenges.map((c) => [c, f(c)])) };
    } catch (e) {
      return { type: 'error', error: e instanceof Error ? `${e.message}\n${e.stack}` : `${e}` };
    }
  });
}
function handle(msg) {
  if (msg.op === 'init') {
    (0, eval)(msg.code + '\n;globalThis.__ytdj_jsc = jsc;');
    ytdjMain = globalThis.__ytdj_jsc;
    return { type: 'ok' };
  }
  if (msg.op === 'solve') {
    if (msg.data.type === 'player') return ytdjMain(msg.data);  // nová verze přehrávače: celé jádro
    const s = compiled(msg.key, msg.data.preprocessed_player);
    if (!s) return { type: 'need_code' };
    return { type: 'result', responses: solve(s, msg.data.requests) };
  }
  return { type: 'error', error: 'unknown op' };
}
let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    let out;
    try { out = handle(JSON.parse(line)); } catch (e) { out = { type: 'error', error: `${e}` }; }
    process.stdout.write(JSON.stringify(out) + '\n');
  }
});
"""


class NodeServer:
    """Jeden trvalý node pro celý resolver (obě vlákna, pod zámkem)."""

    def __init__(self, idle_s: float = IDLE_S, timeout_s: float = TIMEOUT_S) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.node: str | None = None
        self.init_sig: str | None = None
        self.known: list[str] = []  # otisky přehrávačů, které node drží (jako jeho Map)
        self.last_use = 0.0
        self.idle_s = idle_s
        self.timeout_s = timeout_s
        self.starts = 0
        self._reaper: threading.Thread | None = None

    # ---- proces ----

    def _spawn(self, node: str) -> None:
        self.stop_locked()
        code = SERVER_JS.replace("KEEP", str(KEEP_PLAYERS))
        self.proc = subprocess.Popen(
            [node, "--permission", "-e", code],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.node, self.init_sig, self.known = node, None, []
        self.starts += 1
        if self._reaper is None or not self._reaper.is_alive():
            self._reaper = threading.Thread(target=self._reap, name="jsc-idle", daemon=True)
            self._reaper.start()

    def stop_locked(self) -> None:
        p, self.proc = self.proc, None
        self.init_sig, self.known = None, []
        if p is None:
            return
        if p.poll() is None:
            p.kill()
            try:
                p.wait(2)
            except Exception:
                pass
        for f in (p.stdin, p.stdout):
            try:
                if f:
                    f.close()
            except Exception:
                pass

    def _reap(self) -> None:
        while True:
            time.sleep(min(30.0, self.idle_s / 4))
            with self.lock:
                if self.proc is None:
                    return
                if time.monotonic() - self.last_use > self.idle_s:
                    self.stop_locked()
                    return

    def _ask(self, msg: dict, timeout: float | None = None) -> dict:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + (timeout or self.timeout_s)
        out = b""
        while not out.endswith(b"\n"):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise TimeoutError("node neodpověděl")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise EOFError("node skončil")
            out += chunk
        return json.loads(out)

    # ---- API pro poskytovatele ----

    def solve(self, node: str, lib_core: str, player: str, preprocessed: bool,
              requests: list[dict]) -> str:
        """Totéž jako stdout jednorázového node od yt-dlp (JSON text)."""
        with self.lock:
            self.last_use = time.monotonic()
            try:
                return self._solve(node, lib_core, player, preprocessed, requests)
            except Exception:
                self.stop_locked()  # příště čistý start
                raise

    def _solve(self, node, lib_core, player, preprocessed, requests) -> str:
        if self.proc is None or self.proc.poll() is not None or self.node != node:
            self._spawn(node)
        sig = hashlib.sha1(lib_core.encode()).hexdigest()
        if self.init_sig != sig:
            res = self._ask({"op": "init", "code": lib_core})
            if res.get("type") != "ok":
                raise RuntimeError(f"init: {res.get('error')}")
            self.init_sig, self.known = sig, []
        if not preprocessed:
            data = {"type": "player", "player": player, "requests": requests,
                    "output_preprocessed": True}
            return json.dumps(self._ask({"op": "solve", "data": data},
                                        max(self.timeout_s, TIMEOUT_RAW_S)))
        key = hashlib.sha1(player.encode()).hexdigest()
        data = {"type": "preprocessed", "requests": requests}
        if key not in self.known:
            data["preprocessed_player"] = player
        res = self._ask({"op": "solve", "key": key, "data": data})
        if res.get("type") == "need_code":  # node ho mezitím zapomněl
            data["preprocessed_player"] = player
            res = self._ask({"op": "solve", "key": key, "data": data})
        if res.get("type") == "result":
            if key in self.known:
                self.known.remove(key)
            self.known.append(key)
            del self.known[:-KEEP_PLAYERS]
        return json.dumps(res)


SERVER = NodeServer()
_installed = False


def install(on_event=None) -> bool:
    """Zaregistruje v yt-dlp poskytovatele s trvalým node (přednost před
    jednorázovým). Volat jednou po importu yt-dlp. False = verze yt-dlp bez
    EJS/node poskytovatele — pak se nic nemění."""
    global _installed
    if _installed:
        return True
    try:
        from yt_dlp.extractor.youtube.jsc._builtin.node import NodeJCP
        from yt_dlp.extractor.youtube.jsc.provider import register_preference, register_provider
    except Exception:
        return False

    class YtdjNodeJCP(NodeJCP):
        PROVIDER_NAME = "ytdj-node"

        def _construct_stdin(self, player, preprocessed, requests, /):
            self._ytdj_args = (player, preprocessed, [
                {"type": r.type.value, "challenges": r.input.challenges} for r in requests])
            return super()._construct_stdin(player, preprocessed, requests)

        def _run_js_runtime(self, stdin, /):
            args, self._ytdj_args = getattr(self, "_ytdj_args", None), None
            if args is not None:
                t0 = time.monotonic()
                try:
                    lib_core = (f"{self._lib_script.code}\nObject.assign(globalThis, lib);\n"
                                f"{self._core_script.code}")
                    out = SERVER.solve(self.runtime_info.path, lib_core, *args)
                    if on_event:
                        on_event("ok", int((time.monotonic() - t0) * 1000), args[1])
                    return out
                except Exception as exc:  # postaru — jednorázový node
                    if on_event:
                        on_event(f"fallback: {exc}"[:200], int((time.monotonic() - t0) * 1000),
                                 args[1])
            return super()._run_js_runtime(stdin)

    register_provider(YtdjNodeJCP)

    @register_preference(YtdjNodeJCP)
    def _prefer(provider, requests) -> int:
        return 100  # + 900 zděděných od NodeJCP → před jednorázovým node

    _installed = True
    return True
