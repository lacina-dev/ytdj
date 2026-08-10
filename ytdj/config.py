"""Configuration and paths (XDG). Secrets never make it into the repo."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
    "repeat_days": 30,  # don't replay the same thing for N days
    "artist_window": 10,  # max 2 tracks per artist within N songs
    # yt-dlp / mpv
    "ytdl_format": "774/141/251/140/bestaudio",
    "cookies_browser": "",  # "" = autodetect, "none" = no cookies
    "js_runtimes": "node",
    "remote_components": "ejs:github",
    "mpv_extra_args": [],
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
    repeat_days: int
    artist_window: int
    ytdl_format: str
    cookies_browser: str
    js_runtimes: str
    remote_components: str
    mpv_extra_args: list[str] = field(default_factory=list)

    yt_dlp_path: str = ""
    node_bin: str | None = None

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
        if not cfg.cookies_browser:
            cfg.cookies_browser = detect_cookies_browser()
        return cfg

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


def load_secrets() -> None:
    """Loads ~/.config/ytdj/env into os.environ (KEY=value format).

    Codex doesn't need this — it keeps its own login in ~/.codex/auth.json.
    The file remains as a place for possible future variables.
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
