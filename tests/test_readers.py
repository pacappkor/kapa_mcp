# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic fixtures only: no production checkout, credentials or upstream imports."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import struct
import sys
import tempfile
import time
import zlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kapa_mcp.config import Settings
from kapa_mcp.readers import (Catalog, MAX_BYTES, MAX_CHARS, REPOS, _metadata,
                              _no_links, _json, _jsonl, _relation_identity_matches,
                              _bounded, _capture_git, RequestBudgetExceeded)


def _fixture_relation_id(relation):
    fields = [relation[key] for key in ("type", "src", "dst", "scope")]
    return "rel-" + hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Settings resolves DOS short names (e.g. hosted Windows TEMP). Use the
        # same spelling before matching paths in mocked filesystem operations.
        self.root = Path(self.tmp.name).resolve()
        for repo in REPOS:
            (self.root / repo).mkdir()
        self.catalog = Catalog(Settings(self.root, self.root / "jobs"))
        def fixture_state(catalog, repo):
            # Ordinary unit fixtures have no Git repository. Supply an explicit
            # synthetic HEAD manifest; real Git provenance is exercised below.
            query = catalog._query()
            manifest = {}
            for path in catalog._files(repo):
                _, digest = catalog._read(repo, path)
                manifest[path] = query["reads"][(repo, path, digest)]["sha1"]
            query["manifests"][repo] = manifest
            state = {"repo": repo, "revision": "a" * 40, "dirty": False,
                     "object_format": "sha1", "content_head_verified": True, "warnings": []}
            query["states"][repo] = state
            return state
        self.state = patch.object(Catalog, "_git_state", fixture_state)
        self.state.start()
        self.addCleanup(self.state.stop)
        self.write("kapa_knowledge", "deps.lock", json.dumps({"kapa_skills": {"commit": "a" * 40}}))
        subjects = {"subjects": [{"folder": "civil_law", "name_ko": "민법", "aliases": ["민법"]}]}
        self.write("kapa_edu_1", "subjects.json", json.dumps(subjects))
        subjects["subjects"][0].update(folder="compensation_law", name_ko="보상법규", aliases=["보상법규"])
        self.write("kapa_edu_2", "subjects.json", json.dumps(subjects))
        self.q1 = ("%% kapa: exam=1 round=37 year=2026 period=1 subject=civil_law "
                   "subject_ko=민법 seq=1 number=41\n%% source: Q-Net artlSeq=123 file=raw/question.pdf\n"
                   "%% conversion: fixture; review=pending; flags=none\n미불용지 보상 법원\n\\answer{2}\n")
        self.write("kapa_edu_1", "civil_law/37/01.tex", self.q1)
        self.write("kapa_edu_1", "civil_law/37/02.tex", self.q1.replace("미불용지 보상", "미불용지").replace("seq=1", "seq=2"))
        self.write("kapa_edu_1", "civil_law/37/00.tex", "공통 지시문")
        self.write("kapa_edu_1", "answers/answers.csv", "round,period,form,subject,number,answer\n37,1,A,민법,1,2\n37,1,A,민법,2,3\n")
        self.write("kapa_edu_2", "compensation_law/37/01.tex", "%% kapa: exam=2 round=37 period=3 subject=compensation_law subject_ko=감정평가 및 보상법규 seq=1 points=40\n미불용지 보상 물음 1 (40점)")
        self.write("kapa_knowledge", "issues/unpaid-land/README.md", '---\nid: "issue/unpaid-land"\ntitle: "미불용지 보상"\nprogress: "candidate"\nreference_status: "unreviewed"\n---\n본문 미불용지 보상')
        self.write("kapa_skills", "laws/lca/lca-land/SKILL.md", '---\nname: lca-land\ndescription: 미불용지 보상 업무\n---\n# 미불용지')
        self.write("kapa_skills", "laws/lca/lca-land/source.md", "원문 미불용지 보상")
        self.write("kapa_knowledge", "relations/relations.jsonl", "")

    def write(self, repo, path, content):
        target = self.root / repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return target

    def test_korean_ranking_and_pagination(self):
        result = self.catalog.search_questions("미불용지 보상", exam=1, limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["id"], "edu/1/civil_law/37/01")
        self.assertEqual(result["next_offset"], 1)
        tail = self.catalog.search_questions("미불용지 보상", exam=1, limit=1, offset=1)
        self.assertEqual(tail["items"][0]["id"], "edu/1/civil_law/37/02")
        self.assertIsNone(tail["next_offset"])
        self.assertEqual(self.catalog.search_questions("없는단어")["total"], 0)

    def test_filters_and_seq_not_printed_answer_number(self):
        self.assertEqual(self.catalog.search_questions(subject="민법", round=37)["total"], 2)
        self.assertEqual(self.catalog.search_questions(exam=2, subject="보상법규")["total"], 1)
        result = self.catalog.read_question("edu/1/civil_law/37/01")
        self.assertEqual(result["content"], self.q1)
        self.assertEqual(result["metadata"]["kapa"]["number"], "41")
        self.assertEqual(result["answer"]["row"]["number"], "1")
        self.assertEqual(result["answer"]["row"]["answer"], "2")
        self.assertEqual(result["common_content"], "공통 지시문")
        self.assertEqual(result["sha256"], hashlib.sha256(self.q1.encode()).hexdigest())

    def test_answers_cannot_cross_subject_block_or_use_wrong_subject_row(self):
        rows = [f"37,1,A,민법,{i},2" for i in range(1, 41)]
        rows += [f"37,1,A,경제학,{i},4" for i in range(1, 41)]
        header = "round,period,form,subject,number,answer\n"
        self.write("kapa_edu_1", "answers/answers.csv", header + "\n".join(rows) + "\n")
        for seq in (40, 41, 99):
            self.write("kapa_edu_1", f"civil_law/37/{seq:02}.tex", self.q1.replace("seq=1", f"seq={seq}"))
            result = self.catalog.read_question(f"edu/1/civil_law/37/{seq:02}")
            if seq == 40:
                self.assertEqual(result["answer"]["row"]["subject"], "민법")
            else:
                self.assertIsNone(result["answer"])
                self.assertTrue(any("answer_unresolved" in w for w in result["warnings"]))
        rows[1] = "37,1,A,경제학,2,4"
        self.write("kapa_edu_1", "answers/answers.csv", header + "\n".join(rows) + "\n")
        self.assertIsNone(self.catalog.read_question("edu/1/civil_law/37/02")["answer"])

    def test_nonfinite_json_overflow_and_serialization_rejected(self):
        for text in ('1e400', '-1e400', '[{"x":1e400}]', '{"x":-1e999}', 'NaN', 'Infinity'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _json(text)
        self.assertEqual(_json('1e308'), 1e308)
        with self.assertRaises(ValueError):
            _metadata('---\nvalue: 1e400\n---\n')
        class InvalidResult(Catalog):
            @_bounded
            def result(self):
                return {"value": float("inf")}
        with self.assertRaises(ValueError):
            InvalidResult(self.catalog.settings).result()

    def test_request_read_budget_counts_repeated_io_and_resets_after_failure(self):
        size = (self.root / "kapa_skills/laws/lca/lca-land/source.md").stat().st_size
        class Probe(Catalog):
            @_bounded
            def probe(self, repeats):
                for _ in range(repeats):
                    self._read("kapa_skills", "laws/lca/lca-land/source.md")
                return {"bytes": self._query()["bytes_read"]}
        catalog = Probe(self.catalog.settings)
        with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", 2 * size):
            self.assertEqual(catalog.probe(2)["bytes"], 2 * size)
            real_open, opened = Path.open, []
            def observe(path, *args, **kwargs):
                opened.append(path)
                return real_open(path, *args, **kwargs)
            with patch.object(Path, "open", observe):
                with self.assertRaisesRegex(RequestBudgetExceeded, "read-byte"):
                    catalog.probe(3)
            self.assertEqual(len(opened), 2, "The third read must fail before opening its file")
            self.assertEqual(catalog.probe(1)["bytes"], size)

    def test_request_enumeration_budget_is_shared_across_scans(self):
        class Probe(Catalog):
            @_bounded
            def probe(self, repeats):
                for _ in range(repeats):
                    list(self._files("kapa_skills"))
                return {"entries": self._query()["entries_visited"]}
        catalog = Probe(self.catalog.settings)
        entries = catalog.probe(1)["entries"]
        with patch("kapa_mcp.readers.MAX_REQUEST_ENTRIES", entries * 2):
            self.assertEqual(catalog.probe(2)["entries"], entries * 2)
            with self.assertRaisesRegex(RequestBudgetExceeded, "enumeration"):
                catalog.probe(3)

    def test_request_deadline_stops_read_and_enumeration_before_more_io(self):
        clock = [0.0]
        class Probe(Catalog):
            @_bounded
            def probe(self, operation):
                self._read("kapa_skills", "laws/lca/lca-land/source.md")
                clock[0] = 2.0
                if operation == "read":
                    self._read("kapa_skills", "laws/lca/lca-land/source.md")
                else:
                    list(self._files("kapa_skills"))
                return {"unexpected": True}
        catalog = Probe(self.catalog.settings)
        for operation in ("read", "enumerate"):
            clock[0] = 0.0
            with self.subTest(operation=operation), patch("kapa_mcp.readers.REQUEST_TIMEOUT", 1), patch("kapa_mcp.readers.time.monotonic", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(RequestBudgetExceeded, "deadline"):
                    catalog.probe(operation)

    def test_search_budget_exhaustion_is_not_missing_content_or_partial_success(self):
        with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", 1):
            with self.assertRaises(RequestBudgetExceeded):
                self.catalog.search_knowledge("미불용지")

    def test_git_stdout_and_normal_reads_share_request_byte_budget(self):
        size = (self.root / "kapa_skills/laws/lca/lca-land/source.md").stat().st_size
        class Probe(Catalog):
            @_bounded
            def probe(self):
                self._read("kapa_skills", "laws/lca/lca-land/source.md")
                env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
                _capture_git([sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.buffer.write(b'x'*16)"], env)
                return {"bytes": self._query()["bytes_read"]}
        catalog = Probe(self.catalog.settings)
        with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", size + 16):
            self.assertEqual(catalog.probe()["bytes"], size + 16)
        with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", size + 15):
            with self.assertRaises(RequestBudgetExceeded):
                catalog.probe()

    def test_exam2_preserves_full_question(self):
        result = self.catalog.read_question("edu/2/compensation_law/37/01")
        self.assertIn("물음 1 (40점)", result["content"])
        self.assertEqual(result["metadata"]["kapa"]["subject_ko"], "감정평가 및 보상법규")
        self.assertIsNone(result["answer"])

    def test_document_continuation_and_hash(self):
        doc = "kapa_skills:laws/lca/lca-land/source.md"
        first = self.catalog.read_document(doc, max_chars=3)
        last = self.catalog.read_document(doc, offset=first["next_offset"], max_chars=MAX_CHARS)
        self.assertEqual(first["content"] + last["content"], "원문 미불용지 보상")
        self.assertIsNone(last["next_offset"])
        self.assertEqual(first["sha256"], last["sha256"])
        self.assertEqual(self.catalog.read_document(doc, offset=999)["content"], "")

    def test_knowledge_metadata_route_and_page(self):
        result = self.catalog.search_knowledge("미불용지", limit=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["dependency"]["resolved"])
        result = self.catalog.search_knowledge("미불용지", repo="knowledge")
        self.assertEqual(result["items"][0]["metadata"]["reference_status"], "unreviewed")
        route = self.catalog.route_task("미불용지 보상 업무")
        self.assertTrue(route["candidates"])
        self.assertIn("kapa_skills:00-kapa-start/SKILL.md", route["entry_documents"])
        self.assertNotIn(str(self.root), json.dumps(route, ensure_ascii=False))

    def test_lock_mismatch_missing_and_worktree(self):
        for value in ("b" * 40, "WORKTREE", None):
            self.write("kapa_knowledge", "deps.lock", json.dumps({"kapa_skills": {"commit": value}}))
            result = self.catalog.search_knowledge("미불용지")
            self.assertFalse(result["dependency"]["resolved"])
            self.assertTrue(any("version_unresolved" in w for w in result["warnings"]))
        with patch.object(self.catalog, "_git_state", return_value={"revision": "a" * 40, "dirty": True, "warnings": []}):
            self.assertFalse(self.catalog._dependency()["resolved"])

    def test_path_allowlist_rejects_before_open(self):
        bad = ["../README.md", "/README.md", "tools/read.py", ".git/config", "docs/password.md",
               "edu_gh_token.txt", "law_oc.txt", "laws/../../README.md", "laws\\x\\source.md",
               "laws/%2e%2e/source.md", "laws/x/source.md:stream", "laws/x/other.md",
               "rules/skill-update-maintenance/SKILL.md", "C:/Users/name/private.txt"]
        for path in bad:
            with self.subTest(path=path), patch.object(Path, "open", side_effect=AssertionError("must not open")):
                with self.assertRaises(ValueError):
                    self.catalog.read_document("kapa_skills:" + path)
        with self.assertRaises(ValueError):
            self.catalog.read_document("other:README.md")

    def test_invalid_inputs(self):
        for kwargs in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": True}):
            with self.assertRaises(ValueError):
                self.catalog.search_questions(**kwargs)
        for question in ("edu/1/civil_law/37/00", "edu/1/../37/01", "edu/3/civil_law/37/01", "edu/1/civil_law/37/1"):
            with self.assertRaises(ValueError):
                self.catalog.read_question(question)
        for kwargs in ({"offset": -1}, {"max_chars": 0}, {"max_chars": MAX_CHARS + 1}, {"offset": True}):
            with self.assertRaises(ValueError):
                self.catalog.read_document("kapa_skills:laws/lca/lca-land/source.md", **kwargs)

    def test_size_encoding_and_nul_bounds(self):
        path = "laws/lca/lca-land/source.md"
        for content in (b"x" * (MAX_BYTES + 1), b"a\x00b", b"\xff"):
            target = self.root / "kapa_skills" / path
            target.write_bytes(content)
            with self.assertRaises(ValueError):
                self.catalog.read_document("kapa_skills:" + path)
        self.write("kapa_edu_1", "civil_law/37/01.tex", "x" * (MAX_CHARS + 1))
        with self.assertRaisesRegex(ValueError, "pagination"):
            self.catalog.read_question("edu/1/civil_law/37/01")

    def test_reparse_rejected(self):
        self.assert_reparse_rejected(self.catalog)

    def assert_reparse_rejected(self, catalog):
        original = Path.lstat
        target = catalog.workspace / "kapa_skills" / "laws"
        injected = []
        def fake(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == target:
                injected.append(path)
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", fake), patch.object(Path, "open", side_effect=AssertionError("must reject before open")):
            with self.assertRaisesRegex(ValueError, "reparse"):
                catalog.read_document("kapa_skills:laws/lca/lca-land/source.md")
        self.assertTrue(injected, "Reparse attribute must actually be injected into the checked path")

    @unittest.skipUnless(os.name == "nt", "Windows DOS short-name path regression")
    def test_reparse_rejected_with_short_workspace(self):
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(self.root), buffer, len(buffer))
        self.assertTrue(size, "Existing fixture must have a Windows path spelling")
        self.assertLess(size, len(buffer))
        short = Path(buffer.value)
        if short == self.root:
            self.skipTest("Fixture volume has no distinct DOS short-name spelling")
        self.assertTrue(short.samefile(self.root))
        self.assertNotEqual(short, self.root)
        catalog = Catalog(Settings(short, self.root / "jobs"))
        self.assertEqual(catalog.workspace, self.root)
        self.assert_reparse_rejected(catalog)

    @unittest.skipUnless(os.name == "nt", "Windows real junction regression")
    def test_real_junction_rejected_before_content_open(self):
        import _winapi
        target = self.write("kapa_skills", "laws/junction-target/source.md", "synthetic junction content")
        junction = self.root / "kapa_skills" / "laws" / "junction-link"
        _winapi.CreateJunction(str(target.parent), str(junction))
        try:
            self.assertTrue(junction.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            self.assertTrue(junction.samefile(target.parent))
            with patch.object(Path, "open", side_effect=AssertionError("must reject before open")):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    self.catalog.read_document("kapa_skills:laws/junction-link/source.md")
        finally:
            # Remove the junction itself, never recursively traverse its target.
            junction.rmdir()
        self.assertEqual(target.read_text(encoding="utf-8"), "synthetic junction content")

    def test_symlink_rejected_if_supported(self):
        link = self.root / "kapa_skills" / "README.md"
        try:
            link.symlink_to(self.root / "kapa_skills/laws/lca/lca-land/source.md")
        except OSError:
            self.skipTest("Symlink creation not supported for this account")
        with self.assertRaises(ValueError):
            self.catalog.read_document("kapa_skills:README.md")

    def test_allowlisted_hardlink_rejected_before_open(self):
        outside = self.root / "outside-fixture.md"
        outside.write_text("synthetic outside content", encoding="utf-8")
        linked = self.root / "kapa_skills/laws/linked/source.md"
        linked.parent.mkdir()
        os.link(outside, linked)
        self.assertEqual(linked.stat().st_nlink, 2)
        with patch.object(Path, "open", side_effect=AssertionError("hardlink must not be opened")):
            with self.assertRaisesRegex(ValueError, "Hardlink"):
                self.catalog.read_document("kapa_skills:laws/linked/source.md")

    def test_opened_file_hardlink_count_rechecked_before_read(self):
        real_fstat = os.fstat
        def changed(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2,
                                   st_dev=info.st_dev, st_ino=info.st_ino)
        with patch("kapa_mcp.readers.os.fstat", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "hardlinked"):
                self.catalog.read_document("kapa_skills:laws/lca/lca-land/source.md")

    def test_empty_directories_consume_enumeration_budget(self):
        for i in range(12):
            (self.root / "kapa_skills/laws" / ("empty-" + str(i))).mkdir()
        with patch("kapa_mcp.readers.MAX_FILES", 8), patch.object(Path, "open", side_effect=AssertionError("enumeration must not read content")):
            with self.assertRaisesRegex(ValueError, "enumeration limit"):
                list(self.catalog._files("kapa_skills"))

    def test_empty_question_rounds_consume_budget_even_without_hits(self):
        for i in range(10, 20):
            (self.root / "kapa_edu_1/civil_law" / str(i)).mkdir()
        with patch("kapa_mcp.readers.MAX_FILES", 8):
            with self.assertRaisesRegex(ValueError, "enumeration limit"):
                self.catalog.search_questions("no-query-hit", exam=1)

    def test_frontmatter_rejects_duplicate_and_unsafe_yaml(self):
        for text in ('---\nid: 1\nid: 2\n---\n', '---\nid: &alias\n---\n', '---\nid: {"a": 1, "a": 2}\n---\n'):
            with self.assertRaises(ValueError):
                _metadata(text)

    def relation_fixture(self):
        payload = {"PrecService": {"내용": "인용"}}
        digest = hashlib.sha256((json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest()
        evidence = {"source": "https://example.test/source", "quote": "인용"}
        relation = {"src": "case/대법원/1두1", "dst": "issue/unpaid-land", "scope": "",
                    "type": "addresses", "review": "confirmed", "reviewed_by": "fixture",
                    "reviewed_at": "2026-11-09T00:00:00+00:00", "evidence": evidence}
        relation["id"] = _fixture_relation_id(relation)
        record = {"id": "review-1", "target": relation["id"], "decision": "accept", "relation_review": "confirmed",
                  "reviewer_model": "fixture", "reviewed_at": relation["reviewed_at"], "evidence": evidence,
                  "source_sha256": digest}
        self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation, ensure_ascii=False) + "\n")
        self.write("kapa_knowledge", "cases/대법원/2026/1두1.md", '---\nid: "case/대법원/1두1"\nprecSeq: "1"\nsource_url: "https://example.test/source"\nsource_sha256: "' + digest + '"\n---\n인용')
        self.write("kapa_knowledge", "sources/prec/1.json", json.dumps({"payload": payload, "target": "prec", "source_id": "1", "source_url": evidence["source"], "sha256": digest}, ensure_ascii=False))
        self.write("kapa_knowledge", "reviews/2026/check.jsonl", json.dumps(record, ensure_ascii=False) + "\n")
        return relation, record

    def test_relations_filters_evidence_and_digest(self):
        relation, record = self.relation_fixture()
        result = self.catalog.get_relations("issue/unpaid-land", review="confirmed")
        self.assertEqual(result["items"][0]["evidence"], relation["evidence"])
        self.assertTrue(result["items"][0]["review_verified"])
        self.assertEqual(self.catalog.get_relations("issue/unpaid-land", review="unreviewed")["total"], 0)
        record["source_sha256"] = "d" * 64
        self.write("kapa_knowledge", "reviews/2026/check.jsonl", json.dumps(record))
        item = self.catalog.get_relations("issue/unpaid-land")["items"][0]
        self.assertFalse(item["review_verified"])
        self.assertTrue(item["warnings"])

    def test_single_relation_mutations_cannot_reuse_approval(self):
        for field, value in (("dst", "issue/other"), ("type", "cites"),
                             ("src", "case/대법원/2두2"), ("scope", "다른 적용 범위")):
            relation, record = self.relation_fixture()
            original_id = relation["id"]
            relation[field] = value
            record["review_path"] = "reviews/2026/check.jsonl"
            documents = {relation["src"]: {"id": relation["src"], "source_sha256": record["source_sha256"]}}
            self.assertEqual(relation["id"], original_id)
            self.assertFalse(Catalog._valid_review(record, relation, documents))
            self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation))
            with self.subTest(field=field), patch.object(self.catalog, "_verified_source", side_effect=AssertionError("Reject mutated identity before source verification")):
                with self.assertRaisesRegex(ValueError, "immutable fields"):
                    self.catalog.get_relations(relation["dst"], review="confirmed")

    def test_relation_identity_uses_upstream_compact_utf8_and_exact_scope(self):
        known = {"id": "rel-41c816d01cbd55c544e3", "type": "cites", "src": "case/대법원/2008두22129",
                 "dst": "law/009295@289333#a40", "scope": ""}
        self.assertTrue(_relation_identity_matches(known))
        relation, record = self.relation_fixture()
        relation["scope"] = " 한글 범위와 공백 "
        relation["id"] = _fixture_relation_id(relation)
        record["target"] = relation["id"]
        self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation))
        self.write("kapa_knowledge", "reviews/2026/check.jsonl", json.dumps(record))
        item = self.catalog.get_relations("issue/unpaid-land", review="confirmed")["items"][0]
        self.assertTrue(item["review_verified"])
        relation["scope"] = relation["scope"].strip()
        self.assertFalse(_relation_identity_matches(relation))
        del relation["scope"]
        self.assertFalse(_relation_identity_matches(relation))

    def test_relation_rows_are_charged_before_parsing_or_retaining(self):
        rows = []
        for number in range(30):
            relation = {"type": "cites", "src": "case/대법원/1두1", "dst": f"issue/item-{number}", "scope": ""}
            relation["id"] = _fixture_relation_id(relation)
            rows.append(json.dumps(relation))
        self.write("kapa_knowledge", "relations/relations.jsonl", "\n".join(rows))
        with patch("kapa_mcp.readers.MAX_REQUEST_ENTRIES", 3), patch("kapa_mcp.readers._json", wraps=_json) as parse:
            with self.assertRaises(RequestBudgetExceeded):
                self.catalog.get_relations("issue/absent")
        self.assertEqual(parse.call_count, 3, "The fourth row must fail before JSON parsing")

    def test_short_review_rows_share_budget_across_files_before_parsing(self):
        layouts = (("reviews/2026/check.jsonl",), ("reviews/2026/first.jsonl", "reviews/2026/second.jsonl"))
        for paths in layouts:
            self.relation_fixture()
            for index, path in enumerate(paths):
                self.write("kapa_knowledge", path, "{}\n" * (3 if len(paths) == 2 and index == 0 else 2000000))
            with self.subTest(paths=paths), patch.object(self.catalog, "_files", return_value=iter(paths)), patch("kapa_mcp.readers.MAX_REQUEST_ENTRIES", 5), patch("kapa_mcp.readers._json", wraps=_json) as parse, patch.object(self.catalog, "_verified_source", side_effect=AssertionError("Do not verify a partial ledger")):
                with self.assertRaises(RequestBudgetExceeded):
                    self.catalog.get_relations("issue/unpaid-land", review="confirmed")
            self.assertEqual(parse.call_count, 5, "One relation plus four review records, never the next record")

    def test_jsonl_blank_rows_and_deadline_are_checked_before_parse(self):
        class Probe(Catalog):
            @_bounded
            def probe(self):
                return {"records": list(_jsonl("\n\n{}\n\n{}\n"))}
        with patch("kapa_mcp.readers.MAX_REQUEST_ENTRIES", 3), patch("kapa_mcp.readers._json", wraps=_json) as parse:
            with self.assertRaises(RequestBudgetExceeded):
                Probe(self.catalog.settings).probe()
        self.assertEqual(parse.call_count, 1)
        clock = [0.0]
        def slow_parse(text):
            clock[0] = 2.0
            return _json(text)
        with patch("kapa_mcp.readers.REQUEST_TIMEOUT", 1), patch("kapa_mcp.readers.time.monotonic", side_effect=lambda: clock[0]), patch("kapa_mcp.readers._json", side_effect=slow_parse) as parse:
            with self.assertRaisesRegex(RequestBudgetExceeded, "deadline"):
                Probe(self.catalog.settings).probe()
        self.assertEqual(parse.call_count, 1, "Deadline expiry must stop before parsing the next row")

    def test_global_duplicate_relation_ids_rejected_before_selection_or_reviews(self):
        for changes in ({"dst": "issue/other"}, {"type": "cites"}, {"dst": "issue/other", "type": "cites"}, {}):
            relation, _ = self.relation_fixture()
            duplicate = {**relation, **changes}
            self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation) + "\n" + json.dumps(duplicate) + "\n")
            for material, review in (("issue/unpaid-land", "confirmed"), ("issue/other", "confirmed"), ("issue/absent", "rejected")):
                with self.subTest(changes=changes, material=material), patch.object(self.catalog, "_files", side_effect=AssertionError("Must reject before scanning review files")), patch.object(Catalog, "_valid_review", side_effect=AssertionError("Must not attach any approval")):
                    with self.assertRaisesRegex(ValueError, "Duplicate relation ID"):
                        self.catalog.get_relations(material, review=review)

    def test_duplicate_material_ids_rejected_globally_for_types_and_scan_orders(self):
        layouts = (
            ("cases/대법원/2026/1두1.md", "cases/대법원/2026/0-shadow.md", "case/대법원/1두1"),
            ("cases/대법원/2026/1두1.md", "cases/대법원/2026/z-shadow.md", "case/대법원/1두1"),
            ("interpretations/moleg/2026/original.md", "interpretations/molit/2026/shadow.md", "interp/moleg/안건-1"),
            ("cases/대법원/2026/1두1.md", "interpretations/moleg/2026/shadow.md", "case/대법원/1두1"),
        )
        for original, shadow, material_id in layouts:
            relation, record = self.relation_fixture()
            fixture = self.root / "kapa_knowledge/cases/대법원/2026/1두1.md"
            content = fixture.read_text(encoding="utf-8")
            fixture.unlink()
            content = content.replace('id: "case/대법원/1두1"', 'id: ' + json.dumps(material_id, ensure_ascii=False))
            if original.startswith("interpretations/"):
                content = content.replace("precSeq:", "expcSeq:")
            self.write("kapa_knowledge", original, content)
            # The shadow has a different source identity and digest; it must
            # neither replace a valid source nor inherit that source's approval.
            shadow_content = content.replace('"https://example.test/source"', '"https://example.test/other-source"')
            shadow_content = shadow_content.replace('Seq: "1"', 'Seq: "2"')
            shadow_content = shadow_content.replace(record["source_sha256"], "d" * 64)
            if shadow.startswith("interpretations/"):
                shadow_content = shadow_content.replace("precSeq:", "expcSeq:")
            self.write("kapa_knowledge", shadow, shadow_content)
            relation["src"] = material_id
            relation["id"] = _fixture_relation_id(relation)
            record["target"] = relation["id"]
            self.write("kapa_knowledge", "reviews/2026/check.jsonl", json.dumps(record))
            self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation))
            paths = list(self.catalog._files("kapa_knowledge"))
            try:
                for reverse in (False, True):
                    for requested, review in (("issue/unpaid-land", "confirmed"), ("issue/absent", "rejected")):
                        ordered = sorted(paths, reverse=reverse)
                        with self.subTest(original=original, shadow=shadow, reverse=reverse, requested=requested), patch.object(self.catalog, "_files", return_value=iter(ordered)), patch.object(self.catalog, "_verified_source", side_effect=AssertionError("Must reject before source verification")), patch.object(Catalog, "_valid_review", side_effect=AssertionError("Must reject before approval verification")):
                            error = "Invalid material ID" if original.split("/")[0] != shadow.split("/")[0] else "Duplicate material ID"
                            with self.assertRaisesRegex(ValueError, error):
                                self.catalog.get_relations(requested, review=review)
            finally:
                (self.root / "kapa_knowledge" / original).unlink()
                (self.root / "kapa_knowledge" / shadow).unlink()

    def test_material_namespace_must_match_document_path_before_verification(self):
        layouts = (
            ("cases/대법원/2026/1두1.md", (" case/대법원/1두1", "\tcase/대법원/1두1", "interp/moleg/1", "skill/topic/1", "CASE/대법원/1두1", "case//1", "case/대법원/1두1 ")),
            ("interpretations/moleg/2026/interp.md", ("case/대법원/1두1", " interp/moleg/1", "law/moleg/1", "interp//1", "interp/moleg/1/extra")),
        )
        for path, identifiers in layouts:
            for identifier in identifiers:
                relation, record = self.relation_fixture()
                content = (self.root / "kapa_knowledge/cases/대법원/2026/1두1.md").read_text(encoding="utf-8")
                content = content.replace('id: "case/대법원/1두1"', 'id: ' + json.dumps(identifier, ensure_ascii=False))
                self.write("kapa_knowledge", path, content)
                relation["src"] = identifier
                relation["id"] = _fixture_relation_id(relation)
                record["target"] = relation["id"]
                record["source_sha256"] = "d" * 64
                self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation))
                self.write("kapa_knowledge", "reviews/2026/check.jsonl", json.dumps(record))
                try:
                    with self.subTest(path=path, identifier=identifier), patch.object(self.catalog, "_verified_source", side_effect=AssertionError("Must reject before source verification")), patch.object(Catalog, "_valid_review", side_effect=AssertionError("Must reject before approval verification")):
                        with self.assertRaisesRegex(ValueError, "Invalid material ID"):
                            self.catalog.get_relations("issue/unpaid-land", review="confirmed")
                finally:
                    if path.startswith("interpretations/"):
                        (self.root / "kapa_knowledge" / path).unlink()

    def test_review_digest_always_binds_resolved_metadata_without_prefix_heuristic(self):
        relation, record = self.relation_fixture()
        record["review_path"] = "reviews/2026/check.jsonl"
        digest = record["source_sha256"]
        self.assertTrue(Catalog._valid_review(record, relation, {relation["src"]: {"id": relation["src"], "source_sha256": digest}}))
        for identifier in (" case/대법원/1두1", "\tcase/대법원/1두1", "material/1", "skill/topic/1", "case/대법원/1두1"):
            relation["src"] = identifier
            relation["id"] = _fixture_relation_id(relation)
            record["target"] = relation["id"]
            record["source_sha256"] = "d" * 64
            with self.subTest(identifier=identifier):
                self.assertFalse(Catalog._valid_review(record, relation, {identifier: {"id": identifier, "source_sha256": digest}}))

    def test_review_rejects_bad_evidence_time_and_year(self):
        relation, record = self.relation_fixture()
        for key, value in (("reviewed_at", "2026-11-09"), ("review_path", "reviews/2025/check.jsonl"),
                           ("decision", "reject"), ("evidence", {"source": "https://user:pass@example.test/", "quote": "인용"})):
            row = {**record, "review_path": "reviews/2026/check.jsonl", key: value}
            self.assertFalse(Catalog._valid_review(row, relation, {relation["src"]: {"source_sha256": record["source_sha256"]}}))

    def test_source_tamper_invalidates_review(self):
        self.relation_fixture()
        self.write("kapa_knowledge", "sources/prec/1.json", '{"payload": {"changed": true}}')
        item = self.catalog.get_relations("issue/unpaid-land")["items"][0]
        self.assertFalse(item["source_verified"])
        self.assertFalse(item["review_verified"])

    def test_matching_review_evidence_must_match_source_url_and_quote(self):
        for evidence in ({"source": "https://other.test/source", "quote": "인용"},
                         {"source": "https://example.test/source", "quote": "원문에 없는 인용"}):
            with self.subTest(evidence=evidence):
                relation, record = self.relation_fixture()
                relation["evidence"] = record["evidence"] = evidence
                self.write("kapa_knowledge", "relations/relations.jsonl", json.dumps(relation))
                self.write("kapa_knowledge", "reviews/2026/check.jsonl", json.dumps(record))
                item = self.catalog.get_relations("issue/unpaid-land", review="confirmed")["items"][0]
                self.assertTrue(item["source_verified"])
                self.assertFalse(item["review_verified"])
                self.assertTrue(item["warnings"])

    def test_quote_must_be_inside_one_payload_text_value(self):
        source = {"source_url": "https://example.test/source", "payload": {"인용": ["앞", "뒤"]}, "outside_payload": "앞뒤"}
        for quote in ("앞뒤", "인용", "outside_payload"):
            self.assertFalse(Catalog._source_evidence_matches(source, {"source": source["source_url"], "quote": quote}))
        source["payload"]["nested"] = [{"text": "정확한 인용\n본문"}]
        self.assertTrue(Catalog._source_evidence_matches(source, {"source": source["source_url"], "quote": "인용\n본문"}))

    def test_absolute_paths_and_oversized_inputs_not_exposed(self):
        self.write("kapa_skills", "laws/lca/lca-land/source.md", "private C:/Users/fixture/data")
        with self.assertRaisesRegex(ValueError, "withheld"):
            self.catalog.read_document("kapa_skills:laws/lca/lca-land/source.md")
        with self.assertRaisesRegex(ValueError, "input"):
            self.catalog.route_task("가" * 4097)

    def test_content_blocks_configured_posix_roots_and_unc_without_blocking_api_paths(self):
        class Echo(Catalog):
            @_bounded
            def echo(self, value, as_key=False):
                return {"items": [{value: "fixture"} if as_key else {"content": value}]}
        catalog = object.__new__(Echo)
        catalog.workspace = PurePosixPath("/workspace/account/kapa")
        catalog.settings = SimpleNamespace(workspace=catalog.workspace, jobs_dir=PurePosixPath("/root/project"))
        forbidden = ("/workspace/account/kapa", "source /workspace/account/kapa/laws/source.md",
                     "/root/project", "job (/root/project/output.pdf)", "saved /root/project.",
                     r"\\server\share\file.md", "//server/share/file.md", r'json {"path":"\\\\server\\share\\file"}',
                     "file:///workspace/account/kapa/source.md", "C:/Users/fixture/data")
        for value in forbidden:
            for as_key in (False, True):
                with self.subTest(value=value, as_key=as_key), self.assertRaisesRegex(ValueError, "withheld"):
                    catalog.echo(value, as_key=as_key)
        allowed = ("/api/v1/questions/01", "/wiki/평가/절", "/docs/README.md", "laws/lca/source.md",
                   "/workspace/account/kapa-public/README.md", "/root/project-archive/README.md",
                   "prefix/workspace/account/kapa/file.md", "/wiki/root/project",
                   "https://example.test/workspace/account/kapa/source.md", "https://example.test/root/project",
                   "https://example.test/home/manual", r"\begin{question}{1} text \\ \hline")
        for value in allowed:
            with self.subTest(value=value):
                self.assertEqual(catalog.echo(value)["items"][0]["content"], value)


