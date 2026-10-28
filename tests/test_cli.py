import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from geocene_export.cli import main, resume_command


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "export.zip"
        self.checkpoint = self.output.with_name("export.zip.job.json")
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.addCleanup(patch.stopall)
        self.client = patch("geocene_export.cli.Client").start().return_value.__enter__.return_value
        self.client.base_url = "https://collect.geocene.com/"
        self.client.organizations.return_value = [{"id": 7, "display_name": "Example"}]
        self.client.mission_ids.return_value = ["mission-1"]
        self.client.create_export.return_value = {"id": 42, "status": "pending"}
        self.wait = patch(
            "geocene_export.cli.wait_for_export", return_value={"status": "success"}
        ).start()
        self.download = patch("geocene_export.cli.download_export").start()
        patch.dict(os.environ, {"GEOCENE_API_TOKEN": "test-only-token"}).start()

    def invoke(self, *arguments):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(list(arguments))

    def run_arguments(self):
        return ["run", "--non-interactive", "--organization", "7", "--output", str(self.output)]

    def test_run_excludes_metrics_and_cleans_job_file_after_success(self):
        self.assertEqual(self.invoke(*self.run_arguments()), 0)
        self.client.create_export.assert_called_once_with(
            7,
            ["mission-1"],
            include_metrics=False,
            data_options={"events": True, "sensors": True, "tags": True, "mission_logs": True},
        )
        self.assertIn("Export job 42 created", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue().strip(), str(self.output))
        self.assertFalse(self.checkpoint.exists())
        self.client.login.assert_not_called()

    def test_zero_matches_stops_before_submitting(self):
        self.client.mission_ids.return_value = []
        self.assertEqual(self.invoke(*self.run_arguments()), 1)
        self.client.create_export.assert_not_called()
        self.assertIn("No matching", self.stderr.getvalue())

    def test_inaccessible_organization_stops_before_mission_selection(self):
        self.client.organizations.return_value = [{"id": 8, "display_name": "Other"}]
        self.assertEqual(self.invoke(*self.run_arguments()), 1)
        self.client.mission_ids.assert_not_called()
        self.client.create_export.assert_not_called()

    def test_tags_and_metrics_opt_in_are_passed_through(self):
        self.assertEqual(
            self.invoke(*self.run_arguments(), "--tag", "A", "--tag", "B", "--include-metrics"), 0
        )
        self.client.mission_ids.assert_called_once_with(7, ["A", "B"])
        self.client.create_export.assert_called_once_with(
            7,
            ["mission-1"],
            include_metrics=True,
            data_options={"events": True, "sensors": True, "tags": True, "mission_logs": True},
        )

    def test_selective_data_is_sent_saved_and_validated(self):
        self.wait.side_effect = KeyboardInterrupt
        arguments = [
            *self.run_arguments(),
            "--exclude-metrics",
            "--exclude-sensors",
            "--exclude-tags",
            "--exclude-mission-logs",
        ]
        self.assertEqual(self.invoke(*arguments), 130)
        self.client.create_export.assert_called_once_with(
            7,
            ["mission-1"],
            include_metrics=False,
            data_options={"events": True, "sensors": False, "tags": False, "mission_logs": False},
        )
        self.assertEqual(
            json.loads(self.checkpoint.read_text())["expected_files"],
            ["events.csv", "missions.csv"],
        )
        self.wait.side_effect = None
        self.assertEqual(
            self.invoke("resume", "42", "--output", str(self.output), "--non-interactive"), 0
        )
        self.assertEqual(
            self.download.call_args.kwargs["expected_files"], {"missions.csv", "events.csv"}
        )

    def test_metrics_include_and_exclude_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke(*self.run_arguments(), "--include-metrics", "--exclude-metrics")
        self.assertEqual(caught.exception.code, 2)

    def test_token_command_prints_only_requested_token(self):
        self.assertEqual(self.invoke("token", "--non-interactive"), 0)
        self.assertEqual(self.stdout.getvalue().strip(), "test-only-token")
        self.client.create_export.assert_not_called()

    def test_interrupt_preserves_nonsecret_job_file(self):
        self.wait.side_effect = KeyboardInterrupt
        self.assertEqual(self.invoke(*self.run_arguments()), 130)
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(saved["job_id"], 42)
        self.assertNotIn("test-only-token", self.checkpoint.read_text())
        self.assertIn("continues on the server", self.stderr.getvalue())

    def test_resume_reuses_job_and_removes_matching_checkpoint(self):
        self.checkpoint.write_text(json.dumps({"job_id": 42, "api_base_url": self.client.base_url}))
        self.assertEqual(
            self.invoke("resume", "42", "--non-interactive", "--output", str(self.output)), 0
        )
        self.client.create_export.assert_not_called()
        self.client.mission_ids.assert_not_called()
        self.assertEqual(self.wait.call_args.args[1], 42)
        self.assertFalse(self.checkpoint.exists())

    def test_existing_file_is_preserved_before_authentication(self):
        self.output.write_bytes(b"original")
        self.assertEqual(self.invoke(*self.run_arguments()), 1)
        self.client.create_export.assert_not_called()
        self.download.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"original")

    def test_existing_job_file_prevents_duplicate_export(self):
        self.checkpoint.write_text(json.dumps({"job_id": 42}))
        self.assertEqual(self.invoke(*self.run_arguments()), 1)
        self.client.create_export.assert_not_called()

    def test_resume_rejects_checkpoint_for_another_job(self):
        self.checkpoint.write_text(json.dumps({"job_id": 99, "api_base_url": self.client.base_url}))
        self.assertEqual(
            self.invoke("resume", "42", "--non-interactive", "--output", str(self.output)), 1
        )
        self.wait.assert_not_called()

    def test_noninteractive_without_token_never_prompts(self):
        with patch.dict(os.environ, {"GEOCENE_API_TOKEN": ""}), patch("builtins.input") as prompt:
            self.assertEqual(self.invoke(*self.run_arguments()), 1)
        prompt.assert_not_called()
        self.assertIn("GEOCENE_API_TOKEN", self.stderr.getvalue())

    def test_invalid_job_file_reports_an_error_without_traceback(self):
        for contents in ("[]", "null", "{bad JSON"):
            with self.subTest(contents=contents):
                self.checkpoint.write_text(contents)
                self.assertEqual(
                    self.invoke("resume", "42", "--non-interactive", "--output", str(self.output)),
                    1,
                )
        self.wait.assert_not_called()

    def test_checkpoint_cleanup_failure_keeps_success(self):
        self.checkpoint.write_text(json.dumps({"job_id": 42, "api_base_url": self.client.base_url}))
        with patch("pathlib.Path.unlink", side_effect=PermissionError):
            self.assertEqual(
                self.invoke("resume", "42", "--non-interactive", "--output", str(self.output)), 0
            )
        self.assertEqual(self.stdout.getvalue().strip(), str(self.output))
        self.assertIn("File saved", self.stderr.getvalue())

    def test_resume_message_preserves_custom_server_and_output(self):
        self.client.base_url = "https://staging.example.invalid/"
        self.wait.side_effect = KeyboardInterrupt
        self.assertEqual(
            self.invoke(*self.run_arguments(), "--base-url", self.client.base_url), 130
        )
        self.assertIn(str(self.output), self.stderr.getvalue())
        self.assertIn(self.client.base_url, self.stderr.getvalue())

    def test_resume_command_uses_same_python_without_a_console_launcher(self):
        args = Namespace(
            job_id=42, base_url=self.client.base_url, overwrite=False, non_interactive=True
        )
        with (
            patch("geocene_export.cli.sys.executable", "/tmp/client python/bin/python"),
            patch("geocene_export.cli.os.name", "posix"),
        ):
            command = shlex.split(resume_command(args, self.output))
        self.assertEqual(
            command[:5], ["/tmp/client python/bin/python", "-m", "geocene_export", "resume", "42"]
        )
        self.assertIn(str(self.output), command)

    def test_windows_resume_command_invokes_quoted_python_in_powershell(self):
        args = Namespace(
            job_id=42, base_url=self.client.base_url, overwrite=False, non_interactive=True
        )
        with (
            patch("geocene_export.cli.sys.executable", "C:\\Program Files\\Python\\python.exe"),
            patch("geocene_export.cli.os.name", "nt"),
        ):
            command = resume_command(args, self.output)
        self.assertTrue(command.startswith("& 'C:\\Program Files\\Python\\python.exe'"))
        self.assertIn("'-m' 'geocene_export' 'resume' '42'", command)

    def test_status_does_not_print_signed_download_url(self):
        self.client.get_export.return_value = {
            "id": 42,
            "status": "success",
            "progress": 100,
            "download_url": "https://example.invalid/?secret=private",
        }
        self.assertEqual(self.invoke("status", "42", "--non-interactive"), 0)
        self.assertEqual(
            json.loads(self.stdout.getvalue()), {"id": 42, "status": "success", "progress": 100}
        )
        self.assertNotIn("secret", self.stdout.getvalue())

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke(*self.run_arguments(), "--max-wait", "nan")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
