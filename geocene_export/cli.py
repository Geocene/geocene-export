"""The full workflow: authenticate, select, create once, wait, download."""

import argparse
import json
import math
import os
import shlex
import sys
from datetime import date
from pathlib import Path

from .api import Client, ExportError
from .jobs import download_export, wait_for_export
from .prompts import authenticate, choose_data, choose_organization

DATA_FILES = {
    "events": "events.csv",
    "sensors": "sensors.csv",
    "tags": "tags.csv",
    "mission_logs": "mission_logs.csv",
}


def report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def positive_id(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Export Geocene data to a ZIP file.")
    subcommands = command.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "Create an export, wait for it, and download it"),
        ("resume", "Wait for and download an existing export"),
        ("status", "Check an existing export without waiting"),
        ("token", "Sign in and print an API token for scheduler or Make setup"),
    ):
        subcommand = subcommands.add_parser(name, help=help_text)
        subcommand.add_argument(
            "--base-url", default=os.environ.get("GEOCENE_API_URL", "https://collect.geocene.com")
        )
        subcommand.add_argument("--non-interactive", action="store_true", help="Never prompt")
        if name in ("resume", "status"):
            subcommand.add_argument("job_id", type=positive_id)
        if name in ("run", "resume"):
            subcommand.add_argument("--output", type=Path, default=None)
            subcommand.add_argument("--overwrite", action="store_true")
            subcommand.add_argument(
                "--max-wait", type=positive_number, default=1800, metavar="SECONDS"
            )
            subcommand.add_argument(
                "--poll-interval", type=positive_number, default=10, metavar="SECONDS"
            )
            subcommand.add_argument(
                "--download-timeout", type=positive_number, default=1800, metavar="SECONDS"
            )
        if name == "run":
            subcommand.add_argument("--organization", type=positive_id)
            subcommand.add_argument(
                "--tag", action="append", default=[], help="Exact name; repeat for AND"
            )
            metrics = subcommand.add_mutually_exclusive_group()
            metrics.add_argument(
                "--include-metrics",
                dest="include_metrics",
                action="store_true",
                help="Include metrics (large measurement files)",
            )
            metrics.add_argument(
                "--exclude-metrics",
                dest="include_metrics",
                action="store_false",
                help="Exclude metrics (default)",
            )
            subcommand.set_defaults(include_metrics=False)
            for data_type in DATA_FILES:
                subcommand.add_argument(
                    f"--exclude-{data_type.replace('_', '-')}", action="store_true"
                )
    return command


def job_file(output: Path) -> Path:
    return output.with_name(output.name + ".job.json")


def read_job(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or type(saved.get("job_id")) is not int:
            raise ValueError
        return saved
    except (ValueError, UnicodeError):
        raise ExportError(
            "The job file is invalid. Keep the job ID and choose another --output."
        ) from None


def resume_command(args: argparse.Namespace, output: Path) -> str:
    command = [
        sys.executable,
        "-m",
        "geocene_export",
        "resume",
        str(args.job_id),
        "--output",
        str(output),
        "--base-url",
        args.base_url,
    ]
    if args.overwrite:
        command.append("--overwrite")
    if args.non_interactive:
        command.append("--non-interactive")
    if os.name == "nt":
        # The Windows quickstart uses PowerShell; & handles interpreter paths with spaces.
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in command)
    return shlex.join(command)


def prepare_output(args: argparse.Namespace) -> Path:
    output = (args.output or Path(f"geocene-export-{date.today().isoformat()}.zip")).expanduser()
    output = output.absolute()
    if not output.parent.is_dir():
        raise ExportError("The output folder does not exist. Create it or choose another --output.")
    if output.is_dir() or (output.exists() and not args.overwrite):
        raise ExportError("Output already exists. Choose another --output or use --overwrite.")
    checkpoint = job_file(output)
    args.expected_files = {"missions.csv"}
    if args.command == "run" and checkpoint.exists():
        raise ExportError(
            f"An export job is already recorded in {checkpoint}. "
            "Resume that job or choose another --output."
        )
    if args.command == "resume" and checkpoint.exists():
        saved = read_job(checkpoint)
        if (
            saved.get("job_id") != args.job_id
            or saved.get("api_base_url") != args.base_url.rstrip("/") + "/"
        ):
            raise ExportError("The output's job file belongs to a different job or API server.")
        expected = saved.get("expected_files", ["missions.csv"])
        if not isinstance(expected, list) or not all(isinstance(name, str) for name in expected):
            raise ExportError("The job file contains an invalid data selection.")
        args.expected_files = set(expected)
        if "missions.csv" not in args.expected_files or not args.expected_files <= {
            "missions.csv",
            *DATA_FILES.values(),
        }:
            raise ExportError("The job file contains an invalid data selection.")
    return output


