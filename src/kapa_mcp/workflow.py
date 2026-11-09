# SPDX-License-Identifier: GPL-3.0-only
"""Immutable metadata-only bundles with a bounded active lookup window.

At capacity the oldest active bundle directory moves to a private archive;
files are preserved. Exact-ID lookup checks one active and one archive path,
never scans archives, and always repeats source/digest validation. Retention
bounds active enumeration, not total disk usage. Root mutations/metadata loads
serialize across clients with a bounded OS lock; no public API is added.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid

from .readers import REPOS, MAX_RESPONSE_BYTES, _json, _no_links


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _private(path):
    """Apply OS owner access before writing metadata; never invoke a shell."""
    if os.name == "nt":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        identity = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, timeout=10, check=True)
        match = re.search(rb"S-1-[0-9-]+", identity.stdout)
        if not match:
            raise ValueError("Cannot establish private evidence ownership")
        subprocess.run([str(system / "icacls.exe"), str(path), "/inheritance:r", "/grant:r",
                        "*" + match[0].decode("ascii") + ":(OI)(CI)F"],
                       capture_output=True, timeout=10, check=True)
    else:
        path.chmod(0o700)


class WorkflowManager:
    MAX_BUNDLES = 128
    MAX_BUNDLE_BYTES = MAX_RESPONSE_BYTES

    def __init__(self, settings, catalog):
        self.settings, self.catalog = settings, catalog
        jobs = Path(os.path.abspath(settings.jobs_dir))
        workspace = Path(os.path.abspath(settings.workspace))
        for repo in (*REPOS, "kapa_mcp"):
            source = workspace / repo
            if jobs.is_relative_to(source) or source.is_relative_to(jobs):
                raise ValueError("Evidence storage must be separate from source repositories")
        # Check every existing ancestor before mkdir can follow a link.
        for path in reversed((jobs, *jobs.parents)):
            if path.exists() or path.is_symlink():
                _no_links(path)
        jobs.mkdir(parents=True, exist_ok=True)
        _no_links(jobs)
        self.root = jobs / "evidence"
        if not self.root.exists():
            self.root.mkdir(mode=0o700, exist_ok=True)
        _no_links(self.root)
        if not self.root.is_dir():
            raise ValueError("Invalid evidence storage")
        try:
            _private(self.root)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Cannot establish private evidence storage") from None
        self._lock = threading.RLock()

    @contextmanager
    def _storage_lock(self):
        """Crash-released OS lock shared by all clients of this evidence root."""
        with self._lock:
            _no_links(self.root)
            path = self.root / ".lock"
            if path.exists() or path.is_symlink():
                _no_links(path)
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(descriptor, "r+b") as stream:
                    opened = os.fstat(stream.fileno())
                    _no_links(path)
                    expected = path.stat()
                    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > 1
                            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)):
                        raise ValueError("Invalid evidence storage lock")
                    if opened.st_size == 0:
                        stream.write(b"0")
                        stream.flush()
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            stream.seek(0)
                            if os.name == "nt":
                                import msvcrt
                                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise ValueError("Evidence storage is busy") from None
                            time.sleep(0.02)
                    try:
                        yield
                    finally:
                        stream.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(stream, fcntl.LOCK_UN)
            except OSError:
                raise ValueError("Evidence storage unavailable") from None

    def _make_room(self):
        """Called under the root lock; inspect only the bounded active window."""
        directories = []
        with os.scandir(self.root) as entries:
            for index, entry in enumerate(entries):
                if index > self.MAX_BUNDLES + 2:
                    raise ValueError("Evidence active window exceeds resource limit")
                if entry.name in {".lock", "archive", ".staging"}:
                    continue
                if not re.fullmatch(r"bundle-[a-f0-9]{64}", entry.name):
                    raise ValueError("Invalid evidence storage entry")
                directory = self.root / entry.name
                _no_links(directory)
                if not directory.is_dir():
                    raise ValueError("Invalid evidence bundle directory")
                directories.append((directory.stat().st_mtime_ns, entry.name))
        if len(directories) < self.MAX_BUNDLES:
            return
        if len(directories) > self.MAX_BUNDLES or self.MAX_BUNDLES < 1:
            raise ValueError("Evidence active window exceeds resource limit")
        _, oldest = min(directories)
        # Validate the immutable manifest before relocating its owning directory.
        self._load_unlocked(oldest)
        source = self.root / oldest
        with os.scandir(source) as contents:
            for index, entry in enumerate(contents):
                if index or entry.name != "bundle.json":
                    raise ValueError("Unexpected evidence bundle directory contents")
        archive = self.root / "archive"
        archive.mkdir(mode=0o700, exist_ok=True)
        _no_links(archive)
        target = archive / oldest
        # Both absolute targets stay inside this explicitly configured root.
        # Never traverse/overwrite an existing archive entry, even an empty one.
        if (source.parent != self.root or target.parent != archive
                or not source.is_relative_to(self.root) or not target.is_relative_to(self.root)
                or target.exists() or target.is_symlink()):
            raise ValueError("Evidence archive destination is unavailable")
        source.rename(target)

    def _cleanup_staging(self, temporary, identity=None):
        """Best effort, nonrecursive cleanup of this call's private staging path.

        A failed cleanup leaves only reserved staging metadata, never an active
        bundle ID that could consume capacity or block future archival.
        """
        staging = self.root / ".staging"
        if (identity is None or temporary.parent != staging
                or not re.fullmatch(r"stage-[a-f0-9]{32}", temporary.name)):
            return
        try:
            _no_links(self.root)
            _no_links(staging)
            if not temporary.exists():
                return
            _no_links(temporary)
            current = temporary.stat()
            if (current.st_dev, current.st_ino) != identity:
                return
            resolved = temporary.resolve()
            if resolved.parent != staging.resolve() or not resolved.is_relative_to(self.root.resolve()):
                return
            path = temporary / "bundle.json"
            if path.exists() or path.is_symlink():
                _no_links(path)
                path.unlink()
            temporary.rmdir()
        except (OSError, ValueError):
            # Do not delete anything else, scan staging, or let a storage error
            # turn abandoned staging into a permanent active-window blocker.
            pass

    def create_bundle(self, task, document_ids=None, question_ids=None, as_of=None):
        if not isinstance(task, str) or not task.strip() or len(task) > 4096:
            raise ValueError("task must contain 1..4096 characters")
        if not self.catalog._content_safe(task):
            raise ValueError("Task contains an absolute local path")
        snapshot = self.catalog.capture_evidence(document_ids, question_ids, as_of)
        payload = {"schema": 1, "nonce": uuid.uuid4().hex, "task": task,
                   "document_ids": snapshot["document_ids"], "question_ids": snapshot["question_ids"],
                   "as_of": as_of, "snapshot": snapshot, "coverage": snapshot["coverage"],
                   "checks": snapshot["checks"], "missing_items": snapshot["missing_items"]}
        digest = hashlib.sha256(_encode(payload)).hexdigest()
        identifier = "bundle-" + digest
        bundle = dict(payload, bundle_id=identifier, digest=digest)
        data = _encode(bundle)
        if len(data) > self.MAX_BUNDLE_BYTES:
            raise ValueError("Evidence bundle exceeds size limit")
        with self._storage_lock():
            directory = self.root / identifier
            if directory.exists() or directory.is_symlink():
                raise ValueError("Evidence bundle destination already exists")
            staging = self.root / ".staging"
            staging.mkdir(mode=0o700, exist_ok=True)
            _no_links(staging)
            temporary = staging / ("stage-" + uuid.uuid4().hex)
            temporary.mkdir(mode=0o700)
            created = temporary.stat()
            identity = (created.st_dev, created.st_ino)
            try:
                _no_links(temporary)
                path = temporary / "bundle.json"
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    stream = os.fdopen(descriptor, "wb")
                except BaseException:
                    os.close(descriptor)
                    raise
                with stream:
                    if stream.write(data) != len(data):
                        raise OSError("Incomplete evidence metadata write")
                    stream.flush()
                    os.fsync(stream.fileno())
                # Only fully written/fsynced metadata may enter the active ID
                # namespace. Retention runs after staging succeeds, preserving
                # the current active window on open/write/fsync failures.
                self._make_room()
                archive = self.root / "archive"
                if archive.exists() or archive.is_symlink():
                    _no_links(archive)
                archived = archive / identifier
                if (directory.exists() or directory.is_symlink()
                        or archived.exists() or archived.is_symlink()
                        or temporary.parent != staging or directory.parent != self.root
                        or not temporary.resolve().is_relative_to(self.root.resolve())
                        or directory.resolve().parent != self.root.resolve()):
                    raise ValueError("Evidence bundle destination is unavailable")
                temporary.rename(directory)
            except OSError:
                raise ValueError("Cannot persist evidence bundle") from None
            finally:
                self._cleanup_staging(temporary, identity)
        return bundle

    def _load(self, bundle_id):
        with self._storage_lock():
            return self._load_unlocked(bundle_id)

    def _load_unlocked(self, bundle_id):
        if not isinstance(bundle_id, str) or not re.fullmatch(r"bundle-[a-f0-9]{64}", bundle_id):
            raise ValueError("Invalid evidence bundle ID")
        directory = self.root / bundle_id
        if not directory.exists() and not directory.is_symlink():
            directory = self.root / "archive" / bundle_id
        path = directory / "bundle.json"
        try:
            _no_links(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.MAX_BUNDLE_BYTES:
                raise ValueError("Invalid evidence bundle resource")
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValueError("Evidence bundle is not private to its owner")
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
                    raise ValueError("Evidence bundle changed during open")
                data = stream.read(self.MAX_BUNDLE_BYTES + 1)
            if len(data) > self.MAX_BUNDLE_BYTES:
                raise ValueError("Evidence bundle exceeds size limit")
            bundle = _json(data.decode("utf-8"))
            if not isinstance(bundle, dict):
                raise ValueError("Invalid evidence bundle")
            payload = {k: v for k, v in bundle.items() if k not in {"bundle_id", "digest"}}
            digest = hashlib.sha256(_encode(payload)).hexdigest()
            if (bundle.get("schema") != 1 or bundle.get("bundle_id") != bundle_id
                    or bundle.get("digest") != digest or bundle_id != "bundle-" + digest
                    or not self.catalog._content_safe(bundle)):
                raise ValueError("Evidence bundle integrity mismatch")
            return bundle
        except (OSError, UnicodeError, TypeError):
            raise ValueError("Evidence bundle unavailable or malformed") from None

    def _validate(self, bundle):
        previous = bundle["snapshot"]
        current = self.catalog.capture_evidence(bundle["document_ids"], bundle["question_ids"], bundle["as_of"])
        same = _encode(previous) == _encode(current)
        pinned = previous["reproducible"] is True and current["reproducible"] is True
        return {"bundle_id": bundle["bundle_id"], "digest": bundle["digest"],
                "valid": bool(same and pinned), "stale": not same,
                "checks": [{"check": "stored_digest", "passed": True},
                           {"check": "current_sources_match", "passed": same},
                           {"check": "immutable_source_provenance", "passed": pinned}],
                "coverage": previous["coverage"], "missing_items": previous["missing_items"]}

    def validate_bundle(self, bundle_id):
        try:
            return self._validate(self._load(bundle_id))
        except (ValueError, KeyError, TypeError):
            return {"bundle_id": bundle_id if isinstance(bundle_id, str) and re.fullmatch(r"bundle-[a-f0-9]{64}", bundle_id) else None,
                    "valid": False, "stale": True,
                    "checks": [{"check": "bundle_and_source_integrity", "passed": False}]}

    def get_bundle(self, bundle_id):
        bundle = self._load(bundle_id)
        if not self._validate(bundle)["valid"]:
            raise ValueError("Evidence bundle is stale or lacks immutable provenance")
        return bundle
