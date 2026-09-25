#!/usr/bin/env bash
# Vlastní přihlášení Codexu (mozek DJ) na Pi — nezávislé na notebooku.
#
#   packaging/rpi/codex-login.sh             # PI=lacina@192.168.0.24 přepíše cíl
#
# Pi ukáže odkaz a jednorázový kód; otevři odkaz kdekoli (třeba na mobilu),
# přihlas se účtem ChatGPT a kód zadej. Tokeny pak má jen Pi a samo si je
# obnovuje — s notebookem se nepřetahuje. Spusť znovu, když ytdj hlásí, že
# Codex není přihlášený.
set -euo pipefail
PI="${PI:-lacina@10.42.0.149}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lacina_deploy}"
ssh -t -i "$SSH_KEY" -o ConnectTimeout=10 "$PI" '
    export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
    codex login --device-auth && codex login status'
