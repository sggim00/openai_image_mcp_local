"""openai_image_mcp_local — mobile worker (Google Drive mailbox).

On the phone, Claude cannot see this PC's MCP tools, so it uses the Google Drive connector:
it drops a request JSON into Drive, this worker (running on the PC) picks it up, renders
with the same code as server.py, and puts the image + a result JSON back in Drive. The
image is also saved to OUTPUT_DIR with a short code, shared with the PC tools.

    My Drive/openai_image_mcp/
        requests/  <- req_<id>.json from Claude (only "prompt" is required)
        refs/      <- reference pictures (upload from the Drive phone app)
        outputs/   <- <id>.png + <id>.result.json
        done/      <- processed requests
        worker_status.json   <- heartbeat, so Claude can tell whether the PC is up

Usage
    python drive_worker.py                 run in the foreground (Ctrl+C to stop)
    python drive_worker.py --check         verify Google Drive + OpenAI key, create folders
    python drive_worker.py --start         start in the background (no window)
    python drive_worker.py --stop          stop the background worker
    python drive_worker.py --status        is it running? last heartbeat?
    python drive_worker.py --install-autostart / --remove-autostart   (Windows login)
    python drive_worker.py --once          process what is waiting, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
PID_FILE = LOG_DIR / "drive_worker.pid"
LOG_FILE = LOG_DIR / "drive_worker.log"

if sys.stderr is None:  # pythonw: no console
    sys.stdout = sys.stderr = open(os.devnull, "w")

import server as core  # noqa: E402  (same settings, rendering, storage and codes as the PC tools)
from gdrive import Drive, DriveError, q  # noqa: E402

log = logging.getLogger("drive_worker")
_fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_fh)

TOKEN_FILE = Path(os.path.expandvars(os.path.expanduser(
    os.environ.get("GDRIVE_TOKEN_FILE", str(HERE / ".gdrive_token.json")))))
ROOT_NAME = os.environ.get("GDRIVE_ROOT_NAME", "openai_image_mcp")
ROOT_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", "60"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
LOCK_PORT = int(os.environ.get("WORKER_LOCK_PORT", "47821"))
KEEP_AWAKE = os.environ.get("KEEP_AWAKE", "0").lower() in ("1", "true", "yes")
SUBFOLDERS = ("requests", "refs", "outputs", "done")
DRIVE_ID_RE = re.compile(r"(?:/d/|/folders/|[?&]id=)([A-Za-z0-9_-]{20,})")
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")
EXT = {"png": "png", "jpeg": "jpg", "webp": "webp"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RequestError(Exception):
    pass


class Worker:
    def __init__(self, drive: Drive):
        self.drive = drive
        self.f: dict[str, str] = {}
        self.status_id: str | None = None
        self.inflight: set[str] = set()
        self.processed = 0
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)
        self.stopping = False

    async def call(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    # ------------------------------------------------------------------ setup
    def setup_folders(self) -> dict[str, str]:
        d = self.drive
        root = d.meta(ROOT_ID) if ROOT_ID else d.ensure_folder(ROOT_NAME, "root")
        self.f = {"root": root["id"]}
        for name in SUBFOLDERS:
            self.f[name] = d.ensure_folder(name, root["id"])["id"]
        hits = d.list(f"name = 'worker_status.json' and '{root['id']}' in parents and trashed = false", page_size=1)
        self.status_id = hits[0]["id"] if hits else None
        return self.f

    def heartbeat(self, state: str = "running") -> None:
        body = json.dumps({
            "state": state, "host": socket.gethostname(), "last_seen": now_iso(),
            "poll_seconds": POLL_SECONDS, "in_progress": len(self.inflight), "processed_since_start": self.processed,
            "note": "If last_seen is more than a few minutes old, the PC is off, asleep, or the worker is stopped.",
        }, ensure_ascii=False, indent=2).encode()
        try:
            if self.status_id:
                self.drive.update_content(self.status_id, body, "application/json")
            else:
                self.status_id = self.drive.upload(body, "worker_status.json", "application/json", self.f["root"])["id"]
        except DriveError as e:
            log.warning("heartbeat failed: %s", e)

    def recover(self) -> None:
        """Requests claimed by a previous run that never finished (PC shut down mid-render)."""
        hits = self.drive.list(f"'{self.f['done']}' in parents and trashed = false and "
                               "appProperties has { key='state' and value='claimed' }", page_size=100)
        for h in hits:
            rid = request_id(h["name"])
            self.write_result(rid, {"status": "error", "error": "Interrupted: the PC worker stopped before the image "
                                    "finished (PC turned off or restarted). Submit it again."})
            self.drive.update(h["id"], props={"state": "handled"})
            log.info("%s: reported as interrupted", rid)

    # ------------------------------------------------------------------ queue
    def pending(self) -> list[dict]:
        files = self.drive.list(f"'{self.f['requests']}' in parents and trashed = false and "
                                "mimeType != 'application/vnd.google-apps.folder'", page_size=50)
        return [x for x in files if x["name"].lower().endswith(".json") and x["id"] not in self.inflight]

    def claim(self, fid: str) -> bool:
        """Move the request to done/ with our claim token. If a worker on another PC claims
        the same file at the same moment, the last writer wins and the other one backs off."""
        token = os.urandom(6).hex()
        try:
            self.drive.update(fid, add_parent=self.f["done"], remove_parent=self.f["requests"],
                              props={"state": "claimed", "host": socket.gethostname(), "claim": token})
            time.sleep(0.5)
            return (self.drive.meta(fid).get("appProperties") or {}).get("claim") == token
        except DriveError as e:
            log.warning("could not claim %s: %s", fid, e)
            return False

    async def poll_once(self) -> list[asyncio.Task]:
        items = await self.call(self.pending)
        claimed = await asyncio.gather(*(self.call(self.claim, x["id"]) for x in items))
        tasks = []
        for item, ok in zip(items, claimed):
            if ok:
                self.inflight.add(item["id"])
                tasks.append(asyncio.create_task(self.handle(item)))
        return tasks

    async def handle(self, item: dict) -> None:
        rid = request_id(item["name"])
        try:
            async with self.sem:
                result = await self.process(item, rid)
        except RequestError as e:
            result = {"status": "error", "error": str(e)}
        except Exception as e:  # never let one request kill the worker
            log.exception("%s failed", rid)
            result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        try:
            await self.call(self.write_result, rid, result)
            await self.call(self.drive.update, item["id"], None, None, {"state": "handled"})
        except DriveError as e:
            log.error("%s: could not write result: %s", rid, e)
        finally:
            self.inflight.discard(item["id"])
            self.processed += 1
        log.info("%s: %s%s", rid, result["status"], f" — {result.get('error')}" if result.get("error") else "")

    async def process(self, item: dict, rid: str) -> dict:
        req = await self.call(self.read_request, item)
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise RequestError('"prompt" is missing.')
        size = str(req.get("size") or "1024x1024")
        quality = str(req.get("quality") or core.DEFAULT_QUALITY)
        background = str(req.get("background") or "auto")
        fmt = str(req.get("output_format") or "png").lower()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in EXT:
            raise RequestError(f"output_format must be png, jpeg or webp (got {fmt}).")
        refs = req.get("references") or []
        if isinstance(refs, str):
            refs = [refs]
        if len(refs) > core.MAX_REFS:
            raise RequestError(f"Too many references ({len(refs)}); the limit is {core.MAX_REFS}.")
        labels, files = [], []
        for r in refs:
            label, f = await self.resolve_ref(str(r))
            labels.append(label)
            files.append(f)

        t0 = time.time()
        try:
            data, out_fmt = await core._render(prompt, files, size, quality, background, fmt)
        except Exception as e:
            raise RequestError(f"OpenAI: {type(e).__name__}: {e}")
        seconds = round(time.time() - t0, 1)
        meta = core._save(data, out_fmt, {"kind": "mobile", "prompt": prompt, "model": core.MODEL, "size": size,
                                          "quality": quality, "references": labels, "seconds": seconds,
                                          "request": rid}, rid)
        code = meta["id"][:8]
        img = await self.call(self.drive.upload, data, f"{rid}.{EXT[out_fmt]}", core.API_FORMATS[out_fmt],
                              self.f["outputs"], {"code": code, "kind": "mobile"}, prompt)
        return {"status": "done", "code": code, "prompt": prompt, "model": core.MODEL, "size": size,
                "quality": quality, "references": labels, "seconds": seconds,
                "image": {"id": img["id"], "name": img["name"], "url": img.get("webViewLink")},
                "local_path": meta["path"]}

    def read_request(self, item: dict) -> dict:
        if item.get("mimeType") == "application/vnd.google-apps.document":
            raw = self.drive.export_text(item["id"])  # created without disableConversionToGoogleType
        else:
            raw = self.drive.download(item["id"])
        text = raw.decode("utf-8-sig", errors="replace").strip()
        try:
            req = json.loads(text)
        except json.JSONDecodeError as e:
            raise RequestError(f"Bad request JSON: {e}")
        if not isinstance(req, dict):
            raise RequestError("Request JSON must be an object.")
        return req

    def write_result(self, rid: str, result: dict) -> None:
        result = {"id": rid, **result, "finished": now_iso(), "host": socket.gethostname()}
        body = json.dumps(result, ensure_ascii=False, indent=2).encode()
        self.drive.upload(body, f"{rid}.result.json", "application/json", self.f["outputs"])

    # ------------------------------------------------------------------ references
    async def resolve_ref(self, ref: str) -> tuple[str, tuple[str, bytes, str]]:
        """latest | name:<part> | Drive link or id | 8-char code (PC or mobile result) | result id | http(s) URL"""
        s = ref.strip().strip('"').strip("'")
        low = s.lower()
        hit = None
        if low in ("latest", "drive:latest"):
            hit = await self.call(self.newest_image, [self.f["refs"]], None)
            if not hit:
                raise RequestError("refs/ has no images yet. Upload one to My Drive/openai_image_mcp/refs first.")
        elif low.startswith(("name:", "drive:")):
            part = s.split(":", 1)[1].strip()
            hit = await self.call(self.newest_image, [self.f["refs"], self.f["outputs"]], part)
        elif m := DRIVE_ID_RE.search(s):
            try:
                hit = await self.call(self.drive.meta, m.group(1))
            except DriveError as e:
                raise RequestError(f"Drive file {m.group(1)} not found or not accessible ({e.status}).")
        elif BARE_ID_RE.match(s) and not core.CODE_RE.match(low):
            # a bare Drive file id, or an earlier result id such as 20261012_191500_disney_apose
            hit = await self.call(self.newest_image, [self.f["outputs"]], s)
            if not hit:
                try:
                    hit = await self.call(self.drive.meta, s)
                except DriveError:
                    hit = None
        elif core._find_code(s):
            m = core._find_code(s)
            p = Path(m["path"])
            return m["id"][:8], core._as_api_image(p.read_bytes(), p.name)
        elif re.match(r"^https?://", s, re.I):
            try:
                return await core._resolve_one(s)
            except Exception as e:
                raise RequestError(f"Could not download {s}: {e}")
        else:
            hit = await self.call(self.newest_image, [self.f["outputs"]], s)  # earlier result id
        if not hit:
            raise RequestError(f"Reference not found: {s}")
        if not str(hit.get("mimeType", "")).startswith("image/"):
            raise RequestError(f"Reference '{hit.get('name')}' is not an image ({hit.get('mimeType')}).")
        raw = await self.call(self.drive.download, hit["id"])
        try:
            api_file = core._as_api_image(raw, hit["name"])
        except Exception:
            raise RequestError(f"Cannot read '{hit['name']}' as an image (HEIC? convert to JPEG/PNG first).")
        return (hit.get("appProperties") or {}).get("code") or f"drive:{hit['id']}", api_file

    def newest_image(self, folders: list[str], part: str | None) -> dict | None:
        parents = " or ".join(f"'{f}' in parents" for f in folders)
        query = f"({parents}) and mimeType contains 'image/' and trashed = false"
        if part:
            query += f" and name contains '{q(part)}'"
        hits = self.drive.list(query, page_size=1, order="createdTime desc")
        return hits[0] if hits else None

    # ------------------------------------------------------------------ main loop
    async def run(self, once: bool = False) -> None:
        await self.call(self.setup_folders)
        await self.call(self.recover)
        await self.call(self.heartbeat)
        log.info("worker up: host=%s poll=%ss output=%s", socket.gethostname(), POLL_SECONDS, core.OUTPUT_DIR)
        last_beat = time.time()
        tasks: set[asyncio.Task] = set()
        while not self.stopping:
            try:
                for t in await self.poll_once():
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
            except DriveError as e:
                log.warning("poll failed: %s", e)
            if once:
                if tasks:
                    await asyncio.gather(*tasks)
                break
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                await self.call(self.heartbeat)
                last_beat = time.time()
            await asyncio.sleep(POLL_SECONDS)
        if tasks:
            log.info("waiting for %d image(s) to finish", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.call(self.heartbeat, "stopped")
        log.info("worker stopped")


def request_id(name: str) -> str:
    rid = re.sub(r"\.json$", "", name, flags=re.I)
    rid = re.sub(r"^req_", "", rid)
    rid = re.sub(r"[^\w.\-가-힣]+", "_", rid).strip("_")
    return rid or datetime.now().strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------- process control
def single_instance() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return None
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", LOCK_PORT))  # lock free -> nothing running
        return None
    except OSError:
        return pid
    finally:
        probe.close()


def keep_awake(on: bool) -> None:
    if sys.platform.startswith("win"):
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def background_python() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def startup_file() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/openai_image_worker.vbs"


def cmd_start() -> int:
    if running_pid():
        print(f"Already running (pid {running_pid()}).")
        return 0
    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen([background_python(), str(Path(__file__).resolve())], cwd=str(HERE), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=not sys.platform.startswith("win"))
    for _ in range(20):
        time.sleep(0.5)
        if running_pid():
            print(f"Started in the background (pid {running_pid()}). Log: {LOG_FILE}")
            return 0
    print(f"Did not start. See {LOG_FILE}")
    return 1


def cmd_stop() -> int:
    pid = running_pid()
    if not pid:
        print("Not running.")
        return 0
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)
    print(f"Stopped (pid {pid}).")
    return 0


def cmd_status() -> int:
    pid = running_pid()
    print(f"Worker: {'running, pid ' + str(pid) if pid else 'not running'}")
    print(f"Autostart: {'on' if sys.platform.startswith('win') and startup_file().exists() else 'off'}")
    if LOG_FILE.exists():
        print("Last log lines:")
        print("".join(LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines(True)[-5:]).rstrip())
    return 0


def cmd_autostart(on: bool) -> int:
    if not sys.platform.startswith("win"):
        print("Autostart helper is Windows-only.")
        return 1
    f = startup_file()
    if on:
        cmd = f'"{background_python()}" "{Path(__file__).resolve()}"'
        f.write_text('CreateObject("WScript.Shell").Run "' + cmd.replace('"', '""') + '", 0, False\r\n', encoding="utf-16")  # WSH reads UTF-16 (non-ASCII paths)
        print(f"Autostart on: {f}")
    else:
        f.unlink(missing_ok=True)
        print("Autostart off.")
    return 0


def cmd_check() -> int:
    ok = True
    key, src = core._api_key()
    print(f"OpenAI key: {'ok (' + src + ')' if key else 'MISSING — ' + src}")
    ok &= bool(key) or core.BACKEND == "fake"
    try:
        w = Worker(Drive.from_file(TOKEN_FILE))
        f = w.setup_folders()
        print(f"Google Drive: ok — My Drive/{ROOT_NAME}  https://drive.google.com/drive/folders/{f['root']}")
        for k in SUBFOLDERS:
            print(f"  {k:9s} {f[k]}")
    except DriveError as e:
        print(f"Google Drive: FAILED — {e}")
        ok = False
    print(f"Output folder: {core.OUTPUT_DIR}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="openai_image_mcp_local mobile worker (Google Drive mailbox)")
    g = ap.add_mutually_exclusive_group()
    for flag in ("check", "start", "stop", "status", "once", "install-autostart", "remove-autostart"):
        g.add_argument(f"--{flag}", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return cmd_check()
    if a.start:
        return cmd_start()
    if a.stop:
        return cmd_stop()
    if a.status:
        return cmd_status()
    if a.install_autostart or a.remove_autostart:
        return cmd_autostart(a.install_autostart)

    lock = single_instance()
    if lock is None:
        log.info("another worker is already running; exiting")
        print("Another worker is already running.")
        return 0
    PID_FILE.write_text(str(os.getpid()))
    try:
        worker = Worker(Drive.from_file(TOKEN_FILE))
    except DriveError as e:
        log.error("%s", e)
        print(e)
        return 1
    if KEEP_AWAKE:
        keep_awake(True)

    def stop(*_):
        worker.stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass
    try:
        asyncio.run(worker.run(once=a.once))
    finally:
        if KEEP_AWAKE:
            keep_awake(False)
        PID_FILE.unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
