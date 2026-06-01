"""One-shot helper: merge the veda-dashboard entry into the Veda registry.

Run once after building. Safe to re-run (upsert by name). Handles both a bare
list registry and a {"projects": [...]} wrapper, and creates the file if missing.
"""
import json
from pathlib import Path

REGISTRY = Path(r"C:/Users/ADMIN/Documents/code/veda/.veda/projects/registry.json")

ENTRY = {
    "name": "veda-dashboard",
    "description": "Web control panel สำหรับ Veda apps, Docker containers, VMware VMs",
    "repo": None,
    "localPath": "C:/Users/ADMIN/Documents/code/veda-dashboard",
    "runCmd": "uvicorn main:app --port 8765",
    "healthCheck": {"type": "http", "target": "http://localhost:8765"},
    "routineId": None,
    "requirementsReady": True,
    "status": "running",
    "createdAt": "2026-06-01",
    "lastSeen": None,
}


def main() -> None:
    wrapper_key = None
    items = []

    if REGISTRY.exists():
        raw = REGISTRY.read_text(encoding="utf-8").strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                wrapper_key = "projects"
                items = data.get("projects", [])
            elif isinstance(data, list):
                items = data
    else:
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)

    # Upsert by name.
    items = [it for it in items if it.get("name") != ENTRY["name"]]
    items.append(ENTRY)

    out = {"projects": items} if wrapper_key else items
    REGISTRY.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"registry updated: {len(items)} project(s); wrapper={'yes' if wrapper_key else 'no'}")


if __name__ == "__main__":
    main()
