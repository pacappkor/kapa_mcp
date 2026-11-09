# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic immutable-source workflows; no production data or credentials."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from kapa_mcp.config import Settings
from kapa_mcp.readers import Catalog, RequestBudgetExceeded
from kapa_mcp.workflow import WorkflowManager
from test_readers import _git_fixture


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.skills = self.root / "kapa_skills"
        self.pin = _git_fixture(self.skills, {"README.md": "skills fixture"})
        self.knowledge = self.root / "kapa_knowledge"
        self.version_path = "sources/laws/topic/versions/act.json"
        self.index = {"schema": 1, "laws": {"009295": {"versions_file": self.version_path}}, "articles": {}}
        self.versions = {"lsId": "009295", "versions": [
            {"lsiSeq": "123", "efYd": "20200101"},
            {"lsiSeq": "456", "efYd": "20270101"}]}
        blobs = {"docs/law-index.json": json.dumps(self.index), self.version_path: json.dumps(self.versions)}
        vendor = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in blobs.items()}
        lock = {"kapa_skills": {"commit": self.pin, "vendor": {"commit": self.pin, "files": vendor}}}
        evidence = json.dumps({"items": [{"item": "faq/1", "finding": "pilot only"}]})
        self.faq = {"id": "faq/molit/lawgo/1", "kind": "faq", "material_type": "administrative-faq",
                    "case_no": "", "reference_status": "unreviewed", "publication_hold": True,
                    "progress": "candidate", "title": "stored FAQ", "source_envelope_status": "unavailable",
                    "evidence_file": "runs/pilot/held-items-research.json",
                    "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest()}
        catalog = {"schema": 1, "catalog_type": "unpublished-evidence", "records": [self.faq]}
        self.knowledge_files = {"deps.lock": json.dumps(lock), "issues/topic/README.md": "---\nid: issue/topic\nreference_status: confirmed\n---\nfixture analysis",
                                "catalogs/materials.json": json.dumps(catalog),
                                "runs/pilot/held-items-research.json": evidence,
                                **{"vendor/kapa_skills/" + k: v for k, v in blobs.items()}}
        _git_fixture(self.knowledge, self.knowledge_files)
        self.question = "edu/1/civil_law/37/01"
        self.edu_files = {"subjects.json": json.dumps({"subjects": [{"folder": "civil_law", "name_ko": "Civil", "aliases": []}]}),
                          "civil_law/37/01.tex": "%% kapa: exam=1 round=37 period=1 subject=civil_law seq=1\nquestion fixture\n",
                          "civil_law/37/00.tex": "common fixture",
                          "answers/answers.csv": "round,period,subject,number,answer\n37,1,Civil,1,2\n"}
        _git_fixture(self.root / "kapa_edu_1", self.edu_files)
        self.settings = Settings(self.root, self.root / "jobs")
        self.catalog = Catalog(self.settings)
        self.manager = WorkflowManager(self.settings, self.catalog)
        self.document = "kapa_knowledge:issues/topic/README.md"

    def create(self, **kwargs):
        return self.manager.create_bundle("research fixture", [self.document], [self.question], **kwargs)

    def test_restart_exact_ids_and_metadata_only_with_explicit_legal_gaps(self):
        bundle = self.create(as_of="2026-11-09")
        self.assertEqual(bundle["question_ids"], [self.question])
        self.assertEqual(bundle["document_ids"], [self.document])
        self.assertFalse(bundle["coverage"]["legal_verified"])
        self.assertTrue(bundle["missing_items"])
        restored = WorkflowManager(self.settings, Catalog(self.settings))
        self.assertEqual(restored.get_bundle(bundle["bundle_id"]), bundle)
        self.assertTrue(restored.validate_bundle(bundle["bundle_id"])["valid"])
        stored = (self.settings.jobs_dir / "evidence" / bundle["bundle_id"] / "bundle.json").read_text(encoding="utf-8")
        self.assertNotIn("question fixture", stored)
        self.assertNotIn("fixture analysis", stored)
        self.assertNotIn(str(self.root), stored)

    def test_pin_metadata_survives_different_clean_live_skills_revision(self):
        _git_fixture(self.skills, {"README.md": "new unrelated revision"})
        bundle = self.create(as_of="2026-11-09")
        snapshot = bundle["snapshot"]
        self.assertFalse(snapshot["dependency"]["resolved"])
        self.assertTrue(snapshot["pinned_catalog"]["resolved"])
        self.assertEqual(snapshot["pinned_catalog"]["locked_revision"], self.pin)
        self.assertEqual(snapshot["pinned_catalog"]["date_candidates"][0]["version_ids"], ["law/009295@123"])
        self.assertTrue(self.manager.validate_bundle(bundle["bundle_id"])["valid"])

    def test_every_vendor_hash_is_checked_not_only_index(self):
        target = self.knowledge / "vendor/kapa_skills" / self.version_path
        target.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.catalog.pinned_catalog()

    def test_question_auxiliary_change_invalidates_and_rejects_use(self):
        for relative in ("civil_law/37/00.tex", "answers/answers.csv", "subjects.json", "civil_law/37/01.tex"):
            with self.subTest(relative=relative):
                _git_fixture(self.root / "kapa_edu_1", self.edu_files)
                bundle = self.create()
                path = self.root / "kapa_edu_1" / relative
                with path.open("a", encoding="utf-8") as stream:
                    stream.write("\nchanged fixture")
                validation = self.manager.validate_bundle(bundle["bundle_id"])
                self.assertFalse(validation["valid"])
                self.assertTrue(validation["stale"])
                with self.assertRaises(ValueError):
                    self.manager.get_bundle(bundle["bundle_id"])

    def test_digest_tamper_unknown_ids_and_path_inputs_rejected(self):
        bundle = self.create()
        path = self.manager.root / bundle["bundle_id"] / "bundle.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["task"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.manager.validate_bundle(bundle["bundle_id"])["valid"])
        for identifier in ("../bundle.json", "bundle-" + "a" * 64, str(path)):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.manager.get_bundle(identifier)

    def test_duplicate_selection_invalid_dates_and_resource_limits(self):
        for docs, questions, date in (([self.document] * 2, [], None), ([], "bad", None),
                                      ([], [], "2026-02-30"), ([], [], "20261109")):
            with self.assertRaises(ValueError):
                self.manager.create_bundle("task", docs, questions, date)
        with patch.object(self.manager, "MAX_BUNDLES", 1):
            first = self.create()
            second = self.create()
            self.assertNotEqual(first["bundle_id"], second["bundle_id"])
            self.assertEqual(self.manager.get_bundle(first["bundle_id"]), first)
            self.assertTrue((self.manager.root / "archive" / first["bundle_id"] / "bundle.json").is_file())

    def test_aggregate_budget_aborts_creation_without_artifacts(self):
        with patch("kapa_mcp.readers.MAX_REQUEST_BYTES", 128):
            with self.assertRaises(RequestBudgetExceeded):
                self.create()
        self.assertEqual(list(self.manager.root.iterdir()), [])

    def test_faq_and_historical_metadata_never_become_legal_verification(self):
        faq = self.catalog.read_material(self.faq["id"])
        self.assertFalse(faq["source_verified"])
        self.assertFalse(faq["review_verified"])
        self.assertTrue(faq["publication_hold"])
        historical = self.catalog.read_material("law/009295@123")
        self.assertTrue(historical["catalog_verified"])
        self.assertFalse(historical["applicability_verified"])
        self.assertFalse(historical["article_text_available"])
        with self.assertRaises(ValueError):
            self.catalog.read_material("law/009295@123#a1")
        bundle = self.manager.create_bundle("FAQ research", [self.faq["id"], "law/009295@123"])
        self.assertTrue(self.manager.validate_bundle(bundle["bundle_id"])["valid"])
        self.assertFalse(bundle["coverage"]["legal_verified"])
        results = self.catalog.search_knowledge("stored FAQ", repo="knowledge")
        self.assertEqual(results["items"][0]["material_id"], self.faq["id"])

    def test_faq_hash_and_fixed_research_path_enforced(self):
        path = self.knowledge / "runs/pilot/held-items-research.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.catalog.read_material(self.faq["id"])
        self.faq["evidence_file"] = "README.md"
        (self.knowledge / "catalogs/materials.json").write_text(json.dumps({"schema": 1,
            "catalog_type": "unpublished-evidence", "records": [self.faq]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fixed research allowlist"):
            self.catalog.read_material(self.faq["id"])

    def test_bundle_projects_known_metadata_recursively_and_never_persists_bodies(self):
        assertion = {"source_id": "101", "asserted_replied": "20200101",
                     "provenance": {"source_id": "source", "reply_sha256": "machine",
                                    "unknown": "PROVENANCE_BODY"},
                     "excerpt": "NESTED_EXCERPT", "response": "NESTED_RESPONSE",
                     "full_reply": "NESTED_FULL_REPLY", "quote": "NESTED_QUOTE",
                     "unknown": {"source_id": "UNKNOWN_CONTAINER_BODY"}}
        record = dict(self.faq, assertions=[assertion], excerpt="TOP_EXCERPT",
                      response="TOP_RESPONSE", full_reply="TOP_FULL_REPLY", quote="TOP_QUOTE",
                      unknown={"source_id": "TOP_UNKNOWN_BODY"},
                      source_identifiers={"lawgo": "1", "response": "IDENTIFIER_BODY"},
                      provenance={"id": "machine", "assertions": "machine", "response": "source"})
        files = dict(self.knowledge_files)
        files["catalogs/materials.json"] = json.dumps({"schema": 1,
            "catalog_type": "unpublished-evidence", "records": [record]})
        files["issues/topic/README.md"] = ('---\nid: issue/topic\nresponse: FRONTMATTER_RESPONSE\n'
            'applied_version: {"full_reply":"VERSION_BODY"}\n---\nfixture analysis')
        versions = json.loads(json.dumps(self.versions))
        versions["versions"][0]["full_reply"] = "HISTORICAL_BODY"
        raw_versions = json.dumps(versions)
        files["vendor/kapa_skills/" + self.version_path] = raw_versions
        lock = json.loads(files["deps.lock"])
        lock["kapa_skills"]["vendor"]["files"][self.version_path] = hashlib.sha256(raw_versions.encode()).hexdigest()
        files["deps.lock"] = json.dumps(lock)
        _git_fixture(self.knowledge, files)
        material = self.catalog.read_material(record["id"])
        historical = self.catalog.read_material("law/009295@123")
        bundle = self.manager.create_bundle("metadata fixture", [record["id"], self.document, "law/009295@123"])
        projected = bundle["snapshot"]["selected"][0]["record"]
        self.assertEqual(material["record"], projected)
        self.assertEqual(material["id"], record["id"])
        self.assertEqual(material["record"]["title"], self.faq["title"])
        self.assertEqual(material["record"]["evidence_sha256"], record["evidence_sha256"])
        self.assertEqual(projected["assertions"][0], {
            "source_id": "101", "asserted_replied": "20200101",
            "provenance": {"source_id": "source", "reply_sha256": "machine"}})
        self.assertEqual(projected["source_identifiers"], {"lawgo": "1"})
        self.assertEqual(projected["provenance"], {"id": "machine", "assertions": "machine"})
        stored = (self.manager.root / bundle["bundle_id"] / "bundle.json").read_text(encoding="utf-8")
        for body in ("PROVENANCE_BODY", "NESTED_EXCERPT", "NESTED_RESPONSE", "NESTED_FULL_REPLY",
                     "NESTED_QUOTE", "UNKNOWN_CONTAINER_BODY", "TOP_EXCERPT", "TOP_RESPONSE",
                     "TOP_FULL_REPLY", "TOP_QUOTE", "TOP_UNKNOWN_BODY", "IDENTIFIER_BODY",
                     "FRONTMATTER_RESPONSE", "VERSION_BODY", "HISTORICAL_BODY"):
            for output in (stored, json.dumps(material), json.dumps(historical)):
                self.assertNotIn(body, output)
        self.assertEqual(historical["entries"][0]["lsiSeq"], "123")
        # Owning-repository document reads remain the explicit content route.
        raw_document = self.catalog.read_document("kapa_knowledge:catalogs/materials.json")
        self.assertIn("TOP_FULL_REPLY", raw_document["content"])
        self.assertEqual(self.manager.get_bundle(bundle["bundle_id"]), bundle)

    def test_large_supplemental_search_reuses_request_reads_and_checks_each_expected_hash(self):
        files = dict(self.knowledge_files)
        # The supported maximum with shared research would exceed 64 MiB if
        # either the catalog or research were read once per matching record.
        research = json.dumps({"pilot": "x" * (512 * 1024)})
        research_hash = hashlib.sha256(research.encode()).hexdigest()
        records = [dict(self.faq, id=f"faq/molit/lawgo/{index}", title="broad fixture FAQ",
                        evidence_sha256=research_hash) for index in range(1000)]
        files["runs/pilot/held-items-research.json"] = research
        files["catalogs/materials.json"] = json.dumps({"schema": 1,
            "catalog_type": "unpublished-evidence", "records": records})
        _git_fixture(self.knowledge, files)
        for query in ("", "broad"):
            with self.subTest(query=query), patch.object(self.catalog, "_read", wraps=self.catalog._read) as reads:
                result = self.catalog.search_knowledge(query, repo="knowledge", limit=1)
                self.assertEqual(len(result["items"]), 1)
                self.assertEqual(result["next_offset"], 1)
                self.assertEqual(result["total"], 1001 if not query else 1000)
                for path in ("catalogs/materials.json", "runs/pilot/held-items-research.json"):
                    self.assertEqual(sum(call.args == ("kapa_knowledge", path) for call in reads.call_args_list), 1)
        # Cached bytes do not let a later record's wrong expectation pass.
        records[-1]["evidence_sha256"] = "0" * 64
        files["catalogs/materials.json"] = json.dumps({"schema": 1,
            "catalog_type": "unpublished-evidence", "records": records})
        _git_fixture(self.knowledge, files)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.catalog.search_knowledge("broad", repo="knowledge", limit=1)

        # Nor can any result from an earlier request hide changed research.
        files["runs/pilot/held-items-research.json"] = '{"changed":true}'
        _git_fixture(self.knowledge, files)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.catalog.search_knowledge("broad", repo="knowledge", limit=1)

    def test_retention_preserves_exact_id_restart_lookup_without_archive_enumeration(self):
        with patch.object(WorkflowManager, "MAX_BUNDLES", 1):
            first = self.manager.create_bundle("first", [self.document])
            original = (self.manager.root / first["bundle_id"] / "bundle.json").read_bytes()
            for index in range(3):
                self.manager.create_bundle(f"later {index}", [self.document])
            archive = self.manager.root / "archive"
            self.assertEqual((archive / first["bundle_id"] / "bundle.json").read_bytes(), original)
            restored = WorkflowManager(self.settings, Catalog(self.settings))
            real_scandir = os.scandir
            def no_archive_scan(path):
                self.assertNotEqual(Path(path), archive)
                return real_scandir(path)
            with patch("kapa_mcp.workflow.os.scandir", side_effect=no_archive_scan):
                self.assertEqual(restored.get_bundle(first["bundle_id"]), first)
                self.assertTrue(restored.validate_bundle(first["bundle_id"])["valid"])
                self.assertFalse(restored.validate_bundle("bundle-" + "a" * 64)["valid"])
            # Archived metadata still requires current source identity.
            (self.knowledge / "issues/topic/README.md").write_text("changed", encoding="utf-8")
            self.assertFalse(restored.validate_bundle(first["bundle_id"])["valid"])
            with self.assertRaises(ValueError):
                restored.get_bundle(first["bundle_id"])

    def test_retention_serializes_root_clients_and_never_overwrites_archive(self):
        with patch.object(WorkflowManager, "MAX_BUNDLES", 2):
            clients = [WorkflowManager(self.settings, Catalog(self.settings)) for _ in range(3)]
            with ThreadPoolExecutor(max_workers=3) as pool:
                bundles = list(pool.map(lambda index: clients[index % 3].create_bundle(
                    f"client {index}", [self.document]), range(6)))
            active = [path for path in self.manager.root.iterdir() if path.name.startswith("bundle-")]
            self.assertEqual(len(active), 2)
            self.assertEqual(len(list((self.manager.root / "archive").iterdir())), 4)
            for bundle in bundles:
                self.assertEqual(self.manager.get_bundle(bundle["bundle_id"]), bundle)
            # An archive collision must leave both copies untouched.
            oldest = min(active, key=lambda path: (path.stat().st_mtime_ns, path.name))
            collision = self.manager.root / "archive" / oldest.name
            collision.mkdir()
            before = (oldest / "bundle.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "archive destination"):
                self.manager.create_bundle("collision attempt", [self.document])
            self.assertEqual((oldest / "bundle.json").read_bytes(), before)
            self.assertEqual(list(collision.iterdir()), [])

    def test_failed_bundle_io_never_publishes_partial_active_ids_or_leaks_handles(self):
        real_open, real_fdopen = os.open, os.fdopen
        with patch.object(WorkflowManager, "MAX_BUNDLES", 1):
            existing = self.manager.create_bundle("existing", [self.document])
            for failure in ("open", "fdopen", "write", "fsync"):
                with self.subTest(failure=failure):
                    active = [path for path in self.manager.root.iterdir() if path.name.startswith("bundle-")]
                    self.assertEqual(len(active), 1)
                    before = (active[0] / "bundle.json").read_bytes()
                    descriptors, streams = [], []
                    def fail_open(path, *args, **kwargs):
                        metadata = (isinstance(path, (str, os.PathLike))
                                    and Path(path).name == "bundle.json"
                                    and Path(path).parent.parent.name == ".staging")
                        if metadata and failure == "open":
                            raise OSError("synthetic open failure")
                        descriptor = real_open(path, *args, **kwargs)
                        if metadata:
                            descriptors.append(descriptor)
                        return descriptor
                    class PartialWrite:
                        def __init__(self, stream):
                            self.stream = stream
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            self.stream.close()
                        def write(self, data):
                            self.stream.write(data[:10])
                            raise OSError("synthetic partial write failure")
                    def fail_fdopen(descriptor, mode, *args, **kwargs):
                        if mode == "wb" and failure == "fdopen":
                            raise OSError("synthetic stream open failure")
                        stream = real_fdopen(descriptor, mode, *args, **kwargs)
                        if mode == "wb":
                            streams.append(stream)
                            if failure == "write":
                                return PartialWrite(stream)
                        return stream
                    with patch("kapa_mcp.workflow.os.open", side_effect=fail_open), \
                            patch("kapa_mcp.workflow.os.fdopen", side_effect=fail_fdopen), \
                            patch("kapa_mcp.workflow.os.fsync", side_effect=OSError("synthetic fsync failure")):
                        with self.assertRaisesRegex(ValueError, "Cannot persist"):
                            self.manager.create_bundle("failed " + failure, [self.document])
                    for descriptor in descriptors:
                        with self.assertRaises(OSError):
                            os.fstat(descriptor)
                    self.assertTrue(all(stream.closed for stream in streams))
                    self.assertEqual([path for path in self.manager.root.iterdir()
                                      if path.name.startswith("bundle-")], active)
                    self.assertEqual((active[0] / "bundle.json").read_bytes(), before)
                    self.assertEqual(list((self.manager.root / ".staging").iterdir()), [])
                    # Storage recovery cannot leave cap=1 permanently blocked.
                    retry = self.manager.create_bundle("retry " + failure, [self.document])
                    self.assertTrue(self.manager.validate_bundle(retry["bundle_id"])["valid"])
                    self.assertEqual(self.manager.get_bundle(existing["bundle_id"]), existing)

    def test_failed_staging_cleanup_cannot_poison_capacity_or_touch_published_bundles(self):
        with patch.object(WorkflowManager, "MAX_BUNDLES", 1):
            existing = self.manager.create_bundle("existing", [self.document])
            existing_path = self.manager.root / existing["bundle_id"]
            before = (existing_path / "bundle.json").read_bytes()
            real_unlink = Path.unlink
            def cleanup_unavailable(path, *args, **kwargs):
                if path.parent.parent == self.manager.root / ".staging":
                    raise OSError("synthetic cleanup failure")
                return real_unlink(path, *args, **kwargs)
            with patch("kapa_mcp.workflow.os.fsync", side_effect=OSError("synthetic fsync failure")), \
                    patch.object(Path, "unlink", cleanup_unavailable):
                with self.assertRaisesRegex(ValueError, "Cannot persist"):
                    self.manager.create_bundle("failure", [self.document])
            self.assertEqual((existing_path / "bundle.json").read_bytes(), before)
            staging = self.manager.root / ".staging"
            abandoned = list(staging.iterdir())
            self.assertEqual(len(abandoned), 1)
            self.assertTrue((abandoned[0] / "bundle.json").is_file())
            for protected in (self.manager.root, self.manager.root / "archive", existing_path):
                self.manager._cleanup_staging(protected)
            self.assertEqual((existing_path / "bundle.json").read_bytes(), before)
            real_scandir = os.scandir
            def no_staging_scan(path):
                self.assertNotEqual(Path(path), staging)
                return real_scandir(path)
            with patch("kapa_mcp.workflow.os.scandir", side_effect=no_staging_scan):
                retry = self.manager.create_bundle("retry", [self.document])
                self.assertEqual(self.manager.get_bundle(existing["bundle_id"]), existing)
                self.assertTrue(self.manager.validate_bundle(retry["bundle_id"])["valid"])
            self.assertEqual((self.manager.root / "archive" / existing["bundle_id"] / "bundle.json").read_bytes(), before)

    @unittest.skipUnless(os.name != "nt", "POSIX permissions")
    def test_bundle_owner_only_permissions(self):
        bundle = self.create()
        path = self.manager.root / bundle["bundle_id"] / "bundle.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        self.assertFalse(self.manager.validate_bundle(bundle["bundle_id"])["valid"])


if __name__ == "__main__":
    unittest.main()
