-- Run once in a Databricks SQL editor or notebook.
-- Change the catalog only if your Free Edition workspace does not use `workspace`.

CREATE SCHEMA IF NOT EXISTS workspace.data_intelligence;

CREATE VOLUME IF NOT EXISTS workspace.data_intelligence.app_files;

-- Optional verification
SHOW VOLUMES IN workspace.data_intelligence;
DESCRIBE VOLUME workspace.data_intelligence.app_files;
