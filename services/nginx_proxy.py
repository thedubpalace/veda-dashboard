"""Map local ports to their public URLs by reading Nginx Proxy Manager's config.

NPM writes one file per proxy host under /data/nginx/proxy_host/. Each has a
server_name, a root location backed by `set $server` / `set $port`, and any
number of sub-path locations with a literal proxy_pass. Reading those gives an
always-current {port: public url} map without duplicating the routing table in
the dashboard's own settings.
"""
from __future__ import annotations

import re
import socket
import subprocess
import time
from typing import Any

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

CONTAINER = "nginx-proxy-manager"
CONF_GLOB = "/data/nginx/proxy_host/*.conf"
DOCKER_TIMEOUT = 10
CACHE_TTL = 60.0

_FILE_MARKER = "#### npm-conf ####"

_SERVER_NAME = re.compile(r"^\s*server_name\s+([^;]+);", re.M)
_SET_SERVER = re.compile(r"^\s*set\s+\$server\s+\"?([^\";]+)\"?;", re.M)
_SET_PORT = re.compile(r"^\s*set\s+\$port\s+(\d+);", re.M)
_LOCATION = re.compile(r"^\s*location\s+([^\s{]+)\s*\{", re.M)
_PROXY_PASS = re.compile(r"^\s*proxy_pass\s+https?://([^:;/]+):(\d+)", re.M)
_SSL = re.compile(r"^\s*listen\s+443\s+ssl", re.M)

_cache: tuple[float, dict[int, str]] = (0.0, {})


def _local_hosts() -> set[str]:
    """Addresses that mean "this machine" in an NPM target."""
    hosts = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        try:
            sock.connect(("8.8.8.8", 80))
            hosts.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        hosts.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return hosts


def _read_confs() -> list[str]:
    """Return each proxy-host config's text, or [] when NPM isn't reachable."""
    script = f'for f in {CONF_GLOB}; do echo "{_FILE_MARKER}"; cat "$f"; done'
    try:
        proc = subprocess.run(
            ["docker", "exec", CONTAINER, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    return [chunk for chunk in proc.stdout.split(_FILE_MARKER) if chunk.strip()]


def _url(server_name: str, path: str, secure: bool) -> str:
    scheme = "https" if secure else "http"
    path = path.strip()
    if path in ("", "/"):
        return f"{scheme}://{server_name}/"
    # NPM allows regex-ish locations (e.g. "/#/"); only plain prefixes are
    # something a browser can be sent to.
    if not path.startswith("/") or any(c in path for c in "~^*$#"):
        return ""
    return f"{scheme}://{server_name}{path.rstrip('/')}/"


def _parse(conf: str, local: set[str]) -> dict[int, str]:
    name_match = _SERVER_NAME.search(conf)
    if not name_match:
        return {}
    # A proxy host can answer for several names; the first is the canonical one.
    server_name = name_match.group(1).split()[0]
    secure = bool(_SSL.search(conf))
    found: dict[int, str] = {}

    server = _SET_SERVER.search(conf)
    port = _SET_PORT.search(conf)
    if server and port and server.group(1) in local:
        url = _url(server_name, "/", secure)
        if url:
            found[int(port.group(1))] = url

    # Pair each location with the proxy_pass that follows it.
    locations = [(m.start(), m.group(1)) for m in _LOCATION.finditer(conf)]
    for pos, path in locations:
        target = _PROXY_PASS.search(conf, pos)
        if not target:
            continue
        host, target_port = target.group(1), int(target.group(2))
        if host not in local:
            continue
        url = _url(server_name, path, secure)
        # Sub-paths are more specific than a root catch-all, so let them win.
        if url:
            found[target_port] = url
    return found


def public_urls(force: bool = False) -> dict[int, str]:
    """{host port: public URL} for everything NPM routes back to this machine."""
    global _cache
    age, cached = _cache
    now = time.monotonic()
    if not force and cached and now - age < CACHE_TTL:
        return cached

    local = _local_hosts()
    urls: dict[int, str] = {}
    for conf in _read_confs():
        urls.update(_parse(conf, local))

    if urls or not cached:
        _cache = (now, urls)
        return urls
    # NPM briefly unavailable — keep serving the last known map.
    _cache = (now, cached)
    return cached


def url_for_port(port: Any) -> str | None:
    try:
        return public_urls().get(int(port))
    except (TypeError, ValueError):
        return None
