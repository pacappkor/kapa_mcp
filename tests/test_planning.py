# SPDX-License-Identifier: GPL-3.0-only
"""Ordered compilation and authenticated shared-registry regressions."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_render as fixtures
render = fixtures.render


BUILD = '''import json, pathlib, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
def preflight():
    return True
def compile_tex(tex, cwd):
    cmd = ['xelatex', '-interaction=nonstopmode', '-halt-on-error', '-file-line-error', tex.name]
    time.sleep(.15)
    (cwd / 'selection.pdf').write_bytes(b'%PDF-synthetic')
    (ROOT / 'observed.json').write_text(json.dumps({'wrapper': tex.read_text(encoding='utf-8'), 'compiler': cmd}))
    return True, [], []
'''


class PlanningTests(unittest.TestCase):
    _patch = fixtures.RenderTests._patch
    _fake_build = fixtures.RenderTests._fake_build
    _wait = fixtures.RenderTests._wait
    _start = fixtures.RenderTests._start
    tearDown = fixtures.RenderTests.tearDown

    def setUp(self):
        fixtures.RenderTests.setUp(self)
        for rd in ('36', '37'):
            directory = self.repo / 'civil_law' / rd
            (directory / '00.tex').write_text('Common round ' + rd, encoding='utf-8')
            for seq in ('01', '02'):
                (directory / (seq + '.tex')).write_text(
                    '\\begin{question}{' + str(int(seq)) + '}\nRound ' + rd + ' question ' + seq
                    + '\n\\answer{3}\n\\end{question}\n', encoding='utf-8')
        (self.repo / 'tools/build.py').write_text(BUILD, encoding='utf-8', newline='\n')
        self._patch('kapa_mcp.render._BUILD_HASHES', new={hashlib.sha256(BUILD.encode()).hexdigest()})
        self.ids = ['edu/1/civil_law/37/02', 'edu/1/civil_law/36/01', 'edu/1/civil_law/37/01']

    def test_order_renumber_answers_and_common_instruction_blocks(self):
        original = {p: p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        plan = self.manager.create_plan(self.ids)
        job = self._wait(self.manager.start_render_plan(plan['plan_id']))
        self.assertEqual(job['status'], 'succeeded', job)
        snapshot = self.settings.jobs_dir / job['job_id'] / 'snapshot'
        observed = json.loads((snapshot / 'observed.json').read_text())
        wrapper = observed['wrapper']
        self.assertIn('\\usepackage[noanswers]{kapa-exam}', wrapper)
        self.assertEqual(wrapper.count('{00.tex}'), 3)
        positions = [wrapper.index('selected_%03d.tex' % number) for number in (1, 2, 3)]
        self.assertEqual(positions, sorted(positions))
        for n, rd in ((1, '37'), (2, '36'), (3, '37')):
            fragment = (snapshot / 'civil_law' / rd / f'selected_{n:03}.tex').read_text()
            self.assertIn('\\begin{question}{%s}' % n, fragment)
            self.assertIn('\\answer{3}', fragment)
        self.assertIn('-no-shell-escape', observed['compiler'])
        self.assertEqual({p: p.read_bytes() for p in original}, original)
        self.assertEqual(job['plan']['question_ids'], self.ids)

    def test_visible_answers_and_excluded_instructions(self):
        plan = self.manager.create_plan(self.ids, noanswers=False, include_instructions=False)
        job = self._wait(self.manager.start_render_plan(plan['plan_id']))
        self.assertEqual(job['status'], 'succeeded')
        wrapper = (self.settings.jobs_dir / job['job_id'] / 'snapshot/selection.tex').read_text()
        self.assertIn('\\usepackage{kapa-exam}', wrapper)
        self.assertNotIn('{00.tex}', wrapper)

    def test_rejected_selection_and_policy_values(self):
        for ids in ([], self.ids * 40, self.ids[:1] * 2,
                    ['edu/1/civil_law/37/00'], ['../file.tex'],
                    ['edu/1/civil_law/37/01', 'edu/2/civil_law/37/01'],
                    ['edu/1/civil_law/37/01', 'edu/1/economics/37/01']):
            with self.subTest(ids=ids[:3]), self.assertRaises(render.RenderError):
                self.manager.create_plan(ids)
        for kw in ({'noanswers': 1}, {'include_instructions': 'yes'}, {'require_evidence': 1}):
            with self.assertRaises(render.RenderError):
                self.manager.create_plan(self.ids, **kw)
        with self.assertRaises(render.RenderError):
            self.manager.create_plan(['edu/1/civil_law/37/99'])

    def test_longquestion_numbering_and_unsupported_layout(self):
        text = '\\begin{longquestion}{【문제 4】}\nBody\n\\end{longquestion}'
        self.assertIn('{【문제 2】}', render.planning.renumber(text, 2, 2))
        for bad in ('plain text', '\\begin{question}{custom}\nBody', text + '\n' + text):
            with self.assertRaises(ValueError):
                render.planning.renumber(bad, 1, 1)

    def test_source_staleness_and_copy_time_change(self):
        for relative in ('civil_law/37/02.tex', 'civil_law/37/00.tex',
                         'civil_law/37/figures/01-1.png', 'latex/kapa-exam.sty', 'subjects.json'):
            plan = self.manager.create_plan(self.ids)
            path = self.repo / relative
            previous = path.read_bytes()
            path.write_bytes(previous + b' ')
            try:
                with self.assertRaisesRegex(render.RenderError, 'changed'):
                    self.manager.start_render_plan(plan['plan_id'])
            finally:
                path.write_bytes(previous)
        plan = self.manager.create_plan(self.ids)
        prepare = self.manager._prepare
        def changed(*args):
            with (self.rd / '01.tex').open('a') as stream:
                stream.write(' ')
            return prepare(*args)
        with patch.object(self.manager, '_prepare', side_effect=changed):
            with self.assertRaisesRegex(render.RenderError, 'changed'):
                self.manager.start_render_plan(plan['plan_id'])

    def test_defensive_plan_copy_restart_and_artifact_lookup(self):
        plan = self.manager.create_plan(self.ids)
        pid = plan['plan_id']
        plan['question_ids'].reverse()
        plan['source_hashes'].clear()
        self.assertEqual(self.manager.get_plan(pid)['question_ids'], self.ids)
        second = render.RenderManager(self.settings)
        self.assertEqual(second.get_plan(pid), self.manager.get_plan(pid))
        job = self._wait(self.manager.start_render_plan(pid))
        third = render.RenderManager(self.settings)
        self.assertEqual(third.get_job(job['job_id']), job)
        artifact = next(a for a in job['artifacts'] if a['media_type'] == 'application/pdf')
        self.assertTrue(Path(third.get_artifact(job['job_id'], artifact['artifact_id'])['download_path']).is_file())

    def test_required_evidence_persists_and_revalidates_after_restart(self):
        validator = Mock()
        self.manager._evidence_validator = validator
        for kw in ({'require_evidence': True}, {'evidence_bundle_id': '../bad'}):
            with self.assertRaises(render.RenderError):
                self.manager.create_plan(self.ids, **kw)
        plan = self.manager.create_plan(self.ids, evidence_bundle_id='bundle_test', require_evidence=True)
        validator.assert_called_once_with('bundle_test', self.ids)
        restored = render.RenderManager(self.settings)
        self.assertTrue(restored.get_plan(plan['plan_id'])['require_evidence'])
        with self.assertRaisesRegex(render.RenderError, 'hook'):
            restored.start_render_plan(plan['plan_id'])
        stale = Mock(side_effect=ValueError('stale evidence'))
        restored = render.RenderManager(self.settings, evidence_validator=stale)
        with self.assertRaisesRegex(ValueError, 'stale'):
            restored.start_render_plan(plan['plan_id'])
        stale.assert_called_once_with('bundle_test', self.ids)

    def test_disk_metadata_tamper_and_unowned_directory(self):
        unowned = self.settings.jobs_dir / '00000000-0000-0000-0000-000000000000'
        unowned.mkdir()
        (unowned / 'manifest.json').write_text('{}')
        restored = render.RenderManager(self.settings)
        with self.assertRaises(render.RenderError):
            restored.get_job(unowned.name)
        plan = self.manager.create_plan(self.ids)
        path = self.settings.jobs_dir / 'plans' / (plan['plan_id'] + '.json')
        record = json.loads(path.read_text())
        record['require_evidence'] = True
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(render.RenderError, 'changed'):
            render.RenderManager(self.settings)

    def test_manifest_registry_and_artifact_tamper_rejected_on_restart(self):
        plan = self.manager.create_plan(self.ids)
        job = self._wait(self.manager.start_render_plan(plan['plan_id']))
        manifest = self.settings.jobs_dir / job['job_id'] / 'manifest.json'
        original = manifest.read_bytes()
        manifest.write_bytes(original + b' ')
        with self.assertRaisesRegex(render.RenderError, 'changed'):
            render.RenderManager(self.settings)
        manifest.write_bytes(original)
        registry = self.settings.jobs_dir / '.registry.json'
        original_registry = registry.read_bytes()
        data = json.loads(original_registry)
        data['data']['jobs'].clear()
        registry.write_text(json.dumps(data))
        with self.assertRaisesRegex(render.RenderError, 'authentication'):
            render.RenderManager(self.settings)
        registry.write_bytes(original_registry)
        artifact = next(a for a in job['artifacts'] if a['media_type'] == 'application/pdf')
        path = self.settings.jobs_dir / job['job_id'] / artifact['path']
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(render.RenderError, 'changed'):
            render.RenderManager(self.settings)

    def test_two_live_managers_merge_jobs_and_plans(self):
        other = render.RenderManager(self.settings)
        one = self.manager.create_plan(self.ids[:1])
        two = other.create_plan(self.ids[1:2])
        first = self.manager.start_render_plan(one['plan_id'])
        self.assertIn(other.get_job(first['job_id'])['status'], ('queued', 'running', 'succeeded'))
        second = other.start_render_plan(two['plan_id'])
        first = self._wait(first)
        second = self._wait(second)  # lazy refresh of the foreign manager's job
        restored = render.RenderManager(self.settings)
        self.assertEqual(restored.get_job(first['job_id'])['status'], 'succeeded')
        self.assertEqual(restored.get_job(second['job_id'])['status'], 'succeeded')
        self.assertEqual(restored.get_plan(one['plan_id']), one)
        self.assertEqual(restored.get_plan(two['plan_id']), two)

    def test_simultaneous_process_registry_writers_preserve_both_jobs(self):
        source = str(Path(render.__file__).resolve().parents[1])
        script = '''import hashlib, json, pathlib, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from kapa_mcp.config import Settings
from kapa_mcp import render
settings = Settings(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
render._BUILD_HASHES = {hashlib.sha256((settings.workspace / 'kapa_edu_1/tools/build.py').read_bytes()).hexdigest()}
assert render.shutil.which('xelatex') is None
assert render.shutil.which('kpsewhich') is None
# The synthetic build never invokes TeX. Discovery needs its own test double
# here because a new interpreter does not inherit the parent's mock.
with patch.object(render.shutil, 'which', return_value=str(pathlib.Path(sys._base_executable).resolve())):
    manager = render.RenderManager(settings)
    plan = manager.create_plan(['edu/1/civil_law/37/01'])
    job = manager.start_render_plan(plan['plan_id'])
    while job['status'] in ('preparing', 'queued', 'running'):
        time.sleep(.02)
        job = manager.get_job(job['job_id'])
print(json.dumps({'job_id': job['job_id'], 'plan_id': plan['plan_id'], 'status': job['status']}))
'''
        empty_path = self.root / 'empty-path'
        empty_path.mkdir()
        child_env = dict(os.environ, PATH=str(empty_path))
        processes = [subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', script, source, str(self.workspace), str(self.settings.jobs_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env) for _ in range(2)]
        records = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr.decode(errors='replace'))
                records.append(json.loads(stdout))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()  # Only the owned synthetic test process.
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()
        restored = render.RenderManager(self.settings)
        self.assertEqual(len({record['job_id'] for record in records}), 2)
        for record in records:
            self.assertEqual(record['status'], 'succeeded')
            self.assertEqual(restored.get_job(record['job_id'])['status'], 'succeeded')
            self.assertEqual(restored.get_plan(record['plan_id'])['question_ids'], self.ids[2:])

    def test_interrupted_job_uses_lock_liveness_and_is_never_resumed(self):
        plan = self.manager.create_plan(self.ids)
        job = self._wait(self.manager.start_render_plan(plan['plan_id']))
        self.manager._update(job['job_id'], status='running', artifacts=[])
        restored = render.RenderManager(self.settings)
        recovered = restored.get_job(job['job_id'])
        self.assertEqual(recovered['status'], 'failed')
        self.assertIn('interrupted', recovered['error'])
        self.assertEqual(recovered['artifacts'], [])
        self.manager._update(job['job_id'], status='failed', artifacts=[])

    def test_registry_and_manifest_size_limits(self):
        registry = self.settings.jobs_dir / '.registry.json'
        registry.write_bytes(b'x' * (self.manager.REGISTRY_BYTES + 1))
        with self.assertRaises(render.RenderError):
            render.RenderManager(self.settings)

    def test_terminal_metadata_retention_preserves_files_and_accepts_new_jobs(self):
        with patch.object(render.RenderManager, 'MAX_JOBS', 2):
            plan = self.manager.create_plan(self.ids)
            jobs = [self._wait(self.manager.start_render_plan(plan['plan_id'])) for _ in range(2)]
            original_files = {p: p.read_bytes() for job in jobs
                              for p in (self.settings.jobs_dir / job['job_id']).rglob('*') if p.is_file()}
            stale = render.RenderManager(self.settings)
            restored = render.RenderManager(self.settings)
            new = restored.start_render_plan(plan['plan_id'])
            deadline = time.monotonic() + 10
            while new['status'] in ('preparing', 'queued', 'running'):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
                new = restored.get_job(new['job_id'])
            self.assertEqual(new['status'], 'succeeded')
            restarted = render.RenderManager(self.settings)
            self.assertEqual(len(restarted._jobs), 2)
            self.assertEqual(restarted.get_job(new['job_id'])['status'], 'succeeded')
            retired = jobs[0]['job_id']
            with self.assertRaises(render.RenderError):
                stale._update(retired, status='failed')
            with self.assertRaises(render.RenderError):
                stale.get_job(retired)
            self.assertEqual({p: p.read_bytes() for p in original_files}, original_files)
            # A different manager's write cannot resurrect its old cached entry.
            stale.create_plan(self.ids[:1])
            self.assertNotIn(retired, render.RenderManager(self.settings)._jobs)
            self.manager._refresh_registry()

    def test_plan_metadata_retention_preserves_active_plan_and_expired_files(self):
        with patch.object(render.RenderManager, 'MAX_JOBS', 2):
            active_plan = self.manager.create_plan(self.ids)
            unused = self.manager.create_plan(self.ids[:1])
            # Hold the real job's preparation with an event, keeping its owner
            # lock live while another manager requests plan retention capacity.
            import threading
            ready, release = threading.Event(), threading.Event()
            prepare = self.manager._prepare
            outcome = []
            def held(*args):
                ready.set()
                if not release.wait(5):
                    raise RuntimeError('test preparation gate timed out')
                return prepare(*args)
            def start():
                outcome.append(self.manager.start_render_plan(active_plan['plan_id']))
            with patch.object(self.manager, '_prepare', side_effect=held):
                thread = threading.Thread(target=start)
                thread.start()
                try:
                    self.assertTrue(ready.wait(5))
                    other = render.RenderManager(self.settings)
                    newest = other.create_plan(self.ids[1:2])
                    self.assertEqual(other.get_plan(active_plan['plan_id']), active_plan)
                    with self.assertRaises(render.RenderError):
                        other.get_plan(unused['plan_id'])
                    self.assertTrue((self.settings.jobs_dir / 'plans' / (unused['plan_id'] + '.json')).is_file())
                    self.assertEqual(other.get_plan(newest['plan_id']), newest)
                finally:
                    release.set()
                    thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self._wait(outcome[0])['status'], 'succeeded')

    def test_one_entry_windows_allow_repeated_render_and_plan_creation_after_restart(self):
        with patch.object(render.RenderManager, 'MAX_JOBS', 1):
            records = []
            manager = self.manager
            for _ in range(3):
                plan = manager.create_plan(self.ids[:1])
                job = manager.start_render_plan(plan['plan_id'])
                deadline = time.monotonic() + 10
                while job['status'] in ('preparing', 'queued', 'running'):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                    job = manager.get_job(job['job_id'])
                self.assertEqual(job['status'], 'succeeded')
                records.append((plan, job))
                manager = render.RenderManager(self.settings)
            self.assertEqual(len(manager._plans), 1)
            self.assertEqual(len(manager._jobs), 1)
            for plan, job in records[:-1]:
                with self.assertRaises(render.RenderError):
                    manager.get_plan(plan['plan_id'])
                with self.assertRaises(render.RenderError):
                    manager.get_job(job['job_id'])
                self.assertTrue((self.settings.jobs_dir / 'plans' / (plan['plan_id'] + '.json')).is_file())
                artifact = next(a for a in job['artifacts'] if a['media_type'] == 'application/pdf')
                self.assertTrue((self.settings.jobs_dir / job['job_id'] / artifact['path']).is_file())
            self.manager._refresh_registry()

    def test_full_registry_never_retires_a_live_foreign_job(self):
        with patch.object(render.RenderManager, 'MAX_JOBS', 1):
            plan = self.manager.create_plan(self.ids)
            import threading
            ready, release = threading.Event(), threading.Event()
            prepare = self.manager._prepare
            outcome = []
            def held(*args):
                ready.set()
                if not release.wait(5):
                    raise RuntimeError('test preparation gate timed out')
                return prepare(*args)
            def start():
                outcome.append(self.manager.start_render_plan(plan['plan_id']))
            with patch.object(self.manager, '_prepare', side_effect=held):
                thread = threading.Thread(target=start)
                thread.start()
                try:
                    self.assertTrue(ready.wait(5))
                    other = render.RenderManager(self.settings)
                    with self.assertRaisesRegex(render.RenderError, 'active'):
                        other.start_render_plan(plan['plan_id'])
                    job_id = next(iter(self.manager._job_locks))
                    self.assertEqual(other.get_job(job_id)['status'], 'preparing')
                    with self.assertRaisesRegex(render.RenderError, 'active'):
                        other.create_plan(self.ids[:1])
                finally:
                    release.set()
                    thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self._wait(outcome[0])['status'], 'succeeded')

    def test_restored_terminal_polling_caches_validation_but_artifact_reads_check_content(self):
        plan = self.manager.create_plan(self.ids)
        jobs = [self._wait(self.manager.start_render_plan(plan['plan_id'])) for _ in range(3)]
        restored = render.RenderManager(self.settings)
        with patch('kapa_mcp.render._digest', wraps=render._digest) as digest:
            for _ in range(5):
                self.assertEqual(restored.get_job(jobs[0]['job_id'])['status'], 'succeeded')
            digest.assert_not_called()
            # Another writer changes the root registry while all prior terminal
            # artifacts must remain cached rather than being rehashed again.
            newest = self.manager.start_render_plan(plan['plan_id'])
            self.assertEqual(restored.get_job(jobs[0]['job_id'])['status'], 'succeeded')
            self._wait(newest)
            self.assertEqual(restored.get_job(jobs[0]['job_id'])['status'], 'succeeded')
            retained_paths = {self.settings.jobs_dir / job['job_id'] / a['path']
                              for job in jobs for a in job['artifacts']}
            self.assertFalse(any(call.args[0] in retained_paths for call in digest.call_args_list))
            digest.reset_mock()
            artifact = next(a for a in jobs[0]['artifacts'] if a['media_type'] == 'application/pdf')
            path = self.settings.jobs_dir / jobs[0]['job_id'] / artifact['path']
            # Same-size edits require the content digest, not just a size check.
            path.write_bytes(b'x' * path.stat().st_size)
            with self.assertRaisesRegex(render.RenderError, 'changed'):
                restored.get_artifact(jobs[0]['job_id'], artifact['artifact_id'])
            digest.assert_called_once_with(path)

    def test_missing_registry_invalidates_cached_restored_job_and_artifact_ids(self):
        plan = self.manager.create_plan(self.ids)
        job = self._wait(self.manager.start_render_plan(plan['plan_id']))
        artifact = next(a for a in job['artifacts'] if a['media_type'] == 'application/pdf')
        restored_job = render.RenderManager(self.settings)
        restored_artifact = render.RenderManager(self.settings)
        self.assertEqual(restored_job.get_job(job['job_id'])['status'], 'succeeded')
        response = restored_artifact.get_artifact(job['job_id'], artifact['artifact_id'])
        path = Path(response['download_path'])
        original_pdf = path.read_bytes()
        registry = self.settings.jobs_dir / '.registry.json'
        registry.unlink()
        try:
            with self.assertRaises(render.RenderError):
                restored_job.get_job(job['job_id'])
            with self.assertRaises(render.RenderError):
                restored_artifact.get_artifact(job['job_id'], artifact['artifact_id'])
            with self.assertRaises(render.RenderError):
                restored_job.get_plan(plan['plan_id'])
            self.assertIsNone(restored_job._registry_identity)
            self.assertIsNone(restored_artifact._registry_identity)
            self.assertEqual(path.read_bytes(), original_pdf)
        finally:
            self.manager._refresh_registry()


if __name__ == '__main__':
    unittest.main(verbosity=2)
