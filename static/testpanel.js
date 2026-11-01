/* The test panel (templates/testpanel.html, debatt/testpanel.py). The invite link's
   token (#t=…) is kept in this browser's localStorage and sent as a bearer token; it is
   never put in a URL again. Text from the server is only ever set with textContent. */
(function () {
  "use strict";
  var NOKKEL = "tingposten-testpanel";
  var rot = document.querySelector("[data-testpanel]");
  if (!rot) return;
  var api = rot.dataset.api.replace(/\/$/, "");
  var status = rot.querySelector(".testpanel-status");

  function hentToken() {
    var m = location.hash.match(/[#&]t=([A-Za-z0-9_-]{16,100})/);
    if (m) {
      try { localStorage.setItem(NOKKEL, m[1]); } catch (e) {}
      history.replaceState(null, "", location.pathname + location.search);
      return m[1];
    }
    try { return localStorage.getItem(NOKKEL); } catch (e) { return null; }
  }
  var token = hentToken();

  function el(tag, attrs, barn) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "tekst") e.textContent = attrs[k];
      else if (k === "klasse") e.className = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    });
    (barn || []).forEach(function (b) { if (b) e.append(b); });
    return e;
  }

  function kall(metode, sti, data) {
    return fetch(api + sti, {
      method: metode,
      headers: Object.assign({ Authorization: "Bearer " + token }, data ? { "Content-Type": "application/json" } : {}),
      body: data ? JSON.stringify(data) : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.feil || "Noe gikk galt. Prøv igjen.");
        return j;
      });
    });
  }

  var SKALA = [["bor_vite", "Bør folk vite om dette?", ["Nei", "", "", "", "Alle bør"]],
               ["konkret", "Endrer noe seg i hverdagen nå?", ["Ingenting", "", "", "", "Mye"]]];
  var VALG = [["bunke", "Hvor hører saken hjemme?", [["forside", "Forside"], ["kort", "Kort"], ["stoy", "Støy"]]],
              ["klar", "Er kortet klart?", [["ja", "Ja"], ["delvis", "Delvis"], ["nei", "Nei"]]]];
  var BUNKENAVN = { forside: "forside", kort: "kort", stoy: "støy" };
  var antall = { alle: 0, ferdig: 0 };
  // Part 2 (testpanel.funn): once every sak is rated, answers that don't fit together are
  // pointed out; the tester changes them or keeps them. Saves made then are marked del 2.
  var artikler = {}, del2 = null, gjennomgang = false;
  var del2Boks = el("section", { klasse: "testpanel-del2", hidden: "" });

  function ferdig(v) { return v && v.bor_vite != null && v.konkret != null && v.bunke != null; }

  function teller() {
    status.textContent = antall.ferdig + " av " + antall.alle + " saker vurdert.";
  }

  function andreBoks(a) {
    if (!a) return null;
    if (!a.antall) return el("p", { klasse: "testpanel-andre smaatekst", tekst: "Ingen andre har vurdert saken ennå." });
    var deler = [];
    if (a.bor_vite != null) deler.push("bør vite " + String(a.bor_vite).replace(".", ","));
    if (a.konkret != null) deler.push("endring " + String(a.konkret).replace(".", ","));
    var bunker = Object.keys(a.bunker).map(function (k) { return BUNKENAVN[k] + " " + a.bunker[k]; });
    if (bunker.length) deler.push(bunker.join(", "));
    var klar = Object.keys(a.klar).map(function (k) { return k + " " + a.klar[k]; });
    if (klar.length) deler.push("klart: " + klar.join(", "));
    var boks = el("div", { klasse: "testpanel-andre" }, [
      el("p", { klasse: "smaatekst", tekst: (a.antall === 1 ? "1 annen tester: " : a.antall + " andre testere: ") + deler.join(" · ") }),
    ]);
    if (a.kommentarer.length) {
      boks.append(el("ul", { klasse: "smaatekst" }, a.kommentarer.map(function (k) { return el("li", { tekst: "«" + k + "»" }); })));
    }
    return boks;
  }

  function sakBoks(s) {
    var mine = Object.assign({}, s.mine || {});
    var varFerdig = ferdig(mine);
    if (varFerdig) antall.ferdig += 1;
    var andre = el("div", {}, [andreBoks(s.andre)]);
    var melding = el("p", { klasse: "testpanel-melding smaatekst", role: "status", "aria-live": "polite" });

    function lagre(data) {
      melding.textContent = "Lagrer …";
      return kall("POST", "/api/testpanel/vurdering", Object.assign({ sak: s.key, del: gjennomgang ? 2 : 1 }, data)).then(function (r) {
        mine = Object.assign({}, r.mine || {});
        var naFerdig = ferdig(mine);
        if (naFerdig !== varFerdig) { antall.ferdig += naFerdig ? 1 : -1; varFerdig = naFerdig; teller(); }
        andre.replaceChildren(andreBoks(r.andre) || "");
        melding.textContent = "Lagret.";
        if (antall.ferdig === antall.alle) oppdaterFunn();
        return r;
      }).catch(function (f) { melding.textContent = f.message; throw f; });
    }

    function gruppe(navn, sporsmal, valg) {
      var knapper = valg.map(function (v) {
        var b = el("button", { type: "button", "aria-pressed": String(mine[navn] === v[0]), tekst: v[1],
          onclick: function () {
            var ny = mine[navn] === v[0] ? null : v[0];
            var data = {}; data[navn] = ny;
            lagre(data).then(function () {
              knapper.forEach(function (k, i) { k.setAttribute("aria-pressed", String(mine[navn] === valg[i][0])); });
            }).catch(function () {});
          } });
        return b;
      });
      return el("div", { klasse: "testpanel-sporsmal", role: "group", "aria-label": sporsmal }, [
        el("p", { klasse: "testpanel-etikett", tekst: sporsmal }), el("div", { klasse: "testpanel-knapper" }, knapper)]);
    }

    var grupper = SKALA.map(function (g) {
      return gruppe(g[0], g[1], [1, 2, 3, 4, 5].map(function (n) {
        return [n, String(n) + (g[2][n - 1] ? " " + g[2][n - 1].toLowerCase() : "")];
      }));
    }).concat(VALG.map(function (g) { return gruppe(g[0], g[1], g[2]); }));

    var felt = el("textarea", { rows: "2", maxlength: "1000", "aria-label": "Kommentar (valgfritt)",
      placeholder: "Kommentar (valgfritt): hva var uklart, hva manglet?" });
    felt.value = mine.kommentar || "";
    felt.addEventListener("change", function () { lagre({ kommentar: felt.value }).catch(function () {}); });

    var kort = el("dl", { klasse: "testpanel-felt" }, [].concat.apply([], s.felt.map(function (f) {
      return [el("dt", { tekst: f.label }), el("dd", { tekst: f.tekst })];
    })));
    var funnBoks = el("div", { klasse: "testpanel-funn", hidden: "" });
    var behold = el("button", { type: "button", tekst: "Behold svaret", onclick: function () {
      lagre({ behold: true }).then(oppdaterFunn).catch(function () {});
    } });
    var artikkel = el("article", { klasse: "testpanel-sak", id: "s-" + s.key.replace(/[^A-Za-z0-9-]/g, "-") }, [
      el("p", { klasse: "kicker", tekst: s.nr + (s.status ? " · " + s.status : "") }),
      el("h3", { klasse: "testpanel-tittel", tekst: s.overskrift }),
      s.ingress ? el("p", { klasse: "testpanel-ingress", tekst: s.ingress }) : null,
      el("details", {}, [el("summary", { tekst: "Kort fortalt" }), kort]),
      el("p", { klasse: "smaatekst" }, [el("a", { href: s.lenke, target: "_blank", rel: "noopener", tekst: "Les hele saken" })]),
      funnBoks,
      el("div", { klasse: "testpanel-svar" }, grupper.concat([felt, melding])),
      andre,
    ]);
    artikler[s.key] = { el: artikkel, funn: funnBoks, behold: behold };
    return artikkel;
  }

  function tegnFunn() {
    var saker = (del2 && del2.saker) || {};
    var nokler = Object.keys(saker);
    Object.keys(artikler).forEach(function (k) {
      var a = artikler[k], grunner = saker[k];
      a.funn.hidden = !grunner;
      a.el.classList.toggle("har-funn", !!grunner);
      if (grunner) {
        a.funn.replaceChildren(el("p", { klasse: "testpanel-etikett", tekst: "Se over:" }),
          el("ul", {}, grunner.map(function (g) { return el("li", { tekst: g }); })),
          el("p", { klasse: "smaatekst", tekst: "Endre svaret under, eller behold det hvis det er det du mener." }),
          el("div", { klasse: "testpanel-knapper" }, [a.behold]));
      }
    });
    del2Boks.hidden = !(del2 && del2.klar);
    if (!del2 || !del2.klar) return;
    var tekst = nokler.length
      ? (nokler.length === 1 ? "Ett svar" : nokler.length + " svar") + " passer ikke helt med de andre svarene dine."
      : "Svarene dine henger godt sammen.";
    var knapp = el("button", { type: "button", tekst: gjennomgang ? "Vis alle sakene" : "Vis bare sakene å se over",
      onclick: function () { gjennomgang = !gjennomgang; rot.classList.toggle("gjennomgang", gjennomgang); tegnFunn(); } });
    del2Boks.replaceChildren.apply(del2Boks, [
      el("h2", { klasse: "etikett etikett-strek", tekst: "Del 2: Se over svarene dine" }),
      el("p", { tekst: "Takk, du har vurdert alle sakene! " + tekst +
        (nokler.length ? " Det finnes ingen fasit: endre svaret hvis du svarte noe annet enn du mente, ellers trykk «Behold svaret»." : "") }),
      del2.forside ? el("p", { klasse: "testpanel-forside", tekst: del2.forside }) : null,
      nokler.length || gjennomgang ? el("div", { klasse: "testpanel-knapper" }, [knapp]) : null].filter(Boolean));
    if (!nokler.length && gjennomgang) { gjennomgang = false; rot.classList.remove("gjennomgang"); }
  }

  function oppdaterFunn() {
    return kall("GET", "/api/testpanel").then(function (d) { del2 = d.del2; tegnFunn(); }).catch(function () {});
  }

  if (!token) {
    status.textContent = "Du trenger en invitasjonslenke for å bruke testpanelet. Spør redaktøren.";
    return;
  }
  kall("GET", "/api/testpanel").then(function (d) {
    antall.alle = d.moter.reduce(function (n, m) { return n + m.saker.length; }, 0);
    rot.insertBefore(el("p", { klasse: "testpanel-hei", tekst: "Hei, " + d.navn + "!" }), status);
    rot.insertBefore(del2Boks, status.nextSibling);
    if (!antall.alle) { status.textContent = "Ingen saker å vurdere akkurat nå. Kom tilbake senere."; return; }
    d.moter.forEach(function (m) {
      rot.append(el("section", { klasse: "testpanel-mote" }, [
        el("h2", { klasse: "etikett etikett-strek", tekst: m.organ + " · " + m.dato_tekst }),
      ].concat(m.saker.map(sakBoks))));
    });
    teller();
    del2 = d.del2;
    tegnFunn();
  }).catch(function (f) { status.textContent = f.message; });
})();
