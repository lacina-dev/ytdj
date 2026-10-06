#!/usr/bin/env bash
# Druhé jméno jukeboxu v místní síti (mDNS): vedle <hostname>.local i jukebox.local.
#
# Avahi umí vyhlásit jen jedno jméno stroje; další jde přidat jako adresní záznam
# (avahi-publish -a, balík avahi-utils). Je to samostatný klient avahi: jeho záznam
# žije ve vlastní skupině, takže ať se mu stane cokoli (kolize jména, pád, odmítnutí),
# hlavního jména stroje se to netýká. Záznam platí, jen dokud proces běží, a váže se
# na adresu — proto smyčka: při změně adresy (DHCP, Wi-Fi ↔ kabel) se vyhlásí znovu.
#
# Když avahi-publish hned skončí (avahi neběží, nepustí nás, jméno už někdo má),
# nezkouší se to dokola: další pokus až po pauze, která se zdvojuje (15 s … 10 min).
# Pi 6. 10.: první verze zkoušela každých 15 s donekonečna.
#
#   mdns-alias.sh [jméno]        # výchozí jukebox.local
set -u
NAME="${1:-${YTDJ_MDNS_ALIAS:-jukebox.local}}"
CHECK="${YTDJ_MDNS_CHECK:-15}"          # s — jak často hlídat změnu adresy
BACKOFF_MIN="${YTDJ_MDNS_BACKOFF:-15}"  # s — první pauza po neúspěchu
BACKOFF_MAX="${YTDJ_MDNS_BACKOFF_MAX:-600}"
HEALTHY="${YTDJ_MDNS_HEALTHY:-60}"      # s — po takové době běhu se pauza vrací na začátek

current_ip() {
    # adresa, kterou stroj mluví do sítě (bez odeslání jediného paketu)
    ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
}

now() { date +%s; }

pid=""
cleanup() { [ -n "$pid" ] && kill "$pid" 2>/dev/null; exit 0; }
trap cleanup TERM INT

have=""
started=0
retry_at=0
backoff="$BACKOFF_MIN"
while :; do
    ip="$(current_ip)"
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
        # vyhlašování skončilo samo — to nemá; počkat, než se zkusí znovu
        wait "$pid" 2>/dev/null
        code=$?
        pid=""
        if [ $(( $(now) - started )) -ge "$HEALTHY" ]; then
            backoff="$BACKOFF_MIN"
        fi
        retry_at=$(( $(now) + backoff ))
        echo "mdns: vyhlášení $NAME skončilo (kód $code) — další pokus za $backoff s"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
    fi
    if [ "$ip" != "$have" ]; then
        # jiná adresa: starý záznam pryč a nový hned (pauza po neúspěchu se neruší)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null && wait "$pid" 2>/dev/null
        pid=""
        have="$ip"
        [ -z "$ip" ] && echo "mdns: bez adresy, čekám"
    fi
    if [ -z "$pid" ] && [ -n "$ip" ] && [ "$(now)" -ge "$retry_at" ]; then
        echo "mdns: $NAME -> $ip"
        # -R: bez reverzního záznamu — ten k téhle adrese už patří hlavnímu jménu
        avahi-publish -a -R "$NAME" "$ip" &
        pid=$!
        started="$(now)"
    fi
    sleep "$CHECK" &
    wait $!
done
