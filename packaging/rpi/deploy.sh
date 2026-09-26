#!/usr/bin/env bash
# Deploy this checkout to the Raspberry Pi jukebox and refresh it. Re-runnable.
#
#   packaging/rpi/deploy.sh                 # rsync + venv refresh + restart ytdj
#   PI=lacina@192.168.0.24 packaging/rpi/deploy.sh
#   NO_RESTART=1 packaging/rpi/deploy.sh
#
# Runs on the laptop. One-time setup of the Pi (uv, yt-dlp, codex, bgutil,
# cookies, services) is described in packaging/rpi/NOTES.md.
set -euo pipefail

PI="${PI:-lacina@10.42.0.149}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lacina_deploy}"
DEST="${DEST:-ytdj}"                     # relative to the Pi user's home
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ssh_cmd=(ssh -i "$SSH_KEY" -o ConnectTimeout=10)

echo "==> rsync $repo -> $PI:~/$DEST"
rsync -az --delete \
    --exclude .venv --exclude .git --exclude __pycache__ --exclude .claude \
    --exclude '*.pyc' --exclude .pytest_cache \
    -e "${ssh_cmd[*]}" "$repo/" "$PI:$DEST/"

echo "==> refresh venv + WirePlumber config on the Pi"
"${ssh_cmd[@]}" "$PI" DEST="$DEST" NO_RESTART="${NO_RESTART:-}" bash -s <<'REMOTE'
set -euo pipefail
cd "$HOME/$DEST"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# --system-site-packages: the SPI panel uses apt's spidev / gpiod / PIL / numpy.
if [ ! -x .venv/bin/python ]; then
    python3 -m venv --system-site-packages .venv
fi
.venv/bin/pip install -q --disable-pip-version-check -e .

# Bajtkód předem, ne až při startu služby (F-RESTART-08): rsync mění .py
# a co se nezkompiluje tady, kompiluje startující ytdj — o to déle je po
# restartu ticho (celý balík ~5 s na Pi). __pycache__ patřící rootovi (ruční
# běh panelu před ProtectHome) ytdj nezapíše nikdy a kompiloval by pokaždé
# (Pi 26. 9.: ytdj/__pycache__, +0,45 s každého startu).
find ytdj -type d -name __pycache__ ! -writable -print0 \
    | xargs -0 -r sudo -n rm -rf -- \
    || echo "POZOR: __pycache__ patřící rootovi nejde smazat (sudo) — start bude pomalejší"
nice -n 19 .venv/bin/python -m compileall -q ytdj

# USB sound card > 3.5 mm jack > HDMI
conf_dir="$HOME/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$conf_dir"
if ! cmp -s packaging/rpi/51-ytdj-audio-priority.conf "$conf_dir/51-ytdj-audio-priority.conf"; then
    install -m 644 packaging/rpi/51-ytdj-audio-priority.conf "$conf_dir/"
    # a hand-pinned default sink would override the priorities
    rm -f "$HOME/.local/state/wireplumber/default-nodes"
    systemctl --user restart wireplumber.service || true
    echo "wireplumber: priorities installed, restarted"
fi

# ytdj a PO token server mimo jádro 0 (viz cpu-affinity.conf)
aff_changed=
for unit in ytdj ytdj-pot; do
    d="$HOME/.config/systemd/user/$unit.service.d"
    mkdir -p "$d"
    if ! cmp -s packaging/rpi/cpu-affinity.conf "$d/50-cpu-affinity.conf"; then
        install -m 644 packaging/rpi/cpu-affinity.conf "$d/50-cpu-affinity.conf"
        aff_changed=1
    fi
done
if [ -n "$aff_changed" ]; then
    systemctl --user daemon-reload
    systemctl --user restart ytdj-pot.service || true
    echo "cpu: ytdj a ytdj-pot na jádrech 1–3 (restart ytdj níž)"
fi

# delší takt PipeWire proti lupání (viz 50-ytdj-pipewire.conf)
pw_dir="$HOME/.config/pipewire/pipewire.conf.d"
mkdir -p "$pw_dir"
if ! cmp -s packaging/rpi/50-ytdj-pipewire.conf "$pw_dir/50-ytdj-pipewire.conf"; then
    install -m 644 packaging/rpi/50-ytdj-pipewire.conf "$pw_dir/"
    systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service || true
    echo "pipewire: takt nastaven, restartováno"
fi
if [ -z "${NO_RESTART:-}" ] && systemctl --user is-enabled -q ytdj.service 2>/dev/null; then
    systemctl --user restart ytdj.service
    sleep 3
    systemctl --user --no-pager --lines=5 status ytdj.service || true
fi
REMOTE
echo "==> done"
