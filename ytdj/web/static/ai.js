/* AI tvorba na stránce Hlasování (hlášení z jukeboxu #37, FUNKCE F-AI-…).
 *
 * Samostatný kousek: sekci si vloží do #votesView sám (za seznamy hlasů),
 * data bere z /api/ai a mění přes POST /api/ai. Barvy jen z proměnných
 * stránky (--text, --line, --err…), takže sedí Den i Noc. Všechny texty ze
 * serveru jdou přes esc().
 */
(function () {
  "use strict";
  var view = document.getElementById("votesView");
  if (!view || document.getElementById("aiSec")) return;

  var css = document.createElement("style");
  css.textContent =
    "#aiSec { margin-top: 22px; }" +
    "#aiSec .ainow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 10px 12px; margin: 4px 0 10px;" +
    "  border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }" +
    "#aiSec .ainow .meta { flex: 1 1 200px; min-width: 0; }" +
    "#aiSec .ainow .ibtn { flex: none; }" +
    "#aiSec .ainow .kind, #aiSec .ainow .t, #aiSec .ainow .a { display: block; }" +
    "#aiSec .ainow .kind { font-size: .72rem; color: var(--text-faint); text-transform: uppercase; letter-spacing: .08em; }" +
    "#aiSec .ainow .t { font-size: .93rem; overflow-wrap: anywhere; }" +
    "#aiSec .ainow .a { font-size: .8rem; color: var(--text-faint); overflow-wrap: anywhere; }" +
    "#aiSec h3 { margin: 16px 0 4px; font-size: .78rem; font-weight: 650; color: var(--text-faint);" +
    "  text-transform: uppercase; letter-spacing: .08em; }" +
    "#aiSec .why { display: block; font-size: .8rem; color: var(--text-dim); overflow-wrap: anywhere; margin-top: 2px; }" +
    "#aiSec .vs.ai { color: var(--err); background: color-mix(in srgb, var(--err) 13%, transparent); }" +
    "#aiSec details summary { cursor: pointer; min-height: 44px; display: flex; align-items: center;" +
    "  font-size: .85rem; color: var(--text-dim); }" +
    "#aiSec .acts { flex-wrap: wrap; justify-content: flex-end; }" +
    "@media (max-width: 480px) { #aiSec .vlist li { flex-wrap: wrap; } #aiSec .vlist .acts { width: 100%; }" +
    "  #aiSec .vlist .ibtn { flex: 1; } #aiSec .ainow .ibtn { flex: 1 1 100%; } }";
  document.head.appendChild(css);

  var sec = document.createElement("section");
  sec.className = "card vsec";
  sec.id = "aiSec";
  sec.setAttribute("aria-labelledby", "aiH");
  sec.innerHTML =
    '<h2 class="sec-h" id="aiH">AI tvorba <span class="count" id="aiN"></span></h2>' +
    '<p class="hint2" id="aiHint">Interpreti, jejichž hudbu dělá umělá inteligence. Jukebox je sám nenabízí — ' +
    "rádio, podkres ani „pusť oblíbené“ je nepustí; když si je někdo výslovně přeje, zahrají se s poznámkou. " +
    "Označit i vrátit může kdokoli s přezdívkou, vždycky je vidět kdo.</p>" +
    '<div class="ainow" id="aiNow" hidden></div>' +
    '<p class="impmsg" id="aiMsg" role="status" aria-live="polite"></p>' +
    '<ul class="list vlist" id="aiList"></ul>' +
    '<h3 id="aiSugH" hidden>Návrhy — hrají dál, dokud je někdo nepotvrdí</h3>' +
    '<ul class="list vlist" id="aiSug" aria-labelledby="aiSugH"></ul>' +
    '<details id="aiNotBox" hidden><summary id="aiNotS">Není AI</summary><ul class="list vlist" id="aiNot"></ul></details>';
  var grid = view.querySelector(".vgrid");
  if (grid && grid.parentNode === view) view.insertBefore(sec, grid.nextSibling); else view.appendChild(sec);

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function when(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000), now = new Date();
    var hm = d.getHours() + ":" + ("0" + d.getMinutes()).slice(-2);
    if (d.toDateString() === now.toDateString()) return "dnes " + hm;
    return d.getDate() + ". " + (d.getMonth() + 1) + ". " + hm;
  }
  function client() {
    try { return localStorage.getItem("ytdj.client") || ""; } catch (e) { return ""; }
  }
  var A = { data: null, timer: null, busy: false };

  function say(text, kind) {
    var m = $("aiMsg");
    m.textContent = text || "";
    m.className = "impmsg" + (kind ? " " + kind : "");
  }

  function row(it, list, i) {
    var acts = "";
    if (it.state === "ai") acts = btn(list, i, "neni_ai", "Není AI");
    else if (it.state === "navrzeno") acts = btn(list, i, "ai", "Potvrdit: AI") + btn(list, i, "neni_ai", "Není AI");
    else acts = btn(list, i, "ai", "Je to AI");
    var badge = it.state === "ai" ? '<span class="vs ai">AI — nenabízí se</span>'
              : it.state === "navrzeno" ? '<span class="vs pend">návrh</span>' : '<span class="vs low">není AI</span>';
    var by = it.nick ? "označil(a) " + (it.mine ? "ty" : it.nick) : it.by;
    if (it.state === "neni_ai" && it.nick) by = "rozhodl(a) " + (it.mine ? "ty" : it.nick);
    return '<li><span class="meta"><span class="kind">interpret</span><span class="t">' + esc(it.name) + "</span>" +
           '<span class="vss">' + badge + "</span>" +
           (it.reason ? '<span class="why">' + esc(it.reason) + "</span>" : "") +
           '<span class="who2">' + esc(by) + (it.at ? ", " + esc(when(it.at)) : "") + "</span>" +
           '</span><span class="acts">' + acts + "</span></li>";
  }
  function btn(list, i, state, label) {
    return '<button class="ibtn' + (state === "ai" ? " warn" : "") + '" type="button" data-ai="' + list + ":" + i + ":" + state + '">' +
           esc(label) + "</button>";
  }

  function render() {
    var d = A.data || {};
    var ai = d.ai || [], sug = d.suggested || [], not = d.not_ai || [];
    $("aiN").textContent = ai.length ? "· " + ai.length : "";
    $("aiList").innerHTML = ai.length ? ai.map(function (it, i) { return row(it, "ai", i); }).join("")
      : '<li class="empty">Zatím nikdo — kdo z AI se ozve, označ ho tlačítkem nahoře, když hraje.</li>';
    $("aiSugH").hidden = !sug.length;
    $("aiSug").innerHTML = sug.map(function (it, i) { return row(it, "suggested", i); }).join("");
    $("aiNotBox").hidden = !not.length;
    $("aiNotS").textContent = "Není AI (" + not.length + ")";
    $("aiNot").innerHTML = not.map(function (it, i) { return row(it, "not_ai", i); }).join("");
    var n = d.now, box = $("aiNow");
    if (!n || !n.name) { box.hidden = true; box.innerHTML = ""; return; }
    var f = n.flag, act;
    if (f && f.state === "ai") act = '<button class="ibtn" type="button" data-now="neni_ai">Není AI</button>';
    else act = '<button class="ibtn warn" type="button" data-now="ai">🤖 Je to AI</button>';
    var note = f && f.state === "ai" ? (n.wish ? "na seznamu AI — hraje, protože si to někdo přál" : "na seznamu AI")
             : f && f.state === "navrzeno" ? "mezi návrhy" : f && f.state === "neni_ai" ? "potvrzeno, že není AI" : "";
    box.innerHTML = '<span class="meta"><span class="kind">hraje teď</span><span class="t">' + esc(n.name) + "</span>" +
                    '<span class="a">' + esc(n.title) + (note ? " · " + esc(note) : "") + "</span></span>" + act;
    box.hidden = false;
  }

  async function load() {
    try {
      var res = await fetch("/api/ai?client=" + encodeURIComponent(client()), { cache: "no-store" });
      if (!res.ok) throw new Error();
      A.data = await res.json();
      render();
    } catch (e) {
      if (!A.data) $("aiList").innerHTML = '<li class="empty">Seznam teď nejde načíst — jukebox neodpovídá.</li>';
    }
  }

  async function send(body) {
    if (A.busy) return;
    A.busy = true;
    body.client = client();
    try {
      var res = await fetch("/api/ai", { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(body) });
      var d = null;
      try { d = await res.json(); } catch (e) {}
      if (!res.ok) throw new Error((d && d.error) || "Jukebox to teď nevzal — zkus to prosím znovu.");
      say(d.message || "Zapsáno.", "ok");
    } catch (e) {
      say(e.message, "err");
    }
    A.busy = false;
    load();
  }

  sec.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest("[data-ai],[data-now]");
    if (!b || b.disabled) return;
    if (b.hasAttribute("data-now")) { b.disabled = true; send({ state: b.getAttribute("data-now") }); return; }
    var p = b.getAttribute("data-ai").split(":");
    var key = { ai: "ai", suggested: "suggested", not_ai: "not_ai" }[p[0]];
    var it = ((A.data || {})[key] || [])[Number(p[1])];
    if (!it) return;
    b.disabled = true;
    send({ state: p[2], key: it.key });
  });

  function shown() { return !view.hasAttribute("hidden"); }
  function sync() {
    clearInterval(A.timer);
    if (!shown()) return;
    load();
    A.timer = setInterval(function () { if (!document.hidden && shown()) load(); }, 20000);
  }
  new MutationObserver(sync).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  sync();
})();
