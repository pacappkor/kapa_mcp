# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic builds only: these tests never execute TeX or use upstream data."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

# Also runnable directly with python -I -B tests/test_render.py, before install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kapa_mcp.config import Settings
from kapa_mcp import render


FAKE_BUILD = '''import json, os, pathlib, sys, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
tex = pathlib.Path('paper.tex')
cmd = ['xelatex', '-interaction=nonstopmode', '-halt-on-error', '-file-line-error', tex.name]
build = ROOT / 'build'
build.mkdir()
(ROOT / 'observed.json').write_text(json.dumps({'argv':sys.argv[1:], 'compiler':cmd, 'env':dict(os.environ), 'isolated':sys.flags.isolated, 'bytecode':sys.dont_write_bytecode}))
MODE
'''


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.repo = self.workspace / "kapa_edu_1"
        self.rd = self.repo / "civil_law" / "37"
        self.rd.mkdir(parents=True)
        (self.repo / "tools").mkdir()
        (self.repo / "latex").mkdir()
        (self.repo / "subjects.json").write_text(json.dumps({"subjects": [{"folder": "civil_law", "name_ko": "test"}]}), encoding="utf-8")
        (self.repo / "latex" / "kapa-exam.sty").write_text("% fake style", encoding="utf-8")
        for name in ("00.tex", "01.tex", "02.tex"):
            (self.rd / name).write_text("% fake " + name, encoding="utf-8")
        (self.rd / "figures").mkdir()
        (self.rd / "figures" / "01-1.png").write_bytes(b"fake PNG")
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("do not copy", encoding="utf-8")
        (self.repo / "tools" / "unrelated.py").write_text("raise AssertionError", encoding="utf-8")
        (self.rd / "unrelated.txt").write_text("do not copy", encoding="utf-8")
        (self.rd / "unrelated.pdf").write_bytes(b"do not copy")
        (self.repo / "civil_law" / "36").mkdir()
        self.settings = Settings(self.workspace, self.root / "jobs")
        self.manager = render.RenderManager(self.settings)
        self.patches = []
        self._fake_build()
        # Installed Python stands in for discovery only. No TeX process runs.
        self._patch("kapa_mcp.render.shutil.which", return_value=str(Path(sys._base_executable).resolve()))

    def tearDown(self):
        for job in list(self.manager._jobs.values()):
            self._wait(job)

    def _patch(self, target, **kw):
        item = patch(target, **kw)
        value = item.start()
        self.addCleanup(item.stop)
        return value

    def _fake_build(self, mode=None):
        if mode is None:
            mode = "(build / 'paper.pdf').write_bytes(b'%PDF-fake')\n(build / 'paper.log').write_text('Missing character: There is no X in font fake')\nprint('build ok')\nprint('diagnostic', file=sys.stderr)"
        data = FAKE_BUILD.replace("MODE", mode).encode("utf-8")
        (self.repo / "tools" / "build.py").write_bytes(data)
        self._patch("kapa_mcp.render._BUILD_HASHES", new={hashlib.sha256(data).hexdigest()})

    def _start(self, **kw):
        return self.manager.start_render(1, "civil_law", "37", **kw)

    def _wait(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.manager.get_job(job["job_id"])
            if result["status"] not in ("preparing", "queued", "running"):
                return result
            time.sleep(0.02)
        self.fail("Fake build did not terminate")

    def test_round_isolated_argv_environment_manifest_and_metadata(self):
        original = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        with patch.dict(os.environ, {"FAKE_CREDENTIAL_TOKEN": "do-not-inherit", "HTTPS_PROXY": "do-not-inherit", "TEXINPUTS": "untrusted", "PYTHONPATH": "untrusted", "COMSPEC": "untrusted"}):
            job = self._wait(self._start())
        self.assertEqual(job["status"], "succeeded", job)
        job_dir = self.settings.jobs_dir / job["job_id"]
        snapshot = job_dir / "snapshot"
        observed = json.loads((snapshot / "observed.json").read_text())
        self.assertEqual(observed["argv"], ["--noanswers", "civil_law/37"])
        self.assertIn("-no-shell-escape", observed["compiler"])
        self.assertEqual(job["argv"][1:4], ["-I", "-B", "tools/build.py"])
        self.assertEqual(observed["isolated"], 1)
        self.assertTrue(observed["bytecode"])
        env = {key.upper(): value for key, value in observed["env"].items()}
        for name in ("FAKE_CREDENTIAL_TOKEN", "HTTPS_PROXY", "TEXINPUTS", "PYTHONPATH"):
            self.assertNotIn(name, env)
        self.assertEqual(env["OPENIN_ANY"], "r")
        self.assertEqual(env["OPENOUT_ANY"], "p")
        self.assertEqual(env["SHELL_ESCAPE"], "f")
        if os.name == "nt":
            self.assertEqual(Path(env["COMSPEC"]), Path(env["SYSTEMROOT"]) / "System32" / "cmd.exe")
        else:
            self.assertNotIn("COMSPEC", env)
        self.assertTrue(Path(env["HOME"]).is_relative_to(job_dir))
        self.assertFalse((snapshot / ".git").exists())
        self.assertFalse((snapshot / "tools" / "unrelated.py").exists())
        self.assertFalse((snapshot / "civil_law" / "36").exists())
        self.assertFalse((snapshot / "civil_law" / "37" / "unrelated.txt").exists())
        self.assertFalse((snapshot / "civil_law" / "37" / "unrelated.pdf").exists())
        self.assertTrue((snapshot / "civil_law" / "37" / "figures" / "01-1.png").is_file())
        self.assertEqual(original, {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()})
        self.assertEqual(json.loads((job_dir / "manifest.json").read_text(encoding="utf-8")), job)
        self.assertEqual(job["missing_glyphs"], ["X"])
        artifact = next(a for a in job["artifacts"] if a["media_type"] == "application/pdf")
        response = self.manager.get_artifact(job["job_id"], artifact["artifact_id"])
        self.assertTrue(Path(response["download_path"]).is_file())
        self.assertNotIn("content", response)
        self.assertNotIn("base64", response)
        job["warnings"].clear()
        self.assertTrue(self.manager.get_job(job["job_id"])["warnings"])

    def test_single_selection_and_visible_answers(self):
        job = self._wait(self._start(question_id="edu/1/civil_law/37/01", noanswers=False))
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["argv"][4:], ["--single", "civil_law/37/01.tex"])
        snapshot = self.settings.jobs_dir / job["job_id"] / "snapshot"
        self.assertTrue((snapshot / "civil_law/37/00.tex").is_file())
        self.assertFalse((snapshot / "civil_law/37/02.tex").exists())
        self.assertTrue(any("common instructions" in warning for warning in job["warnings"]))

    def test_single_copies_only_selected_sequence_figures(self):
        (self.rd / "figures" / "01-2.pdf").write_bytes(b"selected figure")
        (self.rd / "figures" / "02-1.png").write_bytes(b"x" * 20000)
        self.manager.MAX_FILE_BYTES = 8192
        self.manager.MAX_COPY_BYTES = 12000
        job = self._wait(self._start(question_id="edu/1/civil_law/37/01"))
        self.assertEqual(job["status"], "succeeded", job)
        copied = {entry["path"] for entry in job["inputs"]}
        self.assertIn("civil_law/37/figures/01-1.png", copied)
        self.assertIn("civil_law/37/figures/01-2.pdf", copied)
        self.assertNotIn("civil_law/37/figures/02-1.png", copied)
        snapshot = self.settings.jobs_dir / job["job_id"] / "snapshot"
        self.assertFalse((snapshot / "civil_law/37/figures/02-1.png").exists())
        # Round renders still select all figures and enforce the input limits.
        with self.assertRaisesRegex(render.RenderError, "budget"):
            self._start()

    def test_invalid_selections(self):
        cases = [(0, "civil_law", "37", None), (True, "civil_law", "37", None),
                 (1, "../civil_law", "37", None), (1, "civil_law", "../37", None),
                 (1, "civil_law", "37;echo", None), (1, "civil_law", "37", "../01.tex"),
                 (1, "civil_law", "37", "edu/2/civil_law/37/01"),
                 (1, "civil_law", "37", "edu/1/civil_law/37/00"),
                 (1, "civil_law", "37", "edu/1/civil_law/37/99"),
                 (1, "unknown", "37", None), (1, "civil_law", "38", None)]
        for exam, subject, rd, qid in cases:
            with self.subTest(selection=(exam, subject, rd, qid)):
                with self.assertRaises(render.RenderError):
                    self.manager.start_render(exam, subject, rd, qid)
        with self.assertRaises(render.RenderError):
            self._start(noanswers="false")

    def test_unreviewed_build_rejected_without_execution(self):
        (self.repo / "tools/build.py").write_text("raise AssertionError('never run')")
        with self.assertRaisesRegex(render.RenderError, "Unreviewed"):
            self._start()

    def test_failed_preparations_remain_owned_and_count_toward_limit(self):
        self.manager.MAX_JOBS = 2
        # Copy subjects.json and build.py, then fail on the style budget.
        style = self.repo / "latex" / "kapa-exam.sty"
        style.write_bytes(b"x" * 10000)
        self.manager.MAX_FILE_BYTES = 9000
        for _ in range(2):
            with self.assertRaisesRegex(render.RenderError, "budget"):
                self._start()
        dirs = {p.name for p in self.settings.jobs_dir.iterdir()}
        self.assertEqual(dirs, set(self.manager._jobs))
        self.assertEqual(len(dirs), 2)
        for jid in dirs:
            job = self.manager.get_job(jid)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["stage"], "preparing")
            root = self.settings.jobs_dir / jid
            self.assertEqual(json.loads((root / "manifest.json").read_text(encoding="utf-8")), job)
            self.assertTrue((root / "snapshot" / "tools" / "build.py").is_file())
            self.assertFalse((root / "snapshot" / "observed.json").exists())
        with self.assertRaisesRegex(render.RenderError, "registry"):
            self._start()
        self.assertEqual(dirs, {p.name for p in self.settings.jobs_dir.iterdir()})
        self.assertTrue(self.manager._slots.acquire(blocking=False))
        self.manager._slots.release()

    def test_failed_build_has_bounded_logs(self):
        self._fake_build("print('x' * 50000)\nprint('failure', file=sys.stderr)\nsys.exit(1)")
        self.manager.MAX_LOG_BYTES = 1024
        job = self._wait(self._start())
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["logs_truncated"])
        self.assertEqual(job["returncode"], 1)
        self.assertFalse(any(a["media_type"] == "application/pdf" for a in job["artifacts"]))
        self.assertTrue(all(a["size_bytes"] <= 1024 for a in job["artifacts"]))

    def test_missing_executables(self):
        with patch("kapa_mcp.render.shutil.which", return_value=None):
            job = self._wait(self._start())
        self.assertEqual(job["status"], "missing_dependency")

    def test_failed_compiler_log_preserved_bounded_outside_snapshot(self):
        self._fake_build("(build / 'paper.log').write_bytes(b'COMPILER HEADER\\n' + b'x' * 9000 + b'\\nFINAL ERROR')\nsys.exit(1)")
        self.manager.MAX_LOG_BYTES = 1024
        job = self._wait(self._start())
        self.assertEqual(job["status"], "failed")
        log = next(a for a in job["artifacts"] if a["path"] == "diagnostics/paper.log")
        self.assertTrue(log["truncated"])
        self.assertGreater(log["original_size_bytes"], 9000)
        self.assertEqual(log["size_bytes"], 1024)
        response = self.manager.get_artifact(job["job_id"], log["artifact_id"])
        content = Path(response["download_path"]).read_bytes()
        self.assertTrue(content.startswith(b"COMPILER HEADER"))
        self.assertTrue(content.endswith(b"FINAL ERROR"))
        self.assertIn(b"[compiler log truncated]", content)
        source = self.settings.jobs_dir / job["job_id"] / "snapshot" / "build" / "paper.log"
        source.unlink()
        self.assertEqual(self.manager.get_artifact(job["job_id"], log["artifact_id"]), response)

    def test_full_log_glyph_scan_beyond_retained_prefix_and_across_chunks(self):
        # Split the UTF-8 bytes of a glyph at a 64 KiB scanner boundary. Put
        # another diagnostic deep in the omitted middle of the retained log.
        # Keep fake build itself small; the runtime creates the long log.
        mode = ("prefix = b'Missing character: There is no '\n"
                "data = b'x' * (65536 - len(prefix) - 1) + prefix + '\uac00'.encode('utf-8') + b' in font fake\\n'\n"
                "data += b'x' * 200000 + b'\\nMissing character: There is no Z in font fake\\n' + b'x' * 200000\n"
                "(build / 'paper.log').write_bytes(data)\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        self._fake_build(mode)
        self.manager.MAX_LOG_BYTES = 1024
        job = self._wait(self._start())
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["missing_glyphs"], ["Z", "가"])
        log = next(a for a in job["artifacts"] if a["path"] == "diagnostics/paper.log")
        self.assertEqual(log["size_bytes"], 1024)
        self.assertTrue(log["truncated"])

    def test_glyph_scan_match_set_stays_bounded(self):
        self._fake_build("(build / 'paper.log').write_text(''.join('Missing character: There is no ' + chr(0x4e00+i) + ' in font fake\\n' for i in range(500)), encoding='utf-8')\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        job = self._wait(self._start())
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(len(job["missing_glyphs"]), 100)

    def test_output_log_larger_than_input_file_budget_is_scanned(self):
        # Exercise the real default 32 MiB input threshold, using bounded writes
        # and a late diagnostic that is absent from the retained log prefix.
        target_bytes = self.manager.MAX_FILE_BYTES + 65536
        mode = ("with (build / 'paper.log').open('wb') as log:\n"
                f" for _ in range({target_bytes // 65536}):\n  log.write(b'x' * 65536)\n"
                " log.write(b'\\nMissing character: There is no Z in font fake\\n')\n"
                "(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        self._fake_build(mode)
        job = self._wait(self._start())
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["missing_glyphs"], ["Z"])
        log = next(a for a in job["artifacts"] if a["path"] == "diagnostics/paper.log")
        self.assertGreater(log["original_size_bytes"], self.manager.MAX_FILE_BYTES)
        self.assertLess(log["original_size_bytes"], self.manager.MAX_OUTPUT_BYTES)
        self.assertEqual(log["size_bytes"], self.manager.MAX_LOG_BYTES)
        self.assertTrue(log["truncated"])
        response = self.manager.get_artifact(job["job_id"], log["artifact_id"])
        self.assertTrue(Path(response["download_path"]).is_file())

    def test_missing_tex_packages_exit_code(self):
        self._fake_build("print('dependency missing', file=sys.stderr)\nsys.exit(2)")
        job = self._wait(self._start())
        self.assertEqual(job["status"], "missing_dependency")
        self.assertEqual(job["returncode"], 2)

    def test_zero_without_pdf_is_failure(self):
        self._fake_build("print('no output')")
        self.assertEqual(self._wait(self._start())["status"], "failed")

    def test_timeout_and_slot_recovery(self):
        self._fake_build("print('starting', flush=True)\ntime.sleep(30)")
        self.manager.TIMEOUT_SECONDS = 0.15
        job = self._wait(self._start())
        self.assertEqual(job["status"], "timed_out", job)
        self.assertTrue(job["artifacts"])
        self.assertTrue(self.manager._slots.acquire(blocking=False))
        self.manager._slots.release()

    def test_timeout_terminates_compiler_descendant(self):
        child = "import time; time.sleep(30)"
        mode = ("import subprocess\nchild = subprocess.Popen([sys.executable, '-I', '-B', '-c', "
                + repr(child) + "])\n(ROOT / 'child.pid').write_text(str(child.pid))\ntime.sleep(30)")
        self._fake_build(mode)
        self.manager.TIMEOUT_SECONDS = 0.3
        job = self._wait(self._start())
        self.assertEqual(job["status"], "timed_out", job)
        pid = int((self.settings.jobs_dir / job["job_id"] / "snapshot" / "child.pid").read_text())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0, "Compiler descendant still alive")
                finally:
                    kernel.CloseHandle(handle)
        else:
            # Process groups are killed together on POSIX; a reaper may briefly
            # retain a zombie, so a /proc status is preferable to kill(pid, 0).
            status = Path(f"/proc/{pid}/status")
            if status.exists():
                self.assertIn("Z (zombie)", status.read_text())

    def test_exited_build_leader_still_cleans_descendants_and_leaves_other_process(self):
        sentinel = subprocess.Popen([str(Path(sys._base_executable).resolve()), "-I", "-B", "-c", "import time; time.sleep(30)"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for code, inherit_pipes in ((0, True), (1, True), (0, False)):
                with self.subTest(exit_code=code, inherit_pipes=inherit_pipes):
                    child = "import os,pathlib,time; pathlib.Path('child.ready').write_text(str(os.getpid())); time.sleep(30)"
                    redirect = "" if inherit_pipes else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
                    mode = ("import subprocess\nchild = subprocess.Popen([sys.executable, '-I', '-B', '-c', "
                            + repr(child) + "]" + redirect + ")\n"
                            "deadline = time.monotonic() + 5\n"
                            "while not (ROOT / 'child.ready').exists() and time.monotonic() < deadline:\n time.sleep(.01)\n"
                            "(build / 'paper.pdf').write_bytes(b'%PDF-fake')\nsys.exit(" + str(code) + ")")
                    self._fake_build(mode)
                    job = self._wait(self._start())
                    self.assertEqual(job["status"], "succeeded" if code == 0 else "failed", job)
                    self.assertEqual(job["returncode"], code)
                    pid = int((self.settings.jobs_dir / job["job_id"] / "snapshot" / "child.ready").read_text())
                    self._assert_process_exited(pid)
                    self.assertIsNone(sentinel.poll(), "Unrelated owned test process was terminated")
        finally:
            sentinel.terminate()  # Only this test's own Popen handle.
            sentinel.wait(timeout=5)

    def test_real_stdio_eof_waits_for_active_render_and_descendant_cleanup(self):
        # A real SDK stdio server exits on EOF while its injected renderer is
        # active. No upstream code, actual compiler, or remote service is used.
        child = "import os,pathlib,time; pathlib.Path('child.ready').write_text(str(os.getpid())); time.sleep(4)"
        self._fake_build("import subprocess\n(ROOT / 'build.ready').write_text(str(os.getpid()))\n"
                         "subprocess.Popen([sys.executable, '-I', '-B', '-c', " + repr(child) + "])\ntime.sleep(4)")
        digest = hashlib.sha256((self.repo / "tools/build.py").read_bytes()).hexdigest()
        note = self.root / "stdio-job-id.txt"
        script = "\n".join([
            "import pathlib,sys",
            "sys.path.insert(0, " + repr(str(Path(render.__file__).resolve().parents[1])) + ")",
            "from kapa_mcp import render",
            "from kapa_mcp.config import Settings",
            "from kapa_mcp.server import create_server",
            "render._BUILD_HASHES = {" + repr(digest) + "}",
            "render.shutil.which = lambda _: " + repr(str(Path(sys._base_executable).resolve())),
            "settings = Settings(pathlib.Path(" + repr(str(self.workspace)) + "), pathlib.Path(" + repr(str(self.settings.jobs_dir)) + "))",
            "manager = render.RenderManager(settings)",
            "manager.TIMEOUT_SECONDS = 2",
            "job = manager.start_render(1, 'civil_law', '37')",
            "pathlib.Path(" + repr(str(note)) + ").write_text(job['job_id'])",
            "create_server(settings, catalog=object(), renderer=manager).run(transport='stdio')",
        ])
        env = {}
        if os.name == "nt":
            env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
        sentinel = subprocess.Popen([str(Path(sys._base_executable).resolve()), "-I", "-B", "-c", "import time; time.sleep(30)"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server = None
        try:
            server = subprocess.Popen([sys.executable, "-I", "-B", "-c", script], env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 10
            root = None
            while time.monotonic() < deadline:
                if note.exists():
                    root = self.settings.jobs_dir / note.read_text()
                    if (root / "snapshot/child.ready").exists():
                        break
                if server.poll() is not None:
                    self.fail("Real stdio harness exited before starting the synthetic render: " + server.stderr.read(4096).decode(errors="replace"))
                time.sleep(.02)
            else:
                self.fail("Synthetic stdio render did not start")
            manifest = root / "manifest.json"
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "running")
            build_pid = int((root / "snapshot/build.ready").read_text())
            child_pid = int((root / "snapshot/child.ready").read_text())
            server.stdin.close()  # Graceful actual transport EOF, not terminate().
            server.wait(timeout=12)
            self.assertEqual(server.returncode, 0, server.stderr.read(4096).decode(errors="replace"))
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "timed_out")
            self._assert_process_exited(build_pid)
            self._assert_process_exited(child_pid)
            self.assertIsNone(sentinel.poll(), "Unrelated test process was terminated")
        finally:
            if server is not None:
                if server.stdin is not None and not server.stdin.closed:
                    server.stdin.close()
                try:
                    server.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    server.terminate()  # Only the harness's own Popen handle.
                    server.wait(timeout=5)
                for stream in (server.stdout, server.stderr):
                    stream.close()
            sentinel.terminate()
            sentinel.wait(timeout=5)

    def _assert_process_exited(self, pid):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0, "Owned compiler descendant still alive")
                finally:
                    kernel.CloseHandle(handle)
        else:
            status = Path(f"/proc/{pid}/status")
            if status.exists():
                self.assertIn("Z (zombie)", status.read_text())

    def test_posix_cleanup_kills_owned_group_after_leader_exit(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with (patch("kapa_mcp.render.os.name", "posix"),
              patch("kapa_mcp.render.signal.SIGKILL", 9, create=True),
              patch("kapa_mcp.render.os.killpg", create=True) as kill):
            self.manager._kill(process)
            kill.assert_called_once_with(12345, render.signal.SIGKILL)
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows job assignment boundary")
    def test_job_assignment_failure_does_not_run_build(self):
        with patch.object(render._WindowsJob, "attach_and_resume", side_effect=render.RenderError("assignment unavailable")):
            job = self._wait(self._start())
        self.assertEqual(job["status"], "failed")
        self.assertFalse((self.settings.jobs_dir / job["job_id"] / "snapshot" / "observed.json").exists())

    def test_concurrent_and_registry_limits(self):
        self._fake_build("time.sleep(0.4)\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        first, second = self._start(), self._start()
        with self.assertRaisesRegex(render.RenderError, "Concurrent"):
            self._start()
        self.assertEqual(self._wait(first)["status"], "succeeded")
        self.assertEqual(self._wait(second)["status"], "succeeded")
        self.manager.MAX_JOBS = 2
        with self.assertRaisesRegex(render.RenderError, "registry"):
            self._start()

    def test_copy_and_output_resource_limits(self):
        self.manager.MAX_COPY_BYTES = 1
        with self.assertRaisesRegex(render.RenderError, "budget"):
            self._start()
        self.manager.MAX_COPY_BYTES = render.RenderManager.MAX_COPY_BYTES
        self._fake_build("(build / 'paper.pdf').write_bytes(b'x' * 100000)")
        self.manager.MAX_OUTPUT_BYTES = 20000
        self.assertEqual(self._wait(self._start())["status"], "failed")

    def test_near_copy_cap_inputs_do_not_consume_output_bytes_or_entries(self):
        (self.rd / "figures" / "01-1.png").write_bytes(b'x' * (120 * 1024))
        self._fake_build("(build / 'paper.pdf').write_bytes(b'x' * (20 * 1024))")
        self.manager.MAX_COPY_BYTES = 128 * 1024
        self.manager.MAX_OUTPUT_BYTES = 128 * 1024
        self.manager.MAX_FILES = 10
        job = self._wait(self._start())
        self.assertEqual(job["status"], "succeeded", job)
        root = self.settings.jobs_dir / job["job_id"]
        inputs, dirs = self.manager._copied_inputs[job["job_id"]]
        copied_bytes = sum(p.stat().st_size for p in inputs)
        self.assertGreater(copied_bytes, self.manager.MAX_COPY_BYTES * .9)
        self.assertLess(copied_bytes, self.manager.MAX_COPY_BYTES)
        self.assertGreater(len(inputs) + len(dirs), self.manager.MAX_FILES)
        files = [p for p in root.rglob('*') if p.is_file()]
        self.assertGreater(sum(p.stat().st_size for p in files), self.manager.MAX_OUTPUT_BYTES)
        self.assertLess(sum(p.stat().st_size for p in files if p not in inputs), self.manager.MAX_OUTPUT_BYTES)

    def test_unexpected_generated_files_anywhere_still_consume_output_budget(self):
        for relative in ("build/extra.bin", "civil_law/37/extra.bin", "../private/cache.bin",
                         "../unexpected/extra.bin", "../diagnostics/extra.bin"):
            with self.subTest(path=relative):
                self._fake_build("target = ROOT / " + repr(relative) + "\ntarget.parent.mkdir(parents=True, exist_ok=True)\ntarget.write_bytes(b'x' * 100000)\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
                self.manager.MAX_OUTPUT_BYTES = 20000
                self.assertEqual(self._wait(self._start())["status"], "failed")

    def test_overwritten_copied_input_cannot_hide_generated_output(self):
        self._fake_build("(ROOT / 'civil_law/37/01.tex').write_bytes(b'x' * 100000)\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        self.manager.MAX_OUTPUT_BYTES = 20000
        self.assertEqual(self._wait(self._start())["status"], "failed")

    def test_deleted_or_renamed_copied_files_and_directories_are_rejected(self):
        # Each operation resolves absolute boundaries against this synthetic
        # job's snapshot before deleting/moving, never an upstream checkout.
        cases = (("civil_law/37/00.tex", None, False),
                 ("civil_law/37/02.tex", "civil_law/37/renamed.tex", False),
                 ("civil_law/37/figures", "civil_law/37/moved_figures", True),
                 ("civil_law/37/figures", None, True))
        for relative, renamed, directory in cases:
            with self.subTest(source=relative, destination=renamed):
                mode = ("boundary = ROOT.resolve()\nsource = (ROOT / " + repr(relative) + ").resolve()\n"
                        "assert source != boundary and source.is_relative_to(boundary)\n")
                if renamed:
                    mode += ("dest = (ROOT / " + repr(renamed) + ").resolve()\n"
                             "assert dest != boundary and dest.is_relative_to(boundary)\nsource.rename(dest)\n")
                elif directory:
                    mode += ("figure = (source / '01-1.png').resolve()\n"
                             "assert figure.is_relative_to(boundary)\nfigure.unlink()\nsource.rmdir()\n")
                else:
                    mode += "source.unlink()\n"
                mode += "(build / 'paper.pdf').write_bytes(b'%PDF-fake')"
                self._fake_build(mode)
                job = self._wait(self._start())
                self.assertEqual(job["status"], "failed", job)
                self.assertEqual(job["artifacts"], [])

    def test_new_directories_under_copied_directory_consume_entry_budget(self):
        self._fake_build("for i in range(11):\n (ROOT / 'civil_law/37' / ('extra' + str(i))).mkdir()\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        self.manager.MAX_FILES = 10
        self.assertEqual(self._wait(self._start())["status"], "failed")

    def test_diagnostic_copies_also_consume_output_budget(self):
        self._fake_build("(build / 'paper.log').write_bytes(b'x' * 40000)\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        self.manager.MAX_OUTPUT_BYTES = 70000
        # Original log fits; retaining its diagnostic copy must exceed the cap.
        self.assertEqual(self._wait(self._start())["status"], "failed")

    def test_rejected_diagnostic_copy_never_publishes_artifact_ids(self):
        self._fake_build("(build / 'paper.log').write_bytes(b'x' * 40000)\n(build / 'paper.pdf').write_bytes(b'%PDF-fake')")
        self.manager.MAX_OUTPUT_BYTES = 70000
        collect = self.manager._collect
        staged_ids, exposed_ids = [], []

        def stage(job_id, include_pdf):
            pending = collect(job_id, include_pdf)
            staged_ids.extend(pending)
            for aid in pending:
                try:
                    self.manager.get_artifact(job_id, aid)
                except render.RenderError:
                    pass
                else:
                    exposed_ids.append(aid)
            return pending

        with patch.object(self.manager, "_collect", side_effect=stage):
            job = self._wait(self._start())
        self.assertTrue(staged_ids)
        self.assertEqual(exposed_ids, [])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["artifacts"], [])
        self.assertEqual(self.manager._artifacts[job["job_id"]], {})
        for aid in staged_ids:
            with self.assertRaises(render.RenderError):
                self.manager.get_artifact(job["job_id"], aid)
        manifest = self.settings.jobs_dir / job["job_id"] / "manifest.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["artifacts"], [])

    def test_owner_ids_and_artifact_tampering(self):
        job = self._wait(self._start())
        artifact = next(a for a in job["artifacts"] if a["media_type"] == "application/pdf")
        for value in ("../manifest.json", str(self.repo), "unknown", None):
            with self.assertRaises(render.RenderError):
                self.manager.get_job(value)
            with self.assertRaises(render.RenderError):
                self.manager.get_artifact(job["job_id"], value)
        another = render.RenderManager(self.settings)
        with self.assertRaises(render.RenderError):
            another.get_job(job["job_id"])
        response = self.manager.get_artifact(job["job_id"], artifact["artifact_id"])
        Path(response["download_path"]).write_bytes(b"changed!")
        with self.assertRaisesRegex(render.RenderError, "changed"):
            self.manager.get_artifact(job["job_id"], artifact["artifact_id"])

    def test_artifact_hardlink_rejected(self):
        job = self._wait(self._start())
        artifact = next(a for a in job["artifacts"] if a["media_type"] == "application/pdf")
        response = self.manager.get_artifact(job["job_id"], artifact["artifact_id"])
        path = Path(response["download_path"])
        other = self.root / "outside.pdf"
        other.write_bytes(path.read_bytes())
        path.unlink()
        os.link(other, path)
        with self.assertRaises(render.RenderError):
            self.manager.get_artifact(job["job_id"], artifact["artifact_id"])

    def test_hardlink_input_rejected(self):
        target = self.rd / "01.tex"
        alias = self.root / "other.tex"
        os.link(target, alias)
        with self.assertRaises(render.RenderError):
            self._start()

    def _assert_copy_handle_swap_rejected(self, change):
        # Match Settings' canonical workspace, including Windows TEMP 8.3 names.
        target = (self.rd / "01.tex").resolve()
        other = self.root / "replacement.tex"
        alias = self.root / "after-read.tex"
        other.write_bytes(b"q" * target.stat().st_size)
        original_open = Path.open
        reads = []
        changed = []
        boundary = self.root.resolve()
        # All fixture deletion/replacement/link operations use checked absolute
        # paths within this explicitly owned temporary test directory.
        for path in (target, other, alias):
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.resolve().is_relative_to(boundary))
            self.assertNotEqual(path.resolve(), boundary)

        class TrackedRead:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, size):
                reads.append(size)
                data = self.stream.read(size)
                if change == "hardlink_after":
                    os.link(target, alias)
                elif change == "mtime_after":
                    with original_open(target, "r+b") as writer:
                        writer.write(b"z" * len(data))
                    info = target.stat()
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
                return data

        def swapped_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == target and mode == "rb" and not changed:
                changed.append(True)
                if change == "regular_before":
                    other.replace(target)  # New inode, same size and nlink=1.
                elif change == "hardlink_before":
                    target.unlink()
                    os.link(other, target)
                elif change == "redirect_before":
                    # Deterministically emulate a swapped symlink's resolved
                    # handle without requiring Windows symlink privileges.
                    return TrackedRead(original_open(other, *args, **kwargs))
                return TrackedRead(original_open(path, *args, **kwargs))
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", swapped_open):
            with self.assertRaisesRegex(render.RenderError, "Source handle changed"):
                try:
                    self._start()
                finally:
                    self.assertEqual(len(changed), 1, "Copy swap injection did not run; check canonical path matching")
        self.assertEqual(len(reads), 1 if change in ("hardlink_after", "mtime_after") else 0)
        job = next(iter(self.manager._jobs.values()))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["stage"], "preparing")
        self.assertFalse((self.settings.jobs_dir / job["job_id"] / "snapshot/civil_law/37/01.tex").exists())

    def test_copy_rejects_same_size_file_swap_before_consuming(self):
        self._assert_copy_handle_swap_rejected("regular_before")

    def test_copy_rejects_redirected_handle_before_consuming(self):
        self._assert_copy_handle_swap_rejected("redirect_before")

    def test_copy_rejects_hardlink_swap_before_consuming(self):
        self._assert_copy_handle_swap_rejected("hardlink_before")

    def test_copy_rechecks_open_handle_links_after_read(self):
        self._assert_copy_handle_swap_rejected("hardlink_after")

    def test_copy_rechecks_same_size_modification_after_read(self):
        self._assert_copy_handle_swap_rejected("mtime_after")

    def test_copy_swap_injection_matches_canonical_settings_paths(self):
        # Equivalent spelling, different lexical Path: Settings already resolved
        # the workspace while fixture paths may still contain aliases/8.3 names.
        self.rd = self.rd / ".." / self.rd.name
        self.assertNotEqual(self.rd, self.rd.resolve())
        self._assert_copy_handle_swap_rejected("regular_before")

    def test_directory_junction_rejected(self):
        link = self.rd / "linked"
        target = self.root / "outside"
        target.mkdir()
        if os.name == "nt":
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaisesRegex(render.RenderError, "Links"):
                self._start()
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()

    def test_symlink_and_artifact_link_rejected(self):
        link = self.rd / "03.tex"
        try:
            link.symlink_to(self.rd / "01.tex")
        except OSError:
            self.skipTest("Creating Windows symlinks requires developer mode/privilege; junction covered separately")
        with self.assertRaisesRegex(render.RenderError, "Links"):
            self._start()
        link.unlink()
        job = self._wait(self._start())
        artifact = next(a for a in job["artifacts"] if a["media_type"] == "application/pdf")
        response = self.manager.get_artifact(job["job_id"], artifact["artifact_id"])
        path = Path(response["download_path"])
        path.unlink()
        path.symlink_to(self.rd / "01.tex")
        with self.assertRaisesRegex(render.RenderError, "Links"):
            self.manager.get_artifact(job["job_id"], artifact["artifact_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
