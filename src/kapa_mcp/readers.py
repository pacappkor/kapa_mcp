# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, read-only adapters. No upstream Python modules are imported."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import unicodedata
from contextvars import ContextVar
from functools import wraps
from datetime import datetime
from urllib.parse import urlsplit
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

REPOS = ("kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge")
MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 24000
MAX_FILES = 30000
MAX_RESPONSE_BYTES = 1024 * 1024
GIT_TIMEOUT = 15
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_REQUEST_ENTRIES = 120000
REQUEST_TIMEOUT = 60
_QUERY = ContextVar("reader_query", default=None)
_QUESTION = re.compile(r"edu/([12])/([a-z][a-z0-9_]*)/([0-9]{1,3})/([0-9]{2})\Z")


class RequestBudgetExceeded(RuntimeError):
    """An exhausted request aborts, never becoming a partial provenance result."""


def _request_budget(*, read_bytes=0, entries=0, require_bytes=0):
    query = _QUERY.get()
    if query is None:
        return
    if query["budget_error"]:
        raise RequestBudgetExceeded(query["budget_error"])
    query["bytes_read"] += read_bytes
    query["entries_visited"] += entries
    reason = None
    if time.monotonic() >= query["deadline"]:
        reason = "Reader request deadline exceeded"
    elif query["bytes_read"] + require_bytes > query["byte_limit"]:
        reason = "Reader request cumulative read-byte budget exceeded"
    elif query["entries_visited"] > query["entry_limit"]:
        reason = "Reader request cumulative enumeration budget exceeded"
    if reason:
        query["budget_error"] = reason
        raise RequestBudgetExceeded(reason)


def _parts(relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "%" in relative:
        raise ValueError("Invalid relative content path")
    parts = relative.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or p.endswith((" ", ".")) for p in parts):
        raise ValueError("Invalid relative content path")
    if any(re.search(r"credential|secret|token|law_oc|edu_gh|password|^tools$", p, re.I) for p in parts):
        raise ValueError("Forbidden content path")
    return parts


def _no_links(path):
    _request_budget()
    # Check ancestors before resolve(), including workspace ancestors and Windows junctions.
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except OSError:
            raise ValueError("Content path unavailable") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Links and reparse points are forbidden")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("Hardlinked content is forbidden")


def _charge(count, amount=1):
    _request_budget(entries=amount)
    count += amount
    if count > MAX_FILES:
        raise ValueError("Repository enumeration limit exceeded")
    return count


def _allowed(repo, relative):
    p = _parts(relative)
    if relative in {"README.md", "AGENTS.md", "NOTICE.md"}:
        return True
    if repo.startswith("kapa_edu_"):
        return relative in {"subjects.json", "answers/answers.csv", "docs/SOURCES.md"} or bool(
            re.fullmatch(r"[a-z][a-z0-9_]*/[0-9]{1,3}/[0-9]{2}\.tex", relative))
    if repo == "kapa_skills":
        if relative == "docs/law-index.json":
            return True
        if re.fullmatch(r"sources/laws/[^/]+/versions/[^/]+\.json", relative):
            return True
        if relative == "rules/skill-reading-rules/SKILL.md":
            return True
        return (p[0] in {"00-kapa-start", "laws", "appraisal-practice-standards"}
                and p[-1] in {"SKILL.md", "source.md", "facets.md"})
    if repo == "kapa_knowledge":
        if relative in {"catalogs/materials.json", "runs/pilot/held-items-research.json",
                        "runs/pilot/predecessor-versions-research.json"}:
            return True
        if relative.startswith("vendor/kapa_skills/"):
            inner = relative[len("vendor/kapa_skills/"):]
            return inner == "docs/law-index.json" or bool(re.fullmatch(
                r"sources/laws/[^/]+/versions/[^/]+\.json", inner))
        if relative in {"deps.lock", "docs/SCHEMA.md", "00-knowledge-start/SKILL.md",
                        "rules/knowledge-reading-rules/SKILL.md", "relations/relations.jsonl"}:
            return True
        return bool(re.fullmatch(
            r"(?:issues/[^/]+/(?:README|coverage)\.md|(?:cases|interpretations)/[^/]+/[0-9]{4}/[^/]+\.md|"
            r"reviews/[0-9]{4}/[^/]+\.jsonl|sources/(?:prec|expc|cgmexpc)/[0-9]+\.json|"
            r"runs/[^/]+/[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*\.json)", relative))
    return False


def _json(text):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("Duplicate data key")
            result[k] = v
        return result
    def constant(_):
        raise ValueError("Non-finite data value")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite data value")
        return number
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    except (ValueError, TypeError):
        raise ValueError("Invalid structured content") from None


def _jsonl(text):
    # Count every physical row, including blanks, before parsing or retaining it.
    # StringIO avoids allocating splitlines()'s full list of tiny input strings.
    for line in io.StringIO(text):
        _request_budget(entries=1)
        if not line.strip():
            continue
        record = _json(line)
        if not isinstance(record, dict):
            raise ValueError("JSONL records must be objects")
        yield record


def _relation_identity_matches(relation):
    if not isinstance(relation, dict):
        return False
    fields = [relation.get(key) for key in ("type", "src", "dst", "scope")]
    if not all(isinstance(value, str) for value in fields):
        return False
    try:
        canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError):
        return False
    return relation.get("id") == "rel-" + hashlib.sha256(canonical).hexdigest()[:20]


def _metadata(text):
    result = {}
    if text.startswith("---\n"):
        head, sep, _ = text[4:].partition("\n---\n")
        if not sep:
            raise ValueError("Invalid document metadata")
        if len(head) > 32000:
            raise ValueError("Document metadata exceeds response limits")
        for line in head.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if not sep or not re.fullmatch(r"[A-Za-z_][\w-]*", key) or key in result:
                raise ValueError("Invalid document metadata")
            value = value.strip()
            if value.startswith(('"', '[', '{')) or value in {"null", "true", "false"} or re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
                result[key] = _json(value)
            elif value.startswith("'") and value.endswith("'"):
                result[key] = value[1:-1].replace("''", "'")
            elif value.startswith(("&", "*", "!", "|", ">")):
                raise ValueError("Unsupported document metadata")
            else:
                result[key] = value
    return result


def _norm(text):
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _score(query, text):
    q, t = _norm(query).strip(), _norm(text)
    if not q:
        return 1
    tokens = set(re.findall(r"[\w]+", q))
    hits = sum(token in t for token in tokens)
    return (100 if q in t else 0) + 10 * hits + (20 if tokens and hits == len(tokens) else 0)


