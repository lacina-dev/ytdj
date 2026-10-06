#!/usr/bin/env bash
# Druhé jméno jukeboxu v místní síti (mDNS): vedle <hostname>.local i jukebox.local.
#
# Avahi umí vyhlásit jen jedno jméno stroje; další jde přidat jako adresní záznam
# (avahi-publish -a, balík avahi-utils). Záznam platí, jen dokud proces běží, a váže
# se na adresu — proto smyčka: při změně adresy (DHCP, Wi-Fi ↔ kabel) se vyhlásí znovu.
#
#   mdns-alias.sh [jméno]        # výchozí jukebox.local
set -u
NAME="${1:-${YTDJ_MDNS_ALIAS:-jukebox.local}}"
CHECK="${YTDJ_MDNS_CHECK:-15}"   # s — jak často hlídat změnu adresy

current_ip() {
    # adresa, kterou stroj mluví do sítě (bez odeslání jediného paketu)
    ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
}

pid=""
cleanup() { [ -n "$pid" ] && kill "$pid" 2>/dev/null; exit 0; }
trap cleanup TERM INT

have=""
while :; do
    ip="$(current_ip)"
    if [ "$ip" != "$have" ] || { [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; }; then
        [ -n "$pid" ] && kill "$pid" 2>/dev/null && wait "$pid" 2>/dev/null
        pid=""
        have="$ip"
        if [ -n "$ip" ]; then
            echo "mdns: $NAME -> $ip"
            # -R: nehlásit kolizi kvůli reverznímu záznamu, který už patří hlavnímu jménu
            avahi-publish -a -R "$NAME" "$ip" &
            pid=$!
        else
            echo "mdns: bez adresy, čekám"
        fi
    fi
    sleep "$CHECK" &
    wait $!
done
