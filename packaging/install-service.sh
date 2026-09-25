#!/usr/bin/env bash
# Nainstaluje ytdj jako uživatelskou službu systemd.
#
# Uživatelská (ne systémová) proto, že aplikace potřebuje zvuk, konfiguraci
# a přihlášení Codexu z domovského adresáře. Aby běžela i bez přihlášení —
# tedy jako jukebox na stroji, ke kterému se nikdo nehlásí — se uživateli
# zapne linger: systemd pak jeho manažer nastartuje už při bootu.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/ytdj.service"

mkdir -p "$unit_dir"
sed "s|@INSTALL_DIR@|$repo|g" "$repo/packaging/ytdj.service" > "$unit"
echo "unit:    $unit"

# PO token provider — volitelný. Bez něj YouTube nepustí formáty ke klientům,
# které nesou přihlášení, takže hraje 130 kb/s bez ohledu na Premium.
provider="${BGUTIL_HOME:-$HOME/bgutil-ytdlp-pot-provider}"
node_bin="$(command -v node || ls -d "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -V | tail -1)"
if [ -f "$provider/server/build/main.js" ] && [ -n "$node_bin" ]; then
    # Server se váže natvrdo na "::", tedy na všechna rozhraní, a volbu pro
    # adresu nemá (upstream to plánuje až v příští major verzi). Na jukeboxu
    # v síti by to komukoli okolo dovolilo razit tokeny na tvůj účet, tak to
    # přepíšeme na localhost. Po aktualizaci provideru spusť tenhle skript
    # znovu — záplata se aplikuje na sestavený soubor.
    sed -i -e 's|host: "::"|host: "127.0.0.1"|' -e 's|host: "0\.0\.0\.0"|host: "127.0.0.1"|' \
        "$provider/server/build/main.js"

    sed -e "s|@PROVIDER@|$provider|g" -e "s|@NODE@|$node_bin|g" \
        "$repo/packaging/ytdj-pot.service" > "$unit_dir/ytdj-pot.service"
    echo "unit:    $unit_dir/ytdj-pot.service"
else
    echo "pozn.:   PO token provider není sestavený — Premium formáty zůstanou nedostupné."
    echo "         Návod: README, sekce Premium audio quality."
fi

# Dotykový panel (KeDei 3,5" na GPIO) — jen na Raspberry Pi s Pillow ve venvu
# (extra `panel`, na Pi python3-pil z aptu přes --system-site-packages).
# YTDJ_PANEL=0 ho vynechá, YTDJ_PANEL=1 vynutí. Na notebooku bez displeje
# by se služba jen donekonečna restartovala.
panel=no
model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
case "${YTDJ_PANEL:-auto}" in
    1) panel=yes ;;
    auto) [[ "$model" == Raspberry\ Pi* ]] && "$repo/.venv/bin/python" -c "import PIL" 2>/dev/null && panel=yes ;;
esac
if [ "$panel" = yes ]; then
    # Systémová služba: ovladač potřebuje /dev/mem, tedy roota.
    sed -e "s|@INSTALL_DIR@|$repo|g" -e "s|@HOME@|$HOME|g" "$repo/packaging/ytdj-panel.service" |
        sudo tee /etc/systemd/system/ytdj-panel.service > /dev/null
    sudo mkdir -p /etc/ytdj
    echo "unit:    /etc/systemd/system/ytdj-panel.service"
    # Kernelový ovladač SPI0 by se s naším přímým přístupem k registrům
    # přetahoval o piny — natrvalo ho vypneme (projeví se po restartu).
    bootcfg=/boot/firmware/config.txt
    [ -f "$bootcfg" ] || bootcfg=/boot/config.txt
    if [ -f "$bootcfg" ] && ! grep -qx 'dtparam=spi=off' "$bootcfg"; then
        sudo sed -i -e '/^dtparam=spi=on$/d' -e '/^dtoverlay=spi0-/d' "$bootcfg"
        echo 'dtparam=spi=off' | sudo tee -a "$bootcfg" > /dev/null
        echo "pozn.:   do $bootcfg přidáno dtparam=spi=off (platí po restartu)"
    fi
fi

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
    echo "zapínám linger (spuštění bez přihlášení) — vyžádá si heslo:"
    sudo loginctl enable-linger "$USER"
fi

systemctl --user daemon-reload
[ -f "$unit_dir/ytdj-pot.service" ] && systemctl --user enable --now ytdj-pot.service
systemctl --user enable --now ytdj.service
if [ "$panel" = yes ]; then
    sudo systemctl daemon-reload
    sudo systemctl enable ytdj-panel.service
    sudo systemctl restart ytdj-panel.service
fi
echo
systemctl --user --no-pager --lines=0 status ytdj.service || true
echo
echo "log:     journalctl --user -u ytdj -f"
[ "$panel" = yes ] && echo "panel:   journalctl -u ytdj-panel -f"
echo "web:     http://127.0.0.1:8765"
