"""Native runner smoke test for the packaged Python + Node startup path."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
env = os.environ.copy()
env["PORT"] = "18080"
env["SUYUAN_MELTWATER_PORT"] = "18789"
process = subprocess.Popen([sys.executable, "-u", str(ROOT / "app.py")], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
try:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"服务提前退出：{process.returncode}")
        try:
            with urlopen("http://127.0.0.1:18080/api/meltwater/status", timeout=2) as response:
                payload = json.load(response)
            if payload.get("connected") is True:
                print("Python and bundled Node services are ready")
                break
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("60 秒内未同时启动 Python 与内置融文 Node 服务")
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", 18789), timeout=0.3):
                pass
        except OSError:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("内置融文子进程未随主服务退出")
