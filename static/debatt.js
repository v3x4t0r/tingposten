/* Tingposten debatt: the comment section (sak and debattinnlegg pages), the account box
   and "Mest kommentert" on the Debatt page, the "Logg inn" link in the masthead, and the
   login page.
   Talks to the comment server (data-api); the session is a bearer token kept in
   this site's localStorage, never a cookie. Reader text is only ever set with
   textContent: nothing a reader writes becomes HTML. */
(function () {
  "use strict";
  var NOKKEL = "tingposten-debatt-okt";
  var NAVN = "tingposten-debatt-navn";   // only for the masthead link, so other pages need no request
  var MAKS = 3000;

  function hentOkt() { try { return localStorage.getItem(NOKKEL); } catch (e) { return null; } }
  function lagreOkt(o) {
    try { if (o) localStorage.setItem(NOKKEL, o); else { localStorage.removeItem(NOKKEL); localStorage.removeItem(NAVN); } } catch (e) {}
    if (!o) visKonto();
  }

  /* The masthead's "Logg inn" link: the display name once logged in, from this browser alone. */
  function visKonto(meg) {
    if (meg !== undefined) {
      try { if (meg && meg.navn) localStorage.setItem(NAVN, meg.navn); else localStorage.removeItem(NAVN); } catch (e) {}
    }
    var navn = null, okt = hentOkt();
    try { navn = localStorage.getItem(NAVN); } catch (e) {}
    document.querySelectorAll("[data-konto-lenke]").forEach(function (a) {
      a.textContent = okt ? (navn || "Min konto") : "Logg inn";
      a.classList.toggle("innlogget", !!okt);
    });
  }

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
    meg.then(function (m) { self.meg = m; visKonto(m); return self.last(); });
  };

  Debatt.prototype.last = function () {
    var self = this;
    if (!this.sak) {  // the account box on the Debatt page: no comments to load
      this.innhold.replaceChildren(this.konto(), this.melding);
      return Promise.resolve();
    }
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
          .then(function (svar) {
            skjema.replaceChildren(el("p", { tekst: "Sjekk e-posten din. Lenken virker i 15 minutter, én gang." }));
            // Local test server only (tingposten debatt-lokal): the link comes back here instead of by e-mail.
            if (svar.lenke && svar.lenke.indexOf(location.origin + "/") === 0) {
              skjema.append(el("p", {}, [document.createTextNode("Lokal test: "),
                el("a", { href: svar.lenke, tekst: "logg inn nå" })]));
            }
          })
          .catch(function (f) { knapp.disabled = false; self.si(f.message, true); });
      } }, [
        el("label", { for: "debatt-epost", tekst: (this.sak ? "Logg inn for å skrive. " : "Logg inn for å kommentere sakene. ")
          + "Skriv e-postadressen din, så får du en innloggingslenke:" }),
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
          .then(function (m) { self.meg.navn = m.navn; visKonto(self.meg); self.si(""); self.last(); })
          .catch(function (f) { knapp.disabled = false; self.si(f.message, true); });
      } }, [
        el("label", { for: "debatt-navn", tekst: "Velg navnet som vises over kommentarene dine (det kan være et kallenavn):" }),
        el("div", { klasse: "debatt-rad" }, [navn, el("button", { type: "submit", tekst: "Lagre" })]),
      ]);
      return navnskjema;
    }
    if (!this.meg.quiz) return this.quiz();
    return el("div", { klasse: "debatt-skriv" }, [
      this.sak ? this.skjema(null, null, function () { self.last(); }) : null,
      el("p", { klasse: "debatt-meg smaatekst" }, [
        document.createTextNode(this.sak ? "Du skriver som " : "Du er logget inn som "), el("strong", { tekst: this.meg.navn }),
        document.createTextNode(this.sak ? ". " : ". Kommenter på sakene og debattinnleggene. "),
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

  /* The quiz: five example comments, "lov" or "ikke lov", all must be right. The server
     checks the answers and returns why each one is or isn't allowed. */
  Debatt.prototype.quiz = function () {
    var self = this;
    var boks = el("div", { klasse: "debatt-inn debatt-quiz" }, [
      el("p", {}, [el("strong", { tekst: "Før du skriver første gang: " }),
        document.createTextNode("her er seks oppdiktede kommentarer. Ingen har skrevet dem; de viser bare hvor grensen går. "
          + "Svar for hver av dem om den ville vært lov på Tingposten. Alle må være riktige; du ser forklaringen "
          + "etterpå og kan prøve igjen. "),
        el("a", { href: this.regler, tekst: "Les debattreglene" }), document.createTextNode(".")]),
    ]);
    api(this.api, "GET", "/api/quiz").then(function (q) {
      var skjema = el("form", { onsubmit: function (e) {
        e.preventDefault();
        var svar = {};
        q.sporsmal.forEach(function (s) {
          var valgt = skjema.querySelector("input[name='" + s.id + "']:checked");
          if (valgt) svar[s.id] = valgt.value === "lov";
        });
        if (Object.keys(svar).length < q.sporsmal.length) { self.si("Svar på alle eksemplene.", true); return; }
        api(self.api, "POST", "/api/quiz", { svar: svar }).then(function (r) {
          r.svar.forEach(function (x) {
            var f = skjema.querySelector("[data-forklaring='" + x.id + "']");
            f.textContent = "";
            f.append(el("strong", { klasse: "debatt-dom", tekst: x.riktig ? "Riktig." : "Feil." }),
              document.createTextNode(" " + x.forklaring));
            f.className = "debatt-forklaring " + (x.riktig ? "riktig" : "feil");
          });
          if (r.bestatt) {
            self.meg.quiz = true;
            self.si("Alt riktig. Du kan skrive nå.");
            skjema.querySelector("button").replaceWith(el("button", { type: "button", tekst: self.sak ? "Skriv en kommentar" : "Ferdig",
              onclick: function () { self.si(""); self.last(); } }));
          } else {
            self.si("Ikke alt var riktig. Les forklaringene og prøv igjen.", true);
          }
        }).catch(function (f) { self.si(f.message, true); });
      } });
      q.sporsmal.forEach(function (s, i) {
        var navn = s.id;
        skjema.append(el("fieldset", { klasse: "debatt-sporsmal" }, [
          el("legend", {}, [
            el("span", { klasse: "debatt-eksempel-nr", tekst: "Eksempel " + (i + 1) + " av " + q.sporsmal.length + " · oppdiktet" }),
            el("span", { klasse: "debatt-eksempel", tekst: "«" + s.tekst + "»" }),
            el("span", { klasse: "debatt-eksempel-sporsmal", tekst: "Er denne lov?" })]),
          el("label", {}, [el("input", { type: "radio", name: navn, value: "lov" }), document.createTextNode(" Lov")]),
          el("label", {}, [el("input", { type: "radio", name: navn, value: "ikke" }), document.createTextNode(" Ikke lov")]),
          el("p", { "data-forklaring": navn, klasse: "debatt-forklaring", "aria-live": "polite" }),
        ]));
      });
      skjema.append(el("div", { klasse: "debatt-rad" }, [el("button", { type: "submit", tekst: "Sjekk svarene" })]));
      boks.append(skjema);
    }).catch(function (f) { self.si(f.message, true); });
    return boks;
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
      location.replace(tilbake + (/\/bergen\/debatt\/$/.test(tilbake) ? "#konto" : "#debatt"));
    }).catch(function (f) { status.textContent = f.message; });
  }

  /* --- "Mest kommentert" on the Debatt page ------------------------------- */

  function mestKommentert(rot) {
    var liste = rot.querySelector(".mest-liste"), valg = rot.querySelector("select");
    function hent() {
      api(rot.dataset.api, "GET", "/api/mest-kommentert?periode=" + encodeURIComponent(valg.value)).then(function (data) {
        var rader = data.saker.filter(function (s) { return /^https:\/\//.test(s.lenke); }).map(function (s) {
          return el("li", {}, [el("a", { href: s.lenke, tekst: s.tittel }),
            el("span", { klasse: "mest-antall", tekst: s.antall === 1 ? "1 kommentar" : s.antall + " kommentarer" })]);
        });
        liste.replaceChildren.apply(liste, rader.length ? rader : [el("li", { klasse: "mest-tom", tekst: "Ingen kommentarer i perioden ennå." })]);
      }).catch(function () {
        liste.replaceChildren(el("li", { klasse: "mest-tom", tekst: "Listen er utilgjengelig akkurat nå." }));
      });
    }
    valg.addEventListener("change", hent);
    hent();
  }

  /* --- send a debattinnlegg (the Debatt page; logged in) -------------------- */

  function innleggSkjema(rot) {
    if (!hentOkt()) return;  // the placeholder asks the reader to log in first
    var MAKS_ANBEFALT = 4000;
    function felt(navn, etikett, attrs) {
      var id = "innlegg-" + navn;
      var input = el(attrs.tag || "input", Object.assign({ id: id, name: navn }, attrs.attrs));
      return [el("label", { for: id, tekst: etikett }), input];
    }
    var navn = felt("navn", "Fullt navn (innlegget trykkes med det)", { attrs: { required: true, maxlength: 80, autocomplete: "name" } });
    var om = felt("om", "Hvem du er (valgfritt, f.eks. «lærer, bor i Åsane»)", { attrs: { maxlength: 80 } });
    var tittel = felt("tittel", "Tittel", { attrs: { required: true, maxlength: 120 } });
    var tekst = felt("tekst", "Teksten (tom linje mellom avsnitt)", { tag: "textarea", attrs: { required: true, rows: 14, maxlength: 6000 } });
    var teller = el("p", { klasse: "smaatekst innlegg-teller", "aria-live": "polite" });
    function tell() {
      var n = tekst[1].value.length;
      teller.textContent = n + " av " + MAKS_ANBEFALT + " tegn" + (n > MAKS_ANBEFALT ? ": lengre enn vi ber om, redaktøren kan korte ned." : "");
    }
    tekst[1].addEventListener("input", tell); tell();
    var melding = el("p", { klasse: "debatt-melding", role: "status", "aria-live": "polite" });
    var knapp = el("button", { type: "submit", tekst: "Send innlegget" });
    var skjema = el("form", { klasse: "debatt-inn", onsubmit: function (e) {
      e.preventDefault();
      knapp.disabled = true;
      melding.textContent = "Sender …";
      melding.classList.remove("feil");
      api(rot.dataset.api, "POST", "/api/innlegg", { navn: navn[1].value, om: om[1].value, tittel: tittel[1].value, tekst: tekst[1].value })
        .then(function () {
          rot.replaceChildren(el("p", { tekst: "Takk! Innlegget er sendt. Redaktøren leser det og svarer deg på e-post. "
            + "Endrer redaktøren noe, får du se den nye versjonen før den publiseres." }));
        })
        .catch(function (f) { knapp.disabled = false; melding.textContent = f.message; melding.classList.add("feil"); });
    } }, [].concat(navn, om, tittel, tekst, [teller, el("div", { klasse: "debatt-rad" }, [knapp]), melding]));
    rot.replaceChildren(skjema);
  }

  /* --- the writer approves the editor's version (link in the e-mail) -------- */

  function godkjennSide(rot) {
    var status = rot.querySelector(".godkjenn-status");
    var m = /[#&]t=([A-Za-z0-9_-]{20,100})/.exec(location.hash);
    history.replaceState(null, "", location.pathname);
    if (!m) { status.textContent = "Lenken er ufullstendig. Svar på e-posten, så ordner vi det."; return; }
    var token = m[1];
    api(rot.dataset.api, "POST", "/api/innlegg/godkjenning", { token: token }).then(function (d) {
      var tekst = d.tekst.split(/\n\s*\n/).map(function (a) { return el("p", { tekst: a }); });
      var knapp = el("button", { type: "button", tekst: "Godkjenn denne versjonen" });
      var svar = el("p", { klasse: "debatt-melding", role: "status", "aria-live": "polite" });
      knapp.addEventListener("click", function () {
        knapp.disabled = true;
        api(rot.dataset.api, "POST", "/api/innlegg/godkjenn", { token: token }).then(function () {
          svar.textContent = "Takk! Redaktøren publiserer innlegget, og du får en e-post når det er ute.";
          knapp.remove();
        }).catch(function (f) { knapp.disabled = false; svar.textContent = f.message; });
      });
      status.textContent = d.godkjent ? "Du har allerede godkjent denne versjonen." : "Slik vil innlegget ditt stå på Tingposten:";
      rot.append.apply(rot, [el("div", { klasse: "godkjenn-tekst" }, [el("h2", { tekst: d.tittel }),
        el("p", { klasse: "innlegg-byline" }, [el("span", { klasse: "innlegg-navn", tekst: d.navn }), d.om ? el("span", { klasse: "innlegg-om", tekst: d.om }) : null])]
        .concat(tekst)), d.godkjent ? null : knapp, svar,
        el("p", { klasse: "smaatekst", tekst: "Er du ikke enig i endringene, svarer du bare på e-posten." })]
        .filter(Boolean));
    }).catch(function (f) { status.textContent = f.message; });
  }

  visKonto();
  document.querySelectorAll("[data-debatt-sak]").forEach(function (rot) { new Debatt(rot); });
  document.querySelectorAll("[data-mest-kommentert]").forEach(mestKommentert);
  document.querySelectorAll("[data-innlegg-skjema]").forEach(innleggSkjema);
  document.querySelectorAll("[data-innlegg-godkjenn]").forEach(godkjennSide);
  document.querySelectorAll("[data-debatt-innlogging]").forEach(loggInn);
})();
