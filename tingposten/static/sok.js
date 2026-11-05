/* Tingposten search (the Søk page): loads indeks.json next to the page and matches
   in the browser. Every word in the query must occur in the sak's headline, ingress,
   number, tema or organ; headline hits rank first, then the newest meeting. Reader
   text is only ever set with textContent, and the index text is shown through text
   nodes, so nothing becomes HTML. Nothing is sent anywhere. */
(function () {
  "use strict";
  var form = document.querySelector("[data-sok]");
  if (!form) return;
  var felt = form.querySelector("input[name=q]");
  var ut = document.querySelector("[data-sok-treff]");
  var status = document.querySelector("[data-sok-status]");
  var indeks = null, lasting = null, timer = null, MAKS = 100;

  function norm(s) { return (s || "").toLowerCase().replace(/[^\p{L}\p{N}\s\/.-]/gu, " "); }
  function ord(q) { return norm(q).split(/\s+/).filter(function (o) { return o.length >= 2; }); }

  function hent() {
    if (!lasting) {
      lasting = fetch(form.getAttribute("data-sok"), { credentials: "omit" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (d) {
          indeks = d.map(function (p) {
            p._t = norm(p.t);
            p._a = norm([p.t, p.i, p.n, p.e, p.o].join(" "));
            return p;
          });
          return indeks;
        });
    }
    return lasting;
  }

  function sok(q) {
    var o = ord(q), treff = [];
    if (!o.length) return treff;
    indeks.forEach(function (p) {
      var poeng = 0;
      for (var k = 0; k < o.length; k++) {
        if (p._t.indexOf(o[k]) >= 0) poeng += 3;
        else if (p._a.indexOf(o[k]) >= 0) poeng += 1;
        else return;
      }
      treff.push([poeng, p]);
    });
    treff.sort(function (a, b) { return b[0] - a[0] || (a[1].d < b[1].d ? 1 : a[1].d > b[1].d ? -1 : 0); });
    return treff.map(function (x) { return x[1]; });
  }

  /* Text with the query's words wrapped in <mark>, built from text nodes. */
  function merket(tekst, o) {
    var frag = document.createDocumentFragment(), lav = norm(tekst), i = 0;
    while (i < tekst.length) {
      var neste = -1, lengde = 0;
      for (var k = 0; k < o.length; k++) {
        var pos = lav.indexOf(o[k], i);
        if (pos >= 0 && (neste < 0 || pos < neste)) { neste = pos; lengde = o[k].length; }
      }
      if (neste < 0) { frag.appendChild(document.createTextNode(tekst.slice(i))); break; }
      if (neste > i) frag.appendChild(document.createTextNode(tekst.slice(i, neste)));
      var m = document.createElement("mark");
      m.textContent = tekst.slice(neste, neste + lengde);
      frag.appendChild(m);
      i = neste + lengde;
    }
    return frag;
  }

  function rot() {
    // The page is bergen/sok/, so the site's root is two steps up.
    return form.getAttribute("data-rot") || "../../";
  }

  function vis(q) {
    var o = ord(q), treff = sok(q);
    ut.textContent = "";
    if (!o.length) { status.textContent = q.trim() ? "Skriv minst to tegn." : ""; return; }
    if (!treff.length) { status.textContent = "Ingen treff for «" + q.trim() + "»."; return; }
    status.textContent = (treff.length === 1 ? "Ett treff" : treff.length + " treff") + " for «" + q.trim() + "»"
      + (treff.length > MAKS ? ", de " + MAKS + " beste vises." : ".");
    treff.slice(0, MAKS).forEach(function (p) {
      var li = document.createElement("li");
      li.className = "blokk rad sok-rad";
      var kicker = document.createElement("p");
      kicker.className = "kicker";
      var rad = document.createElement("span");
      rad.className = "kicker-rad";
      var tema = document.createElement("span");
      tema.className = "kicker-tema";
      tema.textContent = p.e;
      rad.appendChild(tema);
      var del = document.createElement("span");
      del.className = "kicker-del";
      del.textContent = p.o + ", " + p.dt;
      rad.appendChild(del);
      if (p.n) {
        var nr = document.createElement("span");
        nr.className = "kicker-del kicker-nr";
        nr.appendChild(merket(p.n, o));
        rad.appendChild(nr);
      }
      kicker.appendChild(rad);
      li.appendChild(kicker);
      var tekst = document.createElement("div");
      tekst.className = "rad-tekst";
      var h = document.createElement("h2");
      h.className = "rad-tittel";
      var a = document.createElement("a");
      a.className = "blokklenke";
      a.href = rot() + p.u;
      a.appendChild(merket(p.t, o));
      h.appendChild(a);
      tekst.appendChild(h);
      if (p.i) {
        var dek = document.createElement("p");
        dek.className = "rad-dek";
        dek.appendChild(merket(p.i, o));
        tekst.appendChild(dek);
      }
      li.appendChild(tekst);
      ut.appendChild(li);
    });
  }

  function kjor(skrivAdresse) {
    var q = felt.value;
    if (skrivAdresse && window.history && history.replaceState) {
      var adresse = q.trim() ? "?q=" + encodeURIComponent(q.trim()) : location.pathname;
      history.replaceState(null, "", adresse);
    }
    if (!ord(q).length) { vis(q); return; }
    status.textContent = indeks ? "" : "Henter sakene …";
    hent().then(function () { vis(felt.value); })
      .catch(function () { status.textContent = "Fikk ikke hentet sakene. Prøv igjen om litt."; });
  }

  form.addEventListener("submit", function (e) { e.preventDefault(); kjor(true); });
  felt.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { kjor(true); }, 150);
  });

  var start = new URLSearchParams(location.search).get("q");
  if (start) { felt.value = start; kjor(false); }
  else hent().catch(function () {});
})();
