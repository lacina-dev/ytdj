#!/usr/bin/env bash
# Noční kontrola a aktualizace yt-dlp na jukeboxu (F-PROVOZ-14).
#
# Proč: 23. 11. 2026 YouTube změnil přehrávač, yt-dlp 2026.08.19 nevyřešil
# novou JS výzvu, YouTube dával jen obrázky náhledů a hudba dva dny stála.
# Vlastník 25. 11.: Pi si má yt-dlp jednou týdně v noci zkusit aktualizovat
# samo a jukebox restartovat, jen když nová verze opravdu funguje.
#
# Spouští ho denně ve 3:40 uživatelský časovač ytdj-ytdlp-update.timer
# (instaluje packaging/rpi/deploy.sh). Ručně:
#
#   packaging/rpi/ytdlp-update.sh             # jako z časovače
#   YTDLP_UPDATE_FORCE=1 packaging/rpi/ytdlp-update.sh   # týdenní kontrolu hned
#
# Co dělá (podrobně v NOTES.md, „When YouTube changes its player“):
#  1. Zdraví NAINSTALOVANÉHO yt-dlp na pevných skladbách se stejnými volbami,
#     jaké dostává resolver (cookies, node, ejs:github…). Zdravé = YouTube
#     nabídne formát „audio only“ (251/140/774…) a yt-dlp z něj opravdu
#     stáhne prvních 256 kB; jen obrázky náhledů nebo video bez zvuku ne.
#  2. Jednou za 7 dní (od posledního úspěšného porovnání s PyPI), nebo hned,
#     když je nainstalovaný nemocný: nejnovější vydání z PyPI se vyzkouší
#     bokem (`uv tool run`, se stejnými pluginy), instalace se nedotkne.
#  3. Je-li novější a zdravé: počká se, až nic nehraje (nejdél do 6:00),
#     nainstaluje se (`uv tool install --force 'yt-dlp>=nová'` se stejnými
#     --with), ověří se pluginy i zdraví. Nemocné → vrátí se stará verze.
#  4. Restart ytdj jen po úspěšné změně verze a jen když nic nehraje (nebo
#     jukebox sám hlásí výpadek). Nestihne-li se, zůstane poznámka ve stavu
#     a restart se zkusí příští noc.
#  Každý běh zapíše jeden řádek `ytdlp.update` do provozního logu.
#
# Nikdy nevypisuje výstup yt-dlp (obsahuje cestu ke cookies, případně PO
# token, a adresy streamů) — jen čísla formátů a krátké známé hlášky.
set -euo pipefail

T0=$(date +%s%3N)
YTDJ_DIR="${YTDJ_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PY="${YTDLP_UPDATE_PY:-$YTDJ_DIR/.venv/bin/python}"
UV="${YTDLP_UPDATE_UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
YTDLP="${YTDLP_UPDATE_BIN:-$(command -v yt-dlp || echo "$HOME/.local/bin/yt-dlp")}"
CURL="${YTDLP_UPDATE_CURL:-curl}"
SYSTEMCTL="${YTDLP_UPDATE_SYSTEMCTL:-systemctl}"
STATUS_URL="${YTDLP_UPDATE_STATUS_URL:-http://127.0.0.1:8765/api/status}"
PYPI_URL="${YTDLP_UPDATE_PYPI_URL:-https://pypi.org/pypi/yt-dlp/json}"
STATE_DIR="${YTDLP_UPDATE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/ytdj}"
STATE="$STATE_DIR/ytdlp-update.state"
# Zkušební skladby (music.youtube.com): stačí, když projde jedna — zmizelé
# video ani výpadek jedné skladby tak nevydávají yt-dlp za nemocné.
#   dQw4w9WgXcQ  Rick Astley – Never Gonna Give You Up (oficiální, 2009;
#                resolver na něm staví šablonu voleb)
#   ljUtuoFt-8c  zkušební skladba diagnostiky (ytdj/diagnose.py TEST_TRACK)
#   AxuTd9rwEHQ  Foo Fighters – Everlong (hrálo na Pi, oficiální nahrávka)
VIDEOS="${YTDLP_UPDATE_VIDEOS:-dQw4w9WgXcQ ljUtuoFt-8c AxuTd9rwEHQ}"
# Pluginy jako v `uv tool install` (NOTES.md). bgutil MUSÍ zůstat 1.3.1 —
# plugin 2.x se serverem 1.3.1 hlásí "major versions are mismatched".
WITH="${YTDLP_UPDATE_WITH:-secretstorage bgutil-ytdlp-pot-provider==1.3.1}"
WEEK_S="${YTDLP_UPDATE_WEEK_S:-604800}"
DEADLINE="${YTDLP_UPDATE_DEADLINE:-06:00}"   # do kdy se čeká, až nic nehraje
POLL_S="${YTDLP_UPDATE_POLL_S:-60}"
CHECK_TIMEOUT="${YTDLP_UPDATE_CHECK_TIMEOUT:-180}"   # s na jednu skladbu
STREAM_BYTES="${YTDLP_UPDATE_STREAM_BYTES:-262144}"   # tolik zvuku musí přijít (~13 s)
FORCE="${YTDLP_UPDATE_FORCE:-}"

