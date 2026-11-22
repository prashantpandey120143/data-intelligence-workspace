import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

APP_TITLE = "Data Intelligence Workspace"
DEFAULT_MAX_MB = 50
MAX_PREVIEW_ROWS = 250
MAX_ANALYSIS_ROWS = 500_000
MAX_CATEGORY_LEVELS = 15

st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")


# --------------------------------------------------------------------------
# Storage backend
# - On Databricks Apps (VOLUME_URI is set): files live in a Unity Catalog Volume.
# - Anywhere else (local run, Streamlit Community Cloud): files live in a local
#   folder (LOCAL_STORAGE_DIR, default ./.local_storage).
# --------------------------------------------------------------------------
VOLUME_URI = os.getenv("VOLUME_URI", "").strip()
USE_DATABRICKS = bool(VOLUME_URI)

if USE_DATABRICKS:
    if not VOLUME_URI.startswith("/Volumes/"):
        raise RuntimeError(f"Unexpected volume resource path: {VOLUME_URI}")
    from databricks.sdk import WorkspaceClient

    VOLUME_ROOT = VOLUME_URI.rstrip("/")
    DBX = WorkspaceClient()
    STORAGE_LABEL = "Unity Catalog Volume"
else:
    VOLUME_ROOT = str(Path(os.getenv("LOCAL_STORAGE_DIR", ".local_storage")).resolve())
    DBX = None
    STORAGE_LABEL = "local folder"

DATA_DIR = f"{VOLUME_ROOT}/uploads"
META_DIR = f"{VOLUME_ROOT}/metadata"
EXPORT_DIR = f"{VOLUME_ROOT}/exports"


class _Entry:
    """Minimal stand-in for the Databricks directory entry (only .path is used)."""

    def __init__(self, path: str):
        self.path = path


def ensure_volume_dirs() -> None:
    for folder in (DATA_DIR, META_DIR, EXPORT_DIR):
        try:
            if USE_DATABRICKS:
                DBX.files.create_directory(folder)
            else:
                Path(folder).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            text = str(exc).lower()
            if "already exists" not in text and "exist" not in text:
                raise RuntimeError(f"Could not access storage directory {folder}: {exc}") from exc


def volume_write_bytes(path: str, data: bytes, overwrite: bool = True) -> None:
    if USE_DATABRICKS:
        DBX.files.upload(path, io.BytesIO(data), overwrite=overwrite)
        return
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def volume_read_bytes(path: str) -> bytes:
    if USE_DATABRICKS:
        response = DBX.files.download(path)
        with response.contents as handle:
            return handle.read()
    return Path(path).read_bytes()


def volume_list(path: str):
    if USE_DATABRICKS:
        return list(DBX.files.list_directory_contents(path))
    folder = Path(path)
    return [_Entry(str(p)) for p in sorted(folder.iterdir())] if folder.exists() else []


ensure_volume_dirs()