def _git_fixture(root, files, fmt="sha1", assume_unchanged=()):
    """Write tiny synthetic objects/index directly; no git commit or upstream writes."""
    metadata = root / ".git"
    (metadata / "objects").mkdir(parents=True, exist_ok=True)
    (metadata / "refs/heads").mkdir(parents=True, exist_ok=True)
    config = "[core]\nrepositoryformatversion = " + ("1" if fmt == "sha256" else "0") + "\nbare = false\nautocrlf = false\n"
    if fmt == "sha256":
        config += "[extensions]\nobjectFormat = sha256\n"
    (metadata / "config").write_text(config, encoding="ascii")
    def object_(kind, data):
        body = kind.encode() + b" " + str(len(data)).encode() + b"\0" + data
        oid = hashlib.new(fmt, body).hexdigest()
        location = metadata / "objects" / oid[:2] / oid[2:]
        location.parent.mkdir(exist_ok=True)
        location.write_bytes(zlib.compress(body))
        return oid
    hierarchy, entries = {}, []
    for name, value in sorted(files.items()):
        data = value.encode("utf-8") if isinstance(value, str) else value
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        oid = object_("blob", data)
        node = hierarchy
        pieces = name.split("/")
        for part in pieces[:-1]:
            node = node.setdefault(part, {})
        node[pieces[-1]] = oid
        info = path.stat()
        numbers = (int(info.st_ctime), info.st_ctime_ns % 10**9, int(info.st_mtime), info.st_mtime_ns % 10**9,
                   info.st_dev, info.st_ino, 0o100644, 0, 0, len(data))
        encoded = name.encode("utf-8")
        flags = min(len(encoded), 0xfff) | (0x8000 if name in assume_unchanged else 0)
        entry = struct.pack("!10I", *(n & 0xffffffff for n in numbers)) + bytes.fromhex(oid) + struct.pack("!H", flags) + encoded + b"\0"
        entries.append(entry + b"\0" * ((-len(entry)) % 8))
    def tree(node):
        output = bytearray()
        for name, value in sorted(node.items(), key=lambda pair: (pair[0] + ("/" if isinstance(pair[1], dict) else "")).encode()):
            directory = isinstance(value, dict)
            oid = tree(value) if directory else value
            output.extend((b"40000" if directory else b"100644") + b" " + name.encode() + b"\0" + bytes.fromhex(oid))
        return object_("tree", bytes(output))
    tree_id = tree(hierarchy)
    commit = object_("commit", (f"tree {tree_id}\nauthor Fixture <fixture@example.invalid> 0 +0000\ncommitter Fixture <fixture@example.invalid> 0 +0000\n\nsynthetic fixture\n").encode())
    (metadata / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (metadata / "refs/heads/main").write_text(commit + "\n", encoding="ascii")
    index = b"DIRC" + struct.pack("!II", 2, len(entries)) + b"".join(entries)
    (metadata / "index").write_bytes(index + hashlib.new(fmt, index).digest())
    return commit


class SupplementalReaderTests(unittest.TestCase):
    setUp = ReaderTests.setUp
    write = ReaderTests.write
    def test_identity_hold_preserves_distinct_assertions_without_formal_identity(self):
        research = '{"items": [{"item": "reply-1730"}]}'
        self.write("kapa_knowledge", "runs/pilot/held-items-research.json", research)
        record = {"id": "identity-hold/molit/reply-1730", "kind": "identity-hold",
                  "identity_status": "unresolved", "publication_hold": True,
                  "assertions": [{"source_id": "101", "date": "2020-01-01"},
                                 {"source_id": "202", "date": "2020-02-01"}],
                  "evidence_file": "runs/pilot/held-items-research.json",
                  "evidence_sha256": hashlib.sha256(research.encode()).hexdigest()}
        self.write("kapa_knowledge", "catalogs/materials.json", json.dumps({
            "schema": 1, "catalog_type": "unpublished-evidence", "records": [record]}))
        result = self.catalog.read_material(record["id"])
        self.assertEqual(result["record"]["assertions"], record["assertions"])
        self.assertFalse(result["formal_interpretation"])
        self.assertFalse(result["source_verified"])
        self.assertFalse(result["review_verified"])
        self.assertTrue(result["missing_items"])

    def test_vendor_allowlist_excludes_statute_bodies_and_arbitrary_research(self):
        for relative in ("vendor/kapa_skills/laws/topic/source.md",
                         "vendor/kapa_skills/README.md", "runs/pilot/arbitrary.json",
                         "catalogs/arbitrary.json"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.catalog.read_document("kapa_knowledge:" + relative)


class GitStateTests(unittest.TestCase):
    def test_head_audit_shares_aggregate_budget_and_never_claims_unseen_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skills = root / "kapa_skills"
            _git_fixture(skills, {"README.md": "a" * 1024, "laws/topic/source.md": "b" * 1024})
            catalog = Catalog(Settings(root, root / "jobs"))
            real_open, opened = Path.open, []
            def observe(path, *args, **kwargs):
                opened.append(path.relative_to(skills).as_posix())
                return real_open(path, *args, **kwargs)
            with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", 1536), patch.object(Path, "open", observe):
                with self.assertRaisesRegex(RequestBudgetExceeded, "read-byte"):
                    catalog._git_state("kapa_skills")
            self.assertEqual(opened, ["README.md"])
            # A small requested document still cannot claim a clean full audit
            # after the unvisited checkout exhausts the shared budget.
            with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", 1536):
                with self.assertRaises(RequestBudgetExceeded):
                    catalog.read_document("kapa_skills:README.md")

    def test_deadline_checked_after_git_query_and_before_cached_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "kapa_skills/.git").mkdir(parents=True)
            (root / "kapa_skills/README.md").write_text("fixture", encoding="utf-8")
            settings = Settings(root, root / "jobs")
            clock = [0.0]
            def slow_git(*args):
                clock[0] = 2.0
                return b"sha1\n"
            with patch("kapa_mcp.readers.REQUEST_TIMEOUT", 1), patch("kapa_mcp.readers.time.monotonic", side_effect=lambda: clock[0]), patch("kapa_mcp.readers._capture_git", side_effect=slow_git) as capture:
                with self.assertRaisesRegex(RequestBudgetExceeded, "deadline"):
                    Catalog(settings)._git_state("kapa_skills")
            self.assertEqual(capture.call_count, 1)
            clock[0] = 0.0
            class Probe(Catalog):
                @_bounded
                def probe(self):
                    state = self._git_state("kapa_skills")
                    if not state["content_head_verified"]:
                        raise AssertionError("Fixture must complete its first audit")
                    clock[0] = 2.0
                    return self._git_state("kapa_skills")
            oid = hashlib.sha1(b"blob 7\0fixture").hexdigest()
            replies = [b"sha1\n", ("a" * 40).encode(), ("100644 blob " + oid + "\tREADME.md\0").encode(), b""]
            with patch("kapa_mcp.readers.REQUEST_TIMEOUT", 1), patch("kapa_mcp.readers.time.monotonic", side_effect=lambda: clock[0]), patch("kapa_mcp.readers._capture_git", side_effect=replies) as capture:
                with self.assertRaisesRegex(RequestBudgetExceeded, "deadline"):
                    Probe(settings).probe()
            self.assertEqual(capture.call_count, 4, "Cached-state lookup must stop without a fifth process")

    def test_head_blobs_detect_assume_unchanged_readme_and_dirty_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skills, knowledge = root / "kapa_skills", root / "kapa_knowledge"
            revision = _git_fixture(skills, {"README.md": "original", "docs/law-index.json": "{}"},
                                    assume_unchanged=("README.md",))
            pin = json.dumps({"kapa_skills": {"commit": revision}})
            _git_fixture(knowledge, {"deps.lock": pin}, assume_unchanged=("deps.lock",))
            catalog = Catalog(Settings(root, root / "jobs"))
            self.assertTrue(catalog._dependency()["resolved"])
            status_outputs = []
            def capture(argv, env):
                output = _capture_git(argv, env)
                if "status" in argv:
                    status_outputs.append(output)
                return output
            (skills / "README.md").write_text("modified", encoding="utf-8")
            with patch("kapa_mcp.readers._capture_git", side_effect=capture):
                document = catalog.read_document("kapa_skills:README.md")
            self.assertEqual(status_outputs, [b""])
            self.assertEqual(document["content"], "modified")
            self.assertTrue(document["dirty"])
            self.assertFalse(document["head_verified"])
            self.assertFalse(catalog._dependency()["resolved"])
            (skills / "README.md").write_text("original", encoding="utf-8")
            (knowledge / "deps.lock").write_text(pin + "\n", encoding="utf-8")
            status_outputs.clear()
            with patch("kapa_mcp.readers._capture_git", side_effect=capture):
                dependency = catalog._dependency()
            self.assertTrue(all(output == b"" for output in status_outputs))
            self.assertFalse(dependency["resolved"])
            self.assertFalse(dependency["lock_provenance_checked"])

    def test_sha256_manifest_and_raw_crlf_blob_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skills = root / "kapa_skills"
            revision = _git_fixture(skills, {"README.md": b"line\n"}, fmt="sha256")
            catalog = Catalog(Settings(root, root / "jobs"))
            first = catalog.read_document("kapa_skills:README.md")
            self.assertEqual(first["revision"], revision)
            self.assertEqual(len(revision), 64)
            self.assertTrue(first["head_verified"])
            self.assertFalse(first["dirty"])
            (skills / "README.md").write_bytes(b"line\r\n")
            changed = catalog.read_document("kapa_skills:README.md")
            self.assertEqual(changed["content"], first["content"])
            self.assertFalse(changed["head_verified"])
            self.assertTrue(changed["dirty"])

    def test_manifest_is_reused_for_thousand_file_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            files = {f"laws/topic-{i:04}/source.md": "fixture source" for i in range(1000)}
            revision = _git_fixture(root / "kapa_skills", files)
            _git_fixture(root / "kapa_knowledge", {"deps.lock": json.dumps({"kapa_skills": {"commit": revision}})})
            catalog = Catalog(Settings(root, root / "jobs"))
            with patch("kapa_mcp.readers._capture_git", wraps=_capture_git) as capture:
                result = catalog.search_knowledge("fixture", repo="skills", limit=1)
            self.assertEqual(result["total"], 1000)
            self.assertTrue(result["items"][0]["head_verified"])
            self.assertTrue(result["dependency"]["resolved"])
            self.assertEqual(capture.call_count, 8)  # Four fixed queries per repo, never per file.
            self.assertEqual(sum("ls-tree" in call.args[0] for call in capture.call_args_list), 2)

    def test_bytes_consumed_before_snapshot_cannot_gain_clean_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skills = root / "kapa_skills"
            _git_fixture(skills, {"README.md": "original"})
            path = skills / "README.md"
            path.write_text("modified", encoding="utf-8")
            class RevertedDuringRead(Catalog):
                def _git_state(self, repo):
                    path.write_text("original", encoding="utf-8")
                    return super()._git_state(repo)
            result = RevertedDuringRead(Settings(root, root / "jobs")).read_document("kapa_skills:README.md")
            self.assertEqual(result["content"], "modified")
            self.assertFalse(result["head_verified"])
            self.assertTrue(result["dirty"])

    def test_unknown_object_format_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "kapa_skills/.git").mkdir(parents=True)
            with patch("kapa_mcp.readers._capture_git", return_value=b"unknown-format\n"):
                state = Catalog(Settings(root, root / "jobs"))._git_state("kapa_skills")
            self.assertIsNone(state["dirty"])
            self.assertFalse(state["content_head_verified"])
            self.assertTrue(any("unsupported Git object format" in warning for warning in state["warnings"]))

    def test_ignored_allowlisted_content_invalidates_clean_provenance_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skills = root / "kapa_skills"
            revision = _git_fixture(skills, {".gitignore": "laws/local/\ntools/\n", "README.md": "fixture"})
            (skills / "tools").mkdir()
            arbitrary = skills / "tools/private-fixture.txt"
            arbitrary.write_text("synthetic ignored arbitrary content", encoding="utf-8")
            knowledge = root / "kapa_knowledge"
            _git_fixture(knowledge, {"deps.lock": json.dumps({"kapa_skills": {"commit": revision}})})
            catalog = Catalog(Settings(root, root / "jobs"))
            real_open = Path.open
            def safe_open(path, *args, **kwargs):
                self.assertNotEqual(path, arbitrary, "Ignored arbitrary files must not be read")
                return real_open(path, *args, **kwargs)
            with patch.object(Path, "open", safe_open):
                self.assertFalse(catalog._git_state("kapa_skills")["dirty"])
                self.assertTrue(catalog._dependency()["resolved"])
                pin = (knowledge / "deps.lock").read_text(encoding="utf-8")
                _git_fixture(knowledge, {".gitignore": "deps.lock\n"})
                (knowledge / "deps.lock").write_text(pin, encoding="utf-8")
                dependency = catalog._dependency()
                self.assertFalse(dependency["resolved"])
                self.assertFalse(dependency["lock_provenance_checked"])
                _git_fixture(knowledge, {"deps.lock": pin})
                (knowledge / ".gitignore").unlink()
                local = skills / "laws/local/source.md"
                local.parent.mkdir(parents=True)
                local.write_text("ignored allowlisted source", encoding="utf-8")
                document = catalog.read_document("kapa_skills:laws/local/source.md")
                self.assertTrue(document["dirty"])
                self.assertFalse(document["head_verified"])
                self.assertTrue(any("head_content" in w for w in document["warnings"]))
                self.assertFalse(catalog._dependency()["resolved"])

    def test_fixed_local_git_queries_and_sanitized_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kapa_skills/.git").mkdir(parents=True)
            catalog = Catalog(Settings(root, root / "jobs"))
            replies = [b"sha1\n", ("a" * 40 + "\n").encode(), b"", b" M README.md\n"]
            with patch.dict(os.environ, {"LAW_OC": "fixture-sensitive", "GIT_CONFIG_COUNT": "1", "GH_TOKEN": "fixture-sensitive"}), patch("kapa_mcp.readers._capture_git", side_effect=replies) as run:
                state = catalog._git_state("kapa_skills")
            self.assertEqual(state["revision"], "a" * 40)
            self.assertTrue(state["dirty"])
            for call in run.call_args_list:
                argv, env = call.args
                self.assertIn("core.fsmonitor=false", argv)
                self.assertIn("core.untrackedCache=false", argv)
                self.assertIn("protocol.allow=never", argv)
                self.assertIn("--no-replace-objects", argv)
                self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")
                self.assertNotIn("LAW_OC", env)
                self.assertNotIn("GH_TOKEN", env)
                self.assertNotIn("GIT_CONFIG_COUNT", env)
                self.assertEqual(argv[0], "git")
            self.assertNotIn(tmp, json.dumps(state))

    def test_git_error_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kapa_skills/.git").mkdir(parents=True)
            catalog = Catalog(Settings(root, root / "jobs"))
            with patch("kapa_mcp.readers._capture_git", side_effect=OSError("sensitive diagnostic " + tmp)):
                result = catalog._git_state("kapa_skills")
            self.assertIsNone(result["revision"])
            self.assertIsNone(result["dirty"])
            self.assertNotIn(tmp, json.dumps(result))


class GitCaptureTests(unittest.TestCase):
    def test_stdout_cap_is_enforced_while_child_is_running_and_child_reaped(self):
        children = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        command = [sys.executable, "-I", "-B", "-c", "import sys,time; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush(); time.sleep(30)"]
        start = time.monotonic()
        with patch("kapa_mcp.readers.MAX_BYTES", 4096), patch("kapa_mcp.readers.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(ValueError, "stdout exceeds"):
                _capture_git(command, dict(os.environ))
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_timeout_covers_no_output_child_and_reaps_it(self):
        children = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        start = time.monotonic()
        with patch("kapa_mcp.readers.GIT_TIMEOUT", 0.15), patch("kapa_mcp.readers.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(ValueError, "timed out"):
                _capture_git([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], dict(os.environ))
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_exact_capture_limit_is_allowed(self):
        with patch("kapa_mcp.readers.MAX_BYTES", 4096):
            output = _capture_git([sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.buffer.write(b'x'*4096)"], dict(os.environ))
        self.assertEqual(output, b"x" * 4096)


if __name__ == "__main__":
    unittest.main()
