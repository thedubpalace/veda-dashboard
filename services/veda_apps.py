"""Read the Veda registry and perform live health checks for each app."""
from __future__ import annotations

import json
import shlex
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import psutil

# Path to the Veda registry. Resolved relative to this file so the dashboard can
# live anywhere as long as the sibling `veda` repo keeps its layout.
REGISTRY_PATH = Path(
    r"C:/Users/ADMIN/Documents/code/veda/.veda/projects/registry.json"
)

HEALTH_TIMEOUT = 3.0


def _load_registry() -> list[dict[str, Any]]:
    """Read registry.json fresh on every call (no caching)."""
    try:
        raw = REGISTRY_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    # Support both a bare list and a wrapper object {"projects": [...]}.
    if isinstance(data, dict):
        data = data.get("projects", [])
    if not isinstance(data, list):
        return []
    return data


def _first_cmd_token(run_cmd: str | None) -> str | None:
    if not run_cmd:
        return None
    try:
        tokens = shlex.split(run_cmd, posix=False)
    except ValueError:
        tokens = run_cmd.split()
    if not tokens:
        return None
    name = tokens[0].strip('"').strip("'")
    # Strip a path prefix and extension, e.g. C:/x/uvicorn.exe -> uvicorn
    name = Path(name).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _check_http(target: str | None) -> bool:
    if not target:
        return False
    try:
        resp = httpx.get(target, timeout=HEALTH_TIMEOUT, follow_redirects=False)
    except Exception:
        return False
    return 200 <= resp.status_code < 400


def _check_port(target: Any) -> bool:
    """target may be a port number or a host:port / url string."""
    host = "localhost"
    port: int | None = None
    if isinstance(target, int):
        port = target
    elif isinstance(target, str):
        t = target.strip()
        if t.isdigit():
            port = int(t)
        elif "://" in t:
            parsed = urlparse(t)
            host = parsed.hostname or host
            port = parsed.port
        elif ":" in t:
            host, _, p = t.rpartition(":")
            host = host or "localhost"
            if p.isdigit():
                port = int(p)
    if port is None:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(HEALTH_TIMEOUT)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _check_process(target: str | None) -> bool:
    if not target:
        return False
    needle = Path(str(target)).name.lower()
    if needle.endswith(".exe"):
        needle = needle[:-4]
    for proc in psutil.process_iter(["name"]):
        try:
            pname = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if pname.endswith(".exe"):
            pname = pname[:-4]
        if pname == needle:
            return True
    return False


def _is_running(app: dict[str, Any]) -> bool:
    hc = app.get("healthCheck") or {}
    hc_type = (hc.get("type") or "null").lower()
    target = hc.get("target")

    if hc_type == "http":
        return _check_http(target)
    if hc_type == "port":
        return _check_port(target)
    if hc_type == "process":
        return _check_process(target)
    # null / unknown: fall back to first token of runCmd as a process name
    return _check_process(_first_cmd_token(app.get("runCmd")))


def list_apps() -> list[dict[str, Any]]:
    """Return registry apps enriched with a live `status` field."""
    apps: list[dict[str, Any]] = []
    for app in _load_registry():
        running = _is_running(app)
        apps.append(
            {
                "name": app.get("name"),
                "description": app.get("description"),
                "status": "running" if running else "stopped",
                "runCmd": app.get("runCmd"),
                "repo": app.get("repo"),
                "localPath": app.get("localPath"),
                "healthCheck": app.get("healthCheck"),
            }
        )
    return apps


def get_app(name: str) -> dict[str, Any] | None:
    for app in _load_registry():
        if app.get("name") == name:
            return app
    return None
