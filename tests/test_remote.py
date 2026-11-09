# SPDX-License-Identifier: GPL-3.0-only
"""Real loopback protocol/auth tests against a separate server process."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from kapa_mcp.config import ConfigurationError, HTTPSettings, transport_from_env


class HTTPConfigurationTests(unittest.TestCase):
    def test_http_startup_without_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {key: value for key, value in os.environ.items() if not key.startswith("KAPA_")}
            env.update(KAPA_WORKSPACE=str(root), KAPA_JOBS_DIR=str(root / "jobs"),
                       KAPA_TRANSPORT="streamable-http")
            result = subprocess.run([sys.executable, "-I", "-B", "-m", "kapa_mcp"],
                                    env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("KAPA_HTTP_TOKEN", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_explicit_transport_and_secret_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(transport_from_env(), "stdio")
            with self.assertRaises(ConfigurationError):
                HTTPSettings.from_env()
        for token in ("", "short", "x" * 32 + "\n", "한" * 32):
            with self.assertRaises(ConfigurationError):
                HTTPSettings(token)
        token = secrets.token_urlsafe(32)
        self.assertNotIn(token, repr(HTTPSettings(token)))
        for port in (0, 80, 65536, True):
            with self.assertRaises(ConfigurationError):
                HTTPSettings(token, port)
        with patch.dict(os.environ, {"KAPA_TRANSPORT": "public"}):
            with self.assertRaises(ConfigurationError):
                transport_from_env()


class LoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_initialize_reads_and_auth_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for alias in ("kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge"):
                (root / alias).mkdir()
            (root / "kapa_skills" / "README.md").write_text("Synthetic private source", encoding="utf-8")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            token = secrets.token_urlsafe(32)
            env = {key: value for key, value in os.environ.items() if not key.startswith("KAPA_")}
            env.update(KAPA_WORKSPACE=str(root), KAPA_JOBS_DIR=str(root / "jobs"),
                       KAPA_TRANSPORT="streamable-http", KAPA_HTTP_PORT=str(port), KAPA_HTTP_TOKEN=token)
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen([sys.executable, "-I", "-B", "-m", "kapa_mcp"],
                                        env=env, stdout=output, stderr=errors)
                try:
                    url = f"http://127.0.0.1:{port}/mcp"
                    async with asyncio.timeout(25):
                        async with httpx2.AsyncClient(trust_env=False, timeout=2) as http:
                            for attempt in range(100):
                                try:
                                    response = await http.get(url)
                                    break
                                except (httpx2.ConnectError, httpx2.ConnectTimeout):
                                    if proc.poll() is not None:
                                        self.fail("HTTP server exited before startup")
                                    await asyncio.sleep(0.05)
                            else:
                                self.fail("HTTP server did not start")
                            self.assertEqual(response.status_code, 401)
                            auth = {"Authorization": f"Bearer {token}"}
                            for method in ("GET", "POST", "DELETE"):
                                response = await http.request(method, url, headers={"Authorization": "Bearer wrong"})
                                self.assertEqual(response.status_code, 401)
                            response = await http.get(url, headers={**auth, "Host": "attacker.invalid"})
                            self.assertEqual(response.status_code, 421)
                            response = await http.get(url, headers={**auth, "Origin": "https://attacker.invalid"})
                            self.assertEqual(response.status_code, 403)
                            response = await http.get(url, headers={**auth, "Origin": "null"})
                            self.assertEqual(response.status_code, 403)
                            response = await http.get(url + "?access_token=" + token, headers=auth)
                            self.assertEqual(response.status_code, 400)
                            response = await http.get(url, headers=[("Authorization", f"Bearer {token}"),
                                                                 ("Authorization", f"Bearer {token}")])
                            self.assertEqual(response.status_code, 401)
                        async with httpx2.AsyncClient(headers={**auth, "Origin": f"http://127.0.0.1:{port}"},
                                                     trust_env=False) as http:
                            # Official SDK transport sends real initialize + initialized.
                            async with Client(streamable_http_client(url, http_client=http),
                                              mode="legacy", read_timeout_seconds=10) as client:
                                self.assertEqual(client.server_info.name, "kapa-mcp")
                                self.assertIsNotNone(client.protocol_version)
                                listed = await client.list_tools()
                                self.assertIn("kapa_list_repositories", {t.name for t in listed.tools})
                                result = await client.call_tool("kapa_list_repositories", {})
                                self.assertFalse(result.is_error)
                                self.assertIsInstance(json.loads(result.content[0].text), dict)
                                result = await client.call_tool("kapa_read_document", {
                                    "document_id": "kapa_skills:README.md"})
                                self.assertFalse(result.is_error)
                                self.assertIn("Synthetic private source", result.content[0].text)
                finally:
                    proc.terminate()
                    await asyncio.to_thread(proc.wait, timeout=10)
                output.seek(0)
                errors.seek(0)
                logs = output.read() + errors.read()
                self.assertNotIn(token.encode(), logs)
                self.assertNotIn(b"attacker.invalid", logs)


if __name__ == "__main__":
    unittest.main()
