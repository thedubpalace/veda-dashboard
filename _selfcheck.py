"""Self-check: import the app and hit each endpoint via FastAPI TestClient.

Writes a human-readable report to _selfcheck.log so results are inspectable
even when stdout is not visible.
"""
import json
import sys
import traceback
from pathlib import Path

LOG = Path(__file__).resolve().parent / "_selfcheck.log"


def main() -> int:
    lines = []

    def log(msg):
        lines.append(str(msg))

    rc = 0
    try:
        from fastapi.testclient import TestClient
        import main as app_module

        client = TestClient(app_module.app)

        r = client.get("/")
        log(f"GET /            -> {r.status_code} (html {len(r.text)} bytes)")
        assert r.status_code == 200

        r = client.get("/api/status")
        log(f"GET /api/status  -> {r.status_code}")
        assert r.status_code == 200
        data = r.json()
        log(f"  veda apps   : {len(data.get('veda', []))}")
        log(f"  docker      : available={data.get('docker', {}).get('available')} "
            f"count={len(data.get('docker', {}).get('containers', []))}")
        log(f"  vmware      : available={data.get('vmware', {}).get('available')} "
            f"count={len(data.get('vmware', {}).get('vms', []))}")

        r = client.get("/api/apps")
        log(f"GET /api/apps    -> {r.status_code} ({len(r.json())} apps)")
        assert r.status_code == 200

        r = client.get("/api/docker")
        log(f"GET /api/docker  -> {r.status_code}")
        assert r.status_code == 200

        r = client.get("/api/vmware")
        log(f"GET /api/vmware  -> {r.status_code}")
        assert r.status_code == 200

        # Unknown app -> 404
        r = client.post("/api/apps/__nope__/stop")
        log(f"POST stop unknown-> {r.status_code} (expect 404)")
        assert r.status_code == 404

        # vmware start without vmx -> 400
        r = client.post("/api/vmware/start", json={})
        log(f"POST vmw start {{}} -> {r.status_code} (expect 400)")
        assert r.status_code == 400

        log("ALL CHECKS PASSED")
    except Exception:
        rc = 1
        log("CHECK FAILED:")
        log(traceback.format_exc())

    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
