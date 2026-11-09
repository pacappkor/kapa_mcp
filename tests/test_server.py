# SPDX-License-Identifier: GPL-3.0-only
"""Protocol tests use installed package imports and synthetic data only."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters

from kapa_mcp.config import ConfigurationError, REPOSITORIES, Settings
from kapa_mcp.server import create_server


class RecordingService:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            return {"method": method, "args": list(args), "kwargs": kwargs}
        return call


class ConfigurationTests(unittest.TestCase):
    def test_explicit_environment_and_no_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            jobs = root / "jobs"
            with patch.dict(os.environ, {"KAPA_WORKSPACE": str(root), "KAPA_JOBS_DIR": str(jobs)}):
                self.assertEqual(Settings.from_env(), Settings(root, jobs))
            self.assertFalse(jobs.exists())
            for name in ("KAPA_WORKSPACE", "KAPA_JOBS_DIR"):
                with patch.dict(os.environ, {"KAPA_WORKSPACE": str(root), "KAPA_JOBS_DIR": str(jobs)}):
                    os.environ.pop(name)
                    with self.assertRaisesRegex(ConfigurationError, name):
                        Settings.from_env()

    def test_rejects_relative_and_upstream_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ConfigurationError, "KAPA_WORKSPACE"):
                Settings(Path("relative"), root / "jobs")
            for alias in REPOSITORIES:
                with self.assertRaisesRegex(ConfigurationError, "separate"):
                    Settings(root, root / alias / "build")
            with self.assertRaisesRegex(ConfigurationError, "separate"):
                Settings(root, root)

    def test_bad_startup_uses_stderr_only(self):
        env = os.environ.copy()
        env.pop("KAPA_WORKSPACE", None)
        env.pop("KAPA_JOBS_DIR", None)
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "kapa_mcp"], env=env,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("KAPA_WORKSPACE is required", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_initialize_tools_resources_prompt_and_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            catalog, renderer = RecordingService(), RecordingService()
            server = create_server(Settings(root, root / "jobs"), catalog=catalog, renderer=renderer)
            # SDK 2.3.0 Client(mode='legacy').__aenter__ sends initialize and
            # notifications/initialized over its in-memory protocol transport.
            async with Client(server, mode="legacy", read_timeout_seconds=10) as client:
                self.assertIsNotNone(client.protocol_version)
                self.assertEqual(client.server_info.name, "kapa-mcp")
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                self.assertEqual(len(tools), 10)
                for name, tool in tools.items():
                    self.assertTrue(name.startswith("kapa_"))
                    annotations = tool.annotations.model_dump(by_alias=True)
                    self.assertEqual(annotations["readOnlyHint"], name != "kapa_start_render")
                    self.assertFalse(annotations["destructiveHint"])
                    self.assertFalse(annotations["openWorldHint"])

                cases = [
                    ("kapa_list_repositories", {}, catalog, "list_repositories", (), {}),
                    ("kapa_route_task", {"task": "미불용지 문제지"}, catalog, "route_task", ("미불용지 문제지",), {}),
                    ("kapa_search_questions", {"query": "토지", "exam": 2, "round": "37", "offset": 3}, catalog,
                     "search_questions", (), {"query": "토지", "exam": 2, "subject": None, "round": "37", "limit": 20, "offset": 3}),
                    ("kapa_read_question", {"question_id": "edu/2/law/37/01"}, catalog,
                     "read_question", ("edu/2/law/37/01",), {}),
                    ("kapa_search_knowledge", {"query": "미불용지"}, catalog, "search_knowledge", (),
                     {"query": "미불용지", "repo": None, "limit": 20, "offset": 0}),
                    ("kapa_read_document", {"document_id": "kapa_skills:entry/SKILL.md", "offset": 4, "max_chars": 100},
                     catalog, "read_document", ("kapa_skills:entry/SKILL.md",), {"offset": 4, "max_chars": 100}),
                    ("kapa_get_relations", {"material_id": "case/demo", "review": "confirmed"}, catalog,
                     "get_relations", ("case/demo",), {"review": "confirmed"}),
                    ("kapa_start_render", {"exam": 1, "subject": "law", "round": "37"}, renderer,
                     "start_render", (), {"exam": 1, "subject": "law", "round": "37", "question_id": None, "noanswers": True}),
                    ("kapa_get_job", {"job_id": "demo"}, renderer, "get_job", ("demo",), {}),
                    ("kapa_get_artifact", {"job_id": "demo", "artifact_id": "pdf"}, renderer,
                     "get_artifact", ("demo", "pdf"), {}),
                ]
                for name, arguments, service, method, args, kwargs in cases:
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, name)
                    self.assertEqual(service.calls[-1], (method, args, kwargs))
                    payload = json.loads(result.content[0].text)
                    self.assertEqual(payload["method"], method)
                invalid = await client.call_tool("kapa_start_render", {"exam": "invalid"})
                self.assertTrue(invalid.is_error)
                resources = await client.list_resources()
                self.assertEqual({str(item.uri) for item in resources.resources},
                                 {"kapa://entry", "kapa://catalog/repositories"})
                entry = await client.read_resource("kapa://entry")
                self.assertEqual(json.loads(entry.contents[0].text)["tool_fallback"], "kapa_route_task")
                prompts = await client.list_prompts()
                self.assertEqual([item.name for item in prompts.prompts], ["kapa_research_and_render"])

    async def test_real_stdio_initialize_list_and_call_with_empty_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for alias in REPOSITORIES:
                (root / alias).mkdir()
            params = StdioServerParameters(
                command=sys.executable, args=["-I", "-B", "-m", "kapa_mcp"],
                env={"KAPA_WORKSPACE": str(root), "KAPA_JOBS_DIR": str(root / "jobs")},
            )
            async with asyncio.timeout(20):
                async with Client(params, mode="legacy", read_timeout_seconds=10) as client:
                    self.assertEqual(client.server_info.name, "kapa-mcp")
                    listed = await client.list_tools()
                    self.assertIn("kapa_list_repositories", {tool.name for tool in listed.tools})
                    result = await client.call_tool("kapa_list_repositories", {})
                    self.assertFalse(result.is_error)
                    self.assertIsInstance(json.loads(result.content[0].text), dict)


if __name__ == "__main__":
    unittest.main()
