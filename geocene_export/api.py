"""Small, deliberately narrow client for Geocene's export API."""

import ipaddress
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests


class ExportError(Exception):
    """An actionable error safe to show without exposing credentials."""


class _NoCredentials(requests.auth.AuthBase):
    def __call__(self, request):
        # Explicit auth suppresses .netrc without disabling environment proxies/CA.
        return request


def _positive_id(value, label):
    if type(value) is not int or value < 1:
        raise ExportError(f"{label} must be a positive integer.")
    return value


class Client:
    def __init__(self, token: str | None = None, base_url="https://collect.geocene.com"):
        try:
            parsed = urlsplit(base_url)
            host = parsed.hostname or ""
            loopback = host.lower() == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            if (
                not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or "\\" in base_url
                or any(ord(char) <= 32 for char in base_url)
                or not (parsed.scheme == "https" or parsed.scheme == "http" and loopback)
            ):
                raise ValueError
            self._origin = (
                parsed.scheme,
                host.lower(),
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except (AttributeError, TypeError, ValueError):
            raise ExportError(
                "Use an HTTPS API base URL; HTTP is allowed only on loopback."
            ) from None
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.session = requests.Session()
        self.session.auth = _NoCredentials()

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _url(self, path):
        try:
            url = urljoin(self.base_url, path)
            parsed = urlsplit(url)
            origin = (
                parsed.scheme,
                (parsed.hostname or "").lower(),
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            if (
                origin != self._origin
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or "\\" in url
                or any(ord(char) <= 32 for char in url)
                or not parsed.path.startswith(urlsplit(self.base_url).path)
            ):
                raise ValueError
            return url
        except (TypeError, ValueError):
            raise ExportError("The API returned an unsafe pagination destination.") from None

    @staticmethod
    def _remaining(deadline):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise ExportError("The API wait deadline was reached.")
        return remaining

    @staticmethod
    def _retry_delay(response, attempt):
        value = response.headers.get("Retry-After", "") if response is not None else ""
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 2**attempt
        return min(15.0, max(0.0, delay)) + random.uniform(0, 0.25)

    def _request(self, method, path, *, params=None, json=None, authenticated=True, deadline=None):
        url = self._url(path)
        headers = {"Accept": "application/json"}
        if authenticated:
            if not self.token:
                raise ExportError("Authenticate first or provide an API token.")
            headers["Authorization"] = f"Token {self.token}"
        for attempt in range(3 if method == "GET" else 1):
            remaining = self._remaining(deadline)
            timeout = (
                (10, 60) if remaining is None else (min(10, remaining / 2), min(60, remaining / 2))
            )
            response = None
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout):
                self._remaining(deadline)
                if method != "GET" or attempt == 2:
                    raise ExportError(
                        "Could not reach the API. A submitted export may already exist; "
                        "check before submitting again."
                    ) from None
            except requests.RequestException:
                raise ExportError("The API request could not be completed.") from None
            else:
                try:
                    self._remaining(deadline)
                    code = response.status_code
                    if 200 <= code < 300:
                        try:
                            data = response.json()
                        except ValueError:
                            raise ExportError(
                                "The API returned an invalid JSON response."
                            ) from None
                        if not isinstance(data, dict):
                            raise ExportError("The API returned an unexpected response format.")
                        return data
                    if not (
                        method == "GET" and attempt < 2 and (code == 429 or 500 <= code <= 599)
                    ):
                        hint = (
                            " Check credentials and organization access."
                            if code in (401, 403)
                            else ""
                        )
                        raise ExportError(f"API request failed (HTTP {code}).{hint}")
                finally:
                    response.close()
            delay = self._retry_delay(response, attempt)
            remaining = self._remaining(deadline)
            time.sleep(delay if remaining is None else min(delay, remaining))
        raise ExportError("The API request could not be completed.")

    def login(self, username, password) -> str:
        data = self._request(
            "POST",
            "api-token-auth/",
            json={"username": username, "password": password},
            authenticated=False,
        )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ExportError("The API did not return an authentication token.")
        self.token = token
        return token

    def organizations(self) -> list[dict]:
        organizations = self._request("GET", "current_user/").get("organizations")
        if not isinstance(organizations, list) or any(
            not isinstance(org, dict)
            or type(org.get("id")) is not int
            or not isinstance(org.get("display_name"), str)
            for org in organizations
        ):
            raise ExportError("The API returned an invalid organization list.")
        return [{"id": org["id"], "display_name": org["display_name"]} for org in organizations]

    def _tags(self, organization):
        path, params, seen = "tags/", {"organization": organization}, set()
        while path:
            url = self._url(path)
            if url in seen or len(seen) >= 1000:
                raise ExportError("The API returned invalid tag pagination.")
            seen.add(url)
            data = self._request("GET", url, params=params)
            rows = data.get("results")
            if not isinstance(rows, list) or any(
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or type(row.get("id")) is not int
                or row["id"] < 1
                for row in rows
            ):
                raise ExportError("The API returned an invalid tag list.")
            yield from rows
            path, params = data.get("next"), None
            if path is not None and not isinstance(path, str):
                raise ExportError("The API returned invalid tag pagination.")

    def mission_ids(self, organization: int, tags: list[str] | None = None) -> list[str]:
        _positive_id(organization, "Organization")
        if organization not in {org["id"] for org in self.organizations()}:
            raise ExportError("The selected organization is not available to this account.")
        tags = [] if tags is None else tags
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags
        ):
            raise ExportError("Tag names must be nonempty strings.")
        params = [("organization", organization), ("archived", "false")]
        if tags:
            available = {row.get("name"): row.get("id") for row in self._tags(organization)}
            if any(tag not in available or type(available[tag]) is not int for tag in tags):
                raise ExportError(
                    "One or more tag names were not found in the selected organization."
                )
            params.extend(("tags", available[tag]) for tag in dict.fromkeys(tags))
            params.append(("tag_logic", "all"))
        data = self._request("GET", "missions/flat_values/", params=params)
        ids = data.get("results", {}).get("id") if isinstance(data.get("results"), dict) else None
        if not isinstance(ids, list) or any(not isinstance(mid, str) or not mid for mid in ids):
            raise ExportError("The API returned an invalid mission selection.")
        return list(dict.fromkeys(ids))

    def create_export(
        self,
        organization: int,
        missions: list[str],
        include_metrics: bool = False,
        *,
        data_options: dict[str, bool] | None = None,
    ) -> dict:
        _positive_id(organization, "Organization")
        if (
            not isinstance(missions, list)
            or not missions
            or any(not isinstance(mid, str) or not mid for mid in missions)
        ):
            raise ExportError("No missions selected. An empty selection cannot be exported.")
        if type(include_metrics) is not bool:
            raise ExportError("The metrics option must be a boolean.")
        metadata = dict.fromkeys(("events", "sensors", "tags", "mission_logs"), True)
        if data_options is not None:
            if not isinstance(data_options, dict) or any(
                key not in metadata for key in data_options
            ):
                raise ExportError(
                    "Data options may contain only events, sensors, tags and mission_logs."
                )
            if any(type(value) is not bool for value in data_options.values()):
                raise ExportError("Data options must be boolean values.")
            metadata.update(data_options)
        data = self._request(
            "POST",
            "data_export_request/",
            json={
                "kind": "all data",
                "missions": missions,
                "filters": {"organization": organization, "metrics": include_metrics, **metadata},
            },
        )
        if type(data.get("id")) is not int or data["id"] < 1:
            raise ExportError(
                "The API did not return a valid job ID. The export may exist; "
                "check before submitting again."
            )
        return data

    def get_export(self, job_id: int, *, deadline: float | None = None) -> dict:
        return self._request(
            "GET", f"data_export_request/{_positive_id(job_id, 'Job ID')}/", deadline=deadline
        )
