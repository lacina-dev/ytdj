# ytdj on a Raspberry Pi 3 (Raspberry Pi OS Lite 64-bit, Debian 13)

One-time setup as done on the jukebox Pi (user `lacina`). Afterwards
`packaging/rpi/deploy.sh` (run on the laptop) syncs the code and refreshes the venv.

## Packages

    sudo apt-get install --no-install-recommends mpv ffmpeg pipewire pipewire-pulse \
        pipewire-alsa wireplumber alsa-utils rtkit nodejs npm python3-venv python3-dev git curl \
        python3-spidev python3-libgpiod python3-pil python3-numpy fonts-dejavu-core libsecret-tools

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
