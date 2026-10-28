# Geocene Export

Create an export, wait for it, and download the ZIP. Metrics are excluded by default.

## Start here

Requires **Python 3.10+**. [Download the ZIP](https://github.com/Geocene/geocene-export/archive/refs/heads/main.zip), extract it, and open a terminal in its folder.

**macOS / Linux**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

On Windows, replace `python` below with `.\.venv\Scripts\python.exe`.
On macOS/Linux, run `source .venv/bin/activate` again when opening a new terminal.

## Guided export

```sh
python -m geocene_export
```

Sign in, choose your organization, tag, and data, then confirm.

## Automated export

Get a token:

```sh
python -m geocene_export token
```

Copy the printed token into an environment variable:

```sh
export GEOCENE_API_TOKEN="YOUR_TOKEN"
```

PowerShell: `$env:GEOCENE_API_TOKEN = "YOUR_TOKEN"`

Use the token alone, without a `Token ` prefix. Then run:

```sh
python -m geocene_export run --non-interactive --organization 123 --output latest.zip --overwrite
```

Replace `123` with your organization ID. `--overwrite` replaces the previous ZIP.

For scheduled jobs, set `GEOCENE_API_TOKEN` in the job and use absolute paths for
Python and the output file. The `export` command only affects the current terminal.

## Choose data

| Option | Effect |
| --- | --- |
| `--include-metrics` | Include metrics |
| `--exclude-metrics` | Exclude metrics (default) |
| `--exclude-events` | Omit `events.csv` |
| `--exclude-sensors` | Omit `sensors.csv` |
| `--exclude-tags` | Omit `tags.csv` |
| `--exclude-mission-logs` | Omit `mission_logs.csv` |

All CSVs are included by default. `missions.csv` is always included.

Add `--tag "Campaign name"` to select missions with that exact tag. Repeat `--tag`
to require all supplied tags. Omit it to export all nonarchived missions in the organization.

## Resume

If interrupted, resume the existing job using its printed ID:

```sh
python -m geocene_export status 456
python -m geocene_export resume 456 --output latest.zip --overwrite
```

Use the same account and output path. Keep the `.job.json` file until completion.

## More

Options: `python -m geocene_export run --help`

[Postman guide](postman/README.md) · [API details](API.md) · [Make workflows](make/README.md)
