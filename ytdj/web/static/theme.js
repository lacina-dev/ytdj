/* Vzhled Auto / Den / Noc (FUNKCE F-WEB-09, F-WEB-10) — společné pro všechny stránky.
   Načítá se v <head> před styly, takže se zvolený vzhled nastaví dřív, než se
   stránka poprvé vykreslí. Volba je v localStorage (jen tohle zařízení); když
   úložiště nejde, stránka jede podle zařízení a volba platí do zavření stránky.
   Barvy jsou v CSS: <html data-theme="light|dark">, bez atributu = podle zařízení. */
(function () {
  "use strict";
  var KEY = "ytdj.theme";
  var BG = { light: "#faf7f2", dark: "#0e1013" };  // --bg obou vzhledů (lišta prohlížeče)
  var root = document.documentElement;
  var current = "";

  function valid(t) { return t === "light" || t === "dark" ? t : ""; }
  function saved() {
    try { return valid(localStorage.getItem(KEY)); } catch (e) { return current; }
  }
  function apply(t) {
    current = valid(t);
    if (current) root.setAttribute("data-theme", current); else root.removeAttribute("data-theme");
    var metas = document.querySelectorAll('meta[name="theme-color"]'), i;
    for (i = 0; i < metas.length; i++) {
      var own = /dark/.test(metas[i].getAttribute("media") || "") ? "dark" : "light";
      metas[i].setAttribute("content", BG[current || own]);
    }
    // přepínač je v HTML skrytý — bez tohoto skriptu by tlačítka nic nedělala
    var boxes = document.querySelectorAll("[data-theme-switch]");
    for (i = 0; i < boxes.length; i++) boxes[i].removeAttribute("hidden");
    var btns = document.querySelectorAll("[data-theme-set]");
    for (i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-pressed", String(valid(btns[i].getAttribute("data-theme-set")) === current));
    }
  }
  function choose(t) {
    t = valid(t);
    try { if (t) localStorage.setItem(KEY, t); else localStorage.removeItem(KEY); } catch (e) {}
    apply(t);
  }

  apply(saved());
  document.addEventListener("DOMContentLoaded", function () { apply(current); });  // tlačítka už existují
  document.addEventListener("click", function (ev) {
    var b = ev.target && ev.target.closest ? ev.target.closest("[data-theme-set]") : null;
    if (b) choose(b.getAttribute("data-theme-set"));
  });
  // změna v jiné záložce a návrat tlačítkem Zpět (stránka z paměti prohlížeče)
  window.addEventListener("storage", function (ev) { if (ev.key === KEY || ev.key === null) apply(saved()); });
  window.addEventListener("pageshow", function (ev) { if (ev.persisted) apply(saved()); });
})();
