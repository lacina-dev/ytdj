#!/usr/bin/env bash
# Vlastní přihlášení YouTube pro Pi — nezávislé na prohlížeči v notebooku.
#
#   packaging/rpi/youtube-login.sh            # PI=lacina@192.168.0.24 přepíše cíl
#
# Pi nemá prohlížeč a yt-dlp se k YouTube neumí přihlásit kódem, jen cookies.
# Kdyby se vzaly z běžného profilu Chrome, sdílelo by Pi relaci s notebookem:
# odhlášení tady (nebo rotace cookies prohlížečem) by ho odstřihlo. Proto:
#
#   1. Chrome se otevře s úplně prázdným, dočasným profilem,
#   2. přihlásíš se účtem s Premium a okno zavřeš — NEODHLAŠUJ se,
#   3. cookies z toho profilu odejdou na Pi a profil se smaže.
#
# Relace pak existuje jen v cookies na Pi. Když Premium formáty časem přestanou
# chodit (ytdj --check-audio to řekne), prostě skript spusť znovu.
set -euo pipefail
PI="${PI:-lacina@10.42.0.149}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lacina_deploy}"
ssh_cmd=(ssh -i "$SSH_KEY" -o ConnectTimeout=10)
chrome="$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || true)"
[ -n "$chrome" ] || { echo "Chrome/Chromium nenalezen"; exit 1; }
command -v yt-dlp >/dev/null || { echo "na notebooku chybí yt-dlp (uv tool install yt-dlp)"; exit 1; }

profile="$(mktemp -d -t ytdj-login-XXXXXX)"
raw="$profile.cookies.txt"
trap 'rm -rf "$profile" "$raw" "$raw.pi"' EXIT

echo "==> Otevírám Chrome s čistým profilem."
echo "    Přihlas se na YouTube účtem s Premium, pak okno ZAVŘI (neodhlašuj se)."
"$chrome" --user-data-dir="$profile" --no-first-run --no-default-browser-check \
    "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://music.youtube.com/" \
    >/dev/null 2>&1 || true

echo "==> Vytahuji cookies z dočasného profilu"
# URL je jen záminka, aby yt-dlp jar načetl a uložil; video se nestahuje
yt-dlp --quiet --no-warnings --cookies-from-browser "chrome:$profile/Default" \
    --cookies "$raw" --skip-download --simulate \
    "https://www.youtube.com/watch?v=jNQXAC9IVRw" >/dev/null 2>&1 || true
[ -s "$raw" ] || { echo "cookies se nepodařilo vytáhnout"; exit 1; }

# jen YouTube a přihlášení Google — nic dalšího z prohlížeče na Pi nepatří
{ head -3 "$raw"; grep -E '^(#HttpOnly_)?(\.?youtube\.com|accounts\.google\.com|\.google\.com)\s' "$raw" || true; } > "$raw.pi"
if ! grep -qE '(SAPISID|__Secure-3PAPISID)' "$raw.pi"; then
    echo "V cookies není přihlášení (SAPISID) — přihlásil ses a okno zavřel až potom?"
    exit 1
fi
echo "    $(grep -cE '\s(TRUE|FALSE)\s' "$raw.pi") cookies"

echo "==> Nahrávám na Pi ($PI)"
"${ssh_cmd[@]}" "$PI" 'f=~/.config/ytdj/cookies.txt; [ -f "$f" ] && cp -p "$f" "$f.bak"; umask 077; cat > "$f.new" && mv "$f.new" "$f" && chmod 600 "$f"' < "$raw.pi"

echo "==> Ověřuji na Pi (trvá to ~30 s)"
"${ssh_cmd[@]}" "$PI" 'cd ~/ytdj && ./run.sh --check-audio 2>&1 | tail -6'
echo "Hotovo. Dočasný profil je smazaný; relace žije jen na Pi."
