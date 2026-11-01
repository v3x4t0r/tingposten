"""The panel's plain-language summary of a run (admin.oppsummer), from run-daily.sh's log."""
from tingposten import admin

SJEKK = """2026-11-01T19:55:01+01:00 start (sjekk)
2026-11-01T19:55:01+01:00 == hent
2026-11-01 19:55:22,915 INFO Bergen bystyre 2026: 369 items, {'ny': 0, 'vedtatt': 0, 'endret': 0, 'forsvunnet': 0}
2026-11-01 19:55:57,939 INFO Byrådet – saker avgjort av byrådet 2026: 245 items, {'ny': 0, 'vedtatt': 0, 'endret': 3, 'forsvunnet': 0}
2026-11-01T19:55:58+01:00 == last-ned
2026-11-01 19:56:01,095 INFO 1 PDF(s) to fetch across 1 meeting(s) since 2026-08-01
2026-11-01T19:56:01+01:00 done""".splitlines()

MANUELL = """2026-11-01T20:05:35+01:00 start (manuell)
2026-11-01T20:05:35+01:00 == hent
2026-11-01 20:05:36,000 INFO Bergen bystyre 2026: 369 items, {'ny': 3, 'vedtatt': 2, 'endret': 0, 'forsvunnet': 0}
2026-11-01T20:05:35+01:00 == last-ned
2026-11-01 20:05:38,491 INFO 4 PDF(s) to fetch across 1 meeting(s) since 2026-08-01
2026-11-01T20:05:44+01:00 == kjor
2026-11-01 20:06:04,645 WARNING codex:standard: malformed answer (codex failed (exit 1): unexpected status 401 Unauthorized
2026-11-01 20:06:21,708 WARNING codex:standard can't answer; claude:fable takes over for the rest of this run
2026-11-01 20:06:30,000 INFO skriv: 3 saker
2026-11-01 20:07:00,000 INFO skriv: {'antall': {'skrevet': 2, 'skrevet (over grensen)': 0, 'feil': 1}, 'feil': ["x: You've hit your 5-hour limit · resets 1:20pm (Europe/Oslo)"]}
2026-11-01 20:07:23,930 INFO klassifiser: 87 saker
2026-11-01 20:09:49,030 INFO omskriv: 4 avviste kort skrives på nytt
2026-11-01 20:09:49,030 INFO skriv: 4 saker
2026-11-01 20:12:22,402 INFO omskriv: {'omskriv': {'antall': {'omskrevet': 4}, 'feil': []}, 'omskriv/sjekk': {'antall': {'sjekket': 4}, 'feil': []}}
2026-11-01 20:12:38,484 INFO artikkel: 8 saker
2026-11-01 20:14:29,795 INFO artikkel: {'antall': {'skrevet': 8}, 'feil': []}
2026-11-01T20:18:11+01:00 == bygg
2026-11-01 20:18:47,023 INFO built /home/x/site: {'sider': 632, 'saker': 537, 'fremhevet': 59, 'moter': 54, 'rettet': 0}
2026-11-01T20:18:47+01:00 == publiser
2026-11-01 20:18:52,755 INFO asked statichost to deploy tingposten: Started build 3LW
2026-11-01 20:18:53,228 WARNING IndexNow svarte 403: {"errorCode":"UserForbiddedToAccessSite"}
2026-11-01T20:18:53+01:00 done""".splitlines()


def test_a_check_with_nothing_new_says_so():
    o = admin.oppsummer(SJEKK)
    assert o["punkter"] == ["Ingen nye saker eller vedtak hos Bergen kommune.",
                            "1 PDF fra 1 møte kan hentes med «Last ned og publiser»."]
    assert o["advarsler"] == []


def test_a_run_counts_new_work_not_the_classifier_and_names_the_problems():
    o = admin.oppsummer(MANUELL, forrige_bygg=(537, 52))
    p = o["punkter"]
    assert "Nytt hos Bergen kommune: Bergen bystyre (5)." in p and "Hentet 4 PDF-er fra 1 møte." in p
    assert "Skrev 2 nye kort." in p and "Skrev om 4 avviste kort." in p and "Skrev 8 bakgrunnsartikler." in p
    assert "1 sak kunne ikke skrives denne gangen." in p
    assert "Siden har 537 saker, 59 av dem fremhevet (før 52)." in p and "Publisert på tingposten.no." in p
    assert not any("87" in x for x in p)          # «klassifiser: 87 saker» is not 87 new saker
    a = " ".join(o["advarsler"])
    assert "Codex er logget ut" in a and "codex login" in a
    assert "nullstilles kl. 13.20" in a and "IndexNow" in a


def test_the_last_run_carries_its_summary_and_the_build_before(tmp_path):
    logg = tmp_path / "2026-11-01.log"
    logg.write_text("\n".join(MANUELL[:1] + ["2026-11-01 08:00:00,000 INFO built /x: {'sider': 1, 'saker': 537, "
                                             "'fremhevet': 52, 'moter': 1}", "2026-11-01T08:00:01+01:00 done"]
                              + MANUELL) + "\n")
    s = admin.siste_kjoring(tmp_path)
    assert "Siden har 537 saker, 59 av dem fremhevet (før 52)." in s["oppsummering"]["punkter"]


def test_waiting_work_on_an_empty_data_dir(tmp_path):
    assert admin.arbeid([], tmp_path, vent=True) == {"kort": 0, "omskriv": 0, "artikler": 0}
