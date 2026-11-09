# SPDX-License-Identifier: GPL-3.0-only
"""Local, trusted-checkout rendering; no arbitrary TeX, commands or paths.

Only the reviewed build program below is accepted. Its job-local copy receives
one audited argv hardening edit; the checkout is never modified. Environment
filtering, TeX output policy, budgets and process-tree termination are
defence in depth, NOT an OS/network sandbox. Trusted TeX can still access host
resources; deploy an OS-isolated worker before accepting untrusted checkouts.
No hard memory/CPU quota is claimed. The limits here bound concurrent jobs,
elapsed time, copied files/bytes, output files/bytes and retained log bytes.

Artifact responses contain metadata and a local download_path, never binary
chunks. The stdio server must keep one manager per owner/session; an HTTP
transport must additionally authenticate downloads. Registries intentionally
do not discover jobs from disk or survive a manager restart.

Workers are non-daemon: graceful stdio/interpreter exit waits for active renders
to finish or time out and clean up their owned process families. This can delay
exit by the remaining render deadline plus cleanup time. Forced process death
is outside that guarantee (in particular for POSIX new-session children).
"""

from __future__ import annotations

import copy
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from .config import Settings

# Both edu build.py files were inspected locally, without executing/importing.
# New upstream code requires review and an explicit digest update, not fallback.
_BUILD_HASHES = {"b94e6d0abe4af134208c23927b1f945cebde244faa6dbe0f19e1e19812d659e1"}
_COMPILER_ARGV = "cmd = ['xelatex', '-interaction=nonstopmode', '-halt-on-error', '-file-line-error', tex.name]"
_HARDENED_ARGV = _COMPILER_ARGV.replace("'xelatex',", "'xelatex', '-no-shell-escape',")
_ASSETS = {".png", ".jpg", ".jpeg", ".pdf"}
_SUBJECT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ROUND = re.compile(r"[1-9][0-9]{0,2}\Z")
_QUESTION = re.compile(r"[0-9]{2}\Z")
_LIMITATIONS = [
    "Trusted checkout/TeX only; this worker is not an OS or network sandbox.",
    "No hard memory/CPU quota; time, file, byte and concurrency budgets apply.",
    "openin_any=r permits upstream ../ imports and host file reads; only trusted snapshots are supported.",
    "Build success does not establish visual quality or legal/answer correctness.",
]


class RenderError(ValueError):
    """Invalid selection, unsafe filesystem input, or exhausted job budget."""


