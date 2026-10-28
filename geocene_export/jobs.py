"""Wait for an export and safely save its ZIP without API credentials."""

import math
import os
import tempfile
import time
import zipfile
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .api import ExportError

EXPECTED_FILES = {"missions.csv", "tags.csv", "mission_logs.csv", "events.csv", "sensors.csv"}


def _resume_message(job_id):
    return f"Continue with `python -m geocene_export resume {job_id}`."


def wait_for_export(client, job_id: int, max_wait=1800, poll_interval=10, on_status=print) -> dict:
    """Poll until status=success; progress=100 alone does not mean ready."""
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in (max_wait, poll_interval)
    ):
        raise ExportError("Wait time and polling interval must be positive, finite numbers.")
    deadline = time.monotonic() + max_wait
    last_message = None
    while True:
        if time.monotonic() >= deadline:
            raise ExportError(
                f"Timed out waiting for export job {job_id}. " + _resume_message(job_id)
            )
        try:
            job = client.get_export(job_id, deadline=deadline)
        except ExportError as exc:
            if time.monotonic() >= deadline:
                raise ExportError(
                    f"Timed out waiting for export job {job_id}. " + _resume_message(job_id)
                ) from None
            raise ExportError(
                f"Could not check export job {job_id}: {exc} " + _resume_message(job_id)
            ) from None
        if time.monotonic() >= deadline:
            raise ExportError(
                f"Timed out waiting for export job {job_id}. " + _resume_message(job_id)
            )
        if not isinstance(job, dict):
            raise ExportError(f"Export job {job_id} returned an invalid status response.")
        status = job.get("status")
        if status == "success":
            return job
        if status in ("error", "failed"):
            raise ExportError(f"Export job {job_id} failed. Contact support with this job ID.")
        if status not in ("pending", "processing"):
            raise ExportError(
                f"Export job {job_id} returned an unknown status. Contact support with this job ID."
            )
        progress = job.get("progress")
        suffix = (
            f" ({progress:g}%)" if type(progress) in (int, float) and 0 <= progress <= 100 else ""
        )
        message = f"Export job {job_id}: {status}{suffix}"
        if on_status is not None and message != last_message:
            on_status(message)
            last_message = message
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


class _NoCredentials(requests.auth.AuthBase):
    def __call__(self, request):
        # Explicit auth prevents Requests from reading .netrc. Redirects below
        # are manual, so its redirect handler cannot reintroduce .netrc auth.
        return request