log() { echo "ytdlp-update: $*"; }

# Hudba má přednost: nejnižší priorita procesoru i disku (i při ručním běhu).
renice -n 19 -p $$ > /dev/null 2>&1 || true
ionice -c3 -p $$ > /dev/null 2>&1 || true

mkdir -p "$STATE_DIR"
exec 9> "$STATE_DIR/ytdlp-update.lock"
if ! flock -n 9; then
    log "už běží jiný běh — končím"
    exit 0
fi

umask 077
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ytdlp-update.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

helper() { PYTHONPATH="$YTDJ_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m ytdj.ytdlp_update "$@"; }
now() { date +%s; }

state_get() { [ -f "$STATE" ] && sed -n "s/^$1=//p" "$STATE" | tail -n 1 || true; }
state_set() {
    { [ -f "$STATE" ] && grep -v "^$1=" "$STATE" || true; echo "$1=$2"; } > "$STATE.tmp"
    mv "$STATE.tmp" "$STATE"
}
state_del() {
    [ -f "$STATE" ] || return 0
    { grep -v "^$1=" "$STATE" || true; } > "$STATE.tmp"
    mv "$STATE.tmp" "$STATE"
}

read -r -a WITH_ARR <<< "$WITH"
UV_WITH=()
for w in "${WITH_ARR[@]}"; do UV_WITH+=(--with "$w"); done

# Volby yt-dlp z configu jukeboxu — stejné, jaké dostává resolver.
YT_ARGS=()
if ! helper args > "$WORK/args"; then
    log "volby yt-dlp z configu jukeboxu nejdou načíst ($PY) — končím"
    exit 1
fi
while IFS= read -r -d '' a; do YT_ARGS+=("$a"); done < <(cat "$WORK/args"; printf '\0')
[ "${#YT_ARGS[@]}" -gt 0 ] && [ -z "${YT_ARGS[-1]}" ] && unset 'YT_ARGS[-1]'
# Cookies jen jako kopie: yt-dlp jar na konci zapisuje zpátky a zkoušená
# (třeba vadná) verze nemá sahat na jar, ze kterého hraje resolver.
for i in "${!YT_ARGS[@]}"; do
    if [ "${YT_ARGS[$i]}" = --cookies ] && [ -n "${YT_ARGS[$((i + 1))]:-}" ]; then
        if cp "${YT_ARGS[$((i + 1))]}" "$WORK/cookies.txt" 2> /dev/null; then
            YT_ARGS[$((i + 1))]="$WORK/cookies.txt"
        fi
    fi
done

# ---- zdraví -------------------------------------------------------------

# health <popis> <příkaz…> — nastaví H_OK (0/1), H_FMTS (zvukové formáty),
# H_FMT (vybraný), H_VID, H_POT (plugin bgutil načtený), H_REASON.
# Zdravé = YouTube nabídne formát „audio only“ A yt-dlp z něj opravdu stáhne
# první kus (STREAM_BYTES). Samotný seznam nestačí: stará verze ho na PC
# 25. 11. vypsala i s nevyřešenou JS výzvou. Stahuje yt-dlp sám (jeho
# hlavičky, PO token), adresa streamu nikam nejde.
health() {
    local label=$1; shift
    H_OK=0 H_FMTS="" H_FMT="" H_VID="" H_POT=false H_REASON=""
    local vid err rc bytes fmts reason
    for vid in $VIDEOS; do
        err="$WORK/check.err"
        rm -f "$WORK/table" "$WORK/fmt"
        bytes=$( { timeout -k 10 "$CHECK_TIMEOUT" "$@" "${YT_ARGS[@]}" -v -f bestaudio \
                       --print-to-file formats_table "$WORK/table" \
                       --print-to-file format_id "$WORK/fmt" \
                       --no-simulate --no-part -o - \
                       "https://music.youtube.com/watch?v=$vid" 2> "$err" < /dev/null; \
                   echo $? > "$WORK/rc"; } | head -c "$STREAM_BYTES" | wc -c) || true
        rc=$(cat "$WORK/rc" 2> /dev/null || echo "?")
        grep -q 'PO Token Providers:.*bgutil' "$err" && H_POT=true
        fmts=$(awk '/audio only/ && $1 ~ /^[0-9A-Za-z_-]+$/ {print $1}' "$WORK/table" 2> /dev/null \
               | head -n 12 | paste -sd, -)
        if [ -n "$fmts" ] && [ "${bytes:-0}" -ge "$STREAM_BYTES" ]; then
            H_OK=1 H_FMTS=$fmts H_VID=$vid H_REASON=""
            H_FMT=$(head -n 1 "$WORK/fmt" 2> /dev/null | tr -dc '0-9A-Za-z_-' | cut -c1-20)
            rm -f "$err"
            log "$label: zdravé ($vid: formát $H_FMT ze $fmts, stream se otevřel)"
            return 0
        fi
        # známá hláška; přednost má ERROR (skutečná příčina) před varováním
        reason=$( { grep '^ERROR' "$err"; grep -v '^ERROR' "$err"; } | grep -o -m 1 -E 'n challenge solving failed|Signature solving failed|Requested format is not available|Sign in to confirm[a-z ]*|Video unavailable|This video is not available|No video formats found|HTTP Error [0-9]{3}[A-Za-z ]*|Unable to download API page|Failed to resolve|Temporary failure in name resolution|Connection refused' | head -n 1 || true)
        [ "$rc" = 124 ] && reason="timeout ${CHECK_TIMEOUT}s"
        if [ -z "$reason" ]; then
            if [ -z "$fmts" ]; then reason="rc=$rc, žádný formát audio only"
            else reason="stream se neotevřel (${bytes:-0} B)"; fi
        fi
        H_REASON="$vid: $reason"
        rm -f "$err"
        log "$label: $vid bez zvuku ($reason)"
    done
    return 1
}

installed_version() { "$YTDLP" --version 2> /dev/null | head -n 1 | tr -dc '0-9.' || true; }

# Jsou v prostředí nástroje všechny pluginy (bgutil přesně 1.3.1)?
plugins_ok() {
    local tooldir site w name ver
    tooldir="$("$UV" tool dir 2> /dev/null)/yt-dlp"
    site=$(ls -d "$tooldir"/lib/python3*/site-packages 2> /dev/null | head -n 1)
    [ -n "$site" ] || return 1
    for w in "${WITH_ARR[@]}"; do
        name=$(printf '%s' "${w%%[=<>!~]*}" | tr '-' '_')
        if [[ $w == *==* ]]; then
            ver=${w#*==}
            [ -n "$(find "$site" -maxdepth 1 -iname "$name-$ver.dist-info" | head -n 1)" ] || return 1
        else
            [ -n "$(find "$site" -maxdepth 1 -iname "$name-*.dist-info" | head -n 1)" ] || return 1
        fi
    done
}

install_spec() {  # install_spec <požadavek>  (yt-dlp>=X / yt-dlp==X)
    "$UV" tool install --force "$1" "${UV_WITH[@]}" > "$WORK/uv.out" 2>&1 \
        || { log "uv tool install $1 selhal: $(tail -n 1 "$WORK/uv.out" | cut -c1-200)"; return 1; }
}

# ---- přehrávání ---------------------------------------------------------

service_active() { "$SYSTEMCTL" --user is-active -q ytdj.service 2> /dev/null; }

play_state() {  # idle / outage / busy / unknown
    local j
    j=$("$CURL" -fsS -m 5 "$STATUS_URL" 2> /dev/null) || { echo unknown; return 0; }
    printf '%s' "$j" | helper status 2> /dev/null || echo unknown
}

START=$(now)
if [ -n "${YTDLP_UPDATE_WAIT_S:-}" ]; then
    WAIT_UNTIL=$((START + YTDLP_UPDATE_WAIT_S))
else
    WAIT_UNTIL=$(date -d "today $DEADLINE" +%s 2> /dev/null || echo "$START")
    [ "$WAIT_UNTIL" -gt "$START" ] || WAIT_UNTIL=$START   # po 6:00 se nečeká
fi

# wait_quiet — 0, když smí změna proběhnout (nic nehraje, výpadek, ytdj
# neběží); 1, když do WAIT_UNTIL pořád hraje. Výsledek v QUIET.
wait_quiet() {
    local s
    while true; do
        if ! service_active; then QUIET=not_running; return 0; fi
        s=$(play_state)
        case $s in idle | outage) QUIET=$s; return 0 ;; esac
        QUIET=$s
        [ "$(now)" -lt "$WAIT_UNTIL" ] || return 1
        sleep "$POLL_S"
    done
}

# restart_when_quiet — RESTART = done / not_running / skipped_playing
restart_when_quiet() {
    if wait_quiet; then
        if [ "$QUIET" = not_running ]; then
            RESTART=not_running
        else
            log "restartuji ytdj ($QUIET) — resolver načte novou verzi"
            "$SYSTEMCTL" --user restart ytdj.service
            RESTART=done
        fi
        state_del pending_restart
        state_del upgraded_at
    else
        log "hudba hraje — restart příští noc (poznámka ve stavu)"
        RESTART=skipped_playing
        state_set pending_restart 1
    fi
}

# ---- rozhodnutí ---------------------------------------------------------

EV=()
ACTION=none
RESTART=none
OLD=$(installed_version)
NEW=""
NEWEST=""
REASON=""
WEEKLY=false

finish() {
    local took=$(( $(date +%s%3N) - T0 ))
    helper event ytdlp.update "action=$ACTION" "old=$OLD" "new=$NEW" "newest=$NEWEST" \
        "weekly:=$WEEKLY" "restart=$RESTART" "reason=$REASON" "took_ms:=$took" "${EV[@]}" \
        2> /dev/null || log "(zápis do provozního logu selhal)"
    log "hotovo: $ACTION (yt-dlp ${OLD:-?}${NEW:+ → $NEW}, restart $RESTART${REASON:+, $REASON}) za $((took / 1000)) s"
    exit 0
}

# Restart, který minulý běh nestihl (hrála hudba).
if [ -n "$(state_get pending_restart)" ]; then
    up=$(state_get upgraded_at)
    ts=$("$SYSTEMCTL" --user show ytdj.service -p ActiveEnterTimestamp --value 2> /dev/null || true)
    started=$(date -d "$ts" +%s 2> /dev/null || echo 0)
    if [ -n "$up" ] && [ "$started" -ge "$up" ] 2> /dev/null; then
        log "ytdj už od aktualizace běží znovu — restart netřeba"
        state_del pending_restart
        state_del upgraded_at
    else
        restart_when_quiet
        EV+=("pending_restart:=true")
        [ "$RESTART" = skipped_playing ] && ACTION=skipped_playing || ACTION=restarted
    fi
fi

if health "nainstalovaný ${OLD:-?}" "$YTDLP"; then BEFORE_OK=1; else BEFORE_OK=0; fi
EV+=("before_ok:=$([ $BEFORE_OK = 1 ] && echo true || echo false)" "before_formats=$H_FMTS"
     "before_format=$H_FMT" "before_video=$H_VID" "before_pot:=$H_POT" "before_error=$H_REASON")

last=$(state_get last_check)
if [ -n "$FORCE" ] || [ -z "$last" ] || [ $(( $(now) - last )) -ge "$WEEK_S" ]; then
    WEEKLY=true
fi
if [ $BEFORE_OK = 1 ] && [ $WEEKLY = false ]; then
    finish
fi

if ! pypi=$("$CURL" -fsS -m 30 "$PYPI_URL" 2> /dev/null) \
        || ! NEWEST=$(printf '%s' "$pypi" | helper pypi 2> /dev/null); then
    NEWEST=""
    ACTION=failed REASON="PyPI nedostupné"
    finish
fi
if ! helper newer "$NEWEST" "$OLD" || [ "$NEWEST" = "$(state_get bad_version)" ]; then
    state_set last_check "$(now)"
    [ "$NEWEST" = "$(state_get bad_version)" ] && REASON="vydání $NEWEST po instalaci nefungovalo"
    if [ $BEFORE_OK = 0 ]; then
        ACTION=failed REASON="nainstalovaný nefunguje a novější použitelné vydání není${REASON:+ ($REASON)}"
    fi
    finish
fi

# Novější vydání: nejdřív bokem, instalace zůstává, jak je.
if health "zkouška $NEWEST" "$UV" tool run --from "yt-dlp==$NEWEST" "${UV_WITH[@]}" yt-dlp; then
    CAND_OK=1
else
    CAND_OK=0
fi
EV+=("candidate_ok:=$([ $CAND_OK = 1 ] && echo true || echo false)" "candidate_formats=$H_FMTS" "candidate_format=$H_FMT"
     "candidate_pot:=$H_POT" "candidate_error=$H_REASON")
"$UV" cache prune > /dev/null 2>&1 || true   # dočasné prostředí zkoušky z disku pryč
if [ $CAND_OK = 0 ]; then
    # last_check se neposouvá: zítra se zkusí znovu (opravné vydání)
    REASON="vydání $NEWEST nefunguje"
    [ $BEFORE_OK = 0 ] && ACTION=failed
    finish
fi

# Instalace a restart patří k sobě: když hraje, nic se nemění. Resolver
# drží načtený starý yt-dlp a jeho soubory by se mu pod rukama vyměnily.
# Když je nainstalovaný nemocný, nečeká se — hudba stejně nejde.
if [ $BEFORE_OK = 1 ] && ! wait_quiet; then
    ACTION=skipped_playing RESTART=skipped_playing REASON="hraje hudba, $NEWEST příští noc"
    finish
fi

log "instaluji yt-dlp $NEWEST (místo $OLD)"
state_set upgraded_at "$(now)"
ok=0
if install_spec "yt-dlp>=$NEWEST"; then
    NEW=$(installed_version)
    if ! plugins_ok; then
        REASON="po instalaci chybí plugin ($WITH)"
        log "$REASON"
    elif health "nainstalovaný $NEW" "$YTDLP"; then
        ok=1
    else
        REASON="po instalaci nefunguje ($H_REASON)"
    fi
    EV+=("after_ok:=$([ $ok = 1 ] && echo true || echo false)" "after_formats=$H_FMTS" "after_format=$H_FMT"
         "after_pot:=$H_POT")
fi

if [ $ok = 1 ]; then
    ACTION=upgraded
    state_set last_check "$(now)"
    restart_when_quiet
    finish
fi

# Nepovedlo se — zpátky na původní verzi.
NEW=$(installed_version)
if [ -n "$OLD" ] && [ "$NEW" != "$OLD" ] || ! plugins_ok; then
    log "vracím yt-dlp $OLD"
    # tohle vydání se už nezkouší, dokud nevyjde novější
    state_set bad_version "$NEWEST"
    if install_spec "yt-dlp==$OLD" && plugins_ok; then
        ACTION=rolled_back
        NEW=$(installed_version)
        if health "vrácený $NEW" "$YTDLP"; then rb=true; else rb=false; fi
        EV+=("rollback_ok:=$rb")
    else
        ACTION=failed REASON="${REASON:+$REASON; }návrat na $OLD selhal"
        NEW=$(installed_version)
    fi
else
    ACTION=failed
fi
state_del upgraded_at
finish
