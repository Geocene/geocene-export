import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from geocene_export import jobs
from geocene_export.api import ExportError


def export_zip(names=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in jobs.EXPECTED_FILES if names is None else names:
            archive.writestr(name, "id\n")
    return buffer.getvalue()


def response(body=b"", status=200, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = {"Content-Length": str(len(body))} if headers is None else headers
    result.iter_content.return_value = iter([body])
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.clock = patch.object(jobs.time, "monotonic", side_effect=lambda: self.now)
        self.sleep = patch.object(jobs.time, "sleep", side_effect=self.advance)
        self.clock.start()
        self.sleep.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def test_progress_100_is_not_completion(self):
        client = Mock()
        ready = {"status": "success", "download_url": "https://files.example/export.zip"}
        client.get_export.side_effect = [
            {"status": "pending"},
            {"status": "processing", "progress": 100},
            ready,
        ]
        statuses = []
        self.assertIs(jobs.wait_for_export(client, 42, on_status=statuses.append), ready)
        self.assertEqual(client.get_export.call_count, 3)
        client.get_export.assert_called_with(42, deadline=1800)
        self.assertIn("processing (100%)", statuses[-1])

    def test_timeout_keeps_job_id_and_resume_command(self):
        client = Mock()
        client.get_export.return_value = {"status": "pending"}
        with self.assertRaisesRegex(
            ExportError, "Timed out.*42.*python -m geocene_export resume 42"
        ):
            jobs.wait_for_export(client, 42, max_wait=3, poll_interval=10, on_status=None)
        self.assertEqual(self.now, 3)
        self.assertEqual(client.get_export.call_count, 1)

    def test_request_consumes_same_wait_budget(self):
        def fetch(*args, **kwargs):
            self.assertEqual(kwargs["deadline"], 3)
            self.now = 3
            raise ExportError("Request deadline exceeded")

        with self.assertRaisesRegex(ExportError, "Timed out.*resume 42"):
            jobs.wait_for_export(Mock(get_export=fetch), 42, max_wait=3)

    def test_late_success_does_not_exceed_wait_budget(self):
        def fetch(*args, **kwargs):
            self.now = 4
            return {"status": "success"}

        with self.assertRaisesRegex(ExportError, "Timed out"):
            jobs.wait_for_export(Mock(get_export=fetch), 42, max_wait=3)

    def test_failed_and_unknown_statuses_are_explicit(self):
        for status, message in [
            ("error", "failed"),
            ("failed", "failed"),
            ("unexpected", "unknown status"),
            (None, "unknown status"),
        ]:
            with self.subTest(status=status), self.assertRaisesRegex(ExportError, message):
                jobs.wait_for_export(Mock(get_export=Mock(return_value={"status": status})), 42)

    def test_invalid_response_and_interval(self):
        with self.assertRaisesRegex(ExportError, "invalid status"):
            jobs.wait_for_export(Mock(get_export=Mock(return_value=[])), 42)
        with self.assertRaisesRegex(ExportError, "positive, finite"):
            jobs.wait_for_export(Mock(), 42, poll_interval=0)

    def test_status_repetitions_are_not_logged(self):
        client = Mock()
        client.get_export.side_effect = [{"status": "pending"}] * 2 + [{"status": "success"}]
        report = Mock()
        jobs.wait_for_export(client, 42, on_status=report)
        report.assert_called_once()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "export.zip"
        self.job = {
            "id": 42,
            "status": "success",
            "download_url": "https://files.example/export.zip?secret=token",
        }
        self.session_patch = patch.object(jobs.requests, "Session")
        self.session_factory = self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.session = self.session_factory.return_value.__enter__.return_value
        self.sleep_patch = patch.object(jobs.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def assert_no_partial_files(self):
        self.assertEqual(list(Path(self.directory.name).glob("*.part")), [])
        self.assertEqual(list(Path(self.directory.name).glob(".*.part")), [])

    def test_valid_zip_published_without_api_or_netrc_auth(self):
        payload = export_zip()
        self.session.get.return_value = response(payload)
        self.assertEqual(jobs.download_export(self.job, self.output), self.output)
        self.assertEqual(self.output.read_bytes(), payload)
        self.assertIsInstance(self.session.auth, jobs._NoCredentials)
        request = requests.Request("GET", self.job["download_url"]).prepare()
        self.assertNotIn("Authorization", self.session.auth(request).headers)
        kwargs = self.session.get.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"], {"Accept-Encoding": "identity"})
        self.assert_no_partial_files()

    def test_existing_file_requires_explicit_overwrite(self):
        self.output.write_bytes(b"original")
        self.session.get.return_value = response(export_zip())
        with self.assertRaisesRegex(ExportError, "already exists"):
            jobs.download_export(self.job, self.output)
        self.assertEqual(self.output.read_bytes(), b"original")
        self.session.get.assert_not_called()
        jobs.download_export(self.job, self.output, overwrite=True)
        self.assertNotEqual(self.output.read_bytes(), b"original")

    def test_insecure_or_credential_urls_never_requested(self):
        for url in [
            "http://example.com/export.zip",
            "https://user:pass@example.com/export.zip",
            "https:///export.zip",
            "https://example.com:bad/export.zip",
            None,
        ]:
            with self.subTest(url=url), self.assertRaisesRegex(ExportError, "valid HTTPS"):
                jobs.download_export({**self.job, "download_url": url}, self.output)
        self.session.get.assert_not_called()

    def test_redirects_are_validated_before_following(self):
        first = response(status=302, headers={"Location": "http://unsafe.example/file"})
        self.session.get.return_value = first
        with self.assertRaisesRegex(ExportError, "valid HTTPS"):
            jobs.download_export(self.job, self.output)
        self.assertEqual(self.session.get.call_count, 1)
        first.close.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_relative_https_redirect_downloads(self):
        self.session.get.side_effect = [
            response(status=302, headers={"Location": "/final.zip"}),
            response(export_zip()),
        ]
        jobs.download_export(self.job, self.output)
        self.assertEqual(self.session.get.call_args.args[0], "https://files.example/final.zip")

    def test_expired_url_does_not_leak_signed_query(self):
        self.session.get.return_value = response(status=403)
        with self.assertRaises(ExportError) as caught:
            jobs.download_export(self.job, self.output)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.session.get.call_count, 1)

    def test_incomplete_stream_retries_and_cleans_temp(self):
        self.session.get.side_effect = [
            response(b"short", headers={"Content-Length": "100"}),
            response(export_zip()),
        ]
        jobs.download_export(self.job, self.output)
        self.assertEqual(self.session.get.call_count, 2)
        self.assert_no_partial_files()

    def test_network_retries_are_bounded_and_hide_url(self):
        self.session.get.side_effect = requests.ConnectionError("https://example.com?secret=token")
        with self.assertRaises(ExportError) as caught:
            jobs.download_export(self.job, self.output)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(self.output.exists())
        self.assert_no_partial_files()

    def test_invalid_missing_and_corrupt_zip_never_replace_existing(self):
        payload = bytearray(export_zip())
        index = payload.find(b"id\n")
        payload[index] = ord("x")  # Keep a readable ZIP with a bad entry CRC.
        for bad_payload in [b"not a zip", export_zip(["missions.csv"]), bytes(payload)]:
            with self.subTest(payload=bad_payload[:10]):
                self.output.write_bytes(b"original")
                self.session.get.return_value = response(bad_payload)
                with self.assertRaises(ExportError):
                    jobs.download_export(self.job, self.output, overwrite=True)
                self.assertEqual(self.output.read_bytes(), b"original")
                self.assert_no_partial_files()

    def test_invalid_size_and_non_ready_jobs_rejected(self):
        self.session.get.return_value = response(headers={"Content-Length": "-1"})
        with self.assertRaisesRegex(ExportError, "invalid file size"):
            jobs.download_export(self.job, self.output)
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(ExportError, "not ready"):
            jobs.download_export({**self.job, "status": "processing", "progress": 100}, self.output)

    def test_selected_csvs_allow_excluded_metadata_to_be_absent(self):
        payload = export_zip(["missions.csv", "events.csv"])
        self.session.get.return_value = response(payload)
        jobs.download_export(self.job, self.output, expected_files={"missions.csv", "events.csv"})
        self.assertEqual(self.output.read_bytes(), payload)
        self.assert_no_partial_files()

    def test_selected_csvs_still_require_every_selected_file(self):
        self.session.get.return_value = response(export_zip(["missions.csv"]))
        with self.assertRaisesRegex(ExportError, "missing expected"):
            jobs.download_export(
                self.job, self.output, expected_files={"missions.csv", "events.csv"}
            )
        self.assertFalse(self.output.exists())
        self.assert_no_partial_files()

    def test_missions_only_validation_accepts_resume_without_saved_selection(self):
        payload = export_zip(["missions.csv"])
        self.session.get.return_value = response(payload)
        jobs.download_export(self.job, self.output, expected_files=["missions.csv"])
        self.assertEqual(self.output.read_bytes(), payload)

    def test_expected_files_override_rejects_unknown_files_or_missing_missions(self):
        for expected in [
            set(),
            {"events.csv"},
            {"missions.csv", "metrics.json"},
            "missions.csv",
            ("missions.csv",),
            ["missions.csv", []],
        ]:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(ExportError, "Expected files"),
            ):
                jobs.download_export(self.job, self.output, expected_files=expected)
        self.session.get.assert_not_called()

    def test_download_timeout_must_be_positive_and_finite(self):
        for timeout in [0, -1, float("inf"), float("nan"), "10", True]:
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(ExportError, "positive, finite"),
            ):
                jobs.download_export(self.job, self.output, download_timeout=timeout)
        self.session.get.assert_not_called()

    def test_download_timeout_also_bounds_zip_validation(self):
        self.session.get.return_value = response(export_zip())
        moments = iter([0, 0, 0, 0, 11])
        with patch.object(jobs.time, "monotonic", side_effect=lambda: next(moments, 11)):
            with self.assertRaisesRegex(ExportError, "ZIP validation timed out"):
                jobs.download_export(self.job, self.output, download_timeout=10)
        self.assertFalse(self.output.exists())
        self.assert_no_partial_files()

    def test_transient_download_honors_retry_after(self):
        self.session.get.side_effect = [
            response(status=429, headers={"Retry-After": "7"}),
            response(export_zip()),
        ]
        jobs.download_export(self.job, self.output)
        jobs.time.sleep.assert_called_once_with(7)

    def test_destination_creation_race_preserves_new_file(self):
        self.session.get.return_value = response(export_zip())

        def competing_writer(source, destination):
            self.output.write_bytes(b"other writer")
            raise FileExistsError

        with patch.object(jobs.os, "link", side_effect=competing_writer):
            with self.assertRaisesRegex(ExportError, "already exists"):
                jobs.download_export(self.job, self.output)
        self.assertEqual(self.output.read_bytes(), b"other writer")
        self.assert_no_partial_files()


if __name__ == "__main__":
    unittest.main()
