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

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
    echo "zapínám linger (spuštění bez přihlášení) — vyžádá si heslo:"
    sudo loginctl enable-linger "$USER"
fi

systemctl --user daemon-reload
[ -f "$unit_dir/ytdj-pot.service" ] && systemctl --user enable --now ytdj-pot.service
systemctl --user enable --now ytdj.service
echo
systemctl --user --no-pager --lines=0 status ytdj.service || true
echo
echo "log:     journalctl --user -u ytdj -f"
echo "web:     http://127.0.0.1:8765"
