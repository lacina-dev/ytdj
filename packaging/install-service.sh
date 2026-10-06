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

# Obrazovka „právě hraje“ na telce přes HDMI — jen na Raspberry Pi s Pillow
# a s framebufferem (/dev/fb0). YTDJ_TV=0 ji vynechá, YTDJ_TV=1 vynutí.
# Systémová služba pod obyčejným uživatelem (skupina video), ne root. Do
# config.txt se tu NESAHÁ: když telka při startu Pi neběží, viz
# packaging/rpi/NOTES.md (volitelné hdmi_force_hotplug).
tv=no
case "${YTDJ_TV:-auto}" in
    1) tv=yes ;;
    auto) [[ "$model" == Raspberry\ Pi* ]] && [ -e /dev/fb0 ] \
              && "$repo/.venv/bin/python" -c "import PIL" 2>/dev/null && tv=yes ;;
esac
if [ "$tv" = yes ]; then
    sed -e "s|@INSTALL_DIR@|$repo|g" -e "s|@HOME@|$HOME|g" -e "s|@USER@|$USER|g" \
        "$repo/packaging/ytdj-tv.service" |
        sudo tee /etc/systemd/system/ytdj-tv.service > /dev/null
    echo "unit:    /etc/systemd/system/ytdj-tv.service"
    # Na telce jen jukebox, žádný přihlašovací terminál: výzva na tty1 kreslí
    # do stejné obrazovky. SSH a sériová konzole zůstávají. Zpátky:
    #   sudo systemctl disable --now ytdj-tv && sudo systemctl enable --now getty@tty1
    sudo systemctl disable --now getty@tty1.service 2>/dev/null || true
    echo "pozn.:   přihlašovací výzva na obrazovce (getty@tty1) je vypnutá — na telce je jen jukebox"
fi

# Sandbox Codexu: na Linuxu izoluje příkazy přes bubblewrap, bez něj read-only
# sandbox neplatí a přání od kohokoli ze sítě jdou modelu bez izolace.
if ! command -v bwrap > /dev/null && command -v apt-get > /dev/null; then
    echo "instaluji bubblewrap (sandbox Codexu):"
    sudo apt-get install -y bubblewrap
fi

# Web i na běžném portu 80 (http://jukebox.local bez :8765) — jen na jukeboxu
# (Raspberry Pi) s nftables. Jádro přesměruje port 80 na port webu z config.toml;
# sahá se jen na vlastní tabulku "ip ytdj_web" a port webu funguje dál.
# YTDJ_PORT80=0 to vynechá. Zrušení: sudo systemctl disable --now ytdj-port80
port80=no
if [ "${YTDJ_PORT80:-1}" != 0 ] && [[ "$model" == Raspberry\ Pi* ]] \
        && { command -v nft > /dev/null || [ -x /usr/sbin/nft ]; }; then
    port80=yes
    sudo install -D -m 755 "$repo/packaging/rpi/port80.sh" /usr/local/lib/ytdj/port80.sh
    sed -e "s|@HOME@|$HOME|g" "$repo/packaging/ytdj-port80.service" |
        sudo tee /etc/systemd/system/ytdj-port80.service > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable ytdj-port80.service
    # běží-li už, jen znovu nahrát: pravidla se nahradí naráz, port 80 nevypadne
    sudo systemctl reload-or-restart ytdj-port80.service
    echo "unit:    /etc/systemd/system/ytdj-port80.service (web i na portu 80)"
fi

# Druhé jméno v síti: vedle <hostname>.local i jukebox.local (jen na jukeboxu
# s avahi). Skript jde mimo domovský adresář — služba do něj nevidí. Běží pod
# běžným uživatelem (ne DynamicUser: toho si D-Bus na Pi nedohledá a avahi ho
# nepustí). YTDJ_MDNS_ALIAS=0 druhé jméno vynechá.
if [ "${YTDJ_MDNS_ALIAS:-1}" != 0 ] && [[ "$model" == Raspberry\ Pi* ]] \
        && systemctl is-active -q avahi-daemon 2>/dev/null; then
    command -v avahi-publish > /dev/null || sudo apt-get install -y avahi-utils
    sudo install -D -m 755 "$repo/packaging/rpi/mdns-alias.sh" /usr/local/lib/ytdj/mdns-alias.sh
    sed -e "s|@USER@|$USER|g" "$repo/packaging/ytdj-mdns-alias.service" |
        sudo tee /etc/systemd/system/ytdj-mdns-alias.service > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now ytdj-mdns-alias.service
    echo "unit:    /etc/systemd/system/ytdj-mdns-alias.service (jukebox.local)"
    # Volitelně: opakované ohlašování jmen pro Wi-Fi, která ztrácí multicast
    # (jména .local pak „chvíli fungují a přestanou“). Jen na výslovné přání:
    #   YTDJ_MDNS_ANNOUNCE=1 ./packaging/install-service.sh
    # Zrušení: sudo systemctl disable --now ytdj-mdns-announce
    if [ "${YTDJ_MDNS_ANNOUNCE:-0}" = 1 ]; then
        sudo install -D -m 755 "$repo/packaging/rpi/mdns-announce.py" /usr/local/lib/ytdj/mdns-announce.py
        sed -e "s|@USER@|$USER|g" "$repo/packaging/ytdj-mdns-announce.service" |
            sudo tee /etc/systemd/system/ytdj-mdns-announce.service > /dev/null
        sudo systemctl daemon-reload
        sudo systemctl enable ytdj-mdns-announce.service
        sudo systemctl restart ytdj-mdns-announce.service
        echo "unit:    /etc/systemd/system/ytdj-mdns-announce.service (opakované ohlašování jmen)"
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
if [ "$tv" = yes ]; then
    sudo systemctl daemon-reload
    sudo systemctl enable ytdj-tv.service
    sudo systemctl restart ytdj-tv.service
fi
echo
systemctl --user --no-pager --lines=0 status ytdj.service || true
echo
echo "log:     journalctl --user -u ytdj -f"
[ "$panel" = yes ] && echo "panel:   journalctl -u ytdj-panel -f"
[ "$tv" = yes ] && echo "telka:   journalctl -u ytdj-tv -f"
echo "web:     http://127.0.0.1:8765"
