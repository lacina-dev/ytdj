# ytdj on a Raspberry Pi 3 (Raspberry Pi OS Lite 64-bit, Debian 13)

One-time setup as done on the jukebox Pi (user `lacina`). Afterwards
`packaging/rpi/deploy.sh` (run on the laptop) syncs the code and refreshes the venv.

## Packages

    sudo apt-get install --no-install-recommends mpv ffmpeg pipewire pipewire-pulse \
        pipewire-alsa wireplumber alsa-utils rtkit nodejs npm python3-venv python3-dev git curl \
        python3-spidev python3-libgpiod python3-pil python3-numpy fonts-dejavu-core libsecret-tools

The touch panel's network screen uses `nmcli` (NetworkManager, part of the Raspberry Pi OS
image) and, for the `<hostname>.local` address, `avahi-daemon` (also preinstalled). Its QR
code is drawn by the panel itself — no `qrencode`/`python3-qrcode` needed.

**Node from apt (20.19) is not enough.** Current yt-dlp reports `node-20.19.2 (unsupported)`
and solves no JS challenges; the only result is a format list with storyboards. Node 24 LTS
from nodejs.org goes to `/opt/node`, and symlinks in `/usr/local/bin` put it ahead of
`/usr/bin/node`, both on a login PATH and on the PATH in ytdj.service:

    V=v24.21.0; curl -fsSLO https://nodejs.org/dist/$V/node-$V-linux-arm64.tar.xz   # verify SHASUMS256
    sudo mkdir -p /opt/node && sudo tar -xJf node-$V-linux-arm64.tar.xz -C /opt/node
    sudo ln -sfn /opt/node/node-$V-linux-arm64 /opt/node/current
    for b in node npm npx corepack; do sudo ln -sf /opt/node/current/bin/$b /usr/local/bin/$b; done

## Tools

    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv tool install yt-dlp --with secretstorage --with 'bgutil-ytdlp-pot-provider==1.3.1'
    npm config set prefix ~/.local && npm install -g @openai/codex@latest

Pin the plugin to the server's version. Plugin 2.x against server 1.3.1 fails with
"Plugin and HTTP server major versions are mismatched".

## PO token server (bgutil 1.3.1)

`npx tsc` is too heavy for 1 GB of RAM, so build on the laptop and install only the runtime
dependencies on the Pi. `canvas` downloads a linux-arm64 prebuild there:

    # laptop
    git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider
    cd bgutil-ytdlp-pot-provider/server && npm install && npx tsc
    # Pi
    git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider ~/bgutil-ytdlp-pot-provider
    rsync -a <laptop>/server/build/ pi:bgutil-ytdlp-pot-provider/server/build/
    cd ~/bgutil-ytdlp-pot-provider/server && npm ci --omit=dev

## Login material (headless)

- Codex: copy `~/.codex/auth.json` (and `config.toml`) from the laptop, chmod 600.
  Both machines now share one session. If one of them ends up logged out after a token
  refresh, copy the file again or run `codex login --device-auth` on the Pi.
- Cookies: export on the laptop with
  `yt-dlp --cookies-from-browser chrome:Default --cookies raw.txt --skip-download <any video>`.
  Keep only the `youtube.com`, `google.com` and `accounts.google.com` lines, copy the result to
  `~/.config/ytdj/cookies.txt` (chmod 600), and set `cookies_file` to it in the config, with
  `cookies_browser = "none"`.

## Services

    cd ~/ytdj && python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e .
    ./packaging/install-service.sh        # ytdj + ytdj-pot user units, linger

- `journalctl --user` needs a persistent journal. Raspberry Pi OS keeps it volatile, so
  `/etc/systemd/journald.conf.d/50-ytdj-persistent.conf` sets `Storage=persistent` and
  `SystemMaxUse=64M`.
