"""Thin wrapper around the docker CLI for listing and controlling containers."""
from __future__ import annotations

import json
import subprocess
from typing import Any

_NO_WINDOW = 0
try:  # Hide the console window on Windows when invoking docker.
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

DOCKER_TIMEOUT = 15


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT,
        creationflags=_NO_WINDOW,
    )


def list_containers() -> dict[str, Any]:
    """List all containers (running + stopped). `docker ps -a` as JSON lines."""
    try:
        proc = _run(["ps", "-a", "--format", "{{json .}}"])
    except FileNotFoundError:
        return {"available": False, "containers": []}
    except subprocess.TimeoutExpired:
        return {"available": True, "error": "docker timed out", "containers": []}

    if proc.returncode != 0:
        return {
            "available": True,
            "error": (proc.stderr or "docker error").strip(),
            "containers": [],
        }

    containers: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = (obj.get("State") or "").lower()
        status_text = obj.get("Status") or ""
        running = state == "running" or status_text.lower().startswith("up")
        containers.append(
            {
                "id": obj.get("ID"),
                "name": obj.get("Names"),
                "image": obj.get("Image"),
                "ports": obj.get("Ports") or "",
                "statusText": status_text,
                "status": "running" if running else "stopped",
            }
        )
    return {"available": True, "containers": containers}


def start(name: str) -> dict[str, Any]:
    return _control("start", name)


def stop(name: str) -> dict[str, Any]:
    return _control("stop", name)


def _control(action: str, name: str) -> dict[str, Any]:
    try:
        proc = _run([action, name])
    except FileNotFoundError:
        return {"available": False, "ok": False, "error": "docker not installed"}
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": False, "error": "docker timed out"}

    if proc.returncode == 0:
        return {"available": True, "ok": True, "message": f"{action} {name}"}
    return {
        "available": True,
        "ok": False,
        "error": (proc.stderr or proc.stdout or "docker error").strip(),
    }
