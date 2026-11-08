"""Guards for the localhost HTTP front doors (web API and shim).

A server on 127.0.0.1 is reachable by every web page the user's
browser opens: a page can send it simple cross-site requests, and with
DNS rebinding it can even read the answers. These checks keep the
local trust boundary at "local programs", not "any web page":

- ``Host`` must name a loopback address or an explicitly allowed host
  (the bound address, or MUSE_LINUX_ALLOWED_HOSTS for a reverse
  proxy), which defeats DNS rebinding.
- A request whose ``Origin`` is a page on any other host is refused.
  Pages served from loopback or an allowed host (the bundled web GUI),
  Tauri app origins and requests with no ``Origin`` at all (curl,
  server-side front ends, the CLI) are allowed. A browser request with
  no ``Origin`` that says it came from another site
  (``Sec-Fetch-Site: cross-site`` or ``same-site``, as a page's plain
  GET or an image tag sends) is refused too, unless it is the user
  following a link (``Sec-Fetch-Mode: navigate``).
- JSON endpoints require ``Content-Type: application/json``, so a
  cross-site form or ``text/plain`` fetch cannot reach them without a
  CORS preflight, which a foreign origin never passes.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}
# Origins Tauri gives an app's own pages (gui/src-tauri). Every Tauri
# app on the machine shares them, not only the bundled shell.
DESKTOP_ORIGINS = {"tauri://localhost", "http://tauri.localhost",
                   "https://tauri.localhost"}


def _split_host(value: str) -> str:
    value = (value or "").strip().lower()
    if "://" in value:                     # a URL was given as a host
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):              # [::1]:8770
        return value[1:value.find("]")] if "]" in value else value
    if value.count(":") == 1:              # host:port
        return value.split(":", 1)[0]
    return value


def is_loopback(host: str) -> bool:
    host = _split_host(host)
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


ALLOWED_HOSTS_ENV = "MUSE_LINUX_ALLOWED_HOSTS"


def allowed_hosts(bound_host: str = "", extra=()) -> set[str]:
    """Host names, besides loopback, that may address this server:
    the address it was explicitly bound to, anything passed in, and
    the comma-separated MUSE_LINUX_ALLOWED_HOSTS (for a reverse proxy
    such as tailscale serve that forwards its own host name)."""
    import os

    hosts = {_split_host(h) for h in extra if h}
    bound = _split_host(bound_host)
    if bound and bound not in ("0.0.0.0", "::"):
        hosts.add(bound)
    for item in os.environ.get(ALLOWED_HOSTS_ENV, "").split(","):
        if item.strip():
            hosts.add(_split_host(item))
    return hosts


def host_allowed(host_header: str | None, hosts: set[str]) -> bool:
    if not host_header:
        return False
    host = _split_host(host_header)
    return is_loopback(host) or host in hosts


def origin_allowed(origin: str | None, hosts: set[str]) -> bool:
    """True when the request may proceed as far as Origin is concerned:
    no Origin (not a browser), the desktop shell, or a page served from
    loopback or an allowed host."""
    if not origin:
        return True
    origin = origin.strip().lower()
    if origin in DESKTOP_ORIGINS:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return is_loopback(parts.hostname) or parts.hostname in hosts


FOREIGN_FETCH_SITES = {"cross-site", "same-site"}


def refusal(headers, hosts: set[str]) -> str | None:
    """Why a request must be refused, or None to let it through."""
    if not host_allowed(headers.get("Host"), hosts):
        return "host not allowed"
    origin = headers.get("Origin")
    if not origin_allowed(origin, hosts):
        return "origin not allowed"
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    mode = (headers.get("Sec-Fetch-Mode") or "").strip().lower()
    # following a link to the GUI is fine; a page's own fetch, image or
    # script request is not
    if not origin and site in FOREIGN_FETCH_SITES and mode != "navigate":
        return "cross-site request not allowed"
    return None


def cors_origin(origin: str | None) -> str | None:
    """The Access-Control-Allow-Origin value to send, if any: only
    Tauri app pages need CORS; same-origin pages do not."""
    if origin and origin.strip().lower() in DESKTOP_ORIGINS:
        return origin.strip()
    return None


def is_json(content_type: str | None) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower() == \
        "application/json"
