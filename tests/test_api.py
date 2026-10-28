import unittest
from unittest.mock import Mock, patch

import requests

from geocene_export.api import Client, ExportError


def response(data=None, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = {} if data is None else data
    return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = Client(token="test-token")
        self.client.session = Mock()
        self.request = self.client.session.request

    def test_login_sends_credentials_once_without_auth_or_redirects(self):
        self.request.return_value = response({"token": "new-token"})
        self.assertEqual(self.client.login("name", "private-password"), "new-token")
        self.assertEqual(self.client.token, "new-token")
        kwargs = self.request.call_args.kwargs
        self.assertEqual(kwargs["json"], {"username": "name", "password": "private-password"})
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (10, 60))

    @patch("requests.sessions.get_netrc_auth", return_value=("netrc-user", "netrc-password"))
    def test_api_auth_is_never_replaced_by_netrc(self, netrc):
        with Client(token="test-token") as client:
            prepared = client.session.prepare_request(
                requests.Request(
                    "GET",
                    client.base_url + "current_user/",
                    headers={"Authorization": "Token test-token"},
                )
            )
            self.assertEqual(prepared.headers["Authorization"], "Token test-token")
            login = client.session.prepare_request(
                requests.Request(
                    "POST",
                    client.base_url + "api-token-auth/",
                    json={"username": "example", "password": "private-password"},
                )
            )
            self.assertNotIn("Authorization", login.headers)
            self.assertTrue(client.session.trust_env)
        netrc.assert_not_called()

    def test_api_responses_close_on_success_http_error_and_bad_json(self):
        for status in (200, 403):
            with self.subTest(status=status):
                result = response({"id": 4, "status": "success"}, status=status)
                self.request.return_value = result
                if status == 200:
                    self.client.get_export(4)
                else:
                    with self.assertRaises(ExportError):
                        self.client.get_export(4)
                result.close.assert_called_once_with()
        result = response()
        result.json.side_effect = ValueError("private response")
        self.request.return_value = result
        with self.assertRaises(ExportError):
            self.client.get_export(4)
        result.close.assert_called_once_with()

    def test_post_network_failure_is_never_retried_or_leaked(self):
        self.request.side_effect = requests.Timeout(
            "private-password test-token https://secret.invalid"
        )
        with self.assertRaises(ExportError) as caught:
            self.client.create_export(7, ["m1"])
        self.assertEqual(self.request.call_count, 1)
        for secret in ("private-password", "test-token", "secret.invalid"):
            self.assertNotIn(secret, str(caught.exception))

    def test_post_503_is_never_retried(self):
        self.request.return_value = response(status=503)
        with self.assertRaisesRegex(ExportError, "HTTP 503"):
            self.client.create_export(7, ["m1"])
        self.assertEqual(self.request.call_count, 1)

    def test_export_payload_excludes_metrics_by_default(self):
        self.request.return_value = response({"id": 4, "status": "pending"}, status=201)
        self.client.create_export(7, ["m1"])
        self.assertEqual(
            self.request.call_args.kwargs["json"],
            {
                "kind": "all data",
                "missions": ["m1"],
                "filters": {
                    "organization": 7,
                    "metrics": False,
                    "events": True,
                    "sensors": True,
                    "tags": True,
                    "mission_logs": True,
                },
            },
        )

    def test_export_options_disable_selected_files_and_keep_other_defaults(self):
        self.request.return_value = response({"id": 4}, status=201)
        self.client.create_export(
            7, ["m1"], include_metrics=True, data_options={"events": False, "tags": False}
        )
        self.assertEqual(
            self.request.call_args.kwargs["json"]["filters"],
            {
                "organization": 7,
                "metrics": True,
                "events": False,
                "sensors": True,
                "tags": False,
                "mission_logs": True,
            },
        )

    def test_export_options_can_exclude_all_optional_metadata(self):
        self.request.return_value = response({"id": 4}, status=201)
        options = dict.fromkeys(("events", "sensors", "tags", "mission_logs"), False)
        self.client.create_export(7, ["m1"], data_options=options)
        payload = self.request.call_args.kwargs["json"]
        self.assertEqual(payload["missions"], ["m1"])
        self.assertEqual(payload["filters"], {"organization": 7, "metrics": False, **options})
        self.assertNotIn("missions", payload["filters"])

    def test_export_options_reject_unknown_keys_metrics_and_nonboolean_values(self):
        for options in (
            {"metrics": False},
            {"missions": False},
            {"unknown": True},
            {"events": 0},
            {"tags": "false"},
            {"sensors": None},
            [],
        ):
            with self.subTest(options=options), self.assertRaises(ExportError):
                self.client.create_export(7, ["m1"], data_options=options)
        self.request.assert_not_called()

    def test_empty_mission_selection_fails_before_network(self):
        with self.assertRaisesRegex(ExportError, "empty selection"):
            self.client.create_export(7, [])
        self.request.assert_not_called()

    def test_export_without_job_id_reports_ambiguous_acceptance(self):
        self.request.return_value = response({"status": "pending"}, status=201)
        with self.assertRaisesRegex(ExportError, "may exist; check"):
            self.client.create_export(7, ["m1"])
        self.assertEqual(self.request.call_count, 1)

    def test_organizations_come_from_current_user(self):
        self.request.return_value = response(
            {"organizations": [{"id": 7, "display_name": "Example", "extra": True}]}
        )
        self.assertEqual(self.client.organizations(), [{"id": 7, "display_name": "Example"}])
        self.assertTrue(self.request.call_args.args[1].endswith("/current_user/"))

    def test_tag_names_resolve_across_pages_and_filter_with_all_logic(self):
        self.request.side_effect = [
            response({"organizations": [{"id": 7, "display_name": "Example"}]}),
            response(
                {
                    "results": [{"id": 17, "name": "Country: Kenya"}],
                    "next": "https://collect.geocene.com/tags/?organization=7&page=2",
                }
            ),
            response({"results": [{"id": 23, "name": "Round 2"}], "next": None}),
            response({"results": {"id": ["m1", "m2"]}}),
        ]
        self.assertEqual(self.client.mission_ids(7, ["Country: Kenya", "Round 2"]), ["m1", "m2"])
        params = self.request.call_args.kwargs["params"]
        self.assertEqual(
            params,
            [
                ("organization", 7),
                ("archived", "false"),
                ("tags", 17),
                ("tags", 23),
                ("tag_logic", "all"),
            ],
        )

    def test_unknown_org_and_unknown_tag_never_broaden_selection(self):
        self.request.return_value = response(
            {"organizations": [{"id": 7, "display_name": "Example"}]}
        )
        with self.assertRaisesRegex(ExportError, "not available"):
            self.client.mission_ids(99)
        self.request.side_effect = [
            response({"organizations": [{"id": 7, "display_name": "Example"}]}),
            response({"results": [], "next": None}),
        ]
        with self.assertRaisesRegex(ExportError, "tag names were not found"):
            self.client.mission_ids(7, ["Missing"])
        self.assertTrue(self.request.call_args.args[1].endswith("/tags/"))

    def test_zero_matches_are_returned_without_starting_export(self):
        self.request.side_effect = [
            response({"organizations": [{"id": 7, "display_name": "Example"}]}),
            response({"results": {"id": []}}),
        ]
        self.assertEqual(self.client.mission_ids(7), [])
        self.assertEqual([call.args[0] for call in self.request.call_args_list], ["GET", "GET"])

    def test_pagination_cannot_send_token_to_another_origin(self):
        self.request.return_value = response({"results": [], "next": "https://other.invalid/tags/"})
        with self.assertRaisesRegex(ExportError, "unsafe pagination"):
            list(self.client._tags(7))
        self.assertEqual(self.request.call_count, 1)

    def test_pagination_cycle_is_bounded(self):
        self.request.return_value = response(
            {"results": [], "next": "https://collect.geocene.com/tags/"}
        )
        with self.assertRaisesRegex(ExportError, "invalid tag pagination"):
            list(self.client._tags(7))
        self.assertEqual(self.request.call_count, 1)

    def test_redirect_is_rejected_without_following_or_disclosing_location(self):
        self.request.return_value = response(
            status=302, headers={"Location": "https://secret.invalid/?token=private"}
        )
        with self.assertRaises(ExportError) as caught:
            self.client.get_export(4)
        self.assertEqual(self.request.call_count, 1)
        self.assertFalse(self.request.call_args.kwargs["allow_redirects"])
        self.assertNotIn("secret.invalid", str(caught.exception))

    @patch("geocene_export.api.random.uniform", return_value=0)
    @patch("geocene_export.api.time.sleep")
    def test_get_retries_transient_status_and_bounds_retry_after(self, sleep, jitter):
        self.request.side_effect = [
            response(status=429, headers={"Retry-After": "9999"}),
            response(status=503),
            response({"status": "success"}),
        ]
        self.assertEqual(self.client.get_export(4)["status"], "success")
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [15, 2])

    @patch("geocene_export.api.time.sleep")
    def test_get_network_retries_are_bounded(self, sleep):
        self.request.side_effect = requests.ConnectionError("token=private")
        with self.assertRaises(ExportError):
            self.client.get_export(4)
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("geocene_export.api.time.monotonic", return_value=100)
    def test_elapsed_deadline_stops_before_network(self, monotonic):
        with self.assertRaisesRegex(ExportError, "deadline"):
            self.client.get_export(4, deadline=99)
        self.request.assert_not_called()

    @patch("geocene_export.api.time.monotonic", return_value=100)
    def test_request_timeouts_fit_remaining_deadline(self, monotonic):
        self.request.return_value = response({"status": "processing"})
        self.client.get_export(4, deadline=104)
        self.assertEqual(self.request.call_args.kwargs["timeout"], (2, 2))

    @patch("geocene_export.api.time.monotonic", side_effect=[100, 100, 100, 102])
    @patch("geocene_export.api.time.sleep")
    def test_retry_wait_is_capped_to_remaining_deadline(self, sleep, monotonic):
        self.request.return_value = response(status=429, headers={"Retry-After": "9999"})
        with self.assertRaisesRegex(ExportError, "deadline"):
            self.client.get_export(4, deadline=102)
        sleep.assert_called_once_with(2)
        self.assertEqual(self.request.call_count, 1)

    def test_invalid_json_is_sanitized(self):
        self.request.return_value = response()
        self.request.return_value.json.side_effect = ValueError("private body")
        with self.assertRaisesRegex(ExportError, "invalid JSON") as caught:
            self.client.get_export(4)
        self.assertNotIn("private body", str(caught.exception))

    def test_base_url_requires_https_except_loopback(self):
        for url in (
            "http://example.com",
            "https://user:password@example.com",
            "https://example.com?token=x",
            "https://example.com#fragment",
            "https://example.com\n",
        ):
            with self.subTest(url=url), self.assertRaises(ExportError):
                Client(base_url=url)
        for url in (
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
            "http://localhost:8000",
            "https://example.com",
        ):
            with self.subTest(url=url), Client(base_url=url):
                pass


if __name__ == "__main__":
    unittest.main()
