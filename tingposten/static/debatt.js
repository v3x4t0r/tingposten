/* Tingposten debatt: the comment section on a sak page and the login page.
   Talks to the comment server (data-api); the session is a bearer token kept in
   this site's localStorage, never a cookie. Reader text is only ever set with
   textContent: nothing a reader writes becomes HTML. */
(function () {
  "use strict";
  var NOKKEL = "tingposten-debatt-okt";
  var MAKS = 3000;

  function hentOkt() { try { return localStorage.getItem(NOKKEL); } catch (e) { return null; } }
  function lagreOkt(o) { try { if (o) localStorage.setItem(NOKKEL, o); else localStorage.removeItem(NOKKEL); } catch (e) {} }

  function el(tag, attrs, barn) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === "tekst") e.textContent = attrs[k];
      else if (k === "klasse") e.className = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) e.setAttribute(k, attrs[k]);
    }
    (barn || []).forEach(function (b) { if (b) e.append(b); });
    return e;
  }

  function api(base, metode, sti, kropp) {
    var h = {};
    var okt = hentOkt();
    if (okt) h["Authorization"] = "Bearer " + okt;
    if (kropp !== undefined) h["Content-Type"] = "application/json";
    return fetch(base + sti, { method: metode, headers: h, body: kropp === undefined ? undefined : JSON.stringify(kropp) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (r.status === 401 && okt) lagreOkt(null);
          if (!r.ok) { var f = new Error(data.feil || "Noe gikk galt."); f.status = r.status; throw f; }
          return data;
        });
      });
  }

  var tidsformat = window.Intl && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat("nb", { numeric: "auto" }) : null;
  function naar(iso) {
    var t = new Date(iso), sek = (t - new Date()) / 1000;
    if (tidsformat && Math.abs(sek) < 86400 * 7) {
      var trinn = [[60, "second"], [3600, "minute"], [86400, "hour"], [Infinity, "day"]], forrige = 1;
      for (var i = 0; i < trinn.length; i++) {
        if (Math.abs(sek) < trinn[i][0]) return tidsformat.format(Math.round(sek / forrige), trinn[i][1]);
        forrige = trinn[i][0];
      }
    }
    return t.toLocaleDateString("nb-NO", { day: "numeric", month: "long", year: "numeric" });
  }

  /* --- the comment section ------------------------------------------------ */

  function Debatt(rot) {
    this.rot = rot;
    this.api = rot.dataset.api;
    this.sak = rot.dataset.debattSak;
    this.side = rot.dataset.side;
    this.regler = rot.dataset.regler;
    this.sortering = "best";
    this.meg = null;
    this.innhold = rot.querySelector(".debatt-innhold");
    this.melding = el("p", { klasse: "debatt-melding", role: "status", "aria-live": "polite" });
    this.start();
  }

  Debatt.prototype.si = function (tekst, feil) {
    this.melding.textContent = tekst || "";
    this.melding.classList.toggle("feil", !!feil);
  };

  Debatt.prototype.start = function () {
    var self = this;
    var meg = hentOkt() ? api(this.api, "GET", "/api/meg").catch(function () { return null; }) : Promise.resolve(null);
    meg.then(function (m) { self.meg = m; return self.last(); });
  };

  Debatt.prototype.last = function () {
    var self = this;
    return api(this.api, "GET", "/api/sak/" + this.sak + "/kommentarer?sortering=" + this.sortering)
      .then(function (data) { self.tegn(data); })
      .catch(function () {
        self.innhold.replaceChildren(el("p", { klasse: "debatt-nede", tekst: "Kommentarfeltet er utilgjengelig akkurat nå. Prøv igjen senere." }));
      });
  };

  Debatt.prototype.tegn = function (data) {
    var self = this;
    var sorter = el("div", { klasse: "debatt-sorter", role: "group", "aria-label": "Sorter kommentarene" });
    [["best", "Best"], ["topp", "Topp"], ["nye", "Nye"]].forEach(function (s) {
      sorter.append(el("button", { type: "button", "aria-pressed": String(self.sortering === s[0]), tekst: s[1],
        onclick: function () { self.sortering = s[0]; self.last(); } }));
    });
    var hode = el("div", { klasse: "debatt-topp" }, [
      el("p", { klasse: "debatt-antall", tekst: data.antall === 1 ? "1 kommentar" : data.antall + " kommentarer" }),
      data.antall > 1 ? sorter : null,
    ]);
    var liste = el("ol", { klasse: "debatt-liste" });
    data.kommentarer.forEach(function (k) { liste.append(self.kommentar(k, 0)); });
    this.innhold.replaceChildren(this.konto(), this.melding, hode,
      data.kommentarer.length ? liste : el("p", { klasse: "debatt-tom", tekst: "Ingen har kommentert ennå." }));
  };

  /* Login, display name and account, or the box for a new comment. */
  Debatt.prototype.konto = function () {
    var self = this;
    if (!this.meg) {
      var epost = el("input", { type: "email", name: "epost", required: true, autocomplete: "email", id: "debatt-epost" });
      var skjema = el("form", { klasse: "debatt-inn", onsubmit: function (e) {
        e.preventDefault();
        var knapp = skjema.querySelector("button");
        knapp.disabled = true;
        api(self.api, "POST", "/api/innlogging", { epost: epost.value, tilbake: self.side })
          .then(function () { skjema.replaceChildren(el("p", { tekst: "Sjekk e-posten din. Lenken virker i 15 minutter, én gang." })); })
          .catch(function (f) { knapp.disabled = false; self.si(f.message, true); });
      } }, [
        el("label", { for: "debatt-epost", tekst: "Logg inn for å skrive. Skriv e-postadressen din, så får du en innloggingslenke:" }),
        el("div", { klasse: "debatt-rad" }, [epost, el("button", { type: "submit", tekst: "Send lenke" })]),
        el("p", { klasse: "smaatekst" }, [document.createTextNode("Ingen passord. Les "),
          el("a", { href: this.regler, tekst: "debattreglene" }), document.createTextNode(" og hva vi lagrer.")]),
      ]);
      return skjema;
    }
    if (!this.meg.navn) {
      var navn = el("input", { type: "text", name: "navn", required: true, minlength: 2, maxlength: 30, id: "debatt-navn" });
      var navnskjema = el("form", { klasse: "debatt-inn", onsubmit: function (e) {
        e.preventDefault();
        var knapp = navnskjema.querySelector("button");
        knapp.disabled = true;
        self.si("Sjekker navnet mot debattreglene …");
        api(self.api, "POST", "/api/meg", { navn: navn.value })
          .then(function (m) { self.meg.navn = m.navn; self.si(""); self.last(); })
          .catch(function (f) { knapp.disabled = false; self.si(f.message, true); });
      } }, [
        el("label", { for: "debatt-navn", tekst: "Velg navnet som vises over kommentarene dine (det kan være et kallenavn):" }),
        el("div", { klasse: "debatt-rad" }, [navn, el("button", { type: "submit", tekst: "Lagre" })]),
      ]);
      return navnskjema;
    }
    return el("div", { klasse: "debatt-skriv" }, [
      this.skjema(null, null, function () { self.last(); }),
      el("p", { klasse: "debatt-meg smaatekst" }, [
        document.createTextNode("Du skriver som "), el("strong", { tekst: this.meg.navn }), document.createTextNode(". "),
        el("button", { type: "button", klasse: "lenkeknapp", tekst: "Logg ut", onclick: function () {
          api(self.api, "POST", "/api/utlogging", {}).finally(function () { lagreOkt(null); self.meg = null; self.last(); });
        } }),
        document.createTextNode(" · "),
        el("button", { type: "button", klasse: "lenkeknapp", tekst: "Last ned dataene mine", onclick: function () { self.eksport(); } }),
        document.createTextNode(" · "),
        this.bekreftKnapp("Slett kontoen", "Trykk igjen for å slette kontoen og alle kommentarene dine", function () {
          api(self.api, "DELETE", "/api/meg").then(function () { lagreOkt(null); self.meg = null; self.si("Kontoen er slettet."); self.last(); })
            .catch(function (f) { self.si(f.message, true); });
        }),
      ]),
    ]);
  };

  /* A comment box: new comment (forelder null), reply, or edit (eksisterende). */
  Debatt.prototype.skjema = function (forelder, eksisterende, ferdig) {
    var self = this;
    var id = "debatt-tekst-" + (eksisterende ? "e" + eksisterende.id : forelder ? "s" + forelder : "ny");
    var felt = el("textarea", { id: id, rows: forelder || eksisterende ? 3 : 4, maxlength: MAKS, required: true });
    if (eksisterende) felt.value = eksisterende.tekst;
    var teller = el("span", { klasse: "debatt-teller smaatekst", "aria-live": "off" });
    function tell() { teller.textContent = felt.value.length > MAKS - 300 ? (MAKS - felt.value.length) + " tegn igjen" : ""; }
    felt.addEventListener("input", tell);
    var knapp = el("button", { type: "submit", tekst: eksisterende ? "Lagre endringen" : forelder ? "Svar" : "Publiser" });
    var skjema = el("form", { klasse: "debatt-skjema", onsubmit: function (e) {
      e.preventDefault();
      knapp.disabled = true;
      var kall = eksisterende
        ? api(self.api, "PUT", "/api/kommentar/" + eksisterende.id, { tekst: felt.value })
        : api(self.api, "POST", "/api/kommentar", { sak: self.sak, tekst: felt.value, forelder: forelder });
      kall.then(function (k) {
        felt.value = "";
        if (k.status === "venter") self.vent(k.id);
        ferdig(k);
      }).catch(function (f) { self.si(f.message, true); }).finally(function () { knapp.disabled = false; });
    } }, [
      el("label", { for: id, klasse: forelder || eksisterende ? "vh" : null,
        tekst: eksisterende ? "Endre kommentaren" : forelder ? "Svar" : "Skriv en kommentar" }),
      felt,
      el("div", { klasse: "debatt-rad" }, [knapp, teller]),
      forelder || eksisterende ? null : el("p", { klasse: "smaatekst" }, [
        document.createTextNode("Kommentaren vurderes mot "), el("a", { href: this.regler, tekst: "debattreglene" }),
        document.createTextNode(" før den vises. Alle meninger er lov; personangrep, trusler og hets er det ikke."),
      ]),
    ]);
    return skjema;
  };

  /* While the moderator judges a comment, ask again every few seconds (up to 5 minutes). */
  Debatt.prototype.vent = function (id) {
    var self = this, forsok = 0;
    this.si("Kommentaren vurderes …");
    var t = setInterval(function () {
      forsok += 1;
      api(self.api, "GET", "/api/kommentar/" + id).then(function (k) {
        if (k.status !== "venter" || forsok > 100) {
          clearInterval(t);
          self.si(k.status === "publisert" ? "Kommentaren er publisert." : k.status === "avvist" ? "Kommentaren ble stoppet."
            : k.status === "til_redaktor" ? "Kommentaren venter på redaktøren." : "");
          self.last();
        }
      }).catch(function () { clearInterval(t); });
    }, 3000);
  };

  Debatt.prototype.kommentar = function (k, dybde) {
    var self = this;
    var li = el("li", { klasse: "debatt-k" + (k.egen ? " egen" : ""), id: "k" + k.id });
    if (k.status === "slettet" || k.status === "fjernet" || k.status === "skjult") {
      li.append(el("p", { klasse: "debatt-borte", tekst: k.status === "slettet" ? "Slettet av forfatteren."
        : k.status === "fjernet" ? "Fjernet av redaksjonen." : "Kommentaren er ikke synlig akkurat nå." }));
    } else {
      li.append(el("p", { klasse: "debatt-hvem" }, [
        el("strong", { tekst: k.navn }), document.createTextNode(" · "),
        el("time", { datetime: k.tid, tekst: naar(k.tid) }), k.endret ? document.createTextNode(" · endret") : null,
      ]));
      li.append(el("p", { klasse: "debatt-tekst", tekst: k.tekst }));
      if (k.egen && k.status !== "publisert") li.append(this.status(k));
      li.append(this.handlinger(k, li, dybde));
    }
    if (k.svar && k.svar.length) {
      var svar = el("ol", { klasse: "debatt-svar" + (dybde >= 3 ? " flat" : "") });
      k.svar.forEach(function (s) { svar.append(self.kommentar(s, dybde + 1)); });
      li.append(svar);
    }
    return li;
  };

  Debatt.prototype.status = function (k) {
    var self = this;
    if (k.status === "venter") return el("p", { klasse: "debatt-status", tekst: "Vurderes mot debattreglene …" });
    if (k.status === "til_redaktor") return el("p", { klasse: "debatt-status", tekst: "Venter på redaktøren. Bare du ser den så lenge." });
    var boks = el("div", { klasse: "debatt-status stoppet" }, [
      el("p", {}, [el("strong", { tekst: "Stoppet: " + (k.regel ? k.regel.navn : "brudd på debattreglene") + ". " }),
        document.createTextNode(k.regel ? k.regel.tekst : "")]),
      el("p", { klasse: "smaatekst" }, [document.createTextNode("Bare du ser den. Du kan endre den, eller "),
        el("button", { type: "button", klasse: "lenkeknapp", tekst: "be redaktøren se på den", onclick: function () {
          api(self.api, "POST", "/api/kommentar/" + k.id + "/manuell", {}).then(function () { self.last(); })
            .catch(function (f) { self.si(f.message, true); });
        } }), document.createTextNode(".")]),
    ]);
    return boks;
  };

  Debatt.prototype.handlinger = function (k, li, dybde) {
    var self = this;
    var rad = el("div", { klasse: "debatt-handlinger" });
    if (k.status === "publisert") {
      var poeng = el("span", { klasse: "debatt-poeng", tekst: String(k.poeng), "aria-label": k.poeng + " poeng" });
      var opp = el("button", { type: "button", klasse: "stem opp", "aria-label": "Stem opp", "aria-pressed": String(k.min_stemme === 1), tekst: "▲" });
      var ned = el("button", { type: "button", klasse: "stem ned", "aria-label": "Stem ned", "aria-pressed": String(k.min_stemme === -1), tekst: "▼" });
      function stem(verdi) {
        if (!self.meg || !self.meg.navn) { self.si("Logg inn for å stemme.", true); return; }
        var ny = k.min_stemme === verdi ? 0 : verdi;
        api(self.api, "POST", "/api/kommentar/" + k.id + "/stem", { verdi: ny }).then(function (s) {
          k.min_stemme = s.min_stemme; k.poeng = s.poeng;
          poeng.textContent = String(s.poeng); poeng.setAttribute("aria-label", s.poeng + " poeng");
          opp.setAttribute("aria-pressed", String(s.min_stemme === 1)); ned.setAttribute("aria-pressed", String(s.min_stemme === -1));
        }).catch(function (f) { self.si(f.message, true); });
      }
      opp.addEventListener("click", function () { stem(1); });
      ned.addEventListener("click", function () { stem(-1); });
      if (k.egen) { opp.disabled = true; ned.disabled = true; }
      rad.append(el("span", { klasse: "debatt-stemmer" }, [opp, poeng, ned]));
      if (this.meg && this.meg.navn) {
        rad.append(el("button", { type: "button", klasse: "lenkeknapp", tekst: "Svar", onclick: function () {
          if (li.querySelector(":scope > .debatt-skjema")) return;
          var s = self.skjema(k.id, null, function () { self.last(); });
          li.insertBefore(s, li.querySelector(":scope > .debatt-svar"));
          s.querySelector("textarea").focus();
        } }));
        if (!k.egen) rad.append(this.bekreftKnapp("Rapporter", "Trykk igjen for å rapportere til redaktøren", function () {
          api(self.api, "POST", "/api/kommentar/" + k.id + "/rapporter", {}).then(function () { self.si("Takk. Redaktøren ser på den."); })
            .catch(function (f) { self.si(f.message, true); });
        }));
      }
    }
    if (k.egen) {
      rad.append(el("button", { type: "button", klasse: "lenkeknapp", tekst: "Endre", onclick: function () {
        var tekst = li.querySelector(":scope > .debatt-tekst");
        var s = self.skjema(null, k, function () { self.last(); });
        tekst.replaceWith(s);
        s.querySelector("textarea").focus();
      } }));
      rad.append(this.bekreftKnapp("Slett", "Trykk igjen for å slette", function () {
        api(self.api, "DELETE", "/api/kommentar/" + k.id).then(function () { self.last(); }).catch(function (f) { self.si(f.message, true); });
      }));
    }
    return rad;
  };

  /* A button that asks for a second press instead of a browser dialog. */
  Debatt.prototype.bekreftKnapp = function (tekst, sporsmal, gjor) {
    var b = el("button", { type: "button", klasse: "lenkeknapp", tekst: tekst });
    var klar = false, tid;
    b.addEventListener("click", function () {
      if (klar) { clearTimeout(tid); gjor(); return; }
      klar = true; b.textContent = sporsmal;
      tid = setTimeout(function () { klar = false; b.textContent = tekst; }, 5000);
    });
    return b;
  };

  Debatt.prototype.eksport = function () {
    var self = this;
    api(this.api, "GET", "/api/meg/eksport").then(function (data) {
      var blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
      var a = el("a", { href: URL.createObjectURL(blob), download: "tingposten-debatt.json" });
      document.body.append(a); a.click(); a.remove();
    }).catch(function (f) { self.si(f.message, true); });
  };

  /* --- the login page ---------------------------------------------------- */

  function loggInn(rot) {
    var status = rot.querySelector(".logg-inn-status");
    var m = /[#&]t=([A-Za-z0-9_-]{20,100})/.exec(location.hash);
    history.replaceState(null, "", location.pathname);  // the token leaves the address bar at once
    if (!m) { status.textContent = "Lenken er ufullstendig. Be om en ny fra kommentarfeltet."; return; }
    api(rot.dataset.api, "POST", "/api/innlogging/bekreft", { token: m[1] }).then(function (svar) {
      lagreOkt(svar.okt);
      var tilbake = svar.tilbake && svar.tilbake.indexOf(location.origin + "/") === 0 ? svar.tilbake : rot.dataset.forside;
      status.textContent = "Du er logget inn.";
      location.replace(tilbake + "#debatt");
    }).catch(function (f) { status.textContent = f.message; });
  }

  document.querySelectorAll("[data-debatt-sak]").forEach(function (rot) { new Debatt(rot); });
  document.querySelectorAll("[data-debatt-innlogging]").forEach(loggInn);
})();
