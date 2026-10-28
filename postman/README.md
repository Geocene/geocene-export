# Export with Postman

Use [Postman desktop](https://www.postman.com/downloads/). No Python needed.

## Setup

1. Drag both files into Postman:
   - [Collection](geocene-export.postman_collection.json)
   - [Environment](geocene-export.postman_environment.json)
2. Select the imported **Geocene Export** environment.
3. Open its variables and enter your `username` (email) and `password`.
   Keep credentials local; do not click **Share**.

## Export

Open each numbered request and click **Send**:

| Request | What to do |
| --- | --- |
| **1. Sign in** | Saves your token automatically. |
| **2. Organizations** | Copy your organization's ID into `organization_id`. |
| **3. Tags** (optional) | Copy a tag's ID into `tag_id`. Leave it blank for all nonarchived missions. If `next` is present, increase `tags_page` to see more tags. |
| **4. Select missions** | Saves the matching mission IDs. Zero matches stops the export. |
| **5. Create export** | Send once. Saves the job ID. |
| **6. Check export** | Repeat every 10 seconds until `status` is `success`, up to 30 minutes. Stop on `error` or `failed`. |
| **7. Download ZIP** | Use the dropdown beside **Send → Send and Download**. Save as `export.zip`. |

Use individual requests, rather than running the whole collection.

## Choose data

Change these environment variables before **Create export**:

| Variable | Default |
| --- | --- |
| `include_metrics` | `false` |
| `include_events` | `true` |
| `include_sensors` | `true` |
| `include_tags` | `true` |
| `include_mission_logs` | `true` |

Set a value to `false` to omit that data. `missions.csv` is always included.
`tag_id` selects missions; `include_tags` controls whether `tags.csv` is included.

## Resume or start again

- **Resume:** keep `job_id`, then use **Check export** and **Download ZIP**.
- **New export:** clear `job_id`, then repeat **Select missions** and **Create export**.
- **`job_id` is `unknown`:** creation may have reached the server. Check existing
  Geocene exports before clearing it and trying again.

If you already have a token, enter it in `token` without the `Token ` prefix and
skip **Sign in**. Do not share filled environment files.

Collection scripts are tested offline. A live Postman import and export still
need checking before client release.

Offline script checks: `node tests/test_postman.js` from the repository folder.

Postman help: [import files](https://learning.postman.com/docs/getting-started/importing-and-exporting/importing-data)
· [local variables](https://learning.postman.com/latest-v-12/docs/use/send-requests/variables/define-variables)
· [download files](https://support.postman.com/hc/en-us/articles/360033875073-I-m-experiencing-a-long-response-time)
