"""Sandboxed code execution — Phase 6.

Reference-grade sandbox for the `run_code` agent tool: Python executed in an
isolated subprocess (`python -I`), jailed to a per-task working directory
under the code store, with a wall-clock timeout, CPU/memory rlimits, a
stripped environment, and socket creation disabled inside the child.

Honest scope note: this is process-level isolation suitable for a reference
runtime and CI. The spec's production posture ("sandboxed container with no
internet access, no host access") requires container/VM isolation — treat
this module as the seam where that lands, not as its equivalent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_GUARD = """\
import socket as _socket
def _no_net(*a, **k):
    raise OSError("network disabled in Potaga sandbox")
_socket.socket = _no_net
_socket.create_connection = _no_net
del _socket
"""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self, limit: int = 2000) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out,
                "stdout": self.stdout[:limit], "stderr": self.stderr[:limit]}


class Sandbox:
    def __init__(self, workdir: Path, timeout_s: int = 30,
                 cpu_s: int = 20, mem_mb: int = 512) -> None:
        self.workdir = workdir
        self.timeout_s, self.cpu_s, self.mem_mb = timeout_s, cpu_s, mem_mb
        workdir.mkdir(parents=True, exist_ok=True)

    def _limits(self) -> None:  # pragma: no cover - runs in the child
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_s, self.cpu_s))
        mem = self.mem_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        except (ValueError, OSError):
            pass
        os.setsid()

    def run_python(self, code: str) -> ExecResult:
        with tempfile.NamedTemporaryFile("w", suffix=".py", dir=self.workdir,
                                         delete=False) as f:
            f.write(_GUARD + "\n" + code)
            script = f.name
        try:
            proc = subprocess.run(
                [sys.executable, "-I", script],
                cwd=self.workdir,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.workdir)},
                capture_output=True, text=True,
                timeout=self.timeout_s,
                preexec_fn=self._limits,
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr, timed_out=False)
        except subprocess.TimeoutExpired as e:
            return ExecResult(124, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                              "sandbox timeout", timed_out=True)
        finally:
            Path(script).unlink(missing_ok=True)
