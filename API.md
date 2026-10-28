# The four API steps

The code uses the existing Geocene API at `https://collect.geocene.com`.
Endpoints have no `/api/` prefix. These examples describe the implementation in
the current Collect checkout; a live-account smoke test remains a release check.

## 1. Authenticate

`POST /api-token-auth/` with a JSON body:

```json
{"username": "you@example.com", "password": "YOUR_PASSWORD"}
```

Read `token` from the response. Send it on API requests as:

```http
Authorization: Token YOUR_TOKEN
```

This is a user token with that account's permissions, not an export-only key.
There is no OAuth refresh flow in this helper. `GET /current_user/` returns the
organizations available to the account.

## 2. Select missions, then create one job

Resolve exact tag names with `GET /tags/?organization=123` (follow `next` pages).
Then select mission IDs:

```http
GET /missions/flat_values/?organization=123&archived=false&tags=17&tag_logic=all
```

The response is `{"results":{"id":["mission-id-1","mission-id-2"]}}`.
Omit `tags` for all nonarchived missions in the organization; repeat it for more tags.

**Stop if that list is empty.** The server treats an empty `missions` array as
an unrestricted selection within the organization.

Create the export with `POST /data_export_request/`:

```json
{
  "kind": "all data",
  "missions": ["mission-id-1", "mission-id-2"],
  "filters": {
    "organization": 123,
    "metrics": false,
    "events": true,
    "sensors": true,
    "tags": true,
    "mission_logs": true
  }
}
```

The response includes an `id`. Keep it immediately. The server's metrics default
is true, so the explicit JSON boolean `false` matters. In this export kind,
`filters.tags` controls the inclusion of `tags.csv`; it does not select missions.
Set `events`, `sensors`, `tags`, or `mission_logs` to false to omit that CSV.
`missions.csv` is always included. The helper records the selected CSV files
with the job ID so resume validates the same selection.

Creation has no idempotency-key support. If a POST times out, it may still have
created a job. Avoid retrying blindly. This tool retries only safe GET requests.

## 3. Wait for that job

Poll `GET /data_export_request/456/` using the same account:

| Status | Action |
| --- | --- |
| `pending`, `processing` | Wait before checking again |
| `success` | Read `download_url` |
| `failed`, `error` | Stop and report the job ID |

Use a maximum wait and bounded retries. `progress: 100` is not completion: the
server can still be creating metadata or uploading the archive.

## 4. Download

Download the returned HTTPS URL with a separate request. **Do not send the API
Authorization header to the download host.** The presigned URL is itself a
credential, so keep it out of logs.

The helper streams to a temporary file, verifies the ZIP and required CSVs, then
publishes it to the requested path. An interrupted or invalid download leaves
any existing destination unchanged. Server-side metadata errors can otherwise
produce an apparently successful job with missing CSVs; validation catches that.

The API does not currently offer cancellation, expiring/scoped integration keys,
or a dedicated download-URL refresh endpoint. `after` is not implemented by the
current full-export worker, so this helper does not promise incremental exports.
