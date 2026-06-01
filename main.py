"""Veda Dashboard — unified control panel for Veda apps, Docker, and VMware.

Run: uvicorn main:app --reload --port 8765
"""
from __future__ import annotations

import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from services import docker_service, veda_apps, vmware_service

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Veda Dashboard")

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# ----------------------------------------------------------------------------
# Aggregate status
# ----------------------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "veda": veda_apps.list_apps(),
        "docker": docker_service.list_containers(),
        "vmware": vmware_service.list_vms(),
    }


# ----------------------------------------------------------------------------
# Veda apps
# ----------------------------------------------------------------------------
@app.get("/api/apps")
async def apps() -> list[dict[str, Any]]:
    return veda_apps.list_apps()


@app.post("/api/apps/{name}/start")
async def app_start(name: str) -> JSONResponse:
    entry = veda_apps.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    run_cmd = entry.get("runCmd")
    if not run_cmd:
        return JSONResponse({"ok": False, "error": "no runCmd configured"}, status_code=400)

    cwd = entry.get("localPath")
    if cwd and not Path(cwd).is_dir():
        cwd = None

    try:
        args = shlex.split(run_cmd, posix=False)
    except ValueError:
        args = run_cmd.split()

    try:
        subprocess.Popen(
            args,
            cwd=cwd,
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # Fall back to shell so PATH-resolved commands (e.g. uvicorn) work.
        try:
            subprocess.Popen(run_cmd, cwd=cwd, shell=True, creationflags=_NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "message": f"started {name}"})


@app.post("/api/apps/{name}/stop")
async def app_stop(name: str) -> JSONResponse:
    entry = veda_apps.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    killed = 0
    hc = entry.get("healthCheck") or {}
    port = _port_from_healthcheck(hc) or _port_from_cmd(entry.get("runCmd"))

    if port is not None:
        killed += _kill_by_port(port)

    if killed == 0:
        # Fall back: match the first runCmd token against process names.
        token = _first_token(entry.get("runCmd"))
        if token:
            killed += _kill_by_name(token)

    if killed:
        return JSONResponse({"ok": True, "message": f"stopped {name} ({killed} proc)"})
    return JSONResponse({"ok": False, "error": "no matching process found"}, status_code=404)


# ----------------------------------------------------------------------------
# Docker
# ----------------------------------------------------------------------------
@app.get("/api/docker")
async def docker_list() -> dict[str, Any]:
    return docker_service.list_containers()


@app.post("/api/docker/{name}/start")
async def docker_start(name: str) -> JSONResponse:
    return JSONResponse(docker_service.start(name))


@app.post("/api/docker/{name}/stop")
async def docker_stop(name: str) -> JSONResponse:
    return JSONResponse(docker_service.stop(name))


# ----------------------------------------------------------------------------
# VMware
# ----------------------------------------------------------------------------
@app.get("/api/vmware")
async def vmware_list() -> dict[str, Any]:
    return vmware_service.list_vms()


@app.post("/api/vmware/start")
async def vmware_start(payload: dict[str, Any]) -> JSONResponse:
    vmx = payload.get("vmx")
    if not vmx:
        return JSONResponse({"ok": False, "error": "vmx required"}, status_code=400)
    gui = bool(payload.get("gui", False))
    return JSONResponse(vmware_service.start_vm(vmx, gui))


@app.post("/api/vmware/stop")
async def vmware_stop(payload: dict[str, Any]) -> JSONResponse:
    vmx = payload.get("vmx")
    if not vmx:
        return JSONResponse({"ok": False, "error": "vmx required"}, status_code=400)
    force = bool(payload.get("force", False))
    return JSONResponse(vmware_service.stop_vm(vmx, force))


# ----------------------------------------------------------------------------
# Helpers for stopping Veda apps
# ----------------------------------------------------------------------------
def _port_from_healthcheck(hc: dict[str, Any]) -> int | None:
    if (hc.get("type") or "").lower() == "port":
        target = hc.get("target")
        if isinstance(target, int):
            return target
        if isinstance(target, str) and target.isdigit():
            return int(target)
    target = hc.get("target")
    if isinstance(target, str) and "://" in target:
        from urllib.parse import urlparse

        return urlparse(target).port
    return None


def _port_from_cmd(run_cmd: str | None) -> int | None:
    if not run_cmd:
        return None
    tokens = run_cmd.replace("=", " ").split()
    for i, tok in enumerate(tokens):
        if tok in ("--port", "-p") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            return int(tokens[i + 1])
    return None


def _first_token(run_cmd: str | None) -> str | None:
    if not run_cmd:
        return None
    try:
        tokens = shlex.split(run_cmd, posix=False)
    except ValueError:
        tokens = run_cmd.split()
    if not tokens:
        return None
    name = Path(tokens[0].strip('"').strip("'")).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _kill_by_port(port: int) -> int:
    killed = 0
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr and conn.laddr.port == port and conn.pid:
            try:
                proc = psutil.Process(conn.pid)
                proc.terminate()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return killed


def _kill_by_name(name: str) -> int:
    needle = name.lower()
    killed = 0
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            if pname.endswith(".exe"):
                pname = pname[:-4]
            if pname == needle:
                proc.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def _is_port_open(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        return sock.connect_ex(("localhost", port)) == 0
    finally:
        sock.close()
