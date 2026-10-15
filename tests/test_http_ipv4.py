"""The IPv4-first opener failed silently once already: HTTPConnection.__init__
assigns self._create_connection as an instance attribute, so the original
class-level override was shadowed and every request still stalled ~20s on the
dead IPv6 route. These pin the wiring."""

from app.utils import http as http_ipv4


def test_connection_actually_uses_the_ipv4_resolver() -> None:
    connection = http_ipv4._IPv4FirstHTTPSConnection("example.com")
    # The bug: this was socket.create_connection despite the class attribute.
    assert connection._create_connection is http_ipv4._ipv4_first_create_connection


# Address selection and multi-record fallback live in test_http_multi_address.py.


def test_tls_verification_is_left_on() -> None:
    context = getattr(http_ipv4._IPv4FirstHTTPSHandler(), "_context", None)
    assert context is not None
    assert context.verify_mode == 2  # ssl.CERT_REQUIRED
    assert context.check_hostname is True