def _https_url(url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        parsed.port  # Reject malformed ports before issuing any request.
    except (TypeError, ValueError):
        raise ExportError(
            "The export download link must be a valid HTTPS URL without embedded credentials."
        ) from None
    return url


def _download_response(session, url, deadline):
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExportError("The download timed out. Resume the export to try downloading again.")
        response = session.get(
            _https_url(url),
            stream=True,
            allow_redirects=False,
            timeout=(min(10, remaining / 2), min(60, remaining / 2)),
            headers={"Accept-Encoding": "identity"},
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ExportError("The download server returned a redirect without a destination.")
        url = _https_url(urljoin(url, location))
    raise ExportError("The download server returned too many redirects.")


def download_export(
    job: dict,
    output: Path,
    overwrite=False,
    *,
    download_timeout=1800,
    expected_files: set[str] | None = None,
) -> Path:
    """Stream, validate, then atomically publish an export; preserve old files."""
    if (
        type(download_timeout) not in (int, float)
        or not math.isfinite(download_timeout)
        or download_timeout <= 0
    ):
        raise ExportError("Download timeout must be a positive, finite number.")
    if expected_files is None:
        expected_files = EXPECTED_FILES
    elif (
        not isinstance(expected_files, (list, set))
        or any(not isinstance(name, str) for name in expected_files)
        or not set(expected_files).issubset(EXPECTED_FILES)
        or "missions.csv" not in expected_files
    ):
        raise ExportError(
            "Expected files must be a list or set of known export CSV filenames "
            "including missions.csv."
        )
    expected_files = set(expected_files)
    if job.get("status") != "success":
        raise ExportError("The export is not ready to download; its status must be success.")
    url = _https_url(job.get("download_url"))
    output = Path(output).expanduser()
    if output.exists() and not overwrite:
        raise ExportError(
            f"Output already exists: {output}. Choose another path or use --overwrite."
        )
    if not output.parent.is_dir():
        raise ExportError(f"The output directory does not exist: {output.parent}")
    deadline = time.monotonic() + download_timeout
    temp_path = None
    try:
        with requests.Session() as session:
            session.auth = _NoCredentials()
            # Keep environment proxy/CA support for corporate networks while
            # suppressing .netrc and never sharing the API session or headers.
            for attempt in range(3):
                retry_delay = 2**attempt
                try:
                    with _download_response(session, url, deadline) as response:
                        if response.status_code in (429, 500, 502, 503, 504):
                            value = response.headers.get("Retry-After", "")
                            try:
                                retry_delay = float(value)
                            except (TypeError, ValueError):
                                try:
                                    retry_date = parsedate_to_datetime(value)
                                    if retry_date.tzinfo is None:
                                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                                    retry_delay = (
                                        retry_date - datetime.now(timezone.utc)
                                    ).total_seconds()
                                except (TypeError, ValueError, OverflowError):
                                    pass
                            if not math.isfinite(retry_delay):
                                retry_delay = 2**attempt
                            raise requests.RequestException("Transient download response")
                        if response.status_code != 200:
                            hint = (
                                " The stored link may have expired; contact support."
                                if response.status_code in (401, 403)
                                else " Retry the download."
                            )
                            raise ExportError(
                                f"Download failed (HTTP {response.status_code}).{hint}"
                            )
                        if (
                            response.headers.get("Content-Encoding", "identity").lower()
                            != "identity"
                        ):
                            raise ExportError(
                                "The download server returned an unsupported content encoding."
                            )
                        try:
                            expected_size = (
                                int(response.headers["Content-Length"])
                                if "Content-Length" in response.headers
                                else None
                            )
                        except (ValueError, TypeError):
                            raise ExportError(
                                "The download server returned an invalid file size."
                            ) from None
                        if expected_size is not None and expected_size < 0:
                            raise ExportError("The download server returned an invalid file size.")
                        with tempfile.NamedTemporaryFile(
                            dir=output.parent,
                            prefix=f".{output.name}.",
                            suffix=".part",
                            delete=False,
                        ) as temp:
                            temp_path = Path(temp.name)
                            received = 0
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if time.monotonic() >= deadline:
                                    raise ExportError(
                                        "The download timed out. "
                                        "Resume the export to try downloading again."
                                    )
                                received += len(chunk)
                                temp.write(chunk)
                            if expected_size is not None and received != expected_size:
                                raise requests.RequestException("Incomplete download")
                            temp.flush()
                            os.fsync(temp.fileno())
                    break
                except requests.RequestException:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                        temp_path = None
                    remaining = deadline - time.monotonic()
                    if attempt == 2 or remaining <= 0:
                        raise ExportError(
                            "Download interrupted after bounded retries. "
                            "Resume the export to try again."
                        ) from None
                    time.sleep(min(max(0, retry_delay), remaining))
        try:
            with zipfile.ZipFile(temp_path) as archive:
                if not expected_files.issubset(archive.namelist()):
                    raise ExportError(
                        "The ZIP is missing expected export CSV files. "
                        "Contact support with the export job ID."
                    )
                # ZipExtFile verifies each member's CRC when read to EOF. Read
                # in bounded chunks so validation obeys the download deadline.
                for item in archive.infolist():
                    with archive.open(item) as member:
                        while True:
                            if time.monotonic() >= deadline:
                                raise ExportError(
                                    "ZIP validation timed out. Retry the download "
                                    "with a longer --download-timeout."
                                )
                            if not member.read(1024 * 1024):
                                break
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
            raise ExportError(
                "The downloaded file is not a readable, valid ZIP. Resume the export to try again."
            ) from None
        if overwrite:
            os.replace(temp_path, output)
        else:
            os.link(temp_path, output)  # Exclusive creation also closes the exists-check race.
        return output
    except FileExistsError:
        raise ExportError(
            f"Output already exists: {output}. Choose another path or use --overwrite."
        ) from None
    except OSError as exc:
        raise ExportError(
            f"Could not save export to {output}: {exc.strerror or 'filesystem error'}."
        ) from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
