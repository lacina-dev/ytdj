#!/usr/bin/env bash
# Zeštíhlení Raspberry Pi OS pro jukebox — spouští se NA Pi (jako lacina, sudo).
# Opakovatelné. config.txt se projeví po restartu, služby hned.
#
# Pi 3 má 1 GB RAM a vedle hudby na něm nárazově běží yt-dlp s node a Codex.
# Co tady nepotřebujeme, nesmí brát paměť ani CPU — a hlavně nesmí nárazově
# spustit něco těžkého (apt, man-db) uprostřed skladby.
set -euo pipefail
cfg=/boot/firmware/config.txt

# --- config.txt: bez GPU ovladače, minimum paměti pro GPU, bez bluetooth ---
# Obraz jde na KeDei přes náš ovladač (ytdj/panel/kedei.c), HDMI se nepoužívá.
sudo sed -i \
    -e 's/^camera_auto_detect=1$/camera_auto_detect=0/' \
    -e 's/^display_auto_detect=1$/display_auto_detect=0/' \
    -e 's/^dtoverlay=vc4-kms-v3d$/#dtoverlay=vc4-kms-v3d  # ytdj: GPU nepotřebujeme/' \
    -e 's/^max_framebuffers=2$/#max_framebuffers=2/' \
    "$cfg"
# config.txt končí sekcí [all], takže přidané řádky platí pro všechny modely
grep -qx 'gpu_mem=16' "$cfg" || echo 'gpu_mem=16' | sudo tee -a "$cfg" > /dev/null
grep -qx 'dtoverlay=disable-bt' "$cfg" || echo 'dtoverlay=disable-bt' | sudo tee -a "$cfg" > /dev/null

# --- systémové služby, které jukebox nepotřebuje ---
for unit in bluetooth.service hciuart.service udisks2.service rpi-eeprom-update.service \
            apt-daily.timer apt-daily-upgrade.timer man-db.timer; do
    sudo systemctl disable --now "$unit" 2>/dev/null || true
done
# cloud-init už svou práci (uživatel, SSH, síť) udělal při prvním startu
sudo touch /etc/cloud/cloud-init.disabled

# --- uživatelské služby zvuku, které nic nepoužívá ---
# ytdj hraje přes mpv rovnou do PipeWire; PulseAudio vrstvu ani filtry nepotřebuje.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
for unit in pipewire-pulse.socket pipewire-pulse.service filter-chain.service mpris-proxy.service; do
    systemctl --user disable --now "$unit" 2>/dev/null || true
    systemctl --user mask "$unit" 2>/dev/null || true
done

echo "hotovo — config.txt se projeví po restartu (sudo reboot)"
grep -E '^(gpu_mem|dtoverlay|camera_auto|display_auto)' "$cfg"
