# Make workflows

These two importable blueprint files are a starting kit for a tagged Geocene
export. They use Make's legacy HTTP v3 modules with a dedicated API-key keychain.
**They have been checked as JSON and reviewed against exported module schemas;
they have not yet been imported or run in a connected Make account.** Complete
the setup and first-run checks below before sharing them as a supported template.

- [Request export](01-request-export.blueprint.json): select nonarchived missions in
  one organization with one tag, create one export with your data choices, and
  remember its job ID. Metrics default to excluded; the other data files default
  to included.
- [Poll and download](02-poll-download.blueprint.json): check existing jobs once
  per run, store their status, and retrieve the completed ZIP into Make.

The polling blueprint does not save to your computer or cloud drive. Add your
chosen storage module to turn it into a scheduled backup. For a ZIP saved to
your computer, use [the CLI](../README.md).

## One-time setup

1. After [installing the tool](../README.md#start-here), run
   `python -m geocene_export token` in its environment to obtain a token, or use
   `POST /api-token-auth/` as described in [the API contract](../API.md).
   In Make, create an HTTP API-key keychain:
   **Key** = `Token YOUR_TOKEN`, **placement** = header, **parameter name** =
   `Authorization`. Do not put a token or password in these blueprint files.
2. Create a Make data store named `Geocene export jobs`. Generate its data
   structure from [job-record.sample.json](job-record.sample.json). Keep
   `attempts` as **Number**; use **Text** for every other field, including
   `submitted_at` and `job_id`. The sample values are structural examples.
3. Create two empty scenarios. Use each scenario's menu > **Import blueprint**
   to import one file. Open and save every module that requests configuration.
   Select the same job data store in every Data store module, in both scenarios.
   Recheck the record mappings after selecting it. The search module's filter
   must be `state Equal to pending`, with a limit of five records.
4. Select the Geocene API keychain in request modules **3 and 5** of the first
   scenario and module **5** of the polling scenario. The polling scenario's
   **Download a file** module has no API keychain or Authorization header:
   its URL is already signed.
5. In the request scenario's first module, set `organization_id` and
   `mission_tag_id` to your positive **organization ID** and **tag ID**. Both are
   intentionally blank. These are
   numeric IDs, not display names. Find organization IDs with `GET /current_user/`
   and tag IDs with `GET /tags/?organization=YOUR_ORG_ID`; follow pagination when
   looking through tags. Use the same Geocene account for discovery and exports.
6. The same settings module contains five Boolean inclusion options. Keep these
   as actual booleans, not quoted text: `include_metrics` defaults to `false`;
   `include_events`, `include_sensors`, `include_tags`, and `include_mission_logs`
   default to `true`. `include_tags` controls **tags.csv**; it is separate from
   `mission_tag_id`, which selects missions. The backend always includes
   **missions.csv**.
7. Open **Build JSON from inclusion options** (module 4). Create its data structure using
   [export-body.sample.json](export-body.sample.json). Verify `missions` is an
   **Array of Text**, `filters.organization` is **Number**, and
   all five inclusion fields in `filters` are **Boolean**. Recheck these
   mappings after selecting it:

   | Field | Value |
   |---|---|
   | `kind` | `all data` |
   | `missions` | Module 3 → Data → results → id (map the entire array) |
   | `filters.organization` | Module 1 → organization_id |
   | `filters.metrics` | Module 1 → include_metrics (default No / `false`) |
   | `filters.events` | Module 1 → include_events (default Yes / `true`) |
   | `filters.sensors` | Module 1 → include_sensors (default Yes / `true`) |
   | `filters.tags` | Module 1 → include_tags (default Yes / `true`) |
   | `filters.mission_logs` | Module 1 → include_mission_logs (default Yes / `true`) |

8. Keep both scenarios **On demand** for the first-run checks. Clicking **Run once**
   in the request scenario submits a new export. After testing file delivery,
   schedule the request scenario at the required frequency (for example, weekly)
   and the polling scenario at an interval supported by your Make plan.
   Neither blueprint sets or activates a schedule.

This first Make version requires a single tag. For an organization-wide export,
tag names, or several required tags, use the CLI. Do not work around an empty
selection by removing the `missions` field: an empty array has unsafe "all"
semantics on the current API. The request blueprint stops before creation when
the selected mission array is empty.

## Save the file to your destination

The success route is:

`Save download URL → Download a file → Record downloaded_to_make`

Insert your provider's **Upload a file** module between the final two modules.
Map **Data** from Download a file (module 8 → `data`). Set a ZIP filename such as
`geocene-<job_id>.zip`, using the job ID from the search record. Choose your
destination folder or bucket in that module. After validating the upload,
change the final record's `state` to `saved`. Use the job ID in the destination
name so retrying the same job can replace or reuse the same object.

Add a **Skip** error handler (called **Ignore** in older interfaces) to your
Upload module, matching the handlers on polling modules 5 and 8. A failed upload
must skip the rest of this job's route, including the final `saved` record,
and allow other jobs to continue. Its job record remains pending for the next
scheduled run.

Without a storage module, the template records `downloaded_to_make` after it
fetches the file. That state means the file reached this Make run; it does not
mean a durable backup exists. Do not enable unattended backup until the storage
step is configured and tested. Make's file-size, transfer, credit, and execution
allowances depend on the customer's plan; large exports may require the CLI.
The Make blueprint does not validate ZIP completeness or CRCs. For a reliable
backup, validate the archive before recording `saved`, or use the CLI, which
validates the downloaded ZIP before replacing the destination.

## How waiting and recovery work

- Each run reads at most five pending records and makes one job-status request
  per eligible record. There is no sleep/repeater loop. The attempt is persisted
  before the network call. The 97th check of a record already at 96 attempts
  marks it `timed_out` without another API request. This is a check budget, not
  a wall-clock deadline; elapsed time depends on the chosen schedule and backlog.
- `pending` and `processing` retain `state=pending`. Only `success` plus a
  nonempty `download_url` enters the file route. Progress reaching 100 does not
  signal that the ZIP is ready. `error` or `failed` becomes `state=failed`.
- The status router's fourth route is the **fallback**. It records
  `state=needs_attention` for an unknown status or success without a download
  URL. Confirm that route is marked fallback after import.
- The signed URL is saved before downloading. The status request (module 5)
  and download (module 8) each have a **Skip/Ignore** error handler. A request
  failure skips only this job for the current run, leaves its persisted record
  pending, and allows the remaining jobs to run. A later scheduled run checks
  the same job again and obtains its current download URL. Repeated status,
  download, or upload errors consume the same finite 96-attempt budget; they do
  not trigger a new export. Apply the same handler to the storage module you add.
  Other unhandled errors, such as a data-store failure, can still stop a scenario.
  Check execution details and the data-store state: a successful Make run can
  contain a job whose network request was skipped and whose file is still pending.
- Both blueprints leave automatic incomplete-execution retries disabled.
  If export creation times out, inspect existing Geocene export requests before
  trying again: the server may have created the job. If saving the job record
  fails after creation, recover the returned job ID from module 5 and add the
  record manually. Do not rerun the entire request scenario to recover it.
- To retry a `timed_out`, `needs_attention`, or `downloaded_to_make` job, fix the
  cause and edit its store record to `state=pending`, `attempts=0`. Keep the same
  job ID. Resetting the counter explicitly starts a new finite check budget.

## First-run checks before client release

Run once with your own account and a small tagged dataset. Confirm the selected
mission IDs, the explicit `metrics: false` request, a persisted job ID, and the
completed ZIP at your destination. Check that blank IDs and a tag with no
matching nonarchived missions create no export. Check that `processing` with progress
100 waits, the error route records a failure, a missing download URL reaches the
fallback, and a stored attempt count of 96 stops polling. Simulate a failed
status request, download, and upload: each should retain a pending job, consume
one attempt, and allow the next job to run. Rerunning must use the same job ID.

After this account-level check, export the tested scenarios again and use those
exports as the release blueprints. A published Make template with guided setup
is the next distribution step; no Make template has been published by this kit.

See [schema provenance and official references](SOURCES.md).
