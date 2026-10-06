/* Jeden člověk na všech adresách jukeboxu (FUNKCE F-NICK-07 až F-NICK-10, POZADAVKY #69).
   Prohlížeč drží id klienta zvlášť pro každou adresu (jméno i port). Tenhle skript:
   1. na běžné stránce srovná id se serverem (cookie platí pro všechny porty téhož jména)
      a zjistí, které další adresy jukeboxu z tohohle prohlížeče opravdu odpovídají;
   2. když nějaká odpovídá a účet s ní ještě není srovnaný, projde je jednou navigací
      (/identity#give → … → /identity#take) a vrátí se. Každá adresa vydá jen jednorázový
      kód — id klienta se do adresy nepíše. Kódy přijme jen stránka, která cestu sama
      začala (značka v sessionStorage), a jen mezi adresami, které vyjmenoval server.
   Nikdy nenaviguje na adresu, kterou si těsně předtím neověřil. Bez úložiště, bez
   odpovědi serveru nebo bez dalších adres se nic neděje a stránka jede jako dřív.
   Co se dělo (které adresy odpověděly, cesta začala / skončila), hlásí jukeboxu do logu. */
(function () {
  "use strict";
  var K = { client: "ytdj.client", nick: "ytdj.nick", who: "ytdj.who", tag: "ytdj.tag",
            tokens: "ytdj.tokens", theme: "ytdj.theme", miss: "ytdj.id.miss", tried: "ytdj.id.try" };
  var GO = "ytdj.id.go", NOTE = "ytdj.id.note";
  var PROBE_MS = 2500, SYNC_MS = 6000;
  var TRY_GAP = 10 * 60 * 1000;      // další pokus o cestu nejdřív za 10 minut (žádné smyčky)
  // Adresa, která neodpověděla: jméno (.local) zkusit znovu už za 2 minuty — jména se v síti
  // hledají oběžníkem, který se ztrácí, takže chvíli odpovídají a chvíli ne (2 min = jak dlouho
  // si je zařízení pamatuje). Číselná adresa se takhle nemění: tu až za hodinu.
  var MISS_NAME = 2 * 60 * 1000, MISS_ADDR = 60 * 60 * 1000;
  var HOPS_MAX = 8;

  function lget(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lset(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } }
  function sget(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function sset(k, v) { try { sessionStorage.setItem(k, v); return sessionStorage.getItem(k) === v; } catch (e) { return false; } }
  function sdel(k) { try { sessionStorage.removeItem(k); } catch (e) {} }
  function parse(text, fallback) { try { var v = JSON.parse(text); return v && typeof v === "object" ? v : fallback; } catch (e) { return fallback; } }
  function validCid(c) { return /^[A-Za-z0-9_-]{6,40}$/.test(c || ""); }
  function hex(n) {
    var out = "";
    try {
      var a = new Uint8Array(n); crypto.getRandomValues(a);
      for (var i = 0; i < a.length; i++) out += ("0" + a[i].toString(16)).slice(-2);
    } catch (e) { out = ""; }
    return out;
  }
  function newCid() { return "web-" + (hex(9) || Math.random().toString(36).slice(2) + Date.now().toString(36)); }

  function timed(url, opts, ms) {
    var ctl = null, timer = null;
    try { ctl = new AbortController(); opts.signal = ctl.signal; } catch (e) {}
    return new Promise(function (resolve, reject) {
      timer = setTimeout(function () { try { if (ctl) ctl.abort(); } catch (e) {} reject(new Error("timeout")); }, ms);
      fetch(url, opts).then(function (r) { clearTimeout(timer); resolve(r); },
                            function (e) { clearTimeout(timer); reject(e); });
    });
  }
  function post(path, body) {
    return timed(path, { method: "POST", cache: "no-store", credentials: "same-origin",
                         headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, SYNC_MS)
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }
  // Odpovídá na téhle adrese z tohohle prohlížeče tentýž jukebox?
  function probe(origin, jukebox) {
    return timed(origin + "/api/identity/hello", { mode: "cors", credentials: "omit", cache: "no-store" }, PROBE_MS)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return !!(d && jukebox && d.jukebox === jukebox); })
      .catch(function () { return false; });
  }
  // Co server řekl o tom, kdo jsem, zapsat do úložiště téhle adresy.
  function adopt(d) {
    if (!d || !validCid(d.client)) return;
    if (lget(K.client) !== d.client) lset(K.client, d.client);
    if (d.nick) { lset(K.nick, d.nick); lset(K.who, d.nick); }
    if (d.tag) lset(K.tag, d.tag);
    var x = d.extras || {};
    if ((x.theme === "light" || x.theme === "dark") && !lget(K.theme)) lset(K.theme, x.theme);
    if (x.tokens && typeof x.tokens === "object") {
      var mine = parse(lget(K.tokens), {});
      Object.keys(x.tokens).forEach(function (id) { if (!mine[id]) mine[id] = x.tokens[id]; });
      lset(K.tokens, JSON.stringify(mine));
    }
  }
  function noteText(note) {
    if (!note || !note.kind) return "";
    if (note.kind === "merged") {
      return note.dropped && note.kept && note.dropped !== note.kept
        ? "Na téhle adrese jsi měl(a) druhý účet „" + note.dropped + "“. Spojili jsme ho s tvým starším účtem „" + note.kept + "“ — hlasy i přání máš pohromadě."
        : "Na téhle adrese jsi měl(a) druhý účet. Spojili jsme ho s tvým starším — hlasy i přání máš pohromadě.";
    }
    if (note.kind === "adopted" && note.kept) return "Poznali jsme tě z jiné adresy jukeboxu — jsi „" + note.kept + "“.";
    return "";
  }
  function isOrigin(o) { return typeof o === "string" && /^http:\/\/[a-z0-9.-]+(:\d{1,5})?$/.test(o); }
  function missGap(o) { return /^http:\/\/\d+\.\d+\.\d+\.\d+(:\d+)?$/.test(o) ? MISS_ADDR : MISS_NAME; }
  // Do provozního logu jukeboxu: co se dělo v prohlížeči (jen výčty a vlastní adresy; server
  // nic jiného nevezme). Neblokuje a přežije odchod ze stránky.
  function tell(event, data) {
    try {
      data = data || {};
      data.event = event;
      data.client = validCid(lget(K.client)) ? lget(K.client) : "";
      fetch("/api/identity/report", { method: "POST", cache: "no-store", credentials: "same-origin", keepalive: true,
                                      headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
        .catch(function () {});
    } catch (e) {}
  }

  /* ---------- běžná stránka ---------- */

  var interacted = false, leaving = false;
  function boot() {
    var local = lget(K.client);
    if (!validCid(local)) { local = newCid(); lset(K.client, local); }
    var before = local;
    try {
      ["pointerdown", "keydown"].forEach(function (name) {
        window.addEventListener(name, function () { interacted = true; }, true);
      });
      // Zpět z cesty, která nedoběhla (adresa po cestě přestala odpovídat): prohlížeč vrátí
      // tuhle stránku z paměti ve stavu "odcházím" — načíst ji znovu, ať zase žije.
      window.addEventListener("pageshow", function (ev) { if (ev && ev.persisted && leaving) location.reload(); });
    } catch (e) {}
    var lost = parse(sget(GO), null);  // cesta odsud začala a nevrátila se
    if (lost && lost.t) { sdel(GO); tell("trip_lost", { ms: Date.now() - lost.t }); }
    return post("/api/identity/sync", { client: local }).then(function (d) {
      adopt(d);
      var res = { client: validCid(d.client) ? d.client : before, nick: d.nick || "", tag: d.tag || "",
                  changed: validCid(d.client) && d.client !== before, note: noteText(d.note) };
      if (d.known) { plan(d, true); return res; }  // účet tu je: stránka jede hned, cesta jen když nikdo nic nedělá
      // nový prohlížeč: než se zeptáme na přezdívku, podívat se, jestli účet není na jiné adrese
      return plan(d, false).then(function (leaving) { return leaving ? new Promise(function () {}) : res; });
    }).catch(function () { return null; });  // starší server / bez spojení: jako dřív
  }

  // Najde další adresy jukeboxu, které odpovídají a ještě s nimi účet není srovnaný,
  // a vydá se přes ně. Vrací Promise<true>, když stránka odchází.
  function plan(d, known) {
    var here = d.here, own = (d.origins || []).filter(isOrigin), linked = d.linked || [];
    if (!here || own.indexOf(here) < 0 || here !== location.origin) return Promise.resolve(false);
    var todo = own.filter(function (o) { return o !== here && linked.indexOf(o) < 0; });
    if (!todo.length) return Promise.resolve(false);
    if (!lset(K.miss, lget(K.miss) || "{}") || !sset(GO + ".test", "1")) {  // bez úložiště nic
      tell("skip", { why: "storage", n: todo.length });
      return Promise.resolve(false);
    }
    sdel(GO + ".test");
    var now = Date.now();
    var miss = parse(lget(K.miss), {});
    var cand = todo.filter(function (o) { return !(miss[o] && now - miss[o] < missGap(o)); }).slice(0, HOPS_MAX);
    if (!cand.length) return Promise.resolve(false);
    return Promise.all(cand.map(function (o) { return probe(o, d.jukebox); })).then(function (ok) {
      var route = [], fresh = {};
      Object.keys(miss).forEach(function (o) { if (own.indexOf(o) >= 0) fresh[o] = miss[o]; });
      cand.forEach(function (o, i) { if (ok[i]) { route.push(o); delete fresh[o]; } else fresh[o] = Date.now(); });
      lset(K.miss, JSON.stringify(fresh));
      tell("probe", { results: cand.map(function (o, i) { return { o: o, ok: !!ok[i] }; }),
                      ms: Date.now() - now, known: !!known });
      if (!route.length) return false;
      if (Date.now() - (+lget(K.tried) || 0) < TRY_GAP) {  // nedávno jsme cestu zkoušeli — žádné smyčky
        tell("skip", { why: "recent", n: route.length });
        return false;
      }
      if (known && interacted) {  // člověk už něco dělá — nepřerušovat, příště
        tell("skip", { why: "interacting", n: route.length });
        return false;
      }
      var n = hex(16);
      if (!n || !sset(GO, JSON.stringify({ n: n, back: location.href, t: Date.now() }))) {
        tell("skip", { why: "session", n: route.length });
        return false;
      }
      lset(K.tried, String(Date.now()));
      tell("trip_start", { route: route, known: !!known });
      leaving = true;
      // assign, ne replace: kdyby adresa po cestě přestala odpovídat, je tahle stránka
      // v historii a tlačítko Zpět na ni vrátí (mezistránky se už navzájem nahrazují).
      location.assign(route[0] + "/identity#give&to=" + encodeURIComponent(here) + "&n=" + n +
                      "&r=" + encodeURIComponent(route.slice(1).join(",")) + "&c=");
      return true;
    });
  }

  /* ---------- stránka /identity: předání účtu ---------- */

  function params() {
    var out = { mode: "" };
    String(location.hash || "").replace(/^#/, "").split("&").forEach(function (part, i) {
      if (i === 0) { out.mode = part; return; }
      var eq = part.indexOf("=");
      if (eq > 0) { try { out[part.slice(0, eq)] = decodeURIComponent(part.slice(eq + 1)); } catch (e) {} }
    });
    return out;
  }
  function codesOf(text) {
    return String(text || "").split(",").filter(function (c) { return /^[A-Za-z0-9_-]{16,64}$/.test(c); }).slice(0, HOPS_MAX + 2);
  }
  function say(text) {
    try { var el = document.getElementById("msg"); if (el) el.textContent = text; var b = document.getElementById("home"); if (b) b.hidden = false; } catch (e) {}
  }

  // Tahle adresa vydá kód za svůj účet (má-li jaký) a pošle prohlížeč dál.
  function give(p) {
    var local = lget(K.client);
    if (!validCid(local)) local = "";
    var route = String(p.r || "").split(",").filter(Boolean), codes = codesOf(p.c);
    return post("/api/identity/sync", { client: local }).then(function (d) {
      var own = (d.origins || []).filter(isOrigin);
      var fine = d.here && d.here === location.origin && own.indexOf(p.to) >= 0 && p.to !== d.here &&
                 /^[0-9a-f]{32}$/.test(p.n || "") && route.length <= HOPS_MAX &&
                 route.every(function (o) { return own.indexOf(o) >= 0; });
      if (!fine) { say("Tahle stránka jen předává tvůj účet mezi adresami jukeboxu."); return "stopped"; }
      adopt(d);
      var mine = validCid(d.client) ? d.client : "";
      return post("/api/identity/offer", { client: mine, theme: lget(K.theme) || "", tokens: parse(lget(K.tokens), {}) })
        .then(function (o) { if (o && o.code) codes.push(o.code); }, function () {})
        .then(function () { return onward(d, p, route, codes, [], !!d.known); });
    }, function () {
      say("Jukebox teď neodpovídá. Zkus to prosím za chvíli.");
      return "stopped";
    });
  }
  // Další adresa v cestě, která z tohohle prohlížeče odpovídá; jinak zpátky tam, odkud cesta vyšla.
  function onward(d, p, route, codes, skipped, account) {
    if (!route.length) {
      tell("hop", { next: "back", skipped: skipped, account: account });
      location.replace(p.to + "/identity#take&n=" + p.n + "&c=" + codes.join(","));
      return Promise.resolve("back");
    }
    var next = route[0], rest = route.slice(1);
    if (next === d.here || next === p.to) return onward(d, p, rest, codes, skipped, account);
    return probe(next, d.jukebox).then(function (ok) {
      if (!ok) return onward(d, p, rest, codes, skipped.concat([next]), account);
      tell("hop", { next: next, skipped: skipped, account: account });
      location.replace(next + "/identity#give&to=" + encodeURIComponent(p.to) + "&n=" + p.n +
                       "&r=" + encodeURIComponent(rest.join(",")) + "&c=" + codes.join(","));
      return "next";
    });
  }

  // Zpátky na adrese, kde cesta začala: kódy přijme jen ten, kdo ji sám začal.
  function take(p) {
    var go = parse(sget(GO), null);
    sdel(GO);
    var back = go && typeof go.back === "string" && go.back.indexOf(location.origin + "/") === 0 &&
               go.back.indexOf("/identity") !== location.origin.length ? go.back : "/";
    if (!go || !go.n || go.n !== p.n) {
      tell("trip_done", { how: "ignored", codes: codesOf(p.c).length });
      location.replace("/");
      return Promise.resolve("ignored");
    }
    var local = lget(K.client);
    if (!validCid(local)) { local = newCid(); lset(K.client, local); }
    return post("/api/identity/redeem", { client: local, codes: codesOf(p.c) }).then(function (d) {
      adopt(d);
      var text = noteText(d.note);
      if (text) sset(NOTE, text);
      return "taken";
    }, function () { return "failed"; }).then(function (how) {
      tell("trip_done", { how: how, ms: Date.now() - (go.t || Date.now()), codes: codesOf(p.c).length });
      location.replace(back);
      return how;
    });
  }

  var api = { ready: null, done: null };
  // Věta pro člověka, když se s jeho účtem něco stalo (jednou).
  api.note = function (res) {
    var text = (res && res.note) || sget(NOTE) || "";
    sdel(NOTE);
    return text;
  };
  // Stránka zůstala otevřená a účet se mezitím spojil jinde (jiná záložka, jiná adresa):
  // zeptat se s id, které stránka drží; null = beze změny, jinak nový účet a věta pro člověka.
  api.check = function (current) {
    if (!validCid(current)) return Promise.resolve(null);
    return post("/api/identity/sync", { client: current }).then(function (d) {
      if (!validCid(d.client) || d.client === current) return null;
      adopt(d);
      tell("moved", {});
      return { client: d.client, nick: d.nick || "", tag: d.tag || "", changed: true, note: noteText(d.note) };
    }).catch(function () { return null; });
  };
  if (location.pathname === "/identity") {
    var p = params();
    api.done = p.mode === "give" ? give(p) : p.mode === "take" ? take(p)
      : (say("Tahle stránka jen předává tvůj účet mezi adresami jukeboxu."), Promise.resolve("stopped"));
  } else {
    api.ready = boot();
  }
  window.ytdjIdentity = api;
})();
