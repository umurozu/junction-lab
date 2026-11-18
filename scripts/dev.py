"""Start the whole stack locally (3 controllers + center) without Docker.

    python scripts/dev.py            # Ctrl+C stops everything
    python scripts/dev.py --speed 3  # simulate 3x faster

Works on Windows and Linux. Logs of every process are prefixed and merged.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLORS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m"]


def pump(name: str, color: str, proc: subprocess.Popen) -> None:
    # This thread is the only reader of the child's stdout pipe. If it dies, the pipe fills up
    # and the child blocks on its next log write - the whole service freezes. So it must never
    # raise: a line that can't be printed is replaced/skipped, but reading continues.
    assert proc.stdout
    for line in iter(proc.stdout.readline, b""):
        try:
            sys.stdout.write(f"{color}{name:>8}\033[0m | {line.decode('utf-8', errors='replace')}")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - keep draining the pipe no matter what
            pass


def main() -> None:
    # Children log UTF-8 (PYTHONIOENCODING); on Windows our own stdout defaults to the ANSI
    # code page (cp1254), which can't encode e.g. '→' -> UnicodeEncodeError in pump().
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--junctions", default=str(ROOT / "configs" / "junctions.local.json"))
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    py = sys.executable
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "JL_JUNCTIONS": args.junctions,
           "JL_JWT_SECRET": os.environ.get("JL_JWT_SECRET", "dev-secret-change-me-this-is-only-for-local-runs")}
    procs: list[tuple[str, subprocess.Popen]] = []
    for j in json.load(open(args.junctions, encoding="utf-8")):
        cmd = [py, "-m", "junctionlab.controller.main", "--device-id", str(j["device_id"]), "--preset", j["preset"],
               "--name", j["name"], "--snmp-port", str(j["snmp_port"]), "--jlp-port", str(j["jlp_port"]),
               "--bind", "127.0.0.1", "--sim-traffic", "--speed", str(args.speed)]
        procs.append((f"dev{j['device_id']}", subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                                                 stderr=subprocess.STDOUT)))
    time.sleep(1.0)
    procs.append(("center", subprocess.Popen([py, "-m", "junctionlab.center.main", "--port", str(args.port)],
                                             cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)))
    for i, (name, p) in enumerate(procs):
        threading.Thread(target=pump, args=(name, COLORS[i % len(COLORS)], p), daemon=True).start()
    print(f"\n  API      http://127.0.0.1:{args.port}/docs\n  Panel    http://localhost:5173  (cd web && npm run dev)\n")
    try:
        while all(p.poll() is None for _, p in procs):
            time.sleep(0.5)
        print("a process exited, stopping the rest")
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in procs:
            p.terminate()
        for _, p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
