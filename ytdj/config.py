"""Configuration and paths (XDG). Secrets never make it into the repo."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ytdj"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ytdj"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "ytdj"

CONFIG_FILE = CONFIG_DIR / "config.toml"
ENV_FILE = CONFIG_DIR / "env"
BROWSER_AUTH = CONFIG_DIR / "browser.json"
STATE_DB = DATA_DIR / "state.db"
TASTE_FILE = DATA_DIR / "taste.md"
MPV_SOCKET = RUNTIME_DIR / "mpv.sock"

DEFAULTS: dict = {
    # "" = keep Codex's default model; otherwise e.g. "gpt-5.4-mini"
    "codex_model": "",
    # web control; 0 = pick a free port
    "web_enabled": True,
    "web_host": "127.0.0.1",
    "web_port": 8765,
    "language": "cs",
    "location": "CZ",
    # queue
    "queue_target": 5,  # how many tracks to keep queued ahead
    "queue_low": 3,  # refill below this
    "pool_low": 10,  # fetch more radio below this
    "radio_limit": 50,  # how many tracks to request from one seed
    # filters
    "min_duration": 60,
    "max_duration": 600,
    # Strop pro případ, kdy si posluchač vyžádal konkrétního interpreta a
    # kratších skladeb od něj není dost. Živé sety a mixy mají klidně hodinu;
    # zahodit je by znamenalo nezahrát to, o co si člověk řekl.
    "max_duration_request": 5400,
    "repeat_days": 30,  # don't replay the same thing for N days
    "artist_window": 10,  # max 2 tracks per artist within N songs
    # yt-dlp / mpv
    "ytdl_format": "774/141/251/140/bestaudio",
    "cookies_browser": "",  # "" = autodetect, "none" = no cookies
    # Cookies exported to a Netscape cookies.txt. Wins over cookies_browser and
    # is the only thing that works without a desktop session — the browser jar
    # is encrypted with a key that lives in the keyring.
    "cookies_file": "",
    "js_runtimes": "node",
    "remote_components": "ejs:github",
    # Klient, za kterého se yt-dlp vydává. Prázdné = jeho vlastní výběr, který
    # sahá po anonymních klientech (ty cookies odmítají, takže ani Premium).
    # Pro Premium je potřeba klient nesoucí přihlášení, např. "web_music" —
    # ten ovšem bez PO tokenu nedostane formáty vůbec. Viz README.
    "player_client": "",
    "mpv_extra_args": [],
    # Last set volume. mpv starts at it, so a restarted service picks up where
    # it left off instead of coming back at full blast.
    "volume": 100,
}


def _detect_node_bin() -> str | None:
    """mpv runs yt-dlp as a subprocess — node has to be on PATH."""
    if shutil.which("node"):
        return None  # already there
    nvm = Path.home() / ".nvm/versions/node"
    if nvm.is_dir():
        versions = sorted(nvm.iterdir(), reverse=True)
        for v in versions:
            if (v / "bin/node").exists():
                return str(v / "bin")
    return None


def _chrome_profiles_with_youtube_login() -> list[str]:
    """Returns Chrome profiles whose cookies contain a YouTube login."""
    base = Path.home() / ".config/google-chrome"
    if not base.is_dir():
        return []
    found = []
    auth_names = {"SAPISID", "__Secure-3PAPISID", "__Secure-1PSID"}
    for db in base.glob("*/Cookies"):
        try:
            import shutil as _sh
            import tempfile

            tmp = tempfile.mktemp()
            _sh.copy(db, tmp)
            rows = sqlite3.connect(tmp).execute(
                "select name from cookies where host_key like '%youtube%'"
            ).fetchall()
            os.unlink(tmp)
        except Exception:
            continue
        if auth_names & {r[0] for r in rows}:
            found.append(db.parent.name)
    # Default first, then Profile N in ascending order
    found.sort(key=lambda p: (p != "Default", p))
    return found


def chrome_profile_account(profile: str) -> str:
    """Účet Google přihlášený v daném profilu Chrome.

    Na otázku "na co jsem to vlastně připojený" je odpověď účet, ne jméno
    profilu — "Profile 2" nikomu nic neřekne.
    """
    prefs = Path.home() / ".config/google-chrome" / profile / "Preferences"
    if not prefs.is_file():
        return ""
    try:
        import json

        data = json.loads(prefs.read_text())
    except (OSError, ValueError):
        return ""
    for account in data.get("account_info") or []:
        if email := account.get("email"):
            return str(email)
    return ""


def detect_cookies_browser() -> str:
    """Autodetects the spec for yt-dlp --cookies-from-browser.

    Returns e.g. 'chrome:Profile 2', or '' when no profile is logged in.
    Prefers the profile with Premium — but we only find that out at runtime,
    so we take the last logged-in profile (the Premium account tends to be
    the one added later).
    """
    profiles = _chrome_profiles_with_youtube_login()
    if profiles:
        return f"chrome:{profiles[-1]}"
    return ""


@dataclass
class Config:
    codex_model: str
    web_enabled: bool
    web_host: str
    web_port: int
    language: str
    location: str
    queue_target: int
    queue_low: int
    pool_low: int
    radio_limit: int
    min_duration: int
    max_duration: int
    max_duration_request: int
    repeat_days: int
    artist_window: int
    ytdl_format: str
    cookies_browser: str
    cookies_file: str
    player_client: str
    js_runtimes: str
    remote_components: str
    volume: int = 100
    mpv_extra_args: list[str] = field(default_factory=list)

    yt_dlp_path: str = ""
    node_bin: str | None = None
    # Přihlašovací údaj, ne nastavení: nebydlí v config.toml, který čte a
    # zapisuje neautentizovaný web. Načítá se z ~/.config/ytdj/env.
    po_token: str = ""

    @classmethod
    def load(cls) -> "Config":
        data = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("rb") as f:
                data.update(tomllib.load(f))

        yt_dlp = shutil.which("yt-dlp") or str(Path.home() / ".local/bin/yt-dlp")

        cfg = cls(
            **{k: data[k] for k in DEFAULTS},
            yt_dlp_path=yt_dlp,
            node_bin=_detect_node_bin(),
        )
        cfg.po_token = os.environ.get("YTDJ_PO_TOKEN", "").strip()
        cfg.cookies_file = os.path.expanduser(cfg.cookies_file)
        # Autodetection reads the Chrome profiles' SQLite; pointless when a
        # cookie file already decides the question.
        if not cfg.cookies_browser and not cfg.cookies_file:
            cfg.cookies_browser = detect_cookies_browser()
        return cfg

    def cookie_source(self) -> str:
        """What actually ends up on yt-dlp's command line."""
        if self.cookies_file:
            return f"soubor {self.cookies_file}"
        if self.cookies_browser and self.cookies_browser != "none":
            return self.cookies_browser
        return "bez cookies"

    def extractor_args(self) -> str:
        """yt-dlp `--extractor-args`; prázdné, když není co předat."""
        parts = []
        if self.player_client:
            parts.append(f"player_client={self.player_client}")
        if self.po_token:
            parts.append(f"po_token={self.po_token}")
        return "youtube:" + ";".join(parts) if parts else ""

    def fix_cookie_profile(self) -> str | None:
        """Přepne se z profilu, který přihlášení nemá, na ten, který ho má.

        Profil se dá odhlásit nebo vymazat, a v configu zůstane starý zápis.
        yt-dlp na to neřekne nic užitečného ("Extracted 0 cookies") a hraje se
        dál anonymně — jen o něco hůř a bez Premium. Radši se to přepne samo a
        nahlas, než aby to tiše degradovalo.
        """
        if self.cookies_file or not self.cookies_browser.startswith("chrome"):
            return None
        _, _, profile = self.cookies_browser.partition(":")
        available = _chrome_profiles_with_youtube_login()
        if not profile or profile in available:
            return None
        if not available:
            self.cookies_browser = "none"
            return (
                f"profil {profile!r} nemá cookies k YouTube a žádný jiný taky ne "
                "— jede se anonymně"
            )
        self.cookies_browser = f"chrome:{available[-1]}"
        return (
            f"profil {profile!r} nemá cookies k YouTube (odhlášený nebo smazaný) "
            f"— beru {self.cookies_browser}"
        )

    def ensure_dirs(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    def child_env(self) -> dict[str, str]:
        """Environment for mpv — adds node to PATH if it is missing."""
        env = dict(os.environ)
        if self.node_bin:
            env["PATH"] = f"{self.node_bin}:{env.get('PATH', '')}"
        return env


# --------------------------------------------------------------------------
# writing config.toml while preserving comments
# --------------------------------------------------------------------------


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(str(v)) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _key_of(line: str) -> str | None:
    """Returns the key if the line is a top-level assignment."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    key, sep, _ = stripped.partition("=")
    key = key.strip()
    if not sep or not key.replace("_", "").isalnum():
        return None
    return key


def _open_brackets(text: str) -> int:
    """How many square brackets remain unclosed (strings are ignored)."""
    depth = 0
    in_str: str | None = None
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == "#":
            break
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth


def rewrite_config_text(text: str, changes: dict[str, Any]) -> str:
    """Rewrites only the lines of affected keys; comments and order stay.

    New keys are appended at the end of the root section (i.e. before the
    first `[table]`, so they don't fall inside someone else's section).
    """
    lines = text.splitlines()
    out: list[str] = []
    pending = dict(changes)
    first_table = -1
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("[") and first_table < 0:
            first_table = len(out)
        key = _key_of(line) if first_table < 0 else None
        if key in pending:
            # swallow a multi-line array whole so no tail is left after rewriting
            chunk = line
            while _open_brackets(chunk) > 0 and i + 1 < len(lines):
                i += 1
                chunk += "\n" + lines[i]
            out.append(f"{key} = {_toml_scalar(pending.pop(key))}")
            i += 1
            continue
        out.append(line)
        i += 1

    if pending:
        block = [f"{k} = {_toml_scalar(v)}" for k, v in pending.items()]
        at = first_table if first_table >= 0 else len(out)
        if at > 0 and out[at - 1].strip():
            block.insert(0, "")
        out[at:at] = block

    return "\n".join(out).rstrip("\n") + "\n"


def save_values(changes: dict[str, Any]) -> None:
    """Writes the given keys to config.toml. Touches the disk — call in a thread.

    Callers must not run two of these at once: they share one temp file.
    """
    path = CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else "# ytdj — konfigurace\n"
    new_text = rewrite_config_text(text, changes)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)  # atomic, so a crash mid-write can't destroy the config


ENV_TEMPLATE = """\
# ytdj — přihlašovací údaje. Schválně mimo config.toml: ten čte a zapisuje
# webové rozhraní, které nemá žádnou autentizaci.
#
# PO token pro yt-dlp. Potřeba jen tehdy, když neběží bgutil provider
# (packaging/install-service.sh ho umí nastartovat jako službu) — ten si
# tokeny razí sám a tenhle soubor pak nepotřebuješ vůbec.
# Formát: <klient>.gvs+<token>, tedy např. web_music.gvs+XXXXXXXX
#YTDJ_PO_TOKEN=
"""


def write_env_template() -> None:
    """Založí soubor pro tajemství s právy 0600, když ještě není."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text(ENV_TEMPLATE)
    # I kdyby soubor vznikl dřív nebo jinak: token je přihlašovací údaj a
    # nemá co být čitelný pro celý systém.
    with suppress(OSError):
        ENV_FILE.chmod(0o600)


def load_secrets() -> None:
    """Loads ~/.config/ytdj/env into os.environ (KEY=value format).

    Codex doesn't need this — it keeps its own login in ~/.codex/auth.json.
    The file holds what must not end up in config.toml: the PO token.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Keys from an earlier version that called an LLM API directly.
_OBSOLETE_KEYS = ("model", "effort")


def write_default_config() -> Path:
    """Creates config.toml with currently detected values if it is missing.

    If it finds keys from an earlier version in it, it sets the file aside and
    writes a new one — otherwise the user would be looking at settings that no
    longer affect anything.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("rb") as f:
                existing = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            return CONFIG_FILE
        if not any(k in existing for k in _OBSOLETE_KEYS):
            return CONFIG_FILE
        backup = CONFIG_FILE.with_suffix(".toml.bak")
        CONFIG_FILE.rename(backup)
        print(f"config.toml byl z předchozí verze — záloha v {backup}")

    cookies = detect_cookies_browser()
    CONFIG_FILE.write_text(
        f"""# ytdj — konfigurace
# Mozek běží přes Codex CLI na tvém předplatném (ne přes API klíč).
# "" = nechat výchozí model Codexu. Jinak např. "gpt-5.4-mini" (rychlejší).
codex_model = ""
language = "cs"
location = "CZ"

# Webové ovládání na http://127.0.0.1:8765
# Host nech na 127.0.0.1, pokud to nechceš vystavit do sítě.
web_enabled = true
web_host = "127.0.0.1"
web_port = 8765

# Prohlížeč a profil, ze kterého yt-dlp bere cookies.
# "" = autodetekce, "none" = nepoužívat cookies (128k opus, nulové riziko).
cookies_browser = "{cookies}"

# Cookies vyexportované do souboru (Netscape cookies.txt). Má přednost před
# cookies_browser a jako jediné funguje bez přihlášené plochy — jar prohlížeče
# je šifrovaný klíčem z klíčenky, a ta se bez přihlášení neodemkne.
# Export: yt-dlp --cookies-from-browser chrome --cookies ~/.config/ytdj/cookies.txt \\
#           --skip-download https://music.youtube.com/
cookies_file = ""

# Preference formátů: 774 = Opus 256k (Premium), 141 = AAC 256k (Premium),
# 251 = Opus 128k, 140 = AAC 128k.
ytdl_format = "774/141/251/140/bestaudio"

queue_target = 5
queue_low = 3
radio_limit = 50
repeat_days = 30
"""
    )
    return CONFIG_FILE
