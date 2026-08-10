"""Terminal REPL.

Deterministic commands ("další", "pauza", "hlasitěji") are caught by regex
and go straight to the player. Spending seven cents and four seconds on
"next" would be absurd — only what the regex does not catch goes to the model.
"""

from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout

from ..player.base import Player

Handler = Callable[[str], Awaitable[str]]

QUIT = re.compile(r"^(konec|quit|exit|:q)$", re.I)
NEXT = re.compile(r"^(dal(ší|si)|next|skip|n|>)$", re.I)
PAUSE = re.compile(r"^(pauza|pause|p|stop hraní)$", re.I)
RESUME = re.compile(r"^(pokra(č|c)uj|resume|play|hraj dál|hraj dal)$", re.I)
STOP = re.compile(r"^(stop|dost|ticho|vypni)$", re.I)
LOUDER = re.compile(r"^(hlasit(ě|e)ji|nahlas|\+{1,3})$", re.I)
QUIETER = re.compile(r"^(ti(š|s)eji|potichu|-{1,3})$", re.I)
VOLUME = re.compile(r"^(?:hlasitost|vol(?:ume)?)\s+(\d{1,3})$", re.I)
STATUS = re.compile(r"^(co hraje|status|\?)$", re.I)
HELP = re.compile(r"^(pomoc|help|h)$", re.I)

HELP_TEXT = """\
Povely bez modelu (okamžité, zdarma):
  další / next / n      přeskočit
  pauza                 pozastavit
  pokračuj              pokračovat
  stop                  zastavit a vyprázdnit frontu
  hlasitěji / tišeji    hlasitost o 10
  hlasitost 70          nastavit hlasitost
  co hraje / ?          stav
  konec                 ukončit

Cokoli jiného jde na model — např.:
  hraj něco veselýho
  něco klidnějšího, jdu spát
  víc český kapely
  tohle je super, pamatuj si to
"""


class Repl:
    def __init__(self, player: Player, on_prompt: Handler) -> None:
        self.player = player
        self.on_prompt = on_prompt
        self.session: PromptSession = PromptSession()
        self.running = True
        self.status_line = "startuji…"

    def _toolbar(self):
        return HTML(f"<b>{self.status_line}</b>")

    def set_status(self, text: str) -> None:
        self.status_line = text
        # redraw the bottom toolbar without disturbing a half-typed line
        app = self.session.app
        if app.is_running:
            app.invalidate()

    async def _local(self, text: str) -> str | None:
        """Return a reply if the command is handled locally; None otherwise."""
        if QUIT.match(text):
            self.running = False
            return "Čau."
        if HELP.match(text):
            return HELP_TEXT
        if NEXT.match(text):
            await self.player.skip()
            return "⏭"
        if PAUSE.match(text):
            await self.player.toggle_pause(True)
            return "⏸"
        if RESUME.match(text):
            await self.player.toggle_pause(False)
            return "▶"
        if STOP.match(text):
            await self.player.clear_queue()
            await self.player.toggle_pause(True)
            return "⏹ fronta vyprázdněna"
        if LOUDER.match(text):
            st = await self.player.status()
            await self.player.set_volume(st.volume + 10)
            return f"🔊 {min(130, st.volume + 10)}"
        if QUIETER.match(text):
            st = await self.player.status()
            await self.player.set_volume(st.volume - 10)
            return f"🔉 {max(0, st.volume - 10)}"
        if m := VOLUME.match(text):
            vol = int(m.group(1))
            await self.player.set_volume(vol)
            return f"🔊 {vol}"
        if STATUS.match(text):
            st = await self.player.status()
            now = st.current.label() if st.current else "(nic)"
            queue = " | ".join(t.label() for t in st.queue[:5]) or "(prázdná)"
            return f"▶ {now}\n  fronta: {queue}"
        return None

    async def run(self) -> None:
        print(HELP_TEXT)
        with patch_stdout():
            while self.running:
                try:
                    text = await self.session.prompt_async(
                        "» ", bottom_toolbar=self._toolbar
                    )
                except (EOFError, KeyboardInterrupt):
                    self.running = False
                    break

                text = text.strip()
                if not text:
                    continue

                try:
                    reply = await self._local(text)
                    if reply is None:
                        reply = await self.on_prompt(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # the REPL must not die over one command
                    reply = f"chyba: {exc}"

                if reply:
                    print(reply)