- Audio output: `51-ytdj-audio-priority.conf` goes to `~/.config/wireplumber/wireplumber.conf.d/`
  (deploy.sh installs it). Priorities are USB 2000 > 3.5 mm jack 1000 > HDMI 50. Do not use
  `wpctl set-default`: a pinned default overrides the priorities.
- Swap is already zram (`/dev/zram0`, about 900 MB) on this image.

## Web on port 80 (`ytdj-port80.service`)

ytdj runs as an ordinary user and cannot open port 80, so the kernel redirects it: a oneshot
unit loads one nftables table, `ip ytdj_web`, with `tcp dport 80 redirect to :<web_port>` for
connections to the Pi's own addresses (from the network and from the Pi itself). It is a
rewrite inside the kernel, not a proxy — the web still sees the real client address (the PIN
brake and the per-client limits depend on it). Port 8765 keeps working.

- Only that one table is ever touched (created, replaced in one transaction, deleted). No
  filter rules, nothing is blocked, `nftables.service` and `/etc/nftables.conf` are not used.
- The port comes from `web_port` in `~/.config/ytdj/config.toml`. After changing it:
  `sudo systemctl reload ytdj-port80` (the screens notice by themselves when port 80 stops
  answering and show the address with the port again).
- IPv4 only, like the web itself (it listens on `0.0.0.0`) and like `jukebox.local`.
- Look: `sudo nft list table ip ytdj_web`. Dry run of what would be loaded:
  `/usr/local/lib/ytdj/port80.sh print ~/.config/ytdj/config.toml | sudo nft -c -f -`
- Off / rollback: `sudo systemctl disable --now ytdj-port80` (removes the table; the web is
  on `:8765` as before). `YTDJ_PORT80=0 ./packaging/install-service.sh` skips it.
- HTTPS (443) is not served: there is no certificate for a `.local` name. The admin PIN still
  crosses the LAN unencrypted.

The TV screen and the touch panel show an address only after checking that it answers
(`ytdj/panel/webaddr.py`, every few minutes): `jukebox.local`, else `<hostname>.local`, else
the IP — without a port only while port 80 is really served.

## TV over HDMI („právě hraje“, `ytdj-tv.service`)

`install-service.sh` installs `ytdj-tv.service` on a Pi that has `/dev/fb0` and Pillow
(`YTDJ_TV=0` skips it). It is a system unit running as the ordinary user plus group `video`,
draws to the firmware framebuffer and only reads `http://127.0.0.1:8765`. It needs no GPU
driver and no extra GPU memory: `gpu_mem=16` and the commented-out `vc4-kms-v3d` stay as
`slim.sh` left them. After a deploy restart it by hand (`sudo systemctl restart ytdj-tv`);
`deploy.sh` restarts only ytdj.

- The firmware sizes the framebuffer once, at boot. With a TV that is on at boot it is the TV's
  own mode; without one it is 720×480 (composite) and a TV switched on later may show nothing.
  If the TV is often off at boot, the owner can pin HDMI in `/boot/firmware/config.txt`
  (optional, by hand, needs a reboot — the code never edits it):

      hdmi_force_hotplug=1
      hdmi_group=1          # CEA (TVs)
      hdmi_mode=4           # 1280x720 60 Hz; 16 = 1920x1080 60 Hz

- Only the jukebox on the TV, no terminal. The text console draws into the same framebuffer
  (login prompt, cursor, kernel messages), so:
  - `install-service.sh` disables the login prompt on the screen (`getty@tty1`). ssh and the
    serial console are not affected. To get it back:
    `sudo systemctl disable --now ytdj-tv && sudo systemctl enable --now getty@tty1`
    (the two conflict: starting the prompt stops the TV screen).
  - while `ytdj-tv` runs, tty1 is in graphics mode (KD_GRAPHICS, like under a display server):
    the kernel console draws nothing — no cursor, no messages over the picture. systemd gives
    the service tty1 as its controlling terminal (`TTYPath=`, `StandardInput=tty-force`), so
    this needs neither root nor a capability. `tv.console hidden:true` in the events confirms
    it; `hidden:false` carries the reason. When the service stops, tty1 is text again.
  - what still shows: the boot text (rainbow, kernel and systemd messages, a cursor) from
    power-on until the service starts, about 30 s. To quiet that too, the owner can edit
    `/boot/firmware/cmdline.txt` by hand (one line; reboot; nothing in the code touches it):
    either remove `console=tty1` (boot messages only on the serial console), or keep it and
    add `quiet logo.nologo vt.global_cursor_default=0` (short boot text, no logo, no cursor).