def safe_name(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "dataset"
    return f"{stem}{suffix}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def meta_path(dataset_id: str) -> str:
    return f"{META_DIR}/{dataset_id}.json"


def load_catalog() -> list[dict]:
    records: list[dict] = []
    for item in volume_list(META_DIR):
        if not item.path.lower().endswith(".json"):
            continue
        try:
            records.append(json.loads(volume_read_bytes(item.path).decode("utf-8")))
        except Exception:
            continue
    return sorted(records, key=lambda x: x.get("uploaded_at", ""), reverse=True)


def save_record(record: dict) -> None:
    volume_write_bytes(
        meta_path(record["dataset_id"]),
        json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8"),
        overwrite=True,
    )


def inspect_file(file_bytes: bytes, filename: str) -> dict:
    suffix = Path(filename).suffix.lower()
    result = {"sheets": [], "sheet": None}
    if suffix in {".xlsx", ".xls"}:
        excel = pd.ExcelFile(io.BytesIO(file_bytes))
        result["sheets"] = excel.sheet_names
        for sheet in excel.sheet_names:
            try:
                probe = pd.read_excel(excel, sheet_name=sheet, nrows=10)
                if not probe.empty and len(probe.columns) > 0:
                    result["sheet"] = sheet
                    break
            except Exception:
                continue
        if result["sheet"] is None and result["sheets"]:
            result["sheet"] = result["sheets"][0]
    return result


def read_dataset(data: bytes, filename: str, sheet_name: str | None = None) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    buffer = io.BytesIO(data)
    if suffix == ".csv":
        return pd.read_csv(buffer)
    if suffix == ".tsv":
        return pd.read_csv(buffer, sep="\t")
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(buffer, sheet_name=sheet_name or 0)
    if suffix == ".parquet":
        return pd.read_parquet(buffer)
    if suffix == ".json":
        try:
            return pd.read_json(buffer)
        except ValueError:
            buffer.seek(0)
            return pd.read_json(buffer, lines=True)
    raise ValueError(f"Unsupported file type: {suffix}")


def clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    seen: dict[str, int] = {}
    names: list[str] = []
    for raw in out.columns:
        base = re.sub(r"\s+", " ", str(raw).strip()) or "unnamed_column"
        count = seen.get(base, 0)
        seen[base] = count + 1
        names.append(base if count == 0 else f"{base}_{count + 1}")
    out.columns = names
    return out


def to_numeric_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    cleaned = (
        series.astype("string")
        .str.strip()
        .str.replace(r"[$,%€£₹,]", "", regex=True)
        .str.replace(r"\(([^)]+)\)", r"-\1", regex=True)
    )
    return pd.to_numeric(cleaned, errors="coerce")


def parse_date_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")
    return pd.to_datetime(series, errors="coerce", format="mixed")


def name_tokens(name: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[A-Za-z0-9]+", str(name))}


def is_id_like(name: str, series: pd.Series, unique_ratio: float) -> bool:
    tokens = name_tokens(name)
    id_tokens = {"id", "code", "key", "uuid", "identifier", "number", "no", "ref", "reference"}
    return bool(tokens & id_tokens) and unique_ratio >= 0.90


def infer_fields(df: pd.DataFrame) -> list[dict]:
    fields: list[dict] = []
    row_count = len(df)
    semantic_measure = re.compile(
        r"(revenue|sales|amount|value|price|cost|expense|income|total|booking|count|quantity|units|profit|margin|balance|score|rating|rate|volume|hours|salary|pay|budget|target|actual|discount|tax|fee|distance|weight|duration)",
        re.I,
    )
    semantic_date = re.compile(r"(date|time|year|month|week|day|period|timestamp)", re.I)
    semantic_dimension = re.compile(r"(region|country|city|state|department|category|segment|type|status|channel|product|customer|employee|team|location|brand|class|tier)", re.I)

    for col in df.columns:
        s = df[col]
        non_empty = int(s.notna().sum())
        missing = int(row_count - non_empty)
        unique = int(s.dropna().astype(str).nunique())
        unique_ratio = unique / max(non_empty, 1)
        numeric = to_numeric_series(s)
        numeric_ratio = float(numeric.notna().sum() / max(non_empty, 1))
        parsed_dates = parse_date_series(s) if not pd.api.types.is_bool_dtype(s) else pd.Series([pd.NaT] * len(s))
        date_ratio = float(parsed_dates.notna().sum() / max(non_empty, 1))
        semantic_measure_hit = bool(semantic_measure.search(str(col)))
        semantic_date_hit = bool(semantic_date.search(str(col)))
        semantic_dimension_hit = bool(semantic_dimension.search(str(col)))
        id_like = is_id_like(str(col), s, unique_ratio)

        if date_ratio >= 0.80 and (semantic_date_hit or not pd.api.types.is_numeric_dtype(s)):
            kind = "date"
        elif numeric_ratio >= 0.80 and not id_like:
            kind = "number"
        elif unique <= max(MAX_CATEGORY_LEVELS, int(row_count * 0.20)) and unique_ratio < 0.50:
            kind = "category"
        elif id_like:
            kind = "identifier"
        else:
            kind = "text"

        confidence = 55
        if kind == "date":
            confidence += 35 if date_ratio >= 0.95 else 25
        elif kind == "number":
            confidence += 30 if numeric_ratio >= 0.95 else 20
        elif kind == "category":
            confidence += 20
        elif kind == "identifier":
            confidence += 25
        else:
            confidence += 10
        if semantic_measure_hit or semantic_date_hit or semantic_dimension_hit:
            confidence += 8
        confidence = min(99, confidence)

        sample = next((str(v) for v in s.tolist() if pd.notna(v) and str(v) != ""), "—")
        fields.append(
            {
                "name": str(col),
                "type": kind,
                "numeric": numeric,
                "dates": parsed_dates,
                "unique": unique,
                "missing": missing,
                "unique_ratio": unique_ratio,
                "confidence": confidence,
                "sample": sample[:120],
                "is_semantic_measure": semantic_measure_hit,
                "is_semantic_date": semantic_date_hit,
                "is_semantic_dimension": semantic_dimension_hit,
                "id_like": id_like,
            }
        )
    return fields


def measure_score(field: dict, row_count: int) -> float:
    non_missing = max(row_count - field["missing"], 1)
    variability = field["unique"] / non_missing
    score = 0.0
    score += 45 if field["is_semantic_measure"] else 10
    score += min(25, 25 * variability)
    score += min(20, field["confidence"] / 5)
    if field["unique_ratio"] < 0.02:
        score -= 10
    if field["id_like"]:
        score -= 80
    return score


def dimension_score(field: dict) -> float:
    score = 0.0
    if field["type"] == "category":
        score += 45
    elif field["type"] == "text":
        score += 15
    if field["is_semantic_dimension"]:
        score += 20
    if 2 <= field["unique"] <= MAX_CATEGORY_LEVELS:
        score += 25
    elif field["unique"] <= 100:
        score += 10
    if field["id_like"]:
        score -= 60
    score += field["confidence"] / 10
    return score


def rank_fields(df: pd.DataFrame, fields: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    measures = sorted([f for f in fields if f["type"] == "number"], key=lambda f: measure_score(f, len(df)), reverse=True)
    dims = sorted([f for f in fields if f["type"] in {"category", "text"}], key=dimension_score, reverse=True)
    dates = sorted(
        [f for f in fields if f["type"] == "date"],
        key=lambda f: (f["is_semantic_date"], f["confidence"], -f["missing"]),
        reverse=True,
    )
    return measures, dims, dates


def infer_all(df: pd.DataFrame) -> dict:
    fields = infer_fields(df)
    measures, dimensions, dates = rank_fields(df, fields)
    return {
        "fields": fields,
        "measures": measures,
        "dimensions": dimensions,
        "dates": dates,
        "primary_measure": measures[0] if measures else None,
        "primary_dimension": dimensions[0] if dimensions else None,
        "primary_date": dates[0] if dates else None,
    }


def format_number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = float(value)
    if abs(value) >= 1_000_000_000:
        return f"{value/1_000_000_000:.1f}B"
    if abs(value) >= 1_000_000:
        return f"{value/1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value/1_000:.1f}K"
    return f"{value:,.0f}"


def aggregate_by_group(df: pd.DataFrame, measure: str, dimension: str, aggregation: str = "Sum", top_n: int = 10) -> pd.DataFrame:
    work = pd.DataFrame({
        dimension: df[dimension].fillna("Unknown").astype(str),
        "__metric": to_numeric_series(df[measure]),
    })
    agg_map = {"Sum": "sum", "Average": "mean", "Median": "median", "Minimum": "min", "Maximum": "max", "Count": "count"}
    func = agg_map[aggregation]
    out = work.groupby(dimension, dropna=False)["__metric"].agg(func).sort_values(ascending=False).head(top_n).reset_index()
    out.columns = [dimension, "Value"]
    return out


def aggregate_over_time(df: pd.DataFrame, measure: str, date_field: str, aggregation: str = "Sum") -> pd.DataFrame:
    dates = parse_date_series(df[date_field])
    work = pd.DataFrame({"Date": dates, "__metric": to_numeric_series(df[measure])}).dropna(subset=["Date"])
    if work.empty:
        return pd.DataFrame(columns=["Date", "Value"])
    span_days = max((work["Date"].max() - work["Date"].min()).days, 0)
    if span_days <= 62:
        period = work["Date"].dt.to_period("D").dt.to_timestamp()
    elif span_days <= 450:
        period = work["Date"].dt.to_period("W").dt.start_time
    else:
        period = work["Date"].dt.to_period("M").dt.to_timestamp()
    work["Date"] = period
    agg_map = {"Sum": "sum", "Average": "mean", "Median": "median", "Minimum": "min", "Maximum": "max", "Count": "count"}
    out = work.groupby("Date")["__metric"].agg(agg_map[aggregation]).reset_index(name="Value")
    return out.sort_values("Date")


def outlier_summary(df: pd.DataFrame, measure_fields: list[str]) -> pd.DataFrame:
    rows: list[dict] = []
    for col in measure_fields:
        s = to_numeric_series(df[col]).dropna()
        if len(s) < 8:
            continue
        q1, q3 = s.quantile([0.25, 0.75])
        iqr = q3 - q1
        if iqr == 0:
            count = 0
        else:
            lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            count = int(((s < lower) | (s > upper)).sum())
        rows.append({
            "Measure": col,
            "Outliers": count,
            "Outlier %": round(100 * count / max(len(s), 1), 2),
            "Median": float(s.median()),
            "P95": float(s.quantile(0.95)),
        })
    return pd.DataFrame(rows).sort_values("Outliers", ascending=False) if rows else pd.DataFrame(columns=["Measure", "Outliers", "Outlier %", "Median", "P95"])


def correlation_pairs(df: pd.DataFrame, measure_fields: list[str]) -> pd.DataFrame:
    cols = [c for c in measure_fields if c in df.columns]
    if len(cols) < 2:
        return pd.DataFrame(columns=["Measure A", "Measure B", "Correlation"])
    corr = df[cols].apply(to_numeric_series).corr(numeric_only=True)
    rows: list[dict] = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            value = corr.loc[a, b]
            if pd.notna(value):
                rows.append({"Measure A": a, "Measure B": b, "Correlation": round(float(value), 3)})
    return pd.DataFrame(rows).sort_values("Correlation", key=lambda s: s.abs(), ascending=False)


def build_recommendations(df: pd.DataFrame, profile: dict, group_data: pd.DataFrame | None, time_data: pd.DataFrame | None, outliers: pd.DataFrame, correlations: pd.DataFrame) -> list[str]:
    recs: list[str] = []
    if profile["missing"]:
        pct = 100 * profile["missing"] / max(df.shape[0] * df.shape[1], 1)
        recs.append(f"{pct:.1f}% of cells are missing. Review high-missing fields before using them for operational decisions.")
    if profile["duplicate_rows"]:
        recs.append(f"There are {profile['duplicate_rows']:,} exact duplicate rows. Check whether these represent duplicate transactions or legitimate repeats.")
    if profile["primary_measure"]:
        recs.append(f"{profile['primary_measure']} is the strongest automatically detected measure for the current dataset.")
    if group_data is not None and not group_data.empty:
        top = group_data.iloc[0]
        recs.append(f"{top.iloc[0]} is the leading group for the selected measure with {format_number(top['Value'])} under the current aggregation.")
    if time_data is not None and len(time_data) >= 2:
        first = float(time_data["Value"].iloc[0])
        last = float(time_data["Value"].iloc[-1])
        if first != 0:
            delta = (last - first) / abs(first) * 100
            direction = "increased" if delta > 0 else "decreased"
            recs.append(f"The time series {direction} about {abs(delta):.1f}% from the first to the last observed period.")
    if not outliers.empty and int(outliers.iloc[0]["Outliers"]) > 0:
        row = outliers.iloc[0]
        recs.append(f"{row['Measure']} has {int(row['Outliers']):,} IQR-based outlier(s) ({row['Outlier %']:.1f}% of non-missing values).")
    if not correlations.empty:
        strongest = correlations.iloc[0]
        if abs(float(strongest["Correlation"])) >= 0.5:
            recs.append(f"{strongest['Measure A']} and {strongest['Measure B']} show a correlation of {strongest['Correlation']:.3f}; investigate whether this reflects a business relationship or a common driver.")
    if len(profile["measures"]) > 1:
        recs.append(f"The dataset contains {len(profile['measures'])} candidate measures, so consider comparing multiple KPIs rather than relying on a single metric.")
    if len(profile["dates"]) > 0:
        recs.append(f"A date field ({profile['primary_date']}) is available for trend analysis.")
    return recs[:8]


def build_export(
    df: pd.DataFrame,
    records: list[dict],
    profile: dict,
    selected_measures: list[str],
    selected_dimension: str | None,
    selected_date: str | None,
    aggregation: str,
    outliers: pd.DataFrame,
    correlations: pd.DataFrame,
    recommendations: list[str],
) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="data")
        summary_rows = [
            ["Datasets included", len(records)],
            ["Source files", ", ".join(r["original_name"] for r in records)],
            ["Rows analyzed", profile["rows"]],
            ["Columns analyzed", profile["columns"]],
            ["Selected measures", ", ".join(selected_measures) if selected_measures else "None"],
            ["Selected group", selected_dimension or "None"],
            ["Selected date", selected_date or "None"],
            ["Aggregation", aggregation],
            ["Missing cells", profile["missing"]],
            ["Duplicate rows", profile["duplicate_rows"]],
            ["Data quality score", f"{profile['quality']}%"],
            ["Average mapping confidence", f"{profile['avg_confidence']}%"],
        ]
        pd.DataFrame(summary_rows, columns=["Metric", "Value"]).to_excel(writer, index=False, sheet_name="summary")

        mapping = pd.DataFrame([
            [f["name"], f["type"], f["unique"], f["missing"], f["confidence"], f["sample"]]
            for f in profile["fields"]
        ], columns=["Column", "Detected type", "Unique values", "Missing", "Confidence", "Sample"])
        mapping.to_excel(writer, index=False, sheet_name="mapping_review")

        if selected_dimension and selected_measures:
            for measure in selected_measures[:5]:
                grouped = aggregate_by_group(df, measure, selected_dimension, aggregation, 20)
                sheet_name = re.sub(r"[^A-Za-z0-9]", "_", f"by_{measure}")[:31] or "group_analysis"
                grouped.to_excel(writer, index=False, sheet_name=sheet_name)

        if selected_date and selected_measures:
            for measure in selected_measures[:5]:
                timed = aggregate_over_time(df, measure, selected_date, aggregation)
                sheet_name = re.sub(r"[^A-Za-z0-9]", "_", f"trend_{measure}")[:31] or "trend"
                timed.to_excel(writer, index=False, sheet_name=sheet_name)

        outliers.to_excel(writer, index=False, sheet_name="outliers")
        correlations.to_excel(writer, index=False, sheet_name="correlations")
        pd.DataFrame({"Recommendation": recommendations}).to_excel(writer, index=False, sheet_name="recommendations")
    return output.getvalue()


# --------------------------- UI ---------------------------
st.title(APP_TITLE)
st.caption("Import one or many files, automatically profile the data, discover meaningful measures and dimensions, and explore trends, outliers, correlations, and recommendations.")

catalog = load_catalog()

with st.sidebar:
    st.subheader("Workspace")
    st.write(f"Stored datasets: **{len(catalog)}**")
    max_mb = st.number_input("Max upload size (MB)", min_value=5, max_value=200, value=DEFAULT_MAX_MB, step=5)
    st.divider()
    st.caption("Supported: CSV, TSV, XLSX, XLS, Parquet, JSON")
    st.caption(f"Files are retained in the configured {STORAGE_LABEL}.")

uploaded = st.file_uploader(
    "Import files",
    type=["csv", "tsv", "xlsx", "xls", "parquet", "json"],
    accept_multiple_files=True,
    help="Upload one or many files. Previously uploaded files remain available for later analysis.",
)

if uploaded:
    existing_hashes = {r.get("content_hash") for r in catalog}
    newly_saved = 0
    for file in uploaded:
        data = file.getvalue()
        if len(data) > max_mb * 1024 * 1024:
            st.error(f"{file.name}: exceeds the {max_mb} MB upload limit configured above.")
            continue
        content_hash = hashlib.sha256(data).hexdigest()[:16]
        if content_hash in existing_hashes:
            continue
        dataset_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{content_hash}"
        stored = f"{DATA_DIR}/{dataset_id}__{safe_name(file.name)}"
        volume_write_bytes(stored, data, overwrite=False)
        info = inspect_file(data, file.name)
        save_record({
            "dataset_id": dataset_id,
            "original_name": file.name,
            "stored_path": stored,
            "uploaded_at": now_iso(),
            "content_hash": content_hash,
            "size_bytes": len(data),
            "sheets": info["sheets"],
            "default_sheet": info["sheet"],
        })
        existing_hashes.add(content_hash)
        newly_saved += 1
    if newly_saved:
        st.success(f"Imported {newly_saved} new file(s). They are now available in the workspace.")

catalog = load_catalog()
if not catalog:
    st.info("No datasets uploaded yet. Use Import files above to start.")
    st.stop()

labels = {r["dataset_id"]: f"{r['original_name']} · {r['uploaded_at'].replace('T', ' ')[:19]} UTC" for r in catalog}
selected_ids = st.multiselect(
    "Datasets to analyze",
    options=list(labels),
    default=[catalog[0]["dataset_id"]],
    format_func=lambda x: labels[x],
)

if not selected_ids:
    st.warning("Select at least one dataset.")
    st.stop()

frames: list[pd.DataFrame] = []
used_records: list[dict] = []

for dataset_id in selected_ids:
    record = next(r for r in catalog if r["dataset_id"] == dataset_id)
    sheet = record.get("default_sheet")
    if record.get("sheets"):
        sheet = st.selectbox(
            f"Sheet for {record['original_name']}",
            record["sheets"],
            index=max(0, record["sheets"].index(sheet)) if sheet in record["sheets"] else 0,
            key=f"sheet_{dataset_id}",
        )
    try:
        raw = volume_read_bytes(record["stored_path"])
        frame = clean_headers(read_dataset(raw, record["original_name"], sheet_name=sheet))
        if len(frame) > MAX_ANALYSIS_ROWS:
            st.warning(f"{record['original_name']} has {len(frame):,} rows. Analysis is capped at {MAX_ANALYSIS_ROWS:,} rows in this Free Edition app session.")
            frame = frame.head(MAX_ANALYSIS_ROWS).copy()
        frame["_source_file"] = record["original_name"]
        frame["_ingested_at"] = record["uploaded_at"]
        frames.append(frame)
        used_records.append(record)
    except Exception as exc:
        st.error(f"Could not read {record['original_name']}: {exc}")

if not frames:
    st.stop()

combined = pd.concat(frames, ignore_index=True, sort=False)
base_inference = infer_all(combined)

# Session-aware analysis controls. Defaults come from automatic inference.
measure_names = [f["name"] for f in base_inference["measures"]]
dimension_names = [f["name"] for f in base_inference["dimensions"]]
date_names = [f["name"] for f in base_inference["dates"]]

with st.sidebar:
    st.divider()
    st.subheader("Analysis controls")
    mode = st.radio("Selection mode", ["Automatic", "Manual"], index=0, help="Automatic uses semantic and statistical scoring. Manual lets you override the fields driving the analysis.")

    if mode == "Automatic":
        selected_measures = measure_names[:5]
        selected_dimension = dimension_names[0] if dimension_names else None
        selected_date = date_names[0] if date_names else None
    else:
        selected_measures = st.multiselect(
            "Measures",
            options=measure_names,
            default=measure_names[: min(3, len(measure_names))],
            help="Choose one or more numeric fields to analyze.",
        )
        selected_dimension = st.selectbox(
            "Group by",
            options=["None"] + dimension_names,
            index=1 if dimension_names else 0,
            help="Choose a categorical/text field for grouped analysis.",
        )
        selected_dimension = None if selected_dimension == "None" else selected_dimension
        selected_date = st.selectbox(
            "Date field",
            options=["None"] + date_names,
            index=1 if date_names else 0,
            help="Choose a date field for time-series analysis.",
        )
        selected_date = None if selected_date == "None" else selected_date

    aggregation = st.selectbox("Aggregation", ["Sum", "Average", "Median", "Minimum", "Maximum", "Count"], index=0)
    top_n = st.slider("Top groups", min_value=5, max_value=25, value=10, step=5)
    st.caption("Automatic mode still allows the application to inspect multiple measures and dimensions. Manual mode lets you override them.")

# Ensure selected fields still exist after schema union.
selected_measures = [m for m in selected_measures if m in combined.columns]
if selected_dimension and selected_dimension not in combined.columns:
    selected_dimension = None
if selected_date and selected_date not in combined.columns:
    selected_date = None

# Profile for the chosen fields.
fields = base_inference["fields"]
field_map = {f["name"]: f for f in fields}
profile = {
    "rows": int(combined.shape[0]),
    "columns": int(combined.shape[1]),
    "fields": fields,
    "measures": base_inference["measures"],
    "dimensions": base_inference["dimensions"],
    "dates": base_inference["dates"],
    "primary_measure": selected_measures[0] if selected_measures else None,
    "primary_dimension": selected_dimension,
    "primary_date": selected_date,
    "missing": int(combined.isna().sum().sum()),
    "duplicate_rows": int(combined.duplicated().sum()),
}
cell_count = max(profile["rows"] * max(profile["columns"], 1), 1)
profile["quality"] = max(0, round(100 - (profile["missing"] / cell_count * 100)))
profile["avg_confidence"] = round(sum(f["confidence"] for f in fields) / max(len(fields), 1))

primary_measure = selected_measures[0] if selected_measures else None
group_data = aggregate_by_group(combined, primary_measure, selected_dimension, aggregation, top_n) if primary_measure and selected_dimension else None
time_data = aggregate_over_time(combined, primary_measure, selected_date, aggregation) if primary_measure and selected_date else None
outliers = outlier_summary(combined, selected_measures)
correlations = correlation_pairs(combined, selected_measures)
recommendations = build_recommendations(combined, profile, group_data, time_data, outliers, correlations)

# KPI row
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Rows", f"{profile['rows']:,}")
k2.metric("Columns", f"{profile['columns']:,}")
k3.metric("Files", f"{len(used_records):,}")
k4.metric("Data quality", f"{profile['quality']}%")
if primary_measure:
    metric_series = to_numeric_series(combined[primary_measure])
    if aggregation == "Sum":
        primary_value = metric_series.sum()
    elif aggregation == "Average":
        primary_value = metric_series.mean()
    elif aggregation == "Median":
        primary_value = metric_series.median()
    elif aggregation == "Minimum":
        primary_value = metric_series.min()
    elif aggregation == "Maximum":
        primary_value = metric_series.max()
    else:
        primary_value = metric_series.count()
    k5.metric(primary_measure, format_number(primary_value))
else:
    k5.metric("Numeric measures", f"{len(base_inference['measures']):,}")

analysis_tab, field_tab, quality_tab, data_tab, history_tab = st.tabs([
    "Overview",
    "Field intelligence",
    "Quality & anomalies",
    "Data",
    "File history",
])

with analysis_tab:
    st.subheader("Automatic analysis")
    if primary_measure and selected_dimension and group_data is not None and not group_data.empty:
        left, right = st.columns(2)
        with left:
            fig = px.bar(group_data, x=selected_dimension, y="Value", title=f"{primary_measure} by {selected_dimension}")
            fig.update_layout(height=420, margin=dict(l=20, r=20, t=50, b=20))
            st.plotly_chart(fig, use_container_width=True)
        with right:
            if time_data is not None and len(time_data) >= 2:
                fig = px.line(time_data, x="Date", y="Value", markers=True, title=f"{primary_measure} over time")
                fig.update_layout(height=420, margin=dict(l=20, r=20, t=50, b=20))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("A usable date field was not detected with enough periods for a trend chart.")
    elif primary_measure and selected_date and time_data is not None and not time_data.empty:
        fig = px.line(time_data, x="Date", y="Value", markers=True, title=f"{primary_measure} over time")
        fig.update_layout(height=420)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("The application has not found a complete measure + dimension/date combination for the main visual. Use Manual mode to select fields.")

    st.subheader("Measure comparison")
    if len(selected_measures) >= 2:
        comparison_rows = []
        for measure in selected_measures:
            s = to_numeric_series(combined[measure]).dropna()
            comparison_rows.append({
                "Measure": measure,
                "Non-missing": len(s),
                "Sum": float(s.sum()) if not s.empty else None,
                "Average": float(s.mean()) if not s.empty else None,
                "Median": float(s.median()) if not s.empty else None,
                "Std Dev": float(s.std()) if len(s) > 1 else None,
            })
        comparison_df = pd.DataFrame(comparison_rows)
        st.dataframe(comparison_df, use_container_width=True, hide_index=True)
    else:
        st.write("Select multiple measures in Manual mode to compare KPIs here.")

    if selected_dimension and selected_measures:
        st.subheader("Distribution")
        measure_for_distribution = selected_measures[0]
        dist = pd.DataFrame({measure_for_distribution: to_numeric_series(combined[measure_for_distribution])}).dropna()
        if not dist.empty:
            fig = px.histogram(dist, x=measure_for_distribution, nbins=30, title=f"Distribution of {measure_for_distribution}")
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("Smart recommendations")
    if recommendations:
        for rec in recommendations:
            st.markdown(f"- {rec}")
    else:
        st.write("No strong automated recommendations were generated from the current selections.")

    if not correlations.empty and len(selected_measures) >= 2:
        st.subheader("Strongest relationships")
        top_corr = correlations.head(5)
        st.dataframe(top_corr, use_container_width=True, hide_index=True)

with field_tab:
    st.subheader("Field intelligence")
    st.caption("The app classifies fields using type detection, name semantics, cardinality, missingness, and identifier heuristics. These are recommendations, not permanent schema definitions.")
    field_df = pd.DataFrame([
        {
            "Column": f["name"],
            "Detected type": f["type"],
            "Role hint": "Measure" if f in base_inference["measures"][:5] else ("Dimension" if f in base_inference["dimensions"][:8] else ("Date" if f in base_inference["dates"][:5] else "Other")),
            "Unique": f["unique"],
            "Missing": f["missing"],
            "Confidence": f"{f['confidence']}%",
            "Semantic signal": "Measure" if f["is_semantic_measure"] else ("Date" if f["is_semantic_date"] else ("Dimension" if f["is_semantic_dimension"] else "—")),
            "Sample": f["sample"],
        }
        for f in fields
    ])
    st.dataframe(field_df, use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Candidate measures", len(base_inference["measures"]))
    c2.metric("Candidate dimensions", len(base_inference["dimensions"]))
    c3.metric("Candidate date fields", len(base_inference["dates"]))

with quality_tab:
    st.subheader("Data quality")
    q1, q2, q3 = st.columns(3)
    q1.metric("Missing cells", f"{profile['missing']:,}")
    q2.metric("Duplicate rows", f"{profile['duplicate_rows']:,}")
    q3.metric("Quality score", f"{profile['quality']}%")

    missing_df = pd.DataFrame([
        {"Column": f["name"], "Missing": f["missing"], "Missing %": round(100 * f["missing"] / max(profile["rows"], 1), 2)}
        for f in fields
    ]).sort_values("Missing", ascending=False)
    if not missing_df.empty:
        st.subheader("Missingness by field")
        st.dataframe(missing_df.head(20), use_container_width=True, hide_index=True)

    st.subheader("Outlier detection (IQR rule)")
    if outliers.empty:
        st.info("Not enough numeric observations to calculate outliers.")
    else:
        st.dataframe(outliers, use_container_width=True, hide_index=True)

    st.subheader("Correlation analysis")
    if correlations.empty:
        st.info("Select at least two numeric measures to calculate pairwise correlations.")
    else:
        st.dataframe(correlations.head(25), use_container_width=True, hide_index=True)

with data_tab:
    st.subheader("Active data preview")
    st.dataframe(combined.head(MAX_PREVIEW_ROWS), use_container_width=True, height=520, hide_index=True)
    st.caption(f"Showing up to {MAX_PREVIEW_ROWS} rows from {profile['rows']:,} analyzed rows.")

    export_bytes = build_export(
        combined,
        used_records,
        profile,
        selected_measures,
        selected_dimension,
        selected_date,
        aggregation,
        outliers,
        correlations,
        recommendations,
    )
    export_name = f"data_intelligence_analysis_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    if st.download_button(
        "Export analysis workbook",
        data=export_bytes,
        file_name=export_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Exports source data, summary, field intelligence, group/trend analyses, outliers, correlations, and recommendations.",
    ):
        try:
            volume_write_bytes(f"{EXPORT_DIR}/{export_name}", export_bytes, overwrite=True)
            st.success(f"Export saved to the workspace Volume: {export_name}")
        except Exception as exc:
            st.warning(f"Download succeeded, but saving the export to the Volume failed: {exc}")

with history_tab:
    st.subheader("File history")
    history_df = pd.DataFrame([
        {
            "File": r["original_name"],
            "Uploaded UTC": r["uploaded_at"],
            "Size MB": round(r["size_bytes"] / 1024 / 1024, 2),
            "Sheets": ", ".join(r.get("sheets", [])) if r.get("sheets") else "—",
            "Stored path": r["stored_path"],
        }
        for r in catalog
    ])
    st.dataframe(history_df, use_container_width=True, hide_index=True)

st.divider()
st.caption("Data Intelligence Workspace · Databricks-native application · automatic analysis is heuristic/statistical and can be overridden in Manual mode")
