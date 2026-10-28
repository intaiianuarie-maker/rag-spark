from __future__ import annotations

import os
import subprocess
import sys
import tempfile

PKG = os.path.abspath("packages")


def _run(script: str, db: str, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{PKG}:{os.path.abspath('apps/api')}"
    env["AGENT_STORE_DB"] = db
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )


def test_xproc_shared_agent_store() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "agent.db")
        writer = r'''
import os
from stores.agent_stores import set_agent_store_db, get_queue
set_agent_store_db(os.environ["AGENT_STORE_DB"])
import asyncio
asyncio.run(get_queue().enqueue("run-xproc"))
print("WRITER_PID", os.getpid())
'''
        w = _run(writer, db)
        assert w.returncode == 0, w.stderr
        assert "WRITER_PID" in w.stdout
        reader = r'''
import os
from stores.agent_stores import set_agent_store_db, get_queue
set_agent_store_db(os.environ["AGENT_STORE_DB"])
import asyncio
depth = asyncio.run(get_queue().depth())
print("READER_PID", os.getpid())
print("DEPTH", depth)
assert depth == 1
'''
        r = _run(reader, db)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "READER_PID" in r.stdout
        # PIDs differ
        wlines = w.stdout.splitlines()
        rlines = r.stdout.splitlines()
        wpid = int(next(line for line in wlines if line.startswith("WRITER_PID")).split()[-1])
        rpid = int(next(line for line in rlines if line.startswith("READER_PID")).split()[-1])
        assert wpid != rpid
        print("WPID", wpid, "RPID", rpid)