def _safe(path: Path) -> Path:
    """Reject links/reparse points in every existing component before resolving."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RenderError("Links and reparse points are not allowed")
    return path


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(65536), b""):
            h.update(data)
    return h.hexdigest()


def _regular(path: Path, limit: int) -> os.stat_result:
    _safe(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise RenderError("Only bounded regular non-hardlinked files are allowed")
    return info


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check_source_handle(stream, expected):
    opened = os.fstat(stream.fileno())
    # Windows path stat/fstat can report different ctime meanings. Compare the
    # portable identity/size/mtime fields, not the path-to-path cache signature.
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or getattr(opened, "st_file_attributes", 0) & 0x400
            or any(getattr(opened, field) != getattr(expected, field) for field in fields)):
        raise RenderError("Source handle changed during copy")


class _WindowsJob:
    """Private kill-on-close job; never discover or terminate processes by PID.

    Assign the suspended build before resuming it, so descendants cannot escape
    the assignment race. No breakaway permission is enabled. This is lifecycle
    ownership, not an OS access/network sandbox or a memory/CPU quota.
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self._ctypes = ctypes
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel.TerminateJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._native = ctypes.WinDLL("ntdll")
        # Popen closes the initial thread handle. Resume the owned process handle
        # with the native Windows API rather than reopening a thread/PID by name.
        self._native.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._native.NtResumeProcess.restype = wintypes.LONG
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def attach_and_resume(self, process):
        handle = int(process._handle)  # Owned CPython Windows Popen process handle.
        if not self._kernel.AssignProcessToJobObject(self._handle, handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        if self._native.NtResumeProcess(handle) < 0:
            raise RenderError("Could not resume owned build process")

    def close(self):
        if self._handle:
            handle, self._handle = self._handle, None
            try:
                # Also works when the build leader has already exited.
                if not self._kernel.TerminateJobObject(handle, 1):
                    raise self._ctypes.WinError(self._ctypes.get_last_error())
            finally:
                self._kernel.CloseHandle(handle)


class RenderManager:
    """Owner/session-local registry. Public calls return JSON-serializable dicts."""

    MAX_CONCURRENT = 2
    MAX_JOBS = 128
    TIMEOUT_SECONDS = 120
    MAX_FILES = 2048
    MAX_FILE_BYTES = 32 * 1024 * 1024
    MAX_COPY_BYTES = 128 * 1024 * 1024
    MAX_OUTPUT_BYTES = 128 * 1024 * 1024
    MAX_LOG_BYTES = 256 * 1024

    def __init__(self, settings: Settings):
        self.workspace = _safe(Path(settings.workspace))
        self.jobs_dir = _safe(Path(settings.jobs_dir))
        for alias in ("kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge", "kapa_mcp"):
            if _inside(self.jobs_dir, self.workspace / alias):
                raise RenderError("jobs_dir must be outside source repositories")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._slots = threading.BoundedSemaphore(self.MAX_CONCURRENT)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._artifacts: dict[str, dict[str, dict]] = {}
        self._copied_inputs: dict[str, tuple[dict[Path, tuple], set[Path]]] = {}

    def start_render(self, exam, subject, round, question_id=None, noanswers=True):
        if isinstance(exam, bool) or str(exam) not in ("1", "2"):
            raise RenderError("exam must be 1 or 2")
        if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
            raise RenderError("Invalid subject identifier")
        rd = str(round)
        if isinstance(round, bool) or not _ROUND.fullmatch(rd):
            raise RenderError("Invalid round identifier")
        if type(noanswers) is not bool:
            raise RenderError("noanswers must be boolean")
        seq = None
        if question_id is not None:
            prefix = f"edu/{exam}/{subject}/{rd}/"
            if not isinstance(question_id, str) or not question_id.startswith(prefix):
                raise RenderError("Question ID does not match selection")
            seq = question_id[len(prefix):]
            if not _QUESTION.fullmatch(seq) or seq == "00":
                raise RenderError("Invalid question identifier")
        with self._lock:
            if len(self._jobs) >= self.MAX_JOBS:
                raise RenderError("Session job registry is full")
            if not self._slots.acquire(blocking=False):
                raise RenderError("Concurrent render limit reached")
            job_id = str(uuid.uuid4())
            job_dir = _safe(self.jobs_dir / job_id)
            try:
                job_dir.mkdir(mode=0o700)
            except Exception:
                self._slots.release()
                raise
            job = {"job_id": job_id, "status": "preparing", "exam": int(exam),
                   "subject": subject, "round": rd, "question_id": question_id,
                   "noanswers": noanswers, "artifacts": [], "warnings": list(_LIMITATIONS)}
            self._jobs[job_id] = job
            self._artifacts[job_id] = {}
        try:
            self._prepare(job_dir, job, seq)
            self._update(job_id, status="queued")
            threading.Thread(target=self._run, args=(job_id,), daemon=False).start()
        except Exception:
            # Keep ownership and charge failed preparations to MAX_JOBS. Partial
            # copies are already bounded by the input budgets; no deletion or
            # caller-controlled recursive cleanup is needed.
            try:
                self._update(job_id, status="failed", stage="preparing",
                             error="Render preparation failed")
            finally:
                self._slots.release()
            raise
        return self.get_job(job_id)

    def _prepare(self, job_dir, job, seq):
        repo = _safe(self.workspace / f"kapa_edu_{job['exam']}")
        config = _safe(repo / "subjects.json")
        _regular(config, 65536)
        subjects = json.loads(config.read_text(encoding="utf-8"))
        if job["subject"] not in {s["folder"] for s in subjects["subjects"]}:
            raise RenderError("Subject is not in subjects.json")
        source = _safe(repo / job["subject"] / job["round"])
        if not source.is_dir():
            raise RenderError("Round directory not found")
        build = _safe(repo / "tools" / "build.py")
        _regular(build, self.MAX_FILE_BYTES)
        build_bytes = build.read_bytes()
        if hashlib.sha256(build_bytes).hexdigest() not in _BUILD_HASHES:
            raise RenderError("Unreviewed build.py; source digest is not approved")
        code = build_bytes.decode("utf-8")
        if code.count(_COMPILER_ARGV) != 1:
            raise RenderError("Reviewed compiler argv was not found exactly once")
        snapshot = job_dir / "snapshot"
        snapshot.mkdir()
        # Explicit files plus numbered fragments and image assets in this round.
        files = [config, build, _safe(repo / "latex" / "kapa-exam.sty")]
        walked = 0
        for base, dirs, names in os.walk(source, followlinks=False):
            for name in dirs + names:
                walked += 1
                if walked > self.MAX_FILES:
                    raise RenderError("Input file count budget exceeded")
                _safe(Path(base) / name)
            # No hidden dirs or unrelated payloads are copied.
            dirs[:] = [d for d in dirs if re.fullmatch(r"[A-Za-z0-9_-]+", d)]
            for name in names:
                path = Path(base) / name
                if path.parent == source and re.fullmatch(r"[0-9]{2}\.tex", name):
                    if seq is None or name in ("00.tex", f"{seq}.tex"):
                        files.append(path)
                elif (path.parent == source / "figures" and path.suffix.lower() in _ASSETS
                      and re.fullmatch(r"[0-9]{2}-[A-Za-z0-9_-]+\.(?:png|jpe?g|pdf)", name, re.I)
                      and (seq is None or name.startswith(seq + "-"))):
                    files.append(path)
        if seq and source / f"{seq}.tex" not in files:
            raise RenderError("Question not found")
        if not any(p.suffix == ".tex" and p.name != "00.tex" for p in files):
            raise RenderError("Round has no question fragments")
        total = 0
        inputs = []
        copied_files, copied_dirs = {}, {snapshot}
        for path in files:
            _safe(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RenderError("Only regular, non-hardlinked source files are allowed")
            total += info.st_size
            if info.st_size > self.MAX_FILE_BYTES or total > self.MAX_COPY_BYTES:
                raise RenderError("Input byte budget exceeded")
            rel = path.relative_to(repo)
            dest = snapshot / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Check the actual handle before consuming bytes and after reading.
            # This catches ordinary swaps/changes; it does not sandbox a host
            # that can mutate files concurrently. Checkouts must remain trusted.
            with path.open("rb") as stream:
                _check_source_handle(stream, info)
                data = stream.read(self.MAX_FILE_BYTES + 1)
                _check_source_handle(stream, info)
            if len(data) != info.st_size or len(data) > self.MAX_FILE_BYTES:
                raise RenderError("Source changed during copy")
            original_hash = hashlib.sha256(data).hexdigest()
            if path == build:
                if data != build_bytes:
                    raise RenderError("Build source changed during copy")
                data = code.replace(_COMPILER_ARGV, _HARDENED_ARGV).encode("utf-8")
            dest.write_bytes(data)
            copied_files[dest] = _file_identity(dest.stat())
            parent = dest.parent
            while parent != job_dir:
                copied_dirs.add(parent)
                parent = parent.parent
            inputs.append({"path": rel.as_posix(), "sha256": original_hash,
                           "snapshot_sha256": hashlib.sha256(data).hexdigest()})
        with self._lock:
            self._copied_inputs[job["job_id"]] = (copied_files, copied_dirs)
        argv = [sys.executable, "-I", "-B", "tools/build.py"]
        if job["noanswers"]:
            argv.append("--noanswers")
        if seq:
            argv += ["--single", f"{job['subject']}/{job['round']}/{seq}.tex"]
            if (source / "00.tex").is_file():
                job["warnings"].append("Upstream --single does not include 00.tex common instructions; preserved in snapshot only.")
        else:
            argv.append(f"{job['subject']}/{job['round']}")
        job.update(inputs=inputs, argv=argv, limits={
            "timeout_seconds": self.TIMEOUT_SECONDS, "max_concurrent": self.MAX_CONCURRENT,
            "max_files": self.MAX_FILES, "max_output_bytes": self.MAX_OUTPUT_BYTES,
            "max_log_bytes": self.MAX_LOG_BYTES})

    def _environment(self, job_dir):
        # Discover only installed tool executables; never inherit PATH wholesale,
        # proxies, credentials, Python/TeX config, or home/profile locations.
        found = [shutil.which(name) for name in ("xelatex", "kpsewhich")]
        if not all(found):
            raise FileNotFoundError("Required TeX executables are unavailable")
        tool_dirs = []
        for exe in found:
            path = _safe(Path(exe))
            if not path.is_file() or _inside(path, self.workspace) or _inside(path, self.jobs_dir):
                raise RenderError("TeX executables must be installed outside workspace/jobs")
            tool_dirs.append(str(path.parent))
        private = job_dir / "private"
        private.mkdir()
        env = {"PATH": os.pathsep.join(dict.fromkeys(tool_dirs)),
               "HOME": str(private), "USERPROFILE": str(private),
               "APPDATA": str(private), "LOCALAPPDATA": str(private),
               "TMP": str(private), "TEMP": str(private), "TMPDIR": str(private),
               "TEXMFHOME": str(private), "TEXMFVAR": str(private),
               "TEXMFCONFIG": str(private), "TEXMFOUTPUT": str(job_dir / "snapshot" / "build"),
               # Upstream wrappers import ../subject from build/. Paranoid input
               # mode rejects that layout; restricted mode is not a read sandbox.
               "shell_escape": "f", "openin_any": "r", "openout_any": "p",
               "parse_first_line": "f", "MKTEXPK": "0", "MKTEXTFM": "0",
               "OMP_NUM_THREADS": "1", "SOURCE_DATE_EPOCH": "0"}
        if os.name == "nt":
            root = os.environ.get("SystemRoot", r"C:\Windows")
            # Windows XeTeX's fixed PDF backend needs the C runtime command
            # processor to start xdvipdfmx, even with -no-shell-escape. Without
            # COMSPEC it reports an unrelated "can't write ...pdf" error at
            # shipout. Do not inherit a caller-supplied command processor.
            env.update(SystemRoot=root, WINDIR=root,
                       COMSPEC=str(Path(root) / "System32" / "cmd.exe"))
        return env

    def _update(self, job_id, **fields):
        with self._lock:
            self._jobs[job_id].update(fields)
            job_dir = _safe(self.jobs_dir / job_id)
            temporary = _safe(job_dir / "manifest.tmp")
            target = _safe(job_dir / "manifest.json")
            for path in (temporary, target):
                if path.exists():
                    _regular(path, self.MAX_FILE_BYTES)
            temporary.write_text(json.dumps(self._jobs[job_id], ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)

    def _capture(self, stream, path, result):
        count = 0
        try:
            with path.open("xb") as out:
                for block in iter(lambda: stream.read(8192), b""):
                    remaining = max(0, self.MAX_LOG_BYTES - count)
                    out.write(block[:remaining])
                    count += len(block)
        finally:
            result.append(count > self.MAX_LOG_BYTES)
            stream.close()

    @staticmethod
    def _kill(process):
        if os.name != "nt":
            try:
                # Popen started a new session owned by this render. Kill that
                # group even when poll() has already reaped its exited leader.
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    def _check_outputs(self, root):
        # Exempt only exact copied files/directories, never a snapshot subtree.
        # Changed copied files are rejected, so their names cannot hide output.
        copied_files, copied_dirs = self._copied_inputs.get(root.name, ({}, set()))
        count, size = 0, 0
        seen_files, seen_dirs = set(), set()
        for base, dirs, names in os.walk(root, followlinks=False):
            for name in dirs + names:
                path = _safe(Path(base) / name)
                info = path.stat()
                if stat.S_ISDIR(info.st_mode):
                    exempt = path in copied_dirs
                    if exempt:
                        seen_dirs.add(path)
                else:
                    if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                        raise RenderError("Unsafe output file")
                    exempt = path in copied_files
                    if exempt:
                        seen_files.add(path)
                    if exempt and _file_identity(info) != copied_files[path]:
                        raise RenderError("Copied input changed during render")
                    if not exempt:
                        size += info.st_size
                if not exempt:
                    count += 1
                if count > self.MAX_FILES or size > self.MAX_OUTPUT_BYTES:
                    raise RenderError("Output resource budget exceeded")
        if seen_files != copied_files.keys() or seen_dirs != copied_dirs:
            raise RenderError("Copied input deleted or renamed during render")

    def _run(self, job_id):
        job_dir = self.jobs_dir / job_id
        process = None
        family = None
        readers = []
        truncated = []
        pending = {}
        try:
            env = self._environment(job_dir)
            self._update(job_id, status="running")
            with self._lock:
                argv = list(self._jobs[job_id]["argv"])
            if os.name == "nt":
                family = _WindowsJob()
            process = subprocess.Popen(argv, cwd=job_dir / "snapshot", env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False,
                                       start_new_session=os.name != "nt",
                                       creationflags=0x00000004 if os.name == "nt" else 0)  # CREATE_SUSPENDED
            if family is not None:
                family.attach_and_resume(process)
            for stream, name in ((process.stdout, "stdout.log"), (process.stderr, "stderr.log")):
                thread = threading.Thread(target=self._capture, args=(stream, job_dir / name, truncated), daemon=True)
                thread.start()
                readers.append(thread)
            deadline = time.monotonic() + self.TIMEOUT_SECONDS
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                self._check_outputs(job_dir)
                time.sleep(0.05)
            code = process.returncode
            status = "succeeded" if code == 0 else "missing_dependency" if code == 2 else "failed"
            outcome = {"status": status, "returncode": code}
        except FileNotFoundError:
            outcome = {"status": "missing_dependency", "error": "Required build/TeX dependency unavailable"}
        except TimeoutError:
            outcome = {"status": "timed_out", "error": "Render time budget exceeded"}
        except Exception:
            # Never publish exception text containing environment/source content.
            outcome = {"status": "failed", "error": "Render failed or resource/path policy rejected output"}
        finally:
            try:
                # Stop all owned writers before joining pipes, inspecting logs,
                # registering artifacts or publishing a terminal job status.
                try:
                    if family is not None:
                        family.close()
                finally:
                    if process is not None:
                        self._kill(process)
                        process.wait(timeout=10)
                for thread in readers:
                    thread.join(timeout=2)
                if any(thread.is_alive() for thread in readers):
                    raise RenderError("Output pipes did not close")
                if process is not None:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                self._check_outputs(job_dir)
                success = outcome["status"] == "succeeded"
                pending = self._collect(job_id, include_pdf=success)
                if success and not any(a["media_type"] == "application/pdf" for a in pending.values()):
                    outcome = {"status": "failed", "error": "Build returned no PDF"}
                outcome["logs_truncated"] = any(truncated)
                outcome["missing_glyphs"] = self._missing_glyphs(job_dir)
            except Exception:
                pending = {}
                outcome = {"status": "failed", "error": "Render cleanup or output policy rejected result"}
            finally:
                try:
                    self._publish(job_id, outcome, pending)
                finally:
                    self._slots.release()

    def _publish(self, job_id, outcome, pending):
        # Hold the API lock while writing/checking the final manifest. Staged
        # descriptors remain unavailable until diagnostics AND that manifest
        # pass the final resource/path checks. Rejected jobs expose no artifacts.
        with self._lock:
            self._update(job_id, artifacts=list(pending.values()), **outcome)
            try:
                self._check_outputs(self.jobs_dir / job_id)
            except Exception:
                self._artifacts[job_id] = {}
                self._update(job_id, status="failed", artifacts=[],
                             error="Render cleanup or output policy rejected result")
            else:
                self._artifacts[job_id] = pending

    def _missing_glyphs(self, job_dir):
        missing = set()
        build = _safe(job_dir / "snapshot" / "build")
        for path in sorted(build.glob("*.log")):
            # Compiler logs are outputs, not copied input files. Match the
            # allowance already enforced by output accounting and collection.
            _regular(path, self.MAX_OUTPUT_BYTES)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            carry = ""
            scanned = 0
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    scanned += len(block)
                    if scanned > self.MAX_OUTPUT_BYTES:
                        raise RenderError("Compiler log scan byte budget exceeded")
                    text = carry + decoder.decode(block)
                    for match in re.finditer(r"Missing character: There is no (.) ", text):
                        if len(missing) < 100:
                            missing.add(match.group(1))
                    # Match text and UTF-8 may straddle read boundaries; neither
                    # a giant line nor an unbounded match list is accumulated.
                    carry = text[-64:]
                text = carry + decoder.decode(b"", final=True)
                for match in re.finditer(r"Missing character: There is no (.) ", text):
                    if len(missing) < 100:
                        missing.add(match.group(1))
        return sorted(missing)

    def _collect(self, job_id, include_pdf):
        job_dir = _safe(self.jobs_dir / job_id)
        candidates = [(job_dir / "stdout.log", "text/plain"), (job_dir / "stderr.log", "text/plain")]
        build = _safe(job_dir / "snapshot" / "build")
        log_metadata = {}
        if build.is_dir():
            # Keep bounded compiler diagnostics outside the disposable snapshot,
            # on failures too. Large logs retain both the prologue and final error.
            diagnostics = _safe(job_dir / "diagnostics")
            diagnostics.mkdir(exist_ok=True)
            for source in sorted(build.glob("*.log")):
                if not re.fullmatch(r"[A-Za-z0-9_-]+\.log", source.name):
                    continue
                info = _regular(source, self.MAX_OUTPUT_BYTES)
                dest = _safe(diagnostics / source.name)
                with source.open("rb") as stream:
                    if info.st_size <= self.MAX_LOG_BYTES:
                        data = stream.read(self.MAX_LOG_BYTES)
                    else:
                        marker = b"\n[compiler log truncated]\n"[:self.MAX_LOG_BYTES]
                        budget = max(0, self.MAX_LOG_BYTES - len(marker))
                        first = budget // 2
                        data = stream.read(first)
                        stream.seek(-(budget - first), os.SEEK_END)
                        data += marker + stream.read(budget - first)
                with dest.open("xb") as stream:
                    stream.write(data)
                candidates.append((dest, "text/plain"))
                log_metadata[dest] = {"truncated": info.st_size > self.MAX_LOG_BYTES,
                                      "original_size_bytes": info.st_size}
            candidates.append((build / "build_report.json", "application/json"))
            if include_pdf:
                candidates.extend((p, "application/pdf") for p in sorted(build.glob("*.pdf")))
        pending = {}
        for path, media in candidates:
            _safe(path)
            if not path.is_file():
                continue
            info = path.stat()
            if info.st_nlink != 1 or info.st_size > self.MAX_FILE_BYTES:
                raise RenderError("Unsafe or oversized artifact")
            rel = path.relative_to(job_dir).as_posix()
            aid = str(uuid.uuid4())
            metadata = {"artifact_id": aid, "path": rel, "media_type": media,
                        "size_bytes": info.st_size, "sha256": _digest(path)}
            metadata.update(log_metadata.get(path, {}))
            pending[aid] = metadata
        return pending

    def get_job(self, job_id):
        with self._lock:
            if not isinstance(job_id, str) or job_id not in self._jobs:
                raise RenderError("Unknown or unowned job ID")
            return copy.deepcopy(self._jobs[job_id])

    def get_artifact(self, job_id, artifact_id):
        self.get_job(job_id)
        with self._lock:
            if not isinstance(artifact_id, str) or artifact_id not in self._artifacts[job_id]:
                raise RenderError("Unknown or unowned artifact ID")
            result = copy.deepcopy(self._artifacts[job_id][artifact_id])
        root = _safe(self.jobs_dir / job_id)
        path = _safe(root / result["path"])
        if not _inside(path, root) or not path.is_file():
            raise RenderError("Artifact is unavailable")
        info = path.stat()
        if info.st_nlink != 1 or info.st_size != result["size_bytes"] or _digest(path) != result["sha256"]:
            raise RenderError("Artifact changed after registration")
        result.update(job_id=job_id, download_path=str(path))
        return result
