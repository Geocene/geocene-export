"""Offline tests across the CLI, API client, polling and ZIP download layers."""

import contextlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from geocene_export.cli import main


def http_response(body: bytes, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    result.headers["Content-Length"] = str(len(body))
    return result


def json_response(data, status=200):
    return http_response(json.dumps(data).encode(), status=status)


def export_zip(include_events=True):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("missions.csv", "mission_id,mission_name\nmission-1,Example\n")
        if include_events:
            archive.writestr("events.csv", "mission_id,event_kind\nmission-1,cooking\n")
    return buffer.getvalue()


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "selected export.zip"
        self.checkpoint = self.output.with_name(self.output.name + ".job.json")
        self.calls = []
        self.created_payloads = []
        self.statuses = []
        self.download_payload = export_zip()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        environment = {"GEOCENE_API_TOKEN": "integration-only-token", "GEOCENE_API_URL": ""}
        environment_patch = patch.dict(os.environ, environment)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        # An explicit base URL makes the tests independent of a user's environment.
        self.base_url = "https://collect.geocene.com"
        request_patch = patch(
            "requests.Session.request", autospec=True, side_effect=self.api_request
        )
        download_patch = patch("requests.Session.get", autospec=True, side_effect=self.download_get)
        sleep_patch = patch("geocene_export.jobs.time.sleep")
        self.request = request_patch.start()
        self.download = download_patch.start()
        sleep_patch.start()
        self.addCleanup(request_patch.stop)
        self.addCleanup(download_patch.stop)
        self.addCleanup(sleep_patch.stop)

    def invoke(self, arguments):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(arguments)

    def run_arguments(self):
        return [
            "run",
            "--non-interactive",
            "--base-url",
            self.base_url,
            "--organization",
            "7",
            "--tag",
            "Round 2",
            "--output",
            str(self.output),
            "--exclude-metrics",
            "--exclude-sensors",
            "--exclude-tags",
            "--exclude-mission-logs",
            "--max-wait",
            "5",
            "--poll-interval",
            "0.001",
            "--download-timeout",
            "5",
        ]

    def resume_arguments(self):
        return [
            "resume",
            "42",
            "--non-interactive",
            "--base-url",
            self.base_url,
            "--output",
            str(self.output),
            "--max-wait",
            "5",
            "--download-timeout",
            "5",
        ]

    def api_request(self, session, method, url, **kwargs):
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Token integration-only-token")
        self.assertIsNotNone(session.auth)
        path = urlsplit(url).path
        self.calls.append((method, path))
        if method == "GET" and path == "/current_user/":
            return json_response({"id": 1, "organizations": [{"id": 7, "display_name": "Example"}]})
        if method == "GET" and path == "/tags/":
            self.assertEqual(kwargs["params"], {"organization": 7})
            return json_response(
                {"count": 1, "next": None, "results": [{"id": 17, "name": "Round 2"}]}
            )
        if method == "GET" and path == "/missions/flat_values/":
            self.assertEqual(
                kwargs["params"],
                [
                    ("organization", 7),
                    ("archived", "false"),
                    ("tags", 17),
                    ("tag_logic", "all"),
                ],
            )
            return json_response({"results": {"id": ["mission-1"]}})
        if method == "POST" and path == "/data_export_request/":
            self.created_payloads.append(kwargs["json"])
            return json_response(
                {"id": 42, "status": "pending", "progress": None, "download_url": None}, status=201
            )
        if method == "GET" and path == "/data_export_request/42/":
            self.assertTrue(self.checkpoint.exists())
            next_status = self.statuses.pop(0)
            if isinstance(next_status, BaseException):
                raise next_status
            return json_response({"id": 42, **next_status})
        self.fail(f"Unexpected API operation: {method} {path}")

    def download_get(self, session, url, **kwargs):
        self.calls.append(("DOWNLOAD", urlsplit(url).path))
        self.assertEqual(
            url, "https://files.example.invalid/export.zip?signature=private-test-value"
        )
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("Authorization", session.headers)
        self.assertIsNotNone(session.auth)
        return http_response(self.download_payload)

    @staticmethod
    def success():
        return {
            "status": "success",
            "progress": 100,
            "download_url": "https://files.example.invalid/export.zip?signature=private-test-value",
        }

    def test_actual_workflow_waits_past_progress_100_and_downloads_selected_csvs(self):
        self.statuses = [
            {"status": "processing", "progress": 100, "download_url": None},
            self.success(),
        ]
        self.assertEqual(self.invoke(self.run_arguments()), 0)
        self.assertEqual(
            self.created_payloads,
            [
                {
                    "kind": "all data",
                    "missions": ["mission-1"],
                    "filters": {
                        "organization": 7,
                        "metrics": False,
                        "events": True,
                        "sensors": False,
                        "tags": False,
                        "mission_logs": False,
                    },
                }
            ],
        )
        self.assertEqual(
            self.calls[-3:],
            [
                ("GET", "/data_export_request/42/"),
                ("GET", "/data_export_request/42/"),
                ("DOWNLOAD", "/export.zip"),
            ],
        )
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {"missions.csv", "events.csv"})
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(self.stdout.getvalue().strip(), str(self.output))
        self.assertNotIn("private-test-value", self.stdout.getvalue() + self.stderr.getvalue())
        self.assertNotIn("integration-only-token", self.stdout.getvalue() + self.stderr.getvalue())

    def test_interrupted_job_resumes_without_post_and_preserves_selected_file_checks(self):
        self.statuses = [{"status": "processing", "progress": 100}, KeyboardInterrupt()]
        self.assertEqual(self.invoke(self.run_arguments()), 130)
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(saved["expected_files"], ["events.csv", "missions.csv"])
        self.assertEqual(saved["output"], str(self.output))
        self.assertNotIn("integration-only-token", self.checkpoint.read_text())
        self.download.assert_not_called()
        self.assertFalse(self.output.exists())

        # A resumed export must still contain the originally selected events CSV.
        self.download_payload = export_zip(include_events=False)
        self.statuses = [self.success()]
        calls_before_resume = len(self.calls)
        self.assertEqual(self.invoke(self.resume_arguments()), 1)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.checkpoint.exists())
        self.assertIn("missing expected export CSV", self.stderr.getvalue())

        self.download_payload = export_zip()
        self.statuses = [self.success()]
        self.assertEqual(self.invoke(self.resume_arguments()), 0)
        self.assertEqual(self.output.read_bytes(), self.download_payload)
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(len(self.created_payloads), 1)
        self.assertTrue(all(method != "POST" for method, _ in self.calls[calls_before_resume:]))
        self.assertEqual(list(self.output.parent.glob(".*.part")), [])


if __name__ == "__main__":
    unittest.main()
