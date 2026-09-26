"""Souhrn provozního logu: `python -m ytdj.telemetry report`.

    python -m ytdj.telemetry report                   # všechno, co je v logu
    python -m ytdj.telemetry report --since 2h        # posledních 30m / 2h / 3d
    python -m ytdj.telemetry report --since today     # od půlnoci (i "yesterday")
    python -m ytdj.telemetry report --since 2026-09-25 --until 2026-09-26
    python -m ytdj.telemetry report --file kopie/     # složka nebo soubor z Pi
    python -m ytdj.telemetry report --json            # strojově

Čte events.jsonl i rotované events.jsonl.1…N. Jen standardní knihovna a jeden
průchod souborem — na Pi 3 pár MB za pár vteřin. Filtr času porovnává
rovnou text časové značky (lokální čas), JSON se parsuje jen u řádků v okně.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .telemetry import events_file, rotated_files

TS_PREFIX = '{"ts": "'


# --------------------------------------------------------------------------
# načtení
# --------------------------------------------------------------------------


def parse_when(text: str, now: datetime | None = None) -> str:
    """--since/--until → "YYYY-MM-DDTHH:MM:SS" v místním čase (pro porovnání textu)."""
    now = now or datetime.now()
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|h|d)", t)
    if m:
        n = float(m.group(1))
        delta = {"m": timedelta(minutes=n), "min": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[m.group(2)]
        when = now - delta
    elif t in ("today", "dnes"):
        when = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif t in ("yesterday", "včera"):
        when = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    else:
        try:
            when = datetime.fromisoformat(text.strip())
        except ValueError:
            raise ValueError(f"nerozumím času {text!r} (2h, 30m, 3d, today, YYYY-MM-DD)")
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def input_files(paths: list[str] | None) -> list[Path]:
    if not paths:
        return rotated_files(events_file())
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            out += rotated_files(p / "events.jsonl")
        else:
            out += rotated_files(p) or ([p] if p.exists() else [])
    return out


def load_events(files: Iterable[Path], since: str | None = None,
                until: str | None = None) -> Iterator[dict[str, Any]]:
    for path in files:
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if line.startswith(TS_PREFIX):
                    ts = line[8:27]
                    if since and ts < since:
                        continue
                    if until and ts >= until:
                        continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # useknutý řádek (výpadek proudu)
                if isinstance(rec, dict) and isinstance(rec.get("kind"), str):
                    if not line.startswith(TS_PREFIX):  # jiné pořadí klíčů
                        ts = str(rec.get("ts", ""))[:19]
                        if (since and ts < since) or (until and ts >= until):
                            continue
                    yield rec


# --------------------------------------------------------------------------
# statistika
# --------------------------------------------------------------------------


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def stats(values: Iterable[Any]) -> dict[str, Any] | None:
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": vals[0],
        "p10": _pct(vals, 10),
        "median": _pct(vals, 50),
        "p90": _pct(vals, 90),
        "max": vals[-1],
    }


KNOWN = {
    "session.start", "session.end",
    "track.request", "track.start", "track.end", "track.stall", "track.quality",
    "track.mismatch",
    "resolver.resolve", "resolver.get", "resolver.ahead", "resolver.ready",
    "resolver.template", "resolver.exit", "resolver.fallback", "prefetch.ahead",
    "resolver.listen", "resolver.disk", "resolver.drop", "resolver.import_failed",
    "resolver.cancel", "player.resume_track",
    "player.start", "player.priority", "player.died", "player.fail",
    "sys.sample", "sys.throttle", "audio.xrun",
    "web.prompt", "web.control", "web.sse_open", "web.sse_close", "web.restart",
    "web.config", "web.error",
    "request.created", "request.interpreted", "request.queued", "request.started",
    "request.done", "request.removed", "request.turn", "request.play_next",
    "request.handover", "request.background", "request.start", "request.resume",
    "request.steer", "request.meta", "request.background_follow", "request.background_stale", "request.already_playing", "request.reply_fixed", "web.request_action", "sys.loop_lag",
}


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    n = 0
    first = last = None
    kinds: Counter = Counter()
    sessions: dict[str, dict[str, Any]] = {}

    starts: list[dict] = []
    ends: Counter = Counter()
    premature = not_started = stalls = stall_ms = 0
    stall_events = 0
    requests: Counter = Counter()
    skip_total = skip_unprepared = skip_resolving = skip_empty = 0
    quality: Counter = Counter()
    mismatches = 0

    resolve_took: dict[str, list] = defaultdict(list)
    resolve_err = 0
    gets = hits = blocked = get_cancelled = 0
    get_wait_miss: list = []
    fallbacks = 0
    restarts = 0
    resolver_ready: list = []

    xrun_total = 0
    xruns: list[dict] = []

    samples: dict[str, list] = defaultdict(list)
    throttle_flags: set[str] = set()
    self_us: list = []

    prompts: list[dict] = []
    controls: Counter = Counter()
    sse_max = 0
    web_restarts = web_errors = 0

    errors: Counter = Counter()
    other: dict[str, list] = defaultdict(list)

    # fronta přání: id → co o přání víme
    wishes: dict[str, dict] = {}
    turns: Counter = Counter()
    idle_starts: list[dict] = []
    resumes: Counter = Counter()
    steers = 0
    loop_lags: list[dict] = []

    for e in events:
        n += 1
        kind = e["kind"]
        kinds[kind] += 1
        ts = str(e.get("ts", ""))
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
        sid = e.get("sid")
        if sid:
            s = sessions.setdefault(sid, {"start": None, "end": False, "last": ts})
            s["last"] = max(s["last"], ts)
        err = e.get("error")
        if err and (kind != "web.prompt" or (e.get("status") or 200) >= 500):
            errors[(kind, str(err)[:90])] += 1

        if kind == "session.start":
            sessions[sid or "?"]["start"] = ts
        elif kind == "session.end":
            if sid in sessions:
                sessions[sid]["end"] = True
        elif kind == "track.request":
            why = e.get("why") or "?"
            requests[why] += 1
            if why == "skip":
                skip_total += 1
                if not e.get("next_id"):
                    skip_empty += 1
                elif e.get("next_ready") is False:
                    skip_unprepared += 1
                    if e.get("next_resolving"):
                        skip_resolving += 1
        elif kind == "track.start":
            starts.append(e)
        elif kind == "track.end":
            ends[e.get("reason") or "?"] += 1
            if e.get("premature"):
                premature += 1
            if e.get("started") is False:
                not_started += 1
            stalls += int(e.get("stalls") or 0)
            stall_ms += int(e.get("stall_ms") or 0)
        elif kind == "track.stall":
            stall_events += 1
        elif kind == "track.quality":
            quality["premium" if e.get("premium") else f"{e.get('codec') or '?'} {e.get('kbps')}"] += 1
        elif kind == "track.mismatch":
            mismatches += 1
        elif kind == "resolver.resolve":
            resolve_took[e.get("why") or "?"].append(e.get("took_ms"))
            if not e.get("ok", True):
                resolve_err += 1
        elif kind == "resolver.get" and e.get("how") == "cancelled":
            get_cancelled += 1  # mpv skladbu opustilo při načítání (Další)
        elif kind == "resolver.get":
            gets += 1
            if e.get("hit"):
                hits += 1
            else:
                get_wait_miss.append(e.get("wait_ms"))
            if e.get("blocked_by"):
                blocked += 1
        elif kind == "resolver.fallback":
            fallbacks += 1
        elif kind == "resolver.exit":
            restarts += 1
        elif kind == "resolver.ready":
            resolver_ready.append(e.get("startup_ms") or e.get("import_ms"))
        elif kind == "sys.sample":
            for k, v in e.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("xruns",):
                    samples[k].append(v)
            if e.get("self_us") is not None:
                self_us.append(e["self_us"])
        elif kind == "sys.throttle":
            throttle_flags.update(e.get("flags") or [])
        elif kind == "audio.xrun":
            xrun_total += int(e.get("delta") or 0)
            xruns.append(e)
        elif kind == "web.prompt":
            prompts.append(e)
        elif kind == "web.control":
            controls[str(e.get("action"))] += 1
        elif kind in ("web.sse_open", "web.sse_close"):
            sse_max = max(sse_max, int(e.get("clients") or 0))
        elif kind == "web.restart":
            web_restarts += 1
        elif kind == "web.error":
            web_errors += 1
        elif kind == "sys.loop_lag":
            loop_lags.append(e)
        elif kind.startswith("request.") and kind in KNOWN:
            rid = e.get("id")
            rec = wishes.setdefault(str(rid), {}) if rid else None
            if kind == "request.created" and rec is not None:
                rec.update(who=e.get("who") or "?", source=e.get("source") or "?",
                           text=e.get("text"), ts=ts, play_next=bool(e.get("play_next")))
            elif kind == "request.interpreted" and rec is not None:
                rec.update(via=e.get("via"), intent=e.get("intent_kind"),
                           decide_ms=e.get("took_ms"))
            elif kind == "request.queued" and rec is not None:
                rec.setdefault("ahead", e.get("ahead"))
            elif kind == "request.started" and rec is not None:
                rec.setdefault("wait_ms", e.get("wait_ms"))
                rec.setdefault("who", e.get("who") or "?")
            elif kind == "request.done" and rec is not None:
                rec["state"] = e.get("state")
                rec.setdefault("who", e.get("who") or "?")
            elif kind == "request.removed" and rec is not None:
                rec["state"] = "removed"
                rec.setdefault("who", e.get("who") or "?")
            elif kind == "request.turn":
                turns[e.get("who") or "?"] += 1
            elif kind == "request.start":
                idle_starts.append(e)
            elif kind == "request.resume":
                resumes[e.get("reason") or "?"] += 1
            elif kind == "request.steer":
                steers += 1
        elif kind not in KNOWN:
            other[kind].append(e.get("took_ms"))

    # --- skladby ---
    def lat(sel) -> dict | None:
        return stats(s.get("wait_ms") for s in starts if sel(s) and not s.get("paused"))

    by_source = {src: lat(lambda s, src=src: (s.get("source") or "?") == src)
                 for src in sorted({s.get("source") or "?" for s in starts})}
    by_why = {w: lat(lambda s, w=w: (s.get("why") or "?") == w)
              for w in sorted({s.get("why") or "?" for s in starts})}
    requested = [s for s in starts if s.get("requested")]

    # --- sezení: start bez konce = pád / kill (kromě posledního, ten možná běží) ---
    ordered = sorted(sessions.items(), key=lambda kv: kv[1]["last"])
    unclean = [sid for sid, s in ordered[:-1] if s["start"] and not s["end"]]

    sysd: dict[str, Any] = {"samples": kinds["sys.sample"]}
    for key in ("mem_avail_mb", "swap_used_mb", "swapin_ps", "swapout_ps", "temp_c",
                "cpu", "iowait", "load1", "cpu_ytdj", "cpu_mpv", "cpu_resolver",
                "cpu_pwtop", "rss_ytdj_mb", "rss_mpv_mb", "rss_resolver_mb", "majflt_ps",
                "self_us"):
        st = stats(samples.get(key, []))
        if st:
            sysd[key] = st
    sysd["throttle_flags"] = sorted(throttle_flags)

    xrun_list = []
    for x in xruns[-15:]:
        xrun_list.append({k: x.get(k) for k in (
            "ts", "delta", "nodes", "track", "pos_s", "loading", "buffering", "resolving",
            "codex", "last_cpu", "last_iowait", "last_mem_avail_mb", "last_swap_used_mb",
            "last_temp_c") if x.get(k) is not None})
    xrun_ctx = {
        "during_resolve": sum(1 for x in xruns if x.get("resolving")),
        "during_codex": sum(1 for x in xruns if x.get("codex")),
        "during_loading": sum(1 for x in xruns if x.get("loading") or x.get("buffering")),
        "early_in_track": sum(1 for x in xruns if (x.get("pos_s") or 99) < 5),
        # nic nehrálo (typicky probuzení zvukovky při startu ytdj) — neslyšitelné
        "nothing_playing": sum(1 for x in xruns if not x.get("track")),
    }

    other_out = {k: {"n": len(v), "took_ms": stats(v)} for k, v in sorted(other.items())}

    # --- přání: kolik od koho, jak rychle zazněla, jak dopadla ---
    people: dict[str, dict[str, Any]] = {}
    for rec in wishes.values():
        if "who" not in rec:
            continue
        pp = people.setdefault(rec["who"], {"n": 0, "states": Counter(), "waits": []})
        pp["n"] += 1
        pp["states"][rec.get("state") or "open"] += 1
        if rec.get("wait_ms") is not None:
            pp["waits"].append(rec["wait_ms"])
    by_person = {
        who: {"n": pp["n"], "states": dict(pp["states"]), "wait_ms": stats(pp["waits"]),
              "turns": turns.get(who, 0)}
        for who, pp in sorted(people.items(), key=lambda kv: -kv[1]["n"])
    }
    all_waits = [r["wait_ms"] for r in wishes.values() if r.get("wait_ms") is not None]
    states = Counter(r.get("state") or "open" for r in wishes.values() if "who" in r)
    medians = [st["wait_ms"]["median"] for st in by_person.values() if st["wait_ms"]]
    requests_out = {
        "created": sum(1 for r in wishes.values() if "text" in r),
        "states": dict(states),
        "fulfilled": states.get("done", 0) + states.get("playing", 0),
        "notfound": states.get("notfound", 0),
        "error": states.get("error", 0),
        "removed": states.get("removed", 0),
        "wish_to_sound_ms": stats(all_waits),
        "via": dict(Counter(r.get("via") or "?" for r in wishes.values() if r.get("via"))),
        "decide_ms": stats(r.get("decide_ms") for r in wishes.values() if r.get("decide_ms") is not None),
        "by_source": dict(Counter(r.get("source") or "?" for r in wishes.values() if "source" in r)),
        "by_person": by_person,
        # spravedlnost: jak moc se liší typické čekání lidí (1 = všichni stejně)
        "fairness_spread": round(max(medians) / min(medians), 2) if len(medians) > 1 and min(medians) > 0 else None,
        "play_next": sum(1 for r in wishes.values() if r.get("play_next")),
        "idle_starts": {"n": len(idle_starts),
                        "via": dict(Counter(x.get("via") or "?" for x in idle_starts)),
                        "took_ms": stats(x.get("took_ms") for x in idle_starts)},
        "resumes": dict(resumes),
        "steered": steers,
    }

    return {
        "period": {"from": first, "to": last, "events": n,
                   "sessions": len(sessions), "unclean_ends": len(unclean)},
        "kinds": dict(kinds.most_common()),
        "tracks": {
            "started": len(starts),
            "ended": dict(ends),
            "premature": premature,
            "never_started": not_started,
            "stalls": stalls or stall_events,
            "stall_ms": stall_ms,
            "mismatches": mismatches,
            "unexpected_next": sum(1 for x in starts if x.get("unexpected")),
            "requests": dict(requests),
            "quality": dict(quality),
        },
        "latency": {
            "wait_ms": lat(lambda s: True),
            "by_source": by_source,
            "by_why": by_why,
            "load_ms": stats(s.get("load_ms") for s in starts if not s.get("paused")),
            "buffer_ms": stats(s.get("buffer_ms") for s in starts if not s.get("paused")),
            "requested_track_ms": stats(s.get("since_request_ms") for s in requested),
            "paused_excluded": sum(1 for s in starts if s.get("paused")),
        },
        "skips": {
            "total": skip_total,
            # co doopravdy zaznělo: start po přeskočení, který se musel řešit na místě
            "landed_unprepared": sum(1 for x in starts if x.get("why") == "skip"
                                     and x.get("source") != "prefetched"),
            "landed_total": sum(1 for x in starts if x.get("why") == "skip"),
            # podle naší fronty v okamžiku přeskočení
            "unprepared": skip_unprepared,
            "unprepared_but_resolving": skip_resolving, "empty_queue": skip_empty},
        "resolver": {
            "resolve_ms": {w: stats(v) for w, v in resolve_took.items()},
            "resolve_errors": resolve_err,
            "gets": gets, "hits": hits,
            "hit_rate": round(hits / gets, 3) if gets else None,
            "cancelled": get_cancelled,
            "miss_wait_ms": stats(get_wait_miss),
            "blocked_by_ahead": blocked,
            "fallbacks": fallbacks,
            "restarts": restarts,
            "startup_ms": stats(resolver_ready),
        },
        "xruns": {"total": xrun_total, "events": len(xruns), "context": xrun_ctx,
                  "last": xrun_list},
        "system": sysd,
        "web": {
            "prompts": len(prompts),
            "prompt_status": dict(Counter(str(p.get("status")) for p in prompts)),
            "prompt_ms": stats(p.get("took_ms") for p in prompts if p.get("status") == 200),
            "last_prompts": [
                {k: p.get(k) for k in ("ts", "text", "took_ms", "status", "reply", "ua")}
                for p in prompts[-10:]
            ],
            "control": dict(controls),
            "sse_max_clients": sse_max,
            "restarts": web_restarts,
            "errors": web_errors,
        },
        "requests": requests_out,
        "loop_lag": {
            "n": len(loop_lags),
            "lag_ms": stats(x.get("lag_ms") for x in loop_lags),
            "worst": [{k: x.get(k) for k in ("ts", "lag_ms", "stack", "ongoing") if x.get(k)}
                      for x in sorted(loop_lags, key=lambda x: -(x.get("lag_ms") or 0))[:3]],
        },
        "errors": [{"kind": k, "error": m, "n": c} for (k, m), c in errors.most_common(10)],
        "other": other_out,
    }


# --------------------------------------------------------------------------
# text pro člověka (česky)
# --------------------------------------------------------------------------


def _fmt_ms(st: dict | None, unit: str = "s") -> str:
    if not st:
        return "—"
    if unit == "s":
        f = lambda v: f"{v / 1000:.1f}"  # noqa: E731
    else:
        f = lambda v: f"{v:.0f}"  # noqa: E731
    return f"medián {f(st['median'])} {unit}, p90 {f(st['p90'])} {unit}, max {f(st['max'])} {unit} (n={st['n']})"


def _t(ts: str | None) -> str:
    return (ts or "?")[:19].replace("T", " ")


def render(s: dict[str, Any]) -> str:
    out: list[str] = []
    w = out.append
    p = s["period"]
    w(f"ytdj — provozní souhrn {_t(p['from'])} → {_t(p['to'])}")
    w(f"  {p['events']} událostí, běhů aplikace: {p['sessions']}"
      + (f", z toho {p['unclean_ends']} bez čistého konce (pád/kill)" if p["unclean_ends"] else ""))

    t = s["tracks"]
    e = t["ended"]
    w("")
    w("Skladby")
    w(f"  začalo hrát {t['started']}; dohráno {e.get('finished', 0)}, přeskočeno "
      f"{e.get('skipped', 0)}, odsunuto DJ {e.get('replaced', 0)}, chyba {e.get('error', 0)}")
    extra = []
    if t["premature"]:
        extra.append(f"useknuto předčasně {t['premature']}")
    if t["never_started"]:
        extra.append(f"skončilo dřív, než zaznělo {t['never_started']}")
    if t["stalls"]:
        extra.append(f"zaseknutí uprostřed {t['stalls']} ({t['stall_ms'] / 1000:.1f} s)")
    if t["mismatches"]:
        extra.append(f"nesoulad displej/mpv {t['mismatches']}")
    if t["unexpected_next"]:
        extra.append(f"mpv pustilo jinou skladbu, než byla další ve frontě {t['unexpected_next']}×")
    if extra:
        w("  " + ", ".join(extra))
    if t["quality"]:
        w("  kvalita: " + ", ".join(f"{k} {v}×" for k, v in sorted(t["quality"].items(), key=lambda kv: -kv[1])))

    lat = s["latency"]
    w("")
    w("Čekání na zvuk (od požadavku / konce předchozí do zvuku)")
    w(f"  vše: {_fmt_ms(lat['wait_ms'])}")
    names = {"prefetched": "nachystané dopředu", "on_demand": "řešené na místě",
             "direct": "bez resolveru", "unknown": "neznámý zdroj"}
    for src, st in lat["by_source"].items():
        w(f"  {names.get(src, src)}: {_fmt_ms(st)}")
    whys = {"skip": "po přeskočení", "eof": "po dohrání", "replace": "po zásahu DJ",
            "enqueue": "po zařazení do prázdné fronty", "error": "po chybě", "auto": "jiné"}
    for why, st in lat["by_why"].items():
        w(f"  {whys.get(why, why)}: {_fmt_ms(st)}")
    w(f"  načtení (yt-dlp + otevření proudu): {_fmt_ms(lat['load_ms'])}")
    w(f"  plnění bufferu do zvuku: {_fmt_ms(lat['buffer_ms'])}")
    if lat["requested_track_ms"]:
        w(f"  vyžádaná skladba od zařazení do zvuku: {_fmt_ms(lat['requested_track_ms'])}")
    if lat["paused_excluded"]:
        w(f"  (vynecháno {lat['paused_excluded']} startů s pauzou)")

    sk = s["skips"]
    if sk["total"]:
        w("")
        w(f"Přeskočení: {sk['total']}× — zaznělo až po řešení na místě {sk['landed_unprepared']}"
          f" z {sk['landed_total']}; podle fronty nebyla další nachystaná {sk['unprepared']}×"
          + (f" (z toho rozdělaná {sk['unprepared_but_resolving']}×)" if sk["unprepared_but_resolving"] else "")
          + (f", prázdná fronta {sk['empty_queue']}×" if sk["empty_queue"] else ""))

    r = s["resolver"]
    w("")
    w("Resolver (yt-dlp)")
    for why, st in r["resolve_ms"].items():
        label = {"urgent": "na čekající mpv", "ahead": "dopředu",
                 "first": "přednostně (přepnutí)"}.get(why, why)
        w(f"  řešení {label}: {_fmt_ms(st)}")
    if r["gets"]:
        w(f"  dotazy mpv {r['gets']}×, z cache {r['hits']}× ({100 * (r['hit_rate'] or 0):.0f} %)"
          + (f"; čekání při minutí: {_fmt_ms(r['miss_wait_ms'])}" if r["miss_wait_ms"] else "")
          + (f"; zrušeno při Další {r['cancelled']}×" if r.get("cancelled") else ""))
    bits = []
    if r["blocked_by_ahead"]:
        bits.append(f"mpv čekalo na rozdělanou skladbu dopředu {r['blocked_by_ahead']}×")
    if r["resolve_errors"]:
        bits.append(f"chyby {r['resolve_errors']}")
    if r["fallbacks"]:
        bits.append(f"obchvat přes pomalé yt-dlp {r['fallbacks']}×")
    if r["restarts"]:
        bits.append(f"restarty {r['restarts']}")
    if bits:
        w("  " + ", ".join(bits))
    if r["startup_ms"]:
        w(f"  start resolveru: {_fmt_ms(r['startup_ms'])}")

    x = s["xruns"]
    w("")
    w(f"Výpadky zvuku (xrun PipeWire): {x['total']}"
      + (f" v {x['events']} okamžicích" if x["events"] else ""))
    if x["events"]:
        c = x["context"]
        w(f"  při řešení skladby {c['during_resolve']}×, při Codexu {c['during_codex']}×, "
          f"při načítání {c['during_loading']}×, v prvních 5 s skladby {c['early_in_track']}×, "
          f"když nic nehrálo {c['nothing_playing']}×")
        for ev in x["last"][-8:]:
            ctx = []
            if ev.get("resolving"):
                ctx.append("resolver")
            if ev.get("codex"):
                ctx.append("codex")
            if ev.get("loading") or ev.get("buffering"):
                ctx.append("načítání")
            if ev.get("last_cpu") is not None:
                ctx.append(f"cpu {ev['last_cpu']:.0f} %")
            if ev.get("last_iowait"):
                ctx.append(f"iowait {ev['last_iowait']:.0f} %")
            if ev.get("last_mem_avail_mb") is not None:
                ctx.append(f"volno {ev['last_mem_avail_mb']} MB")
            pos = f" @{ev['pos_s']:.0f}s" if isinstance(ev.get("pos_s"), (int, float)) else ""
            w(f"  {_t(ev.get('ts'))}  +{ev.get('delta')}  {' '.join(ev.get('nodes') or [])}"
              f"  {ev.get('track') or ''}{pos}  [{', '.join(ctx)}]")

    sy = s["system"]
    w("")
    w(f"Stroj (vzorků: {sy['samples']})")
    if "mem_avail_mb" in sy:
        m = sy["mem_avail_mb"]
        w(f"  volná paměť: min {m['min']:.0f} MB, p10 {m['p10']:.0f}, medián {m['median']:.0f}")
    if "swap_used_mb" in sy:
        line = f"  swap: max {sy['swap_used_mb']['max']:.0f} MB"
        if "swapin_ps" in sy:
            line += (f", swap-in max {sy['swapin_ps']['max']:.0f} str/s, "
                     f"swap-out max {sy['swapout_ps']['max']:.0f} str/s")
        w(line)
    if "cpu" in sy:
        w(f"  CPU: medián {sy['cpu']['median']:.0f} %, p90 {sy['cpu']['p90']:.0f} %, max {sy['cpu']['max']:.0f} %"
          + (f"; iowait p90 {sy['iowait']['p90']:.0f} %, max {sy['iowait']['max']:.0f} %" if "iowait" in sy else ""))
    procs = []
    for name in ("ytdj", "mpv", "resolver", "pwtop"):
        if f"cpu_{name}" in sy:
            st = sy[f"cpu_{name}"]
            rss = sy.get(f"rss_{name}_mb")
            procs.append(f"{name} {st['median']:.1f}/{st['max']:.1f} %"
                         + (f" {rss['max']:.0f} MB" if rss else ""))
    if procs:
        w("  procesy (CPU medián/max jednoho jádra, RSS max): " + ", ".join(procs))
    if "temp_c" in sy:
        w(f"  teplota: medián {sy['temp_c']['median']:.0f} °C, max {sy['temp_c']['max']:.0f} °C")
    w("  throttling: " + (", ".join(sy["throttle_flags"]) if sy["throttle_flags"] else "nic"))
    if "self_us" in sy:
        w(f"  cena vzorku: medián {sy['self_us']['median'] / 1000:.1f} ms CPU")

    wb = s["web"]
    w("")
    w(f"Web: přání {wb['prompts']}× ({', '.join(f'{k}: {v}' for k, v in wb['prompt_status'].items()) or '—'}), "
      f"odpověď {_fmt_ms(wb['prompt_ms'])}")
    if wb["control"]:
        w("  ovládání: " + ", ".join(f"{k} {v}×" for k, v in sorted(wb["control"].items(), key=lambda kv: -kv[1])))
    if wb["sse_max_clients"]:
        w(f"  nejvíc současně připojených: {wb['sse_max_clients']}")
    if wb["restarts"] or wb["errors"]:
        w(f"  restart z webu {wb['restarts']}×, chyby serveru {wb['errors']}×")
    for pr in wb["last_prompts"]:
        took = f"{(pr.get('took_ms') or 0) / 1000:.0f} s"
        w(f"  {_t(pr.get('ts'))} [{pr.get('status')}, {took}] {str(pr.get('text') or '')[:90]}")
        if pr.get("reply"):
            w(f"      → {str(pr['reply'])[:110]}")

    ll = s.get("loop_lag") or {}
    if ll.get("n"):
        w("")
        w(f"Zaseknutý event loop (web, panel i přání stály): {ll['n']}×, {_fmt_ms(ll['lag_ms'])}")
        for x in ll["worst"]:
            w(f"  {_t(x.get('ts'))} {(x.get('lag_ms') or 0) / 1000:.1f} s: "
              + " ← ".join((x.get("stack") or ["?"])[:4]))

    rq = s.get("requests") or {}
    if rq.get("created") or rq.get("idle_starts", {}).get("n"):
        w("")
        w("Přání (fronta pro víc lidí)")
        w(f"  přijato {rq['created']}; splněno {rq['fulfilled']}, nenašel {rq['notfound']}, "
          f"chyba {rq['error']}, odebráno {rq['removed']}"
          + (f", ještě ve frontě {rq['states'].get('queued', 0) + rq['states'].get('open', 0)}"
             if rq["states"].get("queued") or rq["states"].get("open") else ""))
        w(f"  přání → první zvuk: {_fmt_ms(rq['wish_to_sound_ms'])}")
        if rq["decide_ms"]:
            w(f"  DJ rozhodl za: {_fmt_ms(rq['decide_ms'])}"
              + (" (" + ", ".join(f"{k} {v}×" for k, v in sorted(rq['via'].items(), key=lambda kv: -kv[1])) + ")"
                 if rq["via"] else ""))
        if rq["by_source"]:
            w("  odkud: " + ", ".join(f"{k} {v}×" for k, v in sorted(rq["by_source"].items(), key=lambda kv: -kv[1])))
        for who, pp in rq["by_person"].items():
            st = pp["states"]
            w(f"  {who}: {pp['n']}× (splněno {st.get('done', 0)}, nenašel {st.get('notfound', 0)}"
              + (f", chyba {st['error']}" if st.get("error") else "")
              + (f", odebráno {st['removed']}" if st.get("removed") else "")
              + f"), na řadě {pp['turns']}×, do zvuku {_fmt_ms(pp['wait_ms'])}")
        if rq["fairness_spread"]:
            w(f"  spravedlnost: nejdelší / nejkratší typické čekání = {rq['fairness_spread']}×")
        if rq["play_next"]:
            w(f"  „zařadit hned“ {rq['play_next']}×")
        if rq["steered"]:
            w(f"  změna směru (překvap mě / něco jiného) opravena {rq['steered']}×")
        ist = rq["idle_starts"]
        if ist["n"]:
            w(f"  rozjezd bez přání (▶ v tichu) {ist['n']}×: {_fmt_ms(ist['took_ms'])}"
              + (" (" + ", ".join(f"{k} {v}×" for k, v in ist["via"].items()) + ")" if ist["via"] else ""))
        if rq["resumes"]:
            w("  po restartu: " + ", ".join(f"{k} {v}×" for k, v in rq["resumes"].items()))

    if s["other"]:
        w("")
        w("Další události (DJ, panel, katalog…)")
        for kind, o in s["other"].items():
            w(f"  {kind}: {o['n']}×" + (f", {_fmt_ms(o['took_ms'])}" if o["took_ms"] else ""))

    if s["errors"]:
        w("")
        w("Nejčastější chyby")
        for er in s["errors"]:
            w(f"  {er['n']}× {er['kind']}: {er['error']}")
    return "\n".join(out)


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ytdj.telemetry",
                                 description="Souhrn provozního logu ytdj")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("report", help="souhrn (výchozí)")
    for parser in (ap, rp):
        parser.add_argument("--since", help="2h, 30m, 3d, today, yesterday, YYYY-MM-DD[THH:MM]")
        parser.add_argument("--until", help="stejný formát jako --since")
        parser.add_argument("--file", action="append",
                            help="soubor nebo složka s events.jsonl (i víckrát)")
        parser.add_argument("--json", action="store_true", help="strojový výstup")
    args = ap.parse_args(argv)
    try:
        since = parse_when(args.since) if args.since else None
        until = parse_when(args.until) if args.until else None
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    files = input_files(args.file)
    if not files:
        print(f"žádný log ({events_file()})", file=sys.stderr)
        return 1
    summary = summarize(load_events(files, since, until))
    summary["period"]["files"] = [str(f) for f in files]
    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
