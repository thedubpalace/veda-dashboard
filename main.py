"""Veda Dashboard — unified control panel for Veda apps, Docker, and VMware.

Run: uvicorn main:app --reload --port 8765
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from services import docker_service, veda_apps, vmware_service

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "templates" / "index.html"
SETTINGS_FILE = BASE_DIR / "settings.json"


def _load_settings() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"docker": {"filter": []}}


def _save_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_docker_filter(containers: list[dict], filters: list[str]) -> list[dict]:
    if not filters:
        return containers
    f = [s.lower() for s in filters if s.strip()]
    if not f:
        return containers
    return [c for c in containers if any(kw in (c.get("name") or "").lower() for kw in f)]

app = FastAPI(title="Veda Dashboard")

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

_NEW_GROUP = 0
try:
    _NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
except AttributeError:
    _NEW_GROUP = 0


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # The page is fully client-rendered (data via /api/* fetches), so the
    # template has no server-side variables — serve it as static HTML. This
    # also sidesteps a Jinja2 template-cache bug on this Python/Jinja combo.
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _load_settings()


@app.patch("/api/settings/docker/filter")
async def set_docker_filter(payload: dict[str, Any]) -> JSONResponse:
    filters = payload.get("filter")
    if not isinstance(filters, list):
        return JSONResponse({"ok": False, "error": "filter must be a list"}, status_code=400)
    settings = _load_settings()
    settings.setdefault("docker", {})["filter"] = [str(f) for f in filters if str(f).strip()]
    _save_settings(settings)
    return JSONResponse({"ok": True, "filter": settings["docker"]["filter"]})


# ----------------------------------------------------------------------------
# Aggregate status
# ----------------------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    veda, docker, vmware = await asyncio.gather(
        loop.run_in_executor(None, veda_apps.list_apps),
        loop.run_in_executor(None, docker_service.list_containers),
        loop.run_in_executor(None, vmware_service.list_vms),
    )
    # Merge per-app claude session counts
    all_sessions = _find_claude_sessions()
    path_to_count: dict[str, int] = {}
    for s in all_sessions:
        p = _norm_path(s.get("path"))
        if p:
            path_to_count[p] = path_to_count.get(p, 0) + 1
    for app in veda:
        app["claudeSessions"] = path_to_count.get(_norm_path(app.get("localPath")), 0)

    filters = _load_settings().get("docker", {}).get("filter", [])
    if isinstance(docker, dict) and "containers" in docker:
        docker = dict(docker)
        docker["containers"] = _apply_docker_filter(docker["containers"], filters)
        docker["filtered"] = bool(filters)
        docker["filterList"] = filters
    return {"veda": veda, "docker": docker, "vmware": vmware}


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


@app.post("/api/apps/{name}/restart")
async def app_restart(name: str) -> JSONResponse:
    entry = veda_apps.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    run_cmd = entry.get("runCmd")
    if not run_cmd:
        return JSONResponse({"ok": False, "error": "no runCmd configured"}, status_code=400)

    hc = entry.get("healthCheck") or {}
    port: int | None = _port_from_healthcheck(hc) or _port_from_cmd(run_cmd)
    token: str | None = None if port else _first_token(run_cmd)
    cwd: str | None = entry.get("localPath") or None

    # Spawn a detached Python process that does stop → wait → start.
    # Detached so it survives even when this server process is the one being restarted.
    script = _build_restart_script(port, token, run_cmd, cwd)
    try:
        subprocess.Popen(
            ["python", "-c", script],
            creationflags=_NO_WINDOW | _NEW_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "message": f"restarting {name}…"})


def _build_restart_script(port: int | None, token: str | None, run_cmd: str, cwd: str | None) -> str:
    return f"""
import time, subprocess, socket, shlex
import psutil

time.sleep(0.7)

port = {port!r}
token = {token!r}
run_cmd = {run_cmd!r}
cwd = {cwd!r}

if port:
    for conn in psutil.net_connections(kind='inet'):
        if conn.laddr and conn.laddr.port == port and conn.pid:
            try: psutil.Process(conn.pid).terminate()
            except: pass
else:
    needle = (token or '').lower()
    for proc in psutil.process_iter(['name']):
        try:
            pname = (proc.info.get('name') or '').lower()
            if pname.endswith('.exe'): pname = pname[:-4]
            if pname == needle: proc.terminate()
        except: pass

if port:
    for _ in range(30):
        time.sleep(0.25)
        s = socket.socket()
        s.settimeout(0.5)
        try:
            if s.connect_ex(('localhost', port)) != 0:
                break
        finally:
            s.close()
else:
    time.sleep(1.0)

try:
    args = shlex.split(run_cmd, posix=False)
except Exception:
    args = run_cmd.split()
try:
    subprocess.Popen(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
except FileNotFoundError:
    subprocess.Popen(run_cmd, cwd=cwd, shell=True)
"""


@app.post("/api/apps/{name}/shell")
async def app_shell(name: str) -> JSONResponse:
    entry = veda_apps.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)
    cwd = entry.get("localPath")
    if not cwd or not Path(cwd).is_dir():
        return JSONResponse({"ok": False, "error": "localPath not found"}, status_code=400)
    try:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", f"Set-Location '{cwd}'; claude remote-control"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return JSONResponse({"ok": True, "message": f"opened shell for {name}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
    result = docker_service.list_containers()
    filters = _load_settings().get("docker", {}).get("filter", [])
    if isinstance(result, dict) and "containers" in result:
        result = dict(result)
        result["containers"] = _apply_docker_filter(result["containers"], filters)
        result["filtered"] = bool(filters)
        result["filterList"] = filters
    return result


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
# Claude remote-control sessions
# ----------------------------------------------------------------------------

def _norm_path(p: str | None) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower() if p else ""


def _find_claude_sessions(app_path: str | None = None) -> list[dict[str, Any]]:
    """Return one entry per PowerShell terminal running claude remote-control.

    Pass app_path to filter to sessions whose Set-Location matches that path.
    """
    norm_filter = _norm_path(app_path) if app_path else None
    results = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower().replace(".exe", "")
            if name not in ("powershell", "pwsh"):
                continue
            cmdline = " ".join(str(c) for c in (proc.info.get("cmdline") or []))
            if "remote-control" not in cmdline or "claude" not in cmdline.lower():
                continue
            m = re.search(r"Set-Location\s+'([^']+)'", cmdline)
            path = m.group(1) if m else None
            if norm_filter and _norm_path(path) != norm_filter:
                continue
            results.append({"pid": proc.pid, "path": path})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return results


@app.post("/api/apps/{name}/claude-sessions/kill-all")
async def kill_app_claude_sessions(name: str) -> JSONResponse:
    entry = veda_apps.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)
    sessions = _find_claude_sessions(app_path=entry.get("localPath"))
    killed = 0
    for s in sessions:
        try:
            psutil.Process(s["pid"]).terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    msg = f"terminated {killed} session(s)" if killed else "no sessions found"
    return JSONResponse({"ok": True, "killed": killed, "message": msg})


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
