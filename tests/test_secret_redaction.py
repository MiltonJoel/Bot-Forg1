"""A live forg1 session cookie was written to logs/forg1_monitor.log in
plaintext. Playwright embeds the full request headers in its "Call log", and the
monitor's read handler logged the exception whole. These pin the redaction."""

from app.utils.retry import short_error

# Shape of the real leak, verbatim from the incident.
_PLAYWRIGHT_ERROR = """APIRequestContext.get: getaddrinfo ENOTFOUND www.forg1.com
Call log:
  - → GET https://www.forg1.com/predicciones
    - user-agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0.0.0
    - cookie: forg1_session=eyJ1aWQiOiI0N2I0NjZhYS0yOTM3LTQ0NDYtODI4NS00NDBjMTM=; ph_phc_x=y
"""


def test_call_log_with_the_session_cookie_is_not_logged() -> None:
    line = short_error(Exception(_PLAYWRIGHT_ERROR))
    assert "forg1_session" not in line
    assert "eyJ1aWQ" not in line
    assert "cookie" not in line.lower()


def test_the_useful_diagnosis_survives() -> None:
    # Redaction is worthless if it also destroys the reason.
    assert short_error(Exception(_PLAYWRIGHT_ERROR)) == (
        "APIRequestContext.get: getaddrinfo ENOTFOUND www.forg1.com"
    )


def test_a_cookie_on_the_first_line_is_redacted_too() -> None:
    # Belt and braces for a future Playwright format that leads with headers.
    line = short_error(Exception("boom cookie: forg1_session=SECRETVALUE trailing"))
    assert "SECRETVALUE" not in line
    assert "<redacted>" in line


def test_ordinary_errors_pass_through() -> None:
    assert short_error(ValueError("plain failure")) == "plain failure"
    assert short_error(None) == "None"