def _page(items, limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("limit must be 1..100; offset must be nonnegative")
    end = offset + limit
    nxt = end if end < len(items) else None
    return {"items": items[offset:end], "total": len(items), "offset": offset,
            "limit": limit, "next_offset": nxt, "next_cursor": nxt}


# Evidence storage uses a positive schema at every level. In particular a
# recognized container never grants arbitrary nested keys permission to persist.
_ASSERTION_FIELDS = {key: None for key in (
    "source_id", "source_url", "asserted_replied", "citation", "reply_sha256",
    "title", "upstream_url", "date")}
_PROVENANCE_CATEGORY = frozenset({"source", "machine", "analysis", "review"})
_ASSERTION_FIELDS["provenance"] = {key: _PROVENANCE_CATEGORY for key in _ASSERTION_FIELDS}
_RECORD_FIELDS = {key: None for key in (
    "id", "kind", "agency", "case_no", "material_type", "identity_status",
    "publication_hold", "reference_status", "progress", "evidence_basis",
    "evidence_file", "evidence_item", "evidence_sha256", "existing_document_id",
    "asserted_replied", "reply_sha256", "source_envelope_status", "source_id",
    "source_url", "upstream_url", "title")}
_RECORD_FIELDS.update(source_identifiers={"lawgo": None, "epUnionSn": None},
                      assertions=[_ASSERTION_FIELDS])
_RECORD_FIELDS["provenance"] = {key: _PROVENANCE_CATEGORY for key in (*_RECORD_FIELDS, "limitations")}
_METADATA_FIELDS = {key: None for key in (
    "id", "name", "title", "source_url", "source_sha256", "precSeq", "expcSeq",
    "cgmExpcDatSeq", "applied_version", "reference_status", "progress",
    "material_type", "case_no", "replied", "decided", "agency")}
_METADATA_FIELDS["kapa"] = {key: None for key in (
    "exam", "subject", "subject_ko", "round", "year", "period", "seq", "number", "points")}
_SELECTED_FIELDS = {key: None for key in (
    "id", "repo", "path", "revision", "sha256", "head_verified", "dirty",
    "reference_status", "progress", "version", "source_url", "publication_hold",
    "kind", "title", "source_document_id", "metadata_sha256", "locked_revision",
    "catalog_verified", "article_text_available", "applicability_verified", "review_verified")}
_SELECTED_FIELDS.update(metadata=_METADATA_FIELDS, record=_RECORD_FIELDS,
                        entries=[{key: None for key in (
                            "lsiSeq", "efYd", "ancYd", "ancNo", "name", "kind", "status")}])


def _metadata_projection(value, schema):
    """Copy only scalar leaves explicitly declared by the metadata schema."""
    _request_budget(entries=1)
    if isinstance(schema, frozenset):
        return value if isinstance(value, str) and value in schema else None
    if schema is None:
        return value if value is None or type(value) in (str, bool, int, float) else None
    if isinstance(schema, list):
        return [_metadata_projection(item, schema[0]) for item in value] if isinstance(value, list) else []
    if not isinstance(value, dict):
        return {}
    return {key: _metadata_projection(value[key], rule) for key, rule in schema.items() if key in value}


def _bounded(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        if any(isinstance(v, str) and len(v) > 4096 for v in (*args, *kwargs.values())):
            raise ValueError("Reader input exceeds size limit")
        current = _QUERY.get()
        token = None
        if current is None or current["catalog"] is not self:
            token = _QUERY.set({"catalog": self, "states": {}, "manifests": {}, "reads": {},
                                "supplemental": None, "supplemental_by_id": {},
                                "supplemental_reads": {},
                                "bytes_read": 0, "entries_visited": 0, "budget_error": None,
                                "byte_limit": MAX_REQUEST_BYTES, "entry_limit": MAX_REQUEST_ENTRIES,
                                "deadline": time.monotonic() + REQUEST_TIMEOUT})
        try:
            _request_budget()
            result = method(self, *args, **kwargs)
            if token is not None:
                self._refresh_provenance(result)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ValueError("Response exceeds size limit; narrow the query or range")
            if not self._content_safe(result):
                raise ValueError("Content contains an absolute local path; response withheld")
            _request_budget()
            return result
        except (OSError, UnicodeError, KeyError, TypeError, AttributeError):
            raise ValueError("Reader data unavailable or malformed") from None
        finally:
            if token is not None:
                _QUERY.reset(token)
    return invoke


def _capture_git(command, env):
    """Drain bounded stdout concurrently with a deadline, including stalled pipes."""
    data = bytearray()
    _request_budget(require_bytes=1)
    query = _QUERY.get()
    limit = min(MAX_BYTES, query["byte_limit"] - query["bytes_read"]) if query is not None else MAX_BYTES
    overflow, complete, failed = threading.Event(), threading.Event(), threading.Event()
    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    def drain():
        try:
            while True:
                block = proc.stdout.read(min(65536, limit + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > limit:
                    overflow.set()
                    break
        except (OSError, ValueError):
            failed.set()
        finally:
            complete.set()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + GIT_TIMEOUT
    try:
        while not (complete.is_set() and proc.poll() is not None):
            _request_budget()
            if overflow.is_set():
                raise ValueError("Git stdout exceeds size limit")
            if time.monotonic() >= deadline:
                raise ValueError("Git query timed out")
            time.sleep(0.01)
        if overflow.is_set():
            raise ValueError("Git stdout exceeds size limit")
        if failed.is_set() or proc.returncode:
            raise ValueError("Git query unavailable")
        return bytes(data)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        proc.stdout.close()
        reader.join(timeout=1)
        _request_budget(read_bytes=len(data))


class Catalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspace = Path(os.path.abspath(settings.workspace))
        _no_links(self.workspace)

    def _content_safe(self, result):
        # Inspect decoded strings, not JSON escape spellings. A single leading
        # slash is also used by wiki/API paths, so only configured POSIX roots
        # and recognizable filesystem forms are withheld.
        roots = {os.fspath(self.workspace), os.fspath(self.settings.workspace), os.fspath(self.settings.jobs_dir)}
        configured = []
        for root in roots:
            root = root.replace("\\", "/").rstrip("/")
            if root:
                configured.append(re.compile(r"(?<![\w/])" + re.escape(root) + r"(?=$|[/\s.,;:!?\)\]\}\>\'\"])",
                                             re.I if re.match(r"^[a-z]:", root, re.I) else 0))
        filesystem = re.compile(r"(?i)(?<![a-z])[a-z]:[\\/]|(?<![\w/])/(?:Users|home)/|\bfile://|"
                                r"(?<!\\)\\{2,}[^\s\\/{}]+[\\/]+[^\s\\/{}]+|"
                                r"(?<![\w:/])//[^\s/{}]+/[^\s/{}]+")
        pending = [result]
        while pending:
            _request_budget()
            value = pending.pop()
            if isinstance(value, str):
                if filesystem.search(value) or any(pattern.search(value.replace("\\", "/")) for pattern in configured):
                    return False
            elif isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return True

    def _query(self):
        query = _QUERY.get()
        return query if query is not None and query["catalog"] is self else None

    @staticmethod
    def _mismatch(state):
        state["dirty"] = True
        state["content_head_verified"] = False
        warning = "head_content_mismatch: allowlisted working-tree bytes differ from HEAD or are absent"
        if warning not in state["warnings"]:
            state["warnings"].append(warning)

    def _refresh_provenance(self, result):
        _request_budget()
        query = self._query()
        if not isinstance(result, dict) or query is None:
            return
        state = query["states"].get(result.get("repo"))
        if state is not None and state["dirty"] is not False:
            result["dirty"] = state["dirty"]
            if isinstance(result.get("warnings"), list):
                result["warnings"] = list(dict.fromkeys(result["warnings"] + state["warnings"]))
        if result.get("resolved") is True and "locked_revision" in result:
            skills = query["states"].get("kapa_skills", {})
            lock = query["states"].get("kapa_knowledge", {})
            fmt = lock.get("object_format")
            lock_oid = query["manifests"].get("kapa_knowledge", {}).get("deps.lock")
            lock_matches = lock_oid is not None and all(
                values.get(fmt) == lock_oid for (repo, path, _), values in query["reads"].items()
                if repo == "kapa_knowledge" and path == "deps.lock")
            if skills.get("dirty") is not False or not skills.get("content_head_verified") or not lock_matches:
                result["resolved"] = False
                result["warnings"].append("version_unresolved: bytes read during this request differ from HEAD")
        for value in result.values():
            if isinstance(value, dict):
                self._refresh_provenance(value)
            elif isinstance(value, list):
                for item in value:
                    self._refresh_provenance(item)

    def _root(self, repo):
        if repo not in REPOS:
            raise ValueError("Unknown repository alias")
        root = self.workspace / repo
        _no_links(root)
        if not root.is_dir():
            raise ValueError("Repository unavailable")
        return root

    def _read(self, repo, relative):
        _request_budget()
        if not _allowed(repo, relative):
            raise ValueError("Document is outside content allowlist")
        path = self._root(repo).joinpath(*_parts(relative))
        _no_links(path)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Content is not a bounded regular file")
            # Reject the next file before opening it when the aggregate cannot
            # accommodate its known size. Repeated reads count again as real IO.
            _request_budget(require_bytes=info.st_size)
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or opened.st_size != info.st_size
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                    raise ValueError("Content file changed or is hardlinked")
                data = bytearray()
                while len(data) < info.st_size:
                    _request_budget()
                    block = stream.read(min(65536, info.st_size - len(data)))
                    _request_budget(read_bytes=len(block))
                    if not block:
                        raise ValueError("Content size changed during read")
                    data.extend(block)
                if os.fstat(stream.fileno()).st_size != len(data):
                    raise ValueError("Content size changed during read")
                _request_budget()
            if b"\0" in data:
                raise ValueError("Content exceeds size or encoding limits")
            text = data.decode("utf-8-sig")
        except (OSError, UnicodeError):
            raise ValueError("Content unavailable or invalid UTF-8") from None
        digest = hashlib.sha256(data).hexdigest()
        query = self._query()
        if query is not None:
            query["reads"][(repo, relative, digest)] = {
                fmt: hashlib.new(fmt, f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
                for fmt in ("sha1", "sha256")}
            query["reads"][(repo, relative, digest)]["byte_length"] = len(data)
            state = query["states"].get(repo)
            if state is not None and not self._head_matches(repo, relative, digest, state):
                self._mismatch(state)
        result = (text.replace("\r\n", "\n"), digest)
        if query is not None and repo == "kapa_knowledge" and relative in {
                "catalogs/materials.json", "runs/pilot/held-items-research.json",
                "runs/pilot/predecessor-versions-research.json"}:
            query["supplemental_reads"][relative] = result
        return result

    def _files(self, repo):
        root = self._root(repo)
        count = 0
        # Do not even enumerate excluded .git, tools, credential or build trees.
        top = ({"00-kapa-start", "laws", "appraisal-practice-standards", "rules", "docs", "sources"}
               if repo == "kapa_skills" else
               {"00-knowledge-start", "issues", "cases", "interpretations", "reviews", "relations", "rules", "docs", "sources", "runs", "vendor", "catalogs"})
        for base, dirs, files in os.walk(root, followlinks=False):
            parent = Path(base)
            # Charge every encountered entry before pruning or descending, even
            # empty directories and non-content files that never match a query.
            count = _charge(count, len(dirs) + len(files))
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and (parent != root or d in top
                             or (repo.startswith("kapa_edu_") and d not in {"tools", "build", "latex", "sources"}
                                 and re.fullmatch(r"[a-z][a-z0-9_]*", d))))
            safe_dirs = []
            for d in dirs:
                try:
                    _parts(d)
                    _no_links(parent / d)
                    safe_dirs.append(d)
                except ValueError:
                    continue
            dirs[:] = safe_dirs
            for name in sorted(files):
                rel = (parent / name).relative_to(root).as_posix()
                try:
                    if _allowed(repo, rel):
                        yield rel
                except ValueError:
                    continue

    @_bounded
    def _git_state(self, repo):
        _request_budget()
        query = self._query()
        if query is not None and repo in query["states"]:
            return query["states"][repo]
        root = self._root(repo)
        state = {"repo": repo, "revision": None, "dirty": None,
                 "content_head_verified": False, "object_format": None, "warnings": []}
        try:
            _no_links(root / ".git")
            if not (root / ".git").is_dir():
                raise ValueError("External Git metadata is unsupported")
            # No inherited credentials, Git overrides, hooks, lazy network fetch or global trust edits.
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
            env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
            command = ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                       "-c", "protocol.allow=never", "-c", f"safe.directory={root.as_posix()}",
                       "--no-replace-objects", "-C", str(root)]
            def run(args):
                _request_budget()
                output = _capture_git(command + args, env)
                _request_budget()
                return output
            fmt = run(["rev-parse", "--show-object-format"]).decode("ascii").strip()
            if fmt not in {"sha1", "sha256"}:
                state["warnings"].append("dependency_warning: unsupported Git object format; SHA-1 or SHA-256 required")
                raise ValueError("Unsupported Git object format")
            state["object_format"] = fmt
            width = hashlib.new(fmt).digest_size * 2
            revision = run(["rev-parse", "--verify", "HEAD"]).decode("ascii").strip()
            if not re.fullmatch(r"[a-f0-9]{" + str(width) + "}", revision):
                raise ValueError("Git revision unavailable")
            state["revision"] = revision
            manifest = {}
            count = 0
            for entry in run(["ls-tree", "-r", "-z", "--full-tree", revision]).split(b"\0"):
                if not entry:
                    continue
                count = _charge(count)
                header, sep, path_bytes = entry.partition(b"\t")
                mode, kind, oid = header.decode("ascii").split(" ")
                if not sep or not re.fullmatch(r"[a-f0-9]{" + str(width) + "}", oid):
                    raise ValueError("Invalid Git manifest")
                path = path_bytes.decode("utf-8")
                try:
                    allowed = _allowed(repo, path)
                except ValueError:
                    allowed = False
                if allowed:
                    if mode not in {"100644", "100755"} or kind != "blob" or path in manifest:
                        raise ValueError("Unsafe Git content entry")
                    manifest[path] = oid
            if query is not None:
                query["manifests"][repo] = manifest
            # Status is informative; only exact blob values prove content identity.
            state["dirty"] = bool(run(["status", "--porcelain=v1", "--untracked-files=normal"]))
            paths = set()
            matches = True
            for path in self._files(repo):
                paths.add(path)
                _, digest = self._read(repo, path)
                oid = query["reads"][(repo, path, digest)][fmt]
                matches = matches and manifest.get(path) == oid
            matches = matches and paths == set(manifest)
            # Include bytes consumed before this snapshot (e.g. a lock or the
            # requested document); a later reread must not erase that evidence.
            matches = matches and all(values[fmt] == manifest.get(path)
                                      for (alias, path, _), values in query["reads"].items() if alias == repo)
            state["content_head_verified"] = matches
            if not matches:
                self._mismatch(state)
            if state["dirty"]:
                state["warnings"].append("dirty_worktree: content is working-tree bytes, not a fixed HEAD snapshot")
        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError):
            state["dirty"] = None
            state["content_head_verified"] = False
            state["warnings"].append("dependency_warning: Git HEAD or dirty state unavailable")
        if query is not None:
            query["states"][repo] = state
        return state

    def _head_matches(self, repo, path, digest, state):
        query = self._query()
        if query is None:
            return False
        actual = query["reads"].get((repo, path, digest), {}).get(state.get("object_format"))
        expected = query["manifests"].get(repo, {}).get(path)
        return actual is not None and expected is not None and actual == expected

    @_bounded
    def _dependency(self):
        state = self._git_state("kapa_skills")
        lock_state = self._git_state("kapa_knowledge")
        try:
            text, digest = self._read("kapa_knowledge", "deps.lock")
            pin = _json(text)["kapa_skills"]["commit"]
            lock_provenance = self._head_matches("kapa_knowledge", "deps.lock", digest, lock_state)
        except (ValueError, KeyError, TypeError):
            pin = None
            lock_provenance = False
        resolved = isinstance(pin, str) and bool(re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", pin)) and pin == state["revision"] and state["dirty"] is False and state.get("content_head_verified") is True and lock_provenance
        warnings = [] if resolved else ["version_unresolved: locked dependency lacks matching clean skills HEAD or verified lock provenance; latest content was not substituted"]
        if pin == "WORKTREE":
            warnings.append("dependency_warning: deps.lock uses WORKTREE, not an immutable revision")
        return {"locked_revision": pin, "checkout_revision": state["revision"],
                "checkout_dirty": state["dirty"], "lock_provenance_checked": lock_provenance,
                "resolved": bool(resolved), "warnings": warnings}

    @_bounded
    def list_repositories(self):
        items = []
        for repo in REPOS:
            try:
                items.append(self._git_state(repo))
            except ValueError:
                items.append({"repo": repo, "revision": None, "dirty": None,
                              "warnings": ["dependency_warning: repository unavailable"]})
        return {"repositories": items}

    def _envelope(self, repo, path, digest, state, metadata=None):
        meta = metadata or {}
        verified = self._head_matches(repo, path, digest, state)
        warnings = list(state["warnings"])
        if not verified:
            warnings.append("head_content_unverified: returned bytes are not verified against HEAD")
        return {"id": f"{repo}:{path}", "repo": repo, "path": path,
                "revision": state["revision"], "dirty": state["dirty"] if verified else True if state["revision"] else None,
                "head_verified": verified, "sha256": digest,
                "content_kind": "source" if path.endswith(("source.md", ".tex")) else "machine" if path.endswith((".json", ".jsonl", ".lock", ".csv")) else "analysis" if repo == "kapa_knowledge" else "summary",
                "source_url": meta.get("source_url"), "version": meta.get("applied_version"),
                "coverage": "stored working-tree content only", "metadata": meta,
                "progress": meta.get("progress"), "reference_status": meta.get("reference_status"),
                "warnings": warnings, "next_cursor": None}

    def _subjects(self, repo):
        text, _ = self._read(repo, "subjects.json")
        subjects = _json(text).get("subjects", [])
        if not isinstance(subjects, list):
            raise ValueError("Invalid subjects catalog")
        return subjects

    @_bounded
    def search_questions(self, query="", exam=None, subject=None, round=None, limit=20, offset=0):
        _page([], limit, offset)
        if exam is not None and str(exam) not in {"1", "2"}:
            raise ValueError("exam must be 1 or 2")
        items, warnings, states = [], [], []
        count = 0
        for number in (1, 2):
            if exam is not None and str(exam) != str(number):
                continue
            repo = f"kapa_edu_{number}"
            state = self._git_state(repo)
            states.append(state)
            warnings.extend(state["warnings"])
            for entry in self._subjects(repo):
                count = _charge(count)
                folder = entry["folder"]
                _parts(folder)
                if not re.fullmatch(r"[a-z][a-z0-9_]*", folder):
                    raise ValueError("Invalid subject folder")
                names = [folder, entry.get("name_ko", ""), *entry.get("aliases", [])]
                if subject is not None and _norm(subject).replace(" ", "") not in [_norm(s).replace(" ", "") for s in names]:
                    continue
                root = self._root(repo) / folder
                if not root.exists():
                    continue
                _no_links(root)
                for directory in sorted(root.iterdir()):
                    count = _charge(count)
                    if not re.fullmatch(r"[0-9]{1,3}", directory.name) or (round is not None and str(round) != directory.name):
                        continue
                    _no_links(directory)
                    for file in sorted(directory.iterdir()):
                        count = _charge(count)
                        if not re.fullmatch(r"[0-9]{2}\.tex", file.name) or file.stem == "00":
                            continue
                        path = file.relative_to(self._root(repo)).as_posix()
                        text, digest = self._read(repo, path)
                        score = _score(query, text + " " + " ".join(names))
                        if not score:
                            continue
                        result = self._envelope(repo, path, digest, state, self._question_meta(text))
                        result.update(id=f"edu/{number}/{folder}/{directory.name}/{file.stem}",
                                      score=score, excerpt=text[:600])
                        items.append(result)
        items.sort(key=lambda x: (-x["score"], x["id"]))
        return {**_page(items, limit, offset), "warnings": warnings, "repositories": states}

    @staticmethod
    def _question_meta(text):
        meta = {}
        for label in ("kapa", "source", "conversion"):
            match = re.search(r"^%% " + label + r": (.*)$", text, re.M)
            line = match.group(1) if match else ""
            meta[label] = {m.group(1): m.group(2).strip() for m in re.finditer(
                r"([A-Za-z_]+)=(.*?)(?=\s+[A-Za-z_]+=|$)", line)}
            meta[label + "_raw"] = line
        return meta

    @_bounded
    def read_question(self, question_id):
        match = _QUESTION.fullmatch(question_id)
        if not match or match[4] == "00":
            raise ValueError("Invalid question ID")
        exam, subject, round_, seq = match.groups()
        repo = f"kapa_edu_{exam}"
        entries = self._subjects(repo)
        entry = next((e for e in entries if e["folder"] == subject), None)
        if entry is None:
            raise ValueError("Unknown subject")
        path = f"{subject}/{round_}/{seq}.tex"
        text, digest = self._read(repo, path)
        if len(text) > MAX_CHARS:
            raise ValueError("Question exceeds response limit; use read_document pagination")
        meta = self._question_meta(text)
        kapa = meta["kapa"]
        if any(kapa.get(k) != v for k, v in (("exam", exam), ("subject", subject), ("round", round_))) or kapa.get("seq") != str(int(seq)):
            raise ValueError("Question metadata does not match its ID")
        result = self._envelope(repo, path, digest, self._git_state(repo), meta)
        result.update(id=question_id, content=text, common_content=None, answer=None)
        common = f"{subject}/{round_}/00.tex"
        if (self._root(repo) / common).exists():
            value, common_hash = self._read(repo, common)
            if len(value) > MAX_CHARS:
                raise ValueError("Common instructions exceed response limit")
            result.update(common_content=value, common_document_id=f"{repo}:{common}", common_sha256=common_hash)
        if exam == "1":
            # The CSV uses subject blocks of 40, independent of printed question numbers.
            data, answer_hash = self._read(repo, "answers/answers.csv")
            kapa = meta["kapa"]
            rows = [r for r in csv.DictReader(io.StringIO(data)) if r.get("round") == round_
                    and r.get("period") == kapa.get("period")]
            aliases = {_norm(s).replace(" ", "") for s in entry.get("aliases", []) + [entry.get("name_ko", "")]}
            block = next((i for i in range(0, len(rows), 40) if _norm(rows[i].get("subject", "")).replace(" ", "") in aliases), None)
            index = block + int(seq) - 1 if block is not None else len(rows)
            if (1 <= int(seq) <= 40 and block is not None and index < min(block + 40, len(rows))
                    and _norm(rows[index].get("subject", "")).replace(" ", "") in aliases):
                result["answer"] = {"row": rows[index], "path": "answers/answers.csv", "sha256": answer_hash,
                                    "join": "round,period,subject-block,seq"}
            else:
                result["warnings"].append("answer_unresolved: no matching official answer row")
        else:
            result["warnings"].append("No official answers or explanations for exam 2")
        return result

    @_bounded
    def search_knowledge(self, query, repo=None, limit=20, offset=0):
        _page([], limit, offset)
        aliases = {"skills": "kapa_skills", "knowledge": "kapa_knowledge"}
        repo = aliases.get(repo, repo)
        if repo not in (None, "kapa_skills", "kapa_knowledge"):
            raise ValueError("Unknown knowledge repository")
        results, warnings, states = [], [], []
        dependency = self._dependency()
        for alias in ("kapa_skills", "kapa_knowledge"):
            if repo is not None and alias != repo:
                continue
            state = self._git_state(alias)
            states.append(state)
            warnings.extend(state["warnings"])
            for path in self._files(alias):
                if not path.endswith(".md"):
                    continue
                try:
                    text, digest = self._read(alias, path)
                    meta = _metadata(text)
                except ValueError:
                    warnings.append("content_unavailable: " + alias + ":" + path)
                    continue
                score = _score(query, path + " " + text)
                if score:
                    value = self._envelope(alias, path, digest, state, meta)
                    value.update(score=score, excerpt=text[:600], material_id=meta.get("id"))
                    results.append(value)
            if alias == "kapa_knowledge" and (self._root(alias) / "catalogs/materials.json").exists():
                data, _ = self._supplemental()
                for record in data["records"]:
                    score = _score(query, json.dumps(record, ensure_ascii=False))
                    if score:
                        value = self.read_material(record["id"])
                        value.update(score=score, material_id=record["id"],
                                     excerpt=str(record.get("title", ""))[:600], kind=record["kind"])
                        results.append(value)
        results.sort(key=lambda x: (-x["score"], x["id"]))
        return {**_page(results, limit, offset), "dependency": dependency,
                "repositories": states,
                "warnings": warnings + dependency["warnings"], "status": "ok" if results else "not_indexed"}

    @_bounded
    def read_document(self, document_id, offset=0, max_chars=12000):
        if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS:
            raise ValueError("Invalid document character range")
        repo, sep, path = document_id.partition(":")
        if not sep:
            raise ValueError("Invalid document ID")
        text, digest = self._read(repo, path)
        meta = _metadata(text) if path.endswith(".md") else {}
        result = self._envelope(repo, path, digest, self._git_state(repo), meta)
        end = min(offset + max_chars, len(text))
        nxt = end if end < len(text) else None
        result.update(content=text[offset:end], offset=offset, total_chars=len(text),
                      next_offset=nxt, next_cursor=nxt, truncated=nxt is not None)
        if repo == "kapa_knowledge":
            result["dependency"] = self._dependency()
            result["warnings"].extend(result["dependency"]["warnings"])
        return result

    @_bounded
    def get_relations(self, material_id, review=None):
        if review not in {None, "unreviewed", "confirmed", "rejected", "needs-review"}:
            raise ValueError("Invalid relation review filter")
        repo = "kapa_knowledge"
        text, digest = self._read(repo, "relations/relations.jsonl")
        relations, identifiers = [], set()
        for relation in _jsonl(text):
            identifier = relation.get("id") if isinstance(relation, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r"rel-[a-f0-9]{20}", identifier):
                raise ValueError("Invalid relation ID in ledger")
            if identifier in identifiers:
                raise ValueError("Duplicate relation ID in ledger")
            identifiers.add(identifier)
            relations.append(relation)
        for relation in relations:
            _request_budget()
            if not _relation_identity_matches(relation):
                raise ValueError("Relation ID does not match immutable fields")
        selected = [r for r in relations if material_id in (r.get("src"), r.get("dst")) and (review is None or r.get("review") == review)]
        if len(selected) > 1000:
            raise ValueError("Relation response limit exceeded")
        records, documents = [], {}
        for path in self._files(repo):
            if re.fullmatch(r"reviews/[0-9]{4}/[^/]+\.jsonl", path):
                content, _ = self._read(repo, path)
                for record in _jsonl(content):
                    record["review_path"] = path
                    records.append(record)
            elif path.startswith(("cases/", "interpretations/")) and path.endswith(".md"):
                content, _ = self._read(repo, path)
                meta = _metadata(content)
                if "id" in meta:
                    identifier = meta["id"]
                    namespace = "case" if path.startswith("cases/") else "interp"
                    parts = identifier.split("/") if isinstance(identifier, str) else []
                    if (len(parts) != 3 or parts[0] != namespace or any(not part or part != part.strip() for part in parts)
                            or any(ord(char) < 32 or ord(char) == 127 for char in identifier)):
                        raise ValueError("Invalid material ID in knowledge documents")
                    # Complete the global case/interp identity check before any
                    # source or approval binding. Directory order cannot pick an owner.
                    if identifier in documents:
                        raise ValueError("Duplicate material ID in knowledge documents")
                    documents[identifier] = meta
        items = []
        for relation in selected:
            value = dict(relation)
            matches = [r for r in records if r.get("target") == relation.get("id")]
            source = documents.get(relation.get("src"), {})
            raw_source = self._verified_source(source)
            source_verified = raw_source is not None
            valid = [r for r in matches if source_verified and self._valid_review(r, relation, documents)
                     and self._source_evidence_matches(raw_source, r.get("evidence"))]
            value.update(reviews=matches, review_verified=bool(valid), source_verified=source_verified,
                         warnings=[] if relation.get("review") != "confirmed" or valid else
                         ["review_unverified: confirmed label lacks a valid review, source digest, exact source URL or payload quote"])
            items.append(value)
        state = self._git_state(repo)
        dependency = self._dependency()
        return {"repo": repo, "material_id": material_id, "items": items, "total": len(items),
                "revision": state["revision"], "dirty": state["dirty"], "sha256": digest,
                "dependency": dependency, "warnings": state["warnings"] + dependency["warnings"],
                "coverage": "stored relations and review records; not a legal conclusion"}

    def _source_verified(self, meta):
        return self._verified_source(meta) is not None

    def _verified_source(self, meta):
        try:
            identifiers = [(target, meta.get(key)) for target, key in
                           (("prec", "precSeq"), ("expc", "expcSeq"), ("cgmexpc", "cgmExpcDatSeq")) if meta.get(key) is not None]
            if len(identifiers) != 1:
                return None
            target, identifier = identifiers[0]
            if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]+", identifier):
                return None
            text, _ = self._read("kapa_knowledge", f"sources/{target}/{identifier}.json")
            raw = _json(text)
            canonical = json.dumps(raw["payload"], sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            valid = (digest == meta.get("source_sha256") == raw.get("sha256")
                    and raw.get("target") == target and raw.get("source_id") == identifier
                    and raw.get("source_url") == meta.get("source_url"))
            return raw if valid else None
        except (ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _source_evidence_matches(raw_source, evidence):
        if not isinstance(evidence, dict) or evidence.get("source") != raw_source.get("source_url"):
            return False
        quote = evidence.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            return False
        pending = [raw_source["payload"]]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and quote in value:
                return True
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return False

    @staticmethod
    def _valid_review(record, relation, documents):
        if not _relation_identity_matches(relation):
            return False
        def evidence(e):
            if not isinstance(e, dict) or not isinstance(e.get("source"), str) or not isinstance(e.get("quote"), str) or not e["quote"].strip():
                return False
            try:
                url = urlsplit(e["source"])
                return url.scheme == "https" and bool(url.hostname) and url.username is None and url.password is None
            except ValueError:
                return False
        try:
            at = datetime.fromisoformat(record["reviewed_at"].replace("Z", "+00:00"))
            valid = (at.utcoffset() is not None and str(at.year) == record["review_path"].split("/")[1]
                     and record.get("target") == relation.get("id")
                     and record.get("decision") == "accept" and record.get("relation_review") == "confirmed"
                     and relation.get("review") == "confirmed" and evidence(record.get("evidence"))
                     and evidence(relation.get("evidence")) and record["evidence"] == relation["evidence"]
                     and bool(record.get("id")) and record.get("reviewer_model") == relation.get("reviewed_by")
                     and bool(record.get("reviewer_model")) and record.get("reviewed_at") == relation.get("reviewed_at"))
            source = documents.get(relation.get("src"), {})
            digest = source.get("source_sha256")
            valid = (valid and source.get("id") == relation.get("src") and isinstance(digest, str)
                     and bool(re.fullmatch(r"[a-f0-9]{64}", digest)) and record.get("source_sha256") == digest)
            return bool(valid)
        except (KeyError, ValueError, TypeError, AttributeError):
            return False

    @_bounded
    def route_task(self, task):
        result = self.search_knowledge(task, limit=10)
        entries = ["kapa_skills:00-kapa-start/SKILL.md",
                   "kapa_skills:rules/skill-reading-rules/SKILL.md",
                   "kapa_knowledge:00-knowledge-start/SKILL.md",
                   "kapa_knowledge:rules/knowledge-reading-rules/SKILL.md"]
        return {"task": task, "entry_documents": entries, "candidates": result["items"],
                "repositories": result["repositories"],
                "dependency": result["dependency"], "warnings": result["warnings"],
                "status": result["status"], "next_steps": ["Read entry and topic SKILL documents", "Read complete source sections and applicable versions", "Inspect relations and review evidence"],
                "coverage": "keyword candidates only; source and applicable-version verification remain required"}

    @_bounded
    def pinned_catalog(self, as_of=None):
        """Verify the exact vendored dependency metadata, independently of live skills.

        Version rows establish catalog availability only, never article text or
        legal applicability. The lock and every metadata blob must match HEAD.
        """
        from datetime import date
        if as_of is not None:
            if not isinstance(as_of, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of):
                raise ValueError("as_of must be an ISO calendar date")
            date.fromisoformat(as_of)
        repo = "kapa_knowledge"
        text, lock_hash = self._read(repo, "deps.lock")
        lock = _json(text)["kapa_skills"]
        pin, vendor = lock.get("commit"), lock.get("vendor", {})
        files = vendor.get("files")
        if (not isinstance(pin, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", pin)
                or vendor.get("commit") != pin or not isinstance(files, dict)
                or not 1 <= len(files) <= 512 or "docs/law-index.json" not in files):
            raise ValueError("Pinned metadata manifest unavailable")
        parsed, sources = {}, []
        state = self._git_state(repo)
        for name, expected in sorted(files.items()):
            if (not isinstance(name, str) or not _allowed(repo, "vendor/kapa_skills/" + name)
                    or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                raise ValueError("Invalid pinned metadata manifest")
            path = "vendor/kapa_skills/" + name
            content, digest = self._read(repo, path)
            if digest != expected:
                raise ValueError("Pinned metadata hash mismatch")
            parsed[name] = _json(content)
            sources.append({"document_id": f"{repo}:{path}", "sha256": digest,
                            "revision": state["revision"],
                            "byte_length": self._query()["reads"][(repo, path, digest)]["byte_length"],
                            "head_verified": self._head_matches(repo, path, digest, state)})
        index = parsed["docs/law-index.json"]
        laws = index.get("laws")
        if index.get("schema") != 1 or not isinstance(laws, dict):
            raise ValueError("Invalid pinned law catalog")
        expected_files = {"docs/law-index.json"} | {law["versions_file"] for law in laws.values() if law.get("versions_file")}
        if set(files) != expected_files:
            raise ValueError("Pinned metadata manifest does not match catalog")
        candidates = []
        for law_id, law in sorted(laws.items()):
            path = law.get("versions_file")
            if not path:
                continue
            versions = parsed[path]
            if not isinstance(versions, dict) or versions.get("lsId") != law_id or not isinstance(versions.get("versions"), list):
                raise ValueError("Pinned version identity mismatch")
            eligible = []
            for row in versions["versions"]:
                _request_budget(entries=1)
                effective, sequence = row.get("efYd"), row.get("lsiSeq")
                if not isinstance(effective, str) or not re.fullmatch(r"[0-9]{8}", effective) or not isinstance(sequence, str) or not sequence.isascii() or not sequence.isdigit():
                    raise ValueError("Invalid pinned version row")
                date.fromisoformat(effective[:4] + "-" + effective[4:6] + "-" + effective[6:])
                if as_of is not None and effective <= as_of.replace("-", ""):
                    eligible.append((effective, sequence))
            if eligible:
                latest = max(value[0] for value in eligible)
                candidates.append({"law_id": law_id, "effective_date": latest,
                                   "version_ids": sorted({f"law/{law_id}@{seq}" for ef, seq in eligible if ef == latest})})
        verified = (state["dirty"] is False and state["content_head_verified"]
                    and self._head_matches(repo, "deps.lock", lock_hash, state)
                    and all(source["head_verified"] for source in sources))
        return {"locked_revision": pin, "lock_sha256": lock_hash, "resolved": bool(verified),
                "mode": "hash_verified_vendor_metadata", "sources": sources,
                "as_of": as_of, "date_candidates": candidates, "legal_verified": False,
                "coverage": "pinned catalog/version metadata only; article text and legal applicability unverified"}

    def _supplemental(self):
        query = self._query()
        if query is not None and query["supplemental"] is not None:
            return query["supplemental"]
        text, digest = self._supplemental_read("catalogs/materials.json")
        data = _json(text)
        if data.get("schema") != 1 or data.get("catalog_type") != "unpublished-evidence":
            raise ValueError("Invalid supplemental evidence catalog")
        records = data.get("records")
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError("Invalid supplemental evidence records")
        seen = set()
        for record in records:
            identifier, kind = record.get("id"), record.get("kind")
            pattern = r"faq/molit/lawgo/[0-9]+" if kind == "faq" else r"identity-hold/molit/[^/\s]+" if kind == "identity-hold" else r"(?!)"
            if (not isinstance(identifier, str) or not re.fullmatch(pattern, identifier)
                    or identifier in seen or record.get("publication_hold") is not True
                    or (kind == "faq" and record.get("reference_status") != "unreviewed")
                    or (kind == "faq" and (record.get("material_type") != "administrative-faq"
                                          or record.get("case_no") != ""))
                    or (kind == "identity-hold" and record.get("identity_status") != "unresolved")):
                raise ValueError("Invalid supplemental identity or publication state")
            seen.add(identifier)
            if record.get("evidence_file") not in {"runs/pilot/held-items-research.json", "runs/pilot/predecessor-versions-research.json"}:
                raise ValueError("Supplemental evidence path is outside fixed research allowlist")
        result = data, digest
        if query is not None:
            query["supplemental"] = result
            query["supplemental_by_id"] = {record["id"]: record for record in records}
        return result

    def _supplemental_read(self, path):
        """Reuse exact bytes already charged/read in this request, never across requests."""
        _request_budget()
        query = self._query()
        if query is not None and path in query["supplemental_reads"]:
            return query["supplemental_reads"][path]
        return self._read("kapa_knowledge", path)

    @_bounded
    def read_material(self, material_id):
        """Read an unpublished FAQ/identity record or an exact historical version ID."""
        if not isinstance(material_id, str) or len(material_id) > 1024:
            raise ValueError("Invalid material ID")
        match = re.fullmatch(r"law/([0-9]{6})@([0-9]+)", material_id)
        if match:
            pinned = self.pinned_catalog()
            law_id, sequence = match.groups()
            text, index_hash = self._read("kapa_knowledge", "vendor/kapa_skills/docs/law-index.json")
            hashes = {source["document_id"]: source["sha256"] for source in pinned["sources"]}
            if index_hash != hashes["kapa_knowledge:vendor/kapa_skills/docs/law-index.json"]:
                raise ValueError("Pinned catalog changed during material read")
            law = _json(text).get("laws", {}).get(law_id, {})
            path = law.get("versions_file")
            if not path:
                raise ValueError("Historical version is absent from pinned catalog")
            text, digest = self._read("kapa_knowledge", "vendor/kapa_skills/" + path)
            if digest != hashes["kapa_knowledge:vendor/kapa_skills/" + path]:
                raise ValueError("Pinned version changed during material read")
            rows = [r for r in _json(text)["versions"] if r.get("lsiSeq") == sequence]
            if not rows:
                raise ValueError("Historical version is absent from pinned catalog")
            return {"id": material_id, "kind": "historical-version-metadata",
                    "entries": _metadata_projection(rows, _SELECTED_FIELDS["entries"]),
                    "title": material_id, "source_document_id": "kapa_knowledge:vendor/kapa_skills/" + path,
                    "metadata_sha256": digest, "locked_revision": pinned["locked_revision"],
                    "catalog_verified": pinned["resolved"], "article_text_available": False,
                    "applicability_verified": False, "review_verified": False,
                    "missing_items": ["historical_article_text_unavailable", "legal_applicability_unverified"],
                    "coverage": "exact pinned version membership only; no article or applicability evidence"}
        data, digest = self._supplemental()
        query = self._query()
        record = query["supplemental_by_id"].get(material_id) if query is not None else next(
            (r for r in data["records"] if r["id"] == material_id), None)
        if record is None:
            raise ValueError("Unknown supplemental material ID")
        path = record["evidence_file"]
        _, evidence_hash = self._supplemental_read(path)
        if evidence_hash != record.get("evidence_sha256"):
            raise ValueError("Supplemental evidence hash mismatch")
        state = self._git_state("kapa_knowledge")
        result = self._envelope("kapa_knowledge", "catalogs/materials.json", digest, state)
        result.update(id=material_id, catalog_document_id="kapa_knowledge:catalogs/materials.json",
                      title=record.get("title", material_id), source_url=record.get("source_url"),
                      record=_metadata_projection(record, _RECORD_FIELDS),
                      evidence_document_id="kapa_knowledge:" + path,
                      evidence_sha256=evidence_hash, review_verified=False, source_verified=False,
                      formal_interpretation=False, publication_hold=True,
                      missing_items=["full_source_envelope_unavailable", "formal_identity_and_legal_review_unverified"],
                      coverage="unpublished pilot evidence only; full source and legal verification unavailable")
        return result

    @_bounded
    def capture_evidence(self, document_ids=None, question_ids=None, as_of=None):
        """Capture bounded provenance, never a claim of complete legal research.

        Nested reads share one Catalog budget and HEAD audit. Only metadata is
        returned; the corpus stays in its owning repository.
        """
        from datetime import date
        if as_of is not None:
            if not isinstance(as_of, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of):
                raise ValueError("as_of must be an ISO calendar date")
            date.fromisoformat(as_of)
        selections = []
        for values in (document_ids, question_ids):
            if values is None:
                values = []
            if (not isinstance(values, (list, tuple)) or len(values) > 100
                    or any(not isinstance(value, str) or len(value) > 1024 for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("Evidence selections must contain at most 100 unique IDs")
            selections.append(list(values))
        documents, questions = selections
        selected, anchors, missing = [], set(), []
        for kind, ids, reader in (("document", documents, self.read_document),
                                  ("question", questions, self.read_question)):
            for identifier in ids:
                supplemental = kind == "document" and identifier.startswith(("faq/", "identity-hold/", "law/"))
                item = self.read_material(identifier) if supplemental else reader(identifier)
                if identifier.startswith("law/"):
                    selected.append(_metadata_projection(item, _SELECTED_FIELDS))
                    missing.append({"id": identifier, "reason": "historical_article_text_and_applicability_unavailable"})
                    continue
                repo, path = item["repo"], item["path"]
                anchors.add((repo, path))
                if supplemental:
                    anchors.add((repo, item["evidence_document_id"].split(":", 1)[1]))
                    missing.append({"id": identifier, "reason": "unpublished_source_identity_or_full_source_unavailable"})
                if kind == "question":
                    anchors.add((repo, "subjects.json"))
                    if item.get("common_document_id"):
                        anchors.add((repo, item["common_document_id"].split(":", 1)[1]))
                    if repo == "kapa_edu_1":
                        anchors.add((repo, "answers/answers.csv"))
                    if item.get("answer") is None:
                        missing.append({"id": identifier, "reason": "official_answer_unavailable"})
                selected.append(_metadata_projection(item, _SELECTED_FIELDS))
                missing.append({"id": identifier, "reason": "applicable_date_and_version_not_verified"})
                missing.append({"id": identifier, "reason": "legal_review_not_verified"})
        dependency = self._dependency()
        try:
            pinned = self.pinned_catalog(as_of)
        except (ValueError, KeyError, TypeError, AttributeError):
            pinned = {"resolved": False, "mode": "unavailable", "legal_verified": False}
        # Bind the dependency lock bytes as well as its parsed revision.
        try:
            self._read("kapa_knowledge", "deps.lock")
            anchors.add(("kapa_knowledge", "deps.lock"))
        except ValueError:
            missing.append({"id": "kapa_knowledge:deps.lock", "reason": "dependency_lock_unavailable"})
        query = self._query()
        sources = []
        for repo, path in sorted(anchors):
            state = self._git_state(repo)
            for (alias, relative, digest), blobs in sorted(query["reads"].items()):
                if (alias, relative) == (repo, path):
                    sources.append({"document_id": f"{repo}:{path}", "revision": state["revision"],
                                    "sha256": digest, "git_blob": blobs.get(state.get("object_format")),
                                    "byte_length": blobs["byte_length"],
                                    "head_verified": self._head_matches(repo, path, digest, state)})
        self._refresh_provenance(dependency)
        self._refresh_provenance({"selected": selected})
        states = [dict(query["states"][repo]) for repo in sorted(query["states"])]
        reproducible = (bool(selected) and (dependency["resolved"] or pinned["resolved"])
                        and all(s["head_verified"] for s in sources)
                        and all(s["dirty"] is False and s["content_head_verified"] for s in states))
        if not selected:
            missing.append({"id": None, "reason": "no_sources_selected"})
        if not reproducible:
            missing.append({"id": None, "reason": "immutable_source_or_dependency_provenance_unresolved"})
        return {"document_ids": documents, "question_ids": questions, "as_of": as_of,
                "selected": selected, "sources": sources, "repositories": states,
                "dependency": dependency, "pinned_catalog": pinned, "reproducible": bool(reproducible),
                "coverage": {"scope": "selected stored sources only", "legal_verified": False,
                             "complete_legal_research": False},
                "checks": [{"check": "immutable_source_provenance", "passed": bool(reproducible)},
                           {"check": "applicable_date_and_version", "passed": False},
                           {"check": "legal_review", "passed": False}], "missing_items": missing}
