"""Wrapper around VMware's `vmrun` CLI for listing and controlling VMs."""
from __future__ import annotations

import glob
import subprocess
from pathlib import Path
from typing import Any

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

VMRUN_TIMEOUT = 30

_VMRUN_GLOBS = [
    r"C:/Program Files*/VMware/*/vmrun.exe",
]


def _find_vmrun() -> str | None:
    for pattern in _VMRUN_GLOBS:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def _run(vmrun: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [vmrun, *args],
        capture_output=True,
        text=True,
        timeout=VMRUN_TIMEOUT,
        creationflags=_NO_WINDOW,
    )


def _vm_name(vmx: str) -> str:
    return Path(vmx).stem


def list_vms() -> dict[str, Any]:
    """Return the VMs currently running (`vmrun list`)."""
    vmrun = _find_vmrun()
    if not vmrun:
        return {"available": False, "vms": []}

    try:
        proc = _run(vmrun, ["list"])
    except subprocess.TimeoutExpired:
        return {"available": True, "error": "vmrun timed out", "vms": []}

    if proc.returncode != 0:
        return {
            "available": True,
            "error": (proc.stderr or "vmrun error").strip(),
            "vms": [],
        }

    vms: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("total running vms"):
            continue
        # vmrun list returns absolute .vmx paths for running VMs.
        if line.lower().endswith(".vmx"):
            vms.append(
                {
                    "name": _vm_name(line),
                    "vmx": line,
                    "status": "running",
                }
            )
    return {"available": True, "vms": vms}


def start_vm(vmx: str, gui: bool = False) -> dict[str, Any]:
    mode = "gui" if gui else "nogui"
    return _control(["start", vmx, mode], f"start {_vm_name(vmx)}")


def stop_vm(vmx: str, force: bool = False) -> dict[str, Any]:
    mode = "hard" if force else "soft"
    return _control(["stop", vmx, mode], f"stop {_vm_name(vmx)}")


def _control(args: list[str], label: str) -> dict[str, Any]:
    vmrun = _find_vmrun()
    if not vmrun:
        return {"available": False, "ok": False, "error": "vmrun not found"}
    try:
        proc = _run(vmrun, args)
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": False, "error": "vmrun timed out"}

    if proc.returncode == 0:
        return {"available": True, "ok": True, "message": label}
    return {
        "available": True,
        "ok": False,
        "error": (proc.stderr or proc.stdout or "vmrun error").strip(),
    }
