"""Interactive setup. Automation supplies a token and organization instead."""

import getpass
import os
import sys

from .api import Client, ExportError


def authenticate(client: Client, non_interactive: bool) -> None:
    token = os.environ.get("GEOCENE_API_TOKEN", "").strip()
    if token:
        client.token = token
        return
    if non_interactive or not sys.stdin.isatty():
        raise ExportError("Set GEOCENE_API_TOKEN for unattended use.")
    username = input("Geocene email: ").strip()
    password = getpass.getpass("Password (hidden): ")
    client.login(username, password)
    # The password and token are never written to disk by this tool.


def choose_organization(client: Client, organization: int | None, non_interactive: bool) -> int:
    organizations = client.organizations()
    available = {item["id"]: item for item in organizations}
    if organization is not None:
        if organization not in available:
            raise ExportError("Your account does not have access to that organization.")
        return organization
    if non_interactive or not sys.stdin.isatty():
        raise ExportError("Supply --organization for unattended use.")
    if not available:
        raise ExportError("Your account has no available organizations.")
    for item in organizations:
        print(f"  {item['id']}: {item['display_name']}", file=sys.stderr)
    try:
        selected = int(input("Organization ID: "))
    except ValueError as error:
        raise ExportError("Enter one of the organization IDs shown above.") from error
    if selected not in available:
        raise ExportError("Enter one of the organization IDs shown above.")
    return selected


def choose_data(options: dict[str, bool], include_metrics: bool) -> tuple[dict[str, bool], bool]:
    def ask(label: str, default: bool) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = input(f"Include {label}? {suffix}: ").strip().lower()
            if answer in ("", "y", "yes", "n", "no"):
                return default if not answer else answer in ("y", "yes")
            print("Enter y or n.", file=sys.stderr)

    print("Choose data to export. Missions are always included.", file=sys.stderr)
    metrics = ask("metrics (large measurement files)", include_metrics)
    selected = {key: ask(key.replace("_", " "), value) for key, value in options.items()}
    return selected, metrics
