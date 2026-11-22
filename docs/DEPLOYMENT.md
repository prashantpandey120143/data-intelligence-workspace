# Deployment and Operating Guide

## 1. Create storage
Run once in a Databricks SQL editor or notebook:
```sql
CREATE SCHEMA IF NOT EXISTS workspace.data_intelligence;
CREATE VOLUME IF NOT EXISTS workspace.data_intelligence.app_files;
```

## 2. Workspace source
Create a Workspace folder, for example:
`/Workspace/Users/<your-email>/data-intelligence-workspace`

Upload these files from the repo root:
- `app.py`
- `app.yaml`
- `requirements.txt`

Do not put user datasets in the source folder (the `sample_data/` files are for testing via the app's upload control only).

## 3. Configure the app resource
In Databricks Apps, open the app's resource configuration and add:
- Resource type: UC volume
- UC volume: `/Volumes/workspace/data_intelligence/app_files`
- Permission: `Can read and write`
- Resource key: `volume`

Save the resource.

## 4. Deploy
From the app Overview page:
1. Click Deploy.
2. Select the Workspace folder containing `app.py`, `app.yaml`, and `requirements.txt`.
3. Deploy.
4. Wait for `Running` and `Compute status: Active`.
5. Click the app URL.

## 5. First-use test
Use `Import files` to upload a sample Excel/CSV file. The file is stored under the Volume's `uploads/` folder and retained for future sessions.

## 6. Working with multiple files
Upload more files later. Then use `Datasets to analyze` to choose one or multiple stored files. When multiple files are combined, the app adds `_source_file` and `_ingested_at` fields.

For Excel workbooks with multiple sheets, select the sheet for each selected workbook.

## 7. Automatic vs Manual analysis
Automatic mode ranks candidate measures, dimensions, and date fields using data type, missingness, cardinality, identifier detection, and business-name semantics.

Manual mode lets the user select:
- one or more Measures
- Group by
- Date field
- Aggregation
- Top groups

## 8. Export
On the Data tab click `Export analysis workbook`. The file is downloaded to the browser and also written to the Volume's `exports/` folder.

## 9. Stop the app when finished
From the app Overview page click `Stop` in the top-right.

## 10. Start it later
Open the app again and use the available start/run control. Wait until the status is `Running` before opening the app URL.

## 11. Updating the app
Replace `app.py` and/or `requirements.txt` in the same Workspace source folder, then click `Deploy` again. Re-deploying updates the existing app; a new app is not required.

## 12. Logs
If the app fails to start or an upload fails, open the app's `Logs` page and inspect the latest deployment/runtime error. Do not change the Volume resource unless the logs show a resource/permission problem.

## 13. Git-backed deployments
This repo can be deployed straight from Git (Databricks Git folders) so each deployment maps to a commit or tag.

## Running without Databricks
If `VOLUME_URI` is not set, the app stores files in `./.local_storage` (override with `LOCAL_STORAGE_DIR`). See the main README.
