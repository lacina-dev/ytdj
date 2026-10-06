/* Jeden člověk na všech adresách jukeboxu (FUNKCE F-NICK-07 až F-NICK-10, POZADAVKY #69).
   Prohlížeč drží id klienta zvlášť pro každou adresu (jméno i port). Tenhle skript:
   1. na běžné stránce srovná id se serverem (cookie platí pro všechny porty téhož jména)
      a zjistí, které další adresy jukeboxu z tohohle prohlížeče opravdu odpovídají;
   2. když nějaká odpovídá a účet s ní ještě není srovnaný, projde je jednou navigací
      (/identity#give → … → /identity#take) a vrátí se. Každá adresa vydá jen jednorázový
      kód — id klienta se do adresy nepíše. Kódy přijme jen stránka, která cestu sama
      začala (značka v sessionStorage), a jen mezi adresami, které vyjmenoval server.
   Nikdy nenaviguje na adresu, kterou si těsně předtím neověřil. Bez úložiště, bez
   odpovědi serveru nebo bez dalších adres se nic neděje a stránka jede jako dřív. */
(function () {
  "use strict";
  var K = { client: "ytdj.client", nick: "ytdj.nick", who: "ytdj.who", tag: "ytdj.tag",
            tokens: "ytdj.tokens", theme: "ytdj.theme", miss: "ytdj.id.miss", tried: "ytdj.id.try" };
  var GO = "ytdj.id.go", NOTE = "ytdj.id.note";
  var PROBE_MS = 2500, SYNC_MS = 6000;
  var TRY_GAP = 10 * 60 * 1000;      // další pokus o cestu nejdřív za 10 minut (žádné smyčky)
  var MISS_GAP = 6 * 3600 * 1000;    // adresu, která neodpověděla, zkusit znovu až za 6 hodin
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

  /* ---------- běžná stránka ---------- */

  var interacted = false;
  function boot() {
    var local = lget(K.client);
    if (!validCid(local)) { local = newCid(); lset(K.client, local); }
    var before = local;
    try {
      ["pointerdown", "keydown"].forEach(function (name) {
        window.addEventListener(name, function () { interacted = true; }, true);
      });
    } catch (e) {}
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
    if (!lset(K.miss, lget(K.miss) || "{}") || !sset(GO + ".test", "1")) return Promise.resolve(false);  // bez úložiště nic
    sdel(GO + ".test");
    var now = Date.now();
    if (now - (+lget(K.tried) || 0) < TRY_GAP) return Promise.resolve(false);
    var miss = parse(lget(K.miss), {});
    var cand = own.filter(function (o) {
      return o !== here && linked.indexOf(o) < 0 && !(miss[o] && now - miss[o] < MISS_GAP);
    }).slice(0, HOPS_MAX);
    if (!cand.length) return Promise.resolve(false);
    return Promise.all(cand.map(function (o) { return probe(o, d.jukebox); })).then(function (ok) {
      var route = [], fresh = {};
      Object.keys(miss).forEach(function (o) { if (own.indexOf(o) >= 0) fresh[o] = miss[o]; });
      cand.forEach(function (o, i) { if (ok[i]) { route.push(o); delete fresh[o]; } else fresh[o] = Date.now(); });
      lset(K.miss, JSON.stringify(fresh));
      if (!route.length) return false;
      if (known && interacted) return false;  // člověk už něco dělá — nepřerušovat, příště
      var n = hex(16);
      if (!n || !sset(GO, JSON.stringify({ n: n, back: location.href, t: Date.now() }))) return false;
      lset(K.tried, String(Date.now()));
      location.replace(route[0] + "/identity#give&to=" + encodeURIComponent(here) + "&n=" + n +
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
        .then(function () { return onward(d, p, route, codes); });
    }, function () {
      say("Jukebox teď neodpovídá. Zkus to prosím za chvíli.");
      return "stopped";
    });
  }
  // Další adresa v cestě, která z tohohle prohlížeče odpovídá; jinak zpátky tam, odkud cesta vyšla.
  function onward(d, p, route, codes) {
    if (!route.length) {
      location.replace(p.to + "/identity#take&n=" + p.n + "&c=" + codes.join(","));
      return Promise.resolve("back");
    }
    var next = route[0], rest = route.slice(1);
    if (next === d.here || next === p.to) return onward(d, p, rest, codes);
    return probe(next, d.jukebox).then(function (ok) {
      if (!ok) return onward(d, p, rest, codes);
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
    if (!go || !go.n || go.n !== p.n) { location.replace("/"); return Promise.resolve("ignored"); }
    var local = lget(K.client);
    if (!validCid(local)) { local = newCid(); lset(K.client, local); }
    return post("/api/identity/redeem", { client: local, codes: codesOf(p.c) }).then(function (d) {
      adopt(d);
      var text = noteText(d.note);
      if (text) sset(NOTE, text);
      return "taken";
    }, function () { return "failed"; }).then(function (how) { location.replace(back); return how; });
  }

  var api = { ready: null, done: null };
  // Věta pro člověka, když se s jeho účtem něco stalo (jednou).
  api.note = function (res) {
    var text = (res && res.note) || sget(NOTE) || "";
    sdel(NOTE);
    return text;
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
