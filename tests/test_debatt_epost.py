from tingposten.debatt import epost


def test_login_mail_has_sender_name_date_and_link():
    msg = epost.melding("leser@eksempel.no", "https://debatt.example/inn?t=abc", "tingposten@freetime.no")
    assert msg["From"] == "Tingposten <tingposten@freetime.no>"
    assert msg["Date"]                                    # without one, some providers count it as spam
    assert msg["Message-ID"].endswith("@freetime.no>")
    assert "https://debatt.example/inn?t=abc" in msg.get_content()
