/* Share links on a sak or a debattinnlegg (macros.html, del_lenker). The page already
   has plain Facebook and e-mail links; this makes «Del» open the phone's own share menu,
   which reaches Instagram, Messenger, SMS and the rest, and adds a «Kopier lenke» button. */
(function () {
  "use strict";

  function knapp(tekst, klikk) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "del-knapp";
    b.textContent = tekst;
    b.addEventListener("click", klikk);
    return b;
  }

  document.querySelectorAll("[data-del]").forEach(function (rot) {
    var url = rot.dataset.url, tittel = rot.dataset.tittel;
    var etikett = rot.querySelector(".del-etikett");
    if (navigator.share) {  // «Del» itself opens the share menu (owner 1.11: no separate button)
      var meny = knapp("Del", function () {
        navigator.share({ title: tittel, url: url }).catch(function () {});
      });
      meny.className = "del-etikett del-meny";
      meny.setAttribute("aria-label", "Del via telefonens delemeny (Instagram, SMS og mer)");
      etikett.replaceWith(meny);
    }
    if (navigator.clipboard) {
      var kopier = knapp("Kopier lenke", function () {
        navigator.clipboard.writeText(url).then(function () {
          kopier.textContent = "Lenken er kopiert";
          setTimeout(function () { kopier.textContent = "Kopier lenke"; }, 2500);
        }).catch(function () {});
      });
      kopier.setAttribute("aria-live", "polite");
      rot.append(kopier);
    }
  });
})();