def save_job(client: Client, job_id: int, output: Path, expected_files: set[str]) -> None:
    checkpoint = {
        "job_id": job_id,
        "api_base_url": client.base_url,
        "output": str(output),
        "expected_files": sorted(expected_files),
    }
    try:
        with job_file(output).open("x", encoding="utf-8") as handle:
            json.dump(checkpoint, handle, indent=2)
            handle.write("\n")
    except OSError:
        report("Could not save a job file. Keep the job ID above to resume if interrupted.")


def run(args: argparse.Namespace, client: Client, output: Path) -> int:
    organization = choose_organization(client, args.organization, args.non_interactive)
    tags = args.tag
    if args.guided and not args.non_interactive and sys.stdin.isatty() and not tags:
        tag = input("Mission tag (exact name; Enter for all nonarchived missions): ").strip()
        tags = [tag] if tag else []
    options = {key: not getattr(args, f"exclude_{key}") for key in DATA_FILES}
    if args.guided and not args.non_interactive and sys.stdin.isatty():
        options, args.include_metrics = choose_data(options, args.include_metrics)
    args.expected_files = {"missions.csv"} | {
        DATA_FILES[key] for key, included in options.items() if included
    }
    missions = client.mission_ids(organization, tags)
    if not missions:
        raise ExportError("No matching nonarchived missions. No export was created.")
    report(f"Selected {len(missions)} missions in organization {organization}.")
    report("Files: " + ", ".join(sorted(args.expected_files)))
    report("Metrics included." if args.include_metrics else "Metrics excluded.")
    if not args.non_interactive and sys.stdin.isatty():
        if input("Create this export? [Y/n]: ").strip().lower() not in ("", "y", "yes"):
            report("Stopped before creating an export.")
            return 0
    job = client.create_export(
        organization, missions, include_metrics=args.include_metrics, data_options=options
    )
    args.job_id = job["id"]
    report(f"Export job {args.job_id} created.")
    report(f"Resume with: {resume_command(args, output)}")
    save_job(client, args.job_id, output, args.expected_files)
    return finish(args, client, output)


def finish(args: argparse.Namespace, client: Client, output: Path) -> int:
    report(f"Job {args.job_id}; destination: {output}")
    job = wait_for_export(
        client,
        args.job_id,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
        on_status=report,
    )
    download_export(
        job,
        output,
        overwrite=args.overwrite,
        download_timeout=args.download_timeout,
        expected_files=args.expected_files,
    )
    checkpoint = job_file(output)
    try:
        if checkpoint.exists():
            saved = read_job(checkpoint)
            if saved.get("job_id") == args.job_id and saved.get("api_base_url") == client.base_url:
                checkpoint.unlink()
    except (OSError, ExportError):
        report(
            "File saved. Could not remove the old job file; "
            "remove it before another export to this path."
        )
    print(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(arguments or ["run"])
    args.guided = not arguments
    output = None
    try:
        output = prepare_output(args) if args.command in ("run", "resume") else None
        with Client(base_url=args.base_url) as client:
            authenticate(client, args.non_interactive)
            if args.command == "token":
                print(client.token)
                return 0
            if args.command == "status":
                job = client.get_export(args.job_id)
                print(json.dumps({key: job.get(key) for key in ("id", "status", "progress")}))
                return 0
            if args.command == "run":
                return run(args, client, output)
            return finish(args, client, output)
    except (ExportError, OSError, ValueError) as error:
        report(f"Error: {error}")
        if output is not None and hasattr(args, "job_id"):
            report(f"Resume with: {resume_command(args, output)}")
        return 1
    except (KeyboardInterrupt, EOFError):
        report(
            "Stopped locally. Any submitted export continues on the server; "
            "resume using its job ID."
        )
        if output is not None and hasattr(args, "job_id"):
            report(f"Resume with: {resume_command(args, output)}")
        return 130
