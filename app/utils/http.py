"""Outbound HTTP helpers: IPv4-first connections, and Cloudflare block detection.

--- IPv6 ---

Measured on this box against api.telegram.org: a dual-stack request takes
`timeout + 0.8s` (3s->3.8, 8s->8.8, 20s->21.0) because the IPv6 connect
blackholes until the socket timeout expires and only then falls back to IPv4 —
which connects in 0.8s every time. With the old 10s timeout that put every
request right at the edge, and sends failed at random with "handshake operation
timed out".

So resolve IPv4 first and only use IPv6 when there is no A record. TLS still
verifies against the hostname (HTTPSConnection passes server_hostname
independently of what the socket connected to), so this is not a security
downgrade.
"""

import http.client
import socket
import urllib.request
from typing import Any


def _ipv4_first_create_connection(
    address: tuple[str, int], timeout: Any = None, source_address: Any = None
) -> socket.socket:
    """Connect preferring IPv4, but try EVERY address before giving up.

    Reordering rather than filtering is the whole point. Resolving to a single
    IPv4 and handing that to socket.create_connection loses the stdlib's
    walk over the remaining records: kick.com publishes two A records, and when
    the first one blackholes, that version retried it forever and never reached
    the second — an endless "urlopen error timed out" while the host was
    perfectly reachable. IPv6 stays in the list, last, so an IPv6-only host
    still works.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)  # stable: IPv4 first

    last: Exception = OSError(f"no address for {host!r}")
    for family, socktype, proto, _, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if isinstance(timeout, (int, float)):
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last


class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # HTTPConnection.__init__ assigns self._create_connection as an INSTANCE
        # attribute, so a class-level override is shadowed and silently ignored.
        # It has to be replaced after super().__init__ runs.
        self._create_connection = _ipv4_first_create_connection


class _IPv4FirstHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> Any:
        # HTTPSHandler stores the SSL context privately; reuse it so cert
        # verification stays exactly as urllib configured it.
        context = getattr(self, "_context", None)
        return self.do_open(_IPv4FirstHTTPSConnection, req, context=context)


_opener = urllib.request.build_opener(_IPv4FirstHTTPSHandler())


def urlopen(request: urllib.request.Request | str, timeout: float) -> Any:
    """Drop-in for urllib.request.urlopen that prefers IPv4."""
    return _opener.open(request, timeout=timeout)


# --- Cloudflare -------------------------------------------------------------
# Both forg1 and Kick sit behind Cloudflare. A challenge or block does NOT look
# like an error: it is usually a 403/503 (sometimes even a 200) carrying an
# interstitial page. The RSC parser then finds no predictions and the bot
# reports "nothing running" forever while actually being locked out — the exact
# silent failure these markers exist to turn into a Telegram alert.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "__cf_chl",
    "checking your browser",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "sorry, you have been blocked",
)
_BLOCK_STATUSES = (403, 429, 503)


class CloudflareBlocked(RuntimeError):
    """Cloudflare served a challenge/block instead of the page. Deliberately not
    a PlaywrightError: retrying a block is counterproductive, and callers report
    it to Telegram rather than treating it as a transient read error."""


def cloudflare_reason(status: int, headers: dict[str, str], body: str) -> str | None:
    """Why this response looks like a Cloudflare block, or None if it's normal.

    Deliberately narrow: forg1 is served THROUGH Cloudflare, so `server:
    cloudflare` is present on every healthy 200 too and can never be the signal
    on its own. Only an interstitial body, a blocking status from Cloudflare, or
    an explicit cf-mitigated header counts.
    """
    lowered = body[:8000].lower()  # markers live in the head of the challenge page
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return f"challenge page (HTTP {status}, matched {marker!r})"

    lower_headers = {k.lower(): v for k, v in headers.items()}
    if lower_headers.get("cf-mitigated"):
        return f"cf-mitigated={lower_headers['cf-mitigated']} (HTTP {status})"
    if status in _BLOCK_STATUSES and "cloudflare" in lower_headers.get("server", "").lower():
        return f"HTTP {status} from Cloudflare"
    return None
