#!/usr/bin/env bash
# Web jukeboxu i na běžném portu 80 (http://jukebox.local bez :8765).
#
# ytdj běží jako obyčejný uživatel a port 80 si otevřít nesmí. Místo toho jádro
# příchozí spojení na port 80 přesměruje na port webu (nftables, "redirect").
# Je to přepis cíle uvnitř jádra, žádný prostředník: web dál vidí skutečnou
# adresu toho, kdo se připojil (podle ní se brzdí PIN a počítají limity), a port
# 8765 funguje dál.
#
# Sahá se jen na vlastní tabulku "ip ytdj_web": nic cizího se nemaže ani nemění,
# žádné pravidlo nic nezakazuje (nikoho to neodřízne) a opakované spuštění ji
# jen nahradí — naráz, takže port 80 mezitím nevypadne. Port webu se čte
# z config.toml jukeboxu (web_port), ať není napsaný na dvou místech.
#
#   port80.sh start [config.toml]    nahrát / nahradit pravidla
#   port80.sh stop                   odebrat naši tabulku
#   port80.sh print [config.toml]    jen vypsat, co by se nahrálo
set -eu
TABLE=ytdj_web
NFT="${YTDJ_NFT:-nft}"
cmd="${1:-}"
config="${2:-}"

web_port() {
    local port=""
    if [ -n "$config" ] && [ -r "$config" ]; then
        # jen číslo z řádku `web_port = N` — nic jiného se z cizího souboru nebere
        port="$(sed -n 's/^[[:space:]]*web_port[[:space:]]*=[[:space:]]*\([0-9]\{1,5\}\)[[:space:]]*\(#.*\)\{0,1\}$/\1/p' "$config" | head -1)"
    fi
    echo "${port:-8765}"
}

rules() {
    # první dva řádky: tabulku založit (kdyby nebyla) a smazat — spolu se zbytkem
    # jedna transakce, takže jde o nahrazení bez mezery
    cat <<EOF
table ip $TABLE
delete table ip $TABLE
table ip $TABLE {
    chain prerouting {
        type nat hook prerouting priority -100; policy accept;
        fib daddr type local tcp dport 80 redirect to :$1
    }
    chain output {
        type nat hook output priority -100; policy accept;
        fib daddr type local tcp dport 80 redirect to :$1
    }
}
EOF
}

remove() {
    printf 'table ip %s\ndelete table ip %s\n' "$TABLE" "$TABLE"
}

case "$cmd" in
    start|print)
        port="$(web_port)"
        if [ "$port" = 80 ]; then
            echo "port80: web už na portu 80 poslouchá sám — není co přesměrovat"
            [ "$cmd" = start ] && remove | "$NFT" -f -
            exit 0
        fi
        if [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
            echo "port80: web_port $port není port webu jukeboxu (1024–65535) — nic nenastavuji" >&2
            exit 1
        fi
        if [ "$cmd" = print ]; then
            rules "$port"
        else
            rules "$port" | "$NFT" -f -
            echo "port80: 80 -> $port (tabulka ip $TABLE)"
        fi
        ;;
    stop)
        remove | "$NFT" -f -
        echo "port80: tabulka ip $TABLE odebrána"
        ;;
    *)
        echo "použití: $0 start|stop|print [config.toml]" >&2
        exit 2
        ;;
esac
