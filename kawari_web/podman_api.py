"""Podman control.

A web app running in a container cannot shell out to `podman` - the binary and
the rootless state live on the host. So this talks to Podman's REST API over a
mounted unix socket, and falls back to the CLI when running directly on the host.

On Windows (this branch) the socket is the Podman machine's, inside its VM; run.ps1 finds it with
`podman info` and mounts it into the container, pointing PODMAN_SOCKET at it. Without either
route the app still works; only the container controls are disabled, which the
UI states rather than failing silently.
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
from dataclasses import dataclass

from .winpaths import machine_path


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a unix domain socket."""

    def __init__(self, socket_path: str, timeout: int = 30):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        if not hasattr(socket, "AF_UNIX"):
            # Python on Windows itself: the API socket is the machine's, reachable from a container in it (run.ps1).
            raise OSError("the Podman API socket is reachable only from a container in Podman's machine: "
                          "start ArchonEngine ControlPanel with run.ps1")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


@dataclass
class Result:
    ok: bool
    status: int = 0
    data: object = None
    error: str = ""


class Podman:
    API = "/v5.0.0/libpod"

    def __init__(self, socket_path: str = "", binary: str = "podman"):
        self.socket_path = socket_path
        self.binary = binary

    # ── availability ────────────────────────────────────────────────────
    @property
    def mode(self) -> str:
        if self.socket_path:
            return "socket"
        if shutil.which(self.binary):
            return "cli"
        return "none"

    def available(self) -> tuple[bool, str]:
        mode = self.mode
        if mode == "socket":
            res = self._request("GET", "/info")
            if res.ok and isinstance(res.data, dict):
                version = (res.data.get("version") or {}).get("Version", "?")
                return True, f"podman {version} (API socket)"
            error = res.error or "socket did not answer"
            if "Permission denied" in error or "Errno 13" in error:
                error += (". The socket is mounted but access is denied. Check that the "
                          "socket's owner uid matches yours inside the container: on a "
                          "rootless Podman machine that needs --userns=keep-id, which "
                          "run.ps1 adds (a rootful machine's socket is root's: run.ps1 "
                          "mounts /run/podman/podman.sock then). diagnose.ps1 shows both.")
            return False, error
        if mode == "cli":
            try:
                out = subprocess.run([self.binary, "--version"], capture_output=True,
                                     text=True, timeout=8)
                return out.returncode == 0, (out.stdout or out.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, str(exc)
        return False, ("No Podman access. Mount the rootless API socket and set "
                       "PODMAN_SOCKET, or run the app on the host.")

    # ── transport ───────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body=None, timeout: int = 30) -> Result:
        if not self.socket_path:
            return Result(False, error="no socket configured")
        conn = UnixHTTPConnection(self.socket_path, timeout=timeout)
        try:
            payload = json.dumps(body) if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, self.API + path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            data: object = None
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError:
                    data = raw.decode("utf-8", "replace")
            if 200 <= resp.status < 300:
                return Result(True, resp.status, data)
            message = data.get("message") if isinstance(data, dict) else str(data)
            return Result(False, resp.status, data, message or f"HTTP {resp.status}")
        except (OSError, http.client.HTTPException) as exc:
            return Result(False, error=f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    def _cli(self, args: list, timeout: int = 45) -> Result:
        try:
            out = subprocess.run([self.binary, *args], capture_output=True,
                                 text=True, timeout=timeout)
        except FileNotFoundError:
            return Result(False, error=f"{self.binary} not found")
        except subprocess.TimeoutExpired:
            return Result(False, error="timed out")
        if out.returncode != 0:
            return Result(False, error=(out.stderr or out.stdout).strip())
        return Result(True, data=out.stdout)

    # ── operations ──────────────────────────────────────────────────────
    def inspect(self, container: str) -> dict:
        """Normalised container state: exists, running, status, started_at."""
        if self.mode == "socket":
            res = self._request("GET", f"/containers/{container}/json", timeout=15)
            if not res.ok or not isinstance(res.data, dict):
                return {"exists": False, "running": False, "status": "absent"}
            state = res.data.get("State") or {}
            return {"exists": True,
                    "running": bool(state.get("Running")),
                    "status": state.get("Status", "unknown"),
                    "started_at": state.get("StartedAt", "")}
        res = self._cli(["inspect", "--type", "container", "--format",
                         "{{.State.Status}}|{{.State.StartedAt}}", container], timeout=15)
        if not res.ok or not res.data:
            return {"exists": False, "running": False, "status": "absent"}
        status, _, started = str(res.data).strip().partition("|")
        return {"exists": True, "running": status == "running",
                "status": status or "unknown", "started_at": started}

    def image_exists(self, image: str) -> bool:
        if self.mode == "socket":
            return self._request("GET", f"/images/{image}/exists", timeout=15).status == 204
        return self._cli(["image", "exists", image], timeout=15).ok

    def start(self, container: str) -> Result:
        if self.mode == "socket":
            return self._request("POST", f"/containers/{container}/start")
        return self._cli(["start", container])

    def stop(self, container: str) -> Result:
        if self.mode == "socket":
            return self._request("POST", f"/containers/{container}/stop")
        return self._cli(["stop", container])

    def restart(self, container: str) -> Result:
        if self.mode == "socket":
            return self._request("POST", f"/containers/{container}/restart")
        return self._cli(["restart", container])

    def logs(self, container: str, tail: int = 200) -> str:
        if self.mode == "socket":
            # The libpod logs endpoint streams multiplexed frames; strip the
            # 8-byte header each frame carries so the text reads normally.
            conn = UnixHTTPConnection(self.socket_path, timeout=20)
            try:
                conn.request("GET", f"{self.API}/containers/{container}/logs"
                                    f"?stdout=true&stderr=true&tail={tail}")
                raw = conn.getresponse().read()
            except (OSError, http.client.HTTPException) as exc:
                return f"could not read logs: {exc}"
            finally:
                conn.close()
            return _demultiplex(raw)
        res = self._cli(["logs", "--tail", str(tail), container], timeout=25)
        return str(res.data or res.error)


    # ── image and container lifecycle ───────────────────────────────────
    def image_tags(self, image: str) -> set:
        """Tags present for an image name, e.g. {'latest', '49c945011'}."""
        if self.mode == "socket":
            res = self._request("GET", "/images/json", timeout=20)
            tags = set()
            if res.ok and isinstance(res.data, list):
                for entry in res.data:
                    for name in entry.get("Names") or entry.get("RepoTags") or []:
                        repo, _, tag = name.rpartition(":")
                        if repo.endswith(image):
                            tags.add(tag)
            return tags
        res = self._cli(["images", "--format", "{{.Tag}}", image], timeout=20)
        return {t.strip() for t in str(res.data or "").splitlines() if t.strip()}

    def tag_image(self, source: str, target: str) -> Result:
        if self.mode == "socket":
            repo, _, tag = target.rpartition(":")
            return self._request(
                "POST", f"/images/{source}/tag?repo={repo}&tag={tag}", timeout=30)
        return self._cli(["tag", source, target])

    def build(self, context_tar: bytes, tag: str, job=None,
              dockerfile: str = "Containerfile") -> Result:
        """Build an image from an in-memory tar of the build context.

        The tar is streamed to Podman, so the daemon never needs to see the
        path - which matters because this app's /data is not the host's path.
        """
        if self.mode == "socket":
            conn = UnixHTTPConnection(self.socket_path, timeout=1800)
            try:
                path = (f"{self.API}/build?dockerfile={dockerfile}&t={tag}"
                        f"&pull=false&rm=true")
                conn.putrequest("POST", path)
                conn.putheader("Content-Type", "application/x-tar")
                conn.putheader("Content-Length", str(len(context_tar)))
                conn.endheaders()
                conn.send(context_tar)
                resp = conn.getresponse()
                buffer = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        _emit_build_line(line, job)
                if resp.status >= 400:
                    return Result(False, resp.status, error=f"build HTTP {resp.status}")
                return Result(True, resp.status)
            except (OSError, http.client.HTTPException) as exc:
                return Result(False, error=f"{type(exc).__name__}: {exc}")
            finally:
                conn.close()
        return Result(False, error="image building needs the Podman API socket")

    def create_container(self, spec: dict) -> Result:
        if self.mode == "socket":
            return self._request("POST", "/containers/create", body=spec, timeout=120)
        return Result(False, error="creating the container needs the Podman API socket")

    def remove(self, container: str) -> Result:
        if self.mode == "socket":
            return self._request("DELETE", f"/containers/{container}?force=true",
                                 timeout=60)
        return self._cli(["rm", "-f", container])

    def remove_image(self, ref: str) -> Result:
        """Untag/remove one image reference, e.g. 'kawari:c1d2ac5b6-dev...'.

        Not forced: an image a container still uses stays, which is right - the
        active build's image must never disappear from under the server.
        """
        if self.mode == "socket":
            return self._request("DELETE", f"/images/{ref}", timeout=60)
        return self._cli(["rmi", ref])

    def run_once(self, spec: dict, job=None, timeout: int = 7200) -> Result:
        """Create a container, stream its output into the job, wait, remove it.

        Returns ok only when the process exits 0; ``data`` is the exit code.
        Used for the source builder, whose output is the whole point - so it is
        streamed live rather than read back afterwards.
        """
        if self.mode != "socket":
            return Result(False, error="running a build container needs the Podman API socket")
        created = self.create_container(spec)
        if not created.ok:
            return created
        cid = (created.data or {}).get("Id") or spec.get("name", "")
        try:
            started = self._request("POST", f"/containers/{cid}/start", timeout=60)
            if not started.ok:
                return started
            self._follow_logs(cid, job, timeout)
            waited = self._request("POST", f"/containers/{cid}/wait?condition=stopped",
                                   timeout=timeout)
            if not waited.ok:
                return waited
            code = waited.data if isinstance(waited.data, int) else int(
                str(waited.data).strip() or "1")
            if code != 0:
                return Result(False, data=code, error=f"exited with status {code}")
            return Result(True, data=0)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            return Result(False, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._request("DELETE", f"/containers/{cid}?force=true", timeout=60)

    def _follow_logs(self, cid: str, job, timeout: int) -> None:
        """Stream multiplexed log frames line by line until the container exits."""
        conn = UnixHTTPConnection(self.socket_path, timeout=timeout)
        try:
            conn.request("GET", f"{self.API}/containers/{cid}/logs"
                                f"?follow=true&stdout=true&stderr=true")
            resp = conn.getresponse()
            frames, text = b"", ""
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                frames += chunk
                while len(frames) >= 8:
                    size = int.from_bytes(frames[4:8], "big")
                    if len(frames) < 8 + size:
                        break
                    text += frames[8:8 + size].decode("utf-8", "replace")
                    frames = frames[8 + size:]
                # cargo redraws progress with \r; keep whole lines only
                while "\n" in text:
                    line, text = text.split("\n", 1)
                    line = line.rsplit("\r", 1)[-1].rstrip()
                    if line and job:
                        job.say(line)
            if text.strip() and job:
                job.say(text.strip())
        finally:
            conn.close()


def _emit_build_line(line: bytes, job) -> None:
    text = line.decode("utf-8", "replace").strip()
    if not text:
        return
    try:
        payload = json.loads(text)
    except ValueError:
        if job:
            job.say(text)
        return
    message = payload.get("stream") or payload.get("error") or ""
    if message.strip() and job:
        job.say(message.rstrip())


def container_spec(name: str, image: str, mounts: list, ports: list,
                   selinux_disable: bool) -> dict:
    """A libpod SpecGenerator equivalent to the desktop app's `podman run`."""
    spec = {
        "name": name,
        "image": image,
        "restart_policy": "unless-stopped",
        "remove": False,
        "portmappings": [
            {"host_ip": host_ip, "host_port": port, "container_port": port,
             "protocol": "tcp"}
            for host_ip, port in ports
        ],
        # Sources as Podman's machine sees them (Windows branch): a Windows path from settings.conf or Setup becomes
        # /mnt/<drive>/..., which the API needs and the CLI would have done by itself (`winpaths.py`).
        "mounts": [
            {"destination": dest, "source": machine_path(source), "type": "bind",
             "options": ["rbind", *opts]}
            for source, dest, opts in mounts
        ],
    }
    if selinux_disable:
        spec["selinux_opts"] = ["disable"]
    return spec


def _demultiplex(raw: bytes) -> str:
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        if size <= 0 or i + 8 + size > len(raw):
            break
        out.append(raw[i + 8:i + 8 + size].decode("utf-8", "replace"))
        i += 8 + size
    if not out:
        return raw.decode("utf-8", "replace")
    return "".join(out)