- Sound stays on the USB soundbar: HDMI has the lowest WirePlumber priority (see above).

### Clips on the TV (optional, switched on in the browser)

With „Klipy na telce“ on, a playing track that is itself an official video shows its picture
(muted, in step with the music) instead of the now-playing screen. `ytdj-tv` starts a second,
low-priority mpv for it and stops it again; the music's mpv and resolver are not involved.

This needs the GPU driver and the hardware H.264 decoder, which `slim.sh` normally turns off.
The lines in `/boot/firmware/config.txt` (set by hand on the jukebox on 6 Oct, reboot needed;
no script edits this file for video):

    gpu_mem=64
    dtoverlay=vc4-kms-v3d,cma-128
    max_framebuffers=2

- Back to the lean setup: restore the backup (`/boot/firmware/config.txt.ytdj-pred-videem`) or
  run `YTDJ_SLIM_NO_VIDEO=1 packaging/rpi/slim.sh`, then reboot. Without that variable `slim.sh`
  sees `dtoverlay=vc4-kms-v3d` and leaves the three lines alone.
- Nothing is assumed: at runtime the TV process looks for `/dev/dri/card0`, a connected
  `card0-HDMI-A-1` and `/dev/video10`, and the web's switch says what is missing.
- The player's options are fixed in `ytdj/tv/video.py` (`--vo=gpu --gpu-context=drm
  --hwdec=v4l2m2m --drm-draw-plane=overlay --drm-drmprime-video-plane=primary`, H.264 ≤ 720p).
  Never `--vo=drm` or `v4l2m2m-copy`: scaling in software took the Pi to 84 °C and throttling.
- It turns itself off (and says why in the switch's note) at ≥ 78 °C, under 260 MB of free
  memory, on any current throttling/under-voltage bit, when the music's audio drops out while a
  clip runs, when the picture stutters, or when the player crashes — for 5 minutes, doubling up
  to 30. It comes back only below 72 °C and with ≥ 420 MB free.
- Events: `tv.video_start/stop/seek/guard/skip` in `/var/log/ytdj-tv/events.jsonl`,
  `tv.video_switch` and `tv.video_resolve` in the jukebox's own events.
- Logs: `journalctl -u ytdj-tv`, events in `/var/log/ytdj-tv/events.jsonl` (`tv.screen` says the
  size and pixel format it found, `tv.no_screen` why it idles, `tv.paint` what drawing costs).

## Přihlášení (vlastní pro Pi, nezávislé na notebooku)

Pi má vlastní přihlášení ke Codexu i k YouTube — nic nesdílí s notebookem,
takže odhlášení nebo obnova tokenů tam Pi neodstřihne. Když něco vyprší,
stačí z notebooku spustit jeden skript:

| co nefunguje | příkaz (z kořene repa na notebooku) |
|---|---|
| DJ neodpovídá, log hlásí nepřihlášený Codex | `packaging/rpi/codex-login.sh` — Pi ukáže odkaz a kód, zadáš ho v prohlížeči (třeba na mobilu) |
| hraje jen ~130 kb/s, `./run.sh --check-audio` nehlásí Premium | `packaging/rpi/youtube-login.sh` — otevře Chrome s čistým dočasným profilem, přihlásíš se, okno zavřeš (neodhlašovat!), cookies odejdou na Pi a profil se smaže |

Oba skripty berou `PI=lacina@<adresa>` (výchozí 10.42.0.149, přes Wi-Fi
192.168.0.24).
