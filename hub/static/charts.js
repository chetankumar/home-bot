/* Hover, touch and keyboard layer for the progress chart (ctx.charts). The chart is complete without it:
   every value is also in the table view. Text from the page goes in with textContent only. */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function setup(wrap) {
    var svg = wrap.querySelector("svg");
    var tip = wrap.querySelector(".viz-tip");
    var cross = svg && svg.querySelector(".viz-cross");
    var dots = svg && svg.querySelector(".viz-cdots");
    var band = svg && svg.querySelector(".viz-band");
    if (!svg || !tip || !cross) return;
    var data;
    try { data = JSON.parse(svg.getAttribute("data-chart")); } catch (e) { return; }
    var slot = { actual: "s1", month: "s2", recent: "s3", needed: "s-ref", big: "s-ref", col: "s1" };
    var current = null;

    function show(i) {
      i = Math.max(1, Math.min(data.n, i));
      var day = data.days[i - 1];
      current = i;
      cross.setAttribute("x1", day.x); cross.setAttribute("x2", day.x);
      cross.setAttribute("visibility", "visible");
      if (band && day.cw) {
        band.setAttribute("x", day.x - day.cw / 2); band.setAttribute("width", day.cw);
        band.setAttribute("visibility", "visible");
      }
      while (dots.firstChild) dots.removeChild(dots.firstChild);
      tip.textContent = "";
      tip.appendChild(el("div", "t", day.label));
      day.rows.forEach(function (r) {
        var row = el("div", "r");
        var key = el("span", "viz-key " + (slot[r.key] || "s1"));
        row.appendChild(key);
        row.appendChild(el("span", "n", r.name));
        row.appendChild(el("span", "v", r.value));
        tip.appendChild(row);
        if (r.extra) tip.appendChild(el("div", "x", r.extra));
        if (r.y !== undefined) {
          var ring = document.createElementNS(NS, "circle");
          ring.setAttribute("cx", day.x); ring.setAttribute("cy", r.y); ring.setAttribute("r", 6);
          ring.setAttribute("class", "viz-ring");
          var dot = document.createElementNS(NS, "circle");
          dot.setAttribute("cx", day.x); dot.setAttribute("cy", r.y); dot.setAttribute("r", 4);
          dot.setAttribute("class", "viz-dot " + (slot[r.key] || "s1"));
          dots.appendChild(ring); dots.appendChild(dot);
        }
      });
      tip.removeAttribute("hidden");
      var box = svg.getBoundingClientRect();
      var px = day.x * (box.width / data.W);
      var w = tip.offsetWidth;
      tip.style.left = (px + 14 + w > box.width ? Math.max(0, px - 14 - w) : px + 14) + "px";
    }

    function hide() {
      current = null;
      cross.setAttribute("visibility", "hidden");
      if (band) band.setAttribute("visibility", "hidden");
      tip.setAttribute("hidden", "");
      while (dots.firstChild) dots.removeChild(dots.firstChild);
    }

    function dayAt(ev) {
      var box = svg.getBoundingClientRect();
      var x = (ev.clientX - box.left) * (data.W / box.width);
      return Math.round(((x - data.ML) / data.PW) * data.n);
    }

    svg.addEventListener("pointermove", function (ev) { show(dayAt(ev)); });
    svg.addEventListener("pointerdown", function (ev) { show(dayAt(ev)); });
    svg.addEventListener("pointerleave", function () { if (document.activeElement !== svg) hide(); });
    svg.addEventListener("focus", function () { show(current || data.d || 1); });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", function (ev) {
      var map = { ArrowLeft: -1, ArrowRight: 1, PageDown: -7, PageUp: 7 };
      if (ev.key in map) { ev.preventDefault(); show((current || data.d || 1) + map[ev.key]); }
      else if (ev.key === "Home") { ev.preventDefault(); show(1); }
      else if (ev.key === "End") { ev.preventDefault(); show(data.n); }
      else if (ev.key === "Escape") { hide(); }
    });
  }

  function init() {
    document.querySelectorAll(".viz-wrap").forEach(function (w) {
      if (!w.hasAttribute("data-ready")) { w.setAttribute("data-ready", ""); setup(w); }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
