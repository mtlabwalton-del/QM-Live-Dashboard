"""
SPC / Quality Dashboard
Reads QAP-style data from Google Sheets (public "anyone with link can view")
and plots Value-vs-Time and Cpk-vs-Time charts for every numeric parameter
column found on each tab, grouped by "Sampling Qty".

Sheet layout this app expects (row numbers, 1-indexed, same for every tab):
    Row 4  -> Parameter title
    Row 6  -> USL
    Row 7  -> LSL
    Row 8  -> Sampling Qty (group size used to average points on the graph)
    Row 9+ -> Data. Col A = Date, Col B = Time, Col C = sample counter,
              Col D onward = one column per parameter.
Data columns are auto-detected: any column from D onward that has a
non-empty title in row 4 is treated as a parameter. Columns whose
USL/LSL are not numbers (e.g. "Visual") are skipped from the
numeric/Cpk charts automatically.

Configure your lines (line name -> Google Sheet ID) in LINES below.
Requires a Google Sheets API key stored in Streamlit secrets as
GOOGLE_API_KEY (see README.md).
"""

import re
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# --------------------------------------------------------------------------
# CONFIG — add / edit your production lines here.
# Key   = name shown in the sidebar
# Value = the Google Sheet ID (the long string in the sheet URL between
#          /d/ and /edit)
# --------------------------------------------------------------------------
LINES = {
    "Line 1 - Crankcase Master Metal VSD Short Leg": "1vfOOhvjS2yAix5wfutoKKQNdGPQp4lmlzqqRwHn0i84",
    "Line 2": "1AfTbwyK7e8ftAxSXZyZvgvuEgC9E9CivZsUMkeLudOI",
}

TITLE_ROW = 4          # row with parameter name / graph title
USL_ROW = 6             # row with USL
LSL_ROW = 7             # row with LSL
SAMPLE_QTY_ROW = 8      # row with sampling quantity (group size)
DATA_START_ROW = 9      # first row of actual data
DATE_COL = 0             # column A (0-indexed)
TIME_COL = 1             # column B (0-indexed)
FIRST_PARAM_COL = 3      # column D (0-indexed) -> first parameter column
MAX_COLS = 60            # how many columns wide to scan (A..BH ish, adjust if needed)
MAX_ROWS = 2000          # how many rows deep to scan for data

CPK_TARGET_LINE = 1.33   # common minimum-acceptable Cpk reference line

# --------------------------------------------------------------------------
# Google Sheets API helpers (public API key auth — sheet must be shared as
# "Anyone with the link" -> "Viewer")
# --------------------------------------------------------------------------

def get_api_key() -> str:
    try:
        return st.secrets["GOOGLE_API_KEY"]
    except Exception:
        st.error(
            "Missing GOOGLE_API_KEY in Streamlit secrets. "
            "See README.md for how to get a free Google Sheets API key "
            "and add it in Settings -> Secrets."
        )
        st.stop()


@st.cache_data(ttl=300, show_spinner=False)
def list_tabs(spreadsheet_id: str) -> list[str]:
    """Return the list of tab (worksheet) names in a spreadsheet."""
    api_key = get_api_key()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    params = {"key": api_key, "fields": "sheets.properties.title"}
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        st.error(f"Could not read tab list ({resp.status_code}): {resp.text[:300]}")
        st.stop()
    data = resp.json()
    return [s["properties"]["title"] for s in data.get("sheets", [])]


@st.cache_data(ttl=300, show_spinner=False)
def get_tab_values(spreadsheet_id: str, tab_name: str) -> list[list[str]]:
    """Return the raw grid values (list of rows) for a tab."""
    api_key = get_api_key()
    # Quote the tab name in case it has spaces / special characters.
    safe_tab = tab_name.replace("'", "''")
    rng = f"'{safe_tab}'!A1:BZ{MAX_ROWS}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    params = {"key": api_key, "valueRenderOption": "UNFORMATTED_VALUE",
              "dateTimeRenderOption": "FORMATTED_STRING"}
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        st.error(f"Could not read sheet data ({resp.status_code}): {resp.text[:300]}")
        st.stop()
    return resp.json().get("values", [])


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def to_float(val):
    if val is None:
        return np.nan
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s == "" or s.upper() in {"OK", "NG", "NOT OK", "N/A", "NA", "VISUAL"}:
        return np.nan
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_datetime(date_val, time_val):
    date_str = str(date_val).strip() if date_val not in (None, "") else ""
    time_str = str(time_val).strip() if time_val not in (None, "") else ""
    combined = (date_str + " " + time_str).strip()
    if not combined:
        return pd.NaT
    dt = pd.to_datetime(combined, errors="coerce", dayfirst=True)
    if pd.isna(dt) and date_str:
        dt = pd.to_datetime(date_str, errors="coerce", dayfirst=True)
    return dt


def cell(grid, row_idx, col_idx):
    """Safe access into a ragged list-of-lists grid (0-indexed)."""
    if row_idx < 0 or row_idx >= len(grid):
        return None
    row = grid[row_idx]
    if col_idx < 0 or col_idx >= len(row):
        return None
    val = row[col_idx]
    return val if val != "" else None


# --------------------------------------------------------------------------
# Core: discover parameter columns on a tab
# --------------------------------------------------------------------------

def discover_parameters(grid: list[list[str]]) -> list[dict]:
    """Scan header rows and return metadata for every valid parameter column."""
    if not grid:
        return []
    header_len = max((len(r) for r in grid[:TITLE_ROW + 2]), default=0)
    n_cols = min(max(header_len, MAX_COLS), MAX_COLS)

    params = []
    for col_idx in range(FIRST_PARAM_COL, n_cols):
        title = cell(grid, TITLE_ROW - 1, col_idx)
        if title is None or str(title).strip() == "":
            continue
        usl = to_float(cell(grid, USL_ROW - 1, col_idx))
        lsl = to_float(cell(grid, LSL_ROW - 1, col_idx))
        sample_qty = to_float(cell(grid, SAMPLE_QTY_ROW - 1, col_idx))
        if np.isnan(usl) or np.isnan(lsl):
            # Non-numeric spec (e.g. "Visual" / "Ok"/"Not Ok") -> not a
            # measurable/Cpk-able parameter, skip it.
            continue
        sample_qty = int(sample_qty) if not np.isnan(sample_qty) and sample_qty >= 1 else 1
        params.append({
            "col_idx": col_idx,
            "title": str(title).strip(),
            "usl": usl,
            "lsl": lsl,
            "sample_qty": sample_qty,
        })
    return params


def build_raw_dataframe(grid: list[list[str]], param: dict) -> pd.DataFrame:
    """Pull raw (row-level) date/time/value rows for one parameter column."""
    rows = []
    row_idx = DATA_START_ROW - 1
    empty_streak = 0
    while row_idx < len(grid) and row_idx < MAX_ROWS:
        date_val = cell(grid, row_idx, DATE_COL)
        time_val = cell(grid, row_idx, TIME_COL)
        raw_val = cell(grid, row_idx, param["col_idx"])

        if date_val is None and time_val is None and raw_val is None:
            empty_streak += 1
            if empty_streak >= 3:
                break
            row_idx += 1
            continue
        empty_streak = 0

        dt = parse_datetime(date_val, time_val)
        value = to_float(raw_val)
        rows.append({"datetime": dt, "value": value})
        row_idx += 1

    df = pd.DataFrame(rows)
    return df


def group_and_aggregate(df: pd.DataFrame, sample_qty: int, usl: float, lsl: float) -> pd.DataFrame:
    """Chunk rows into groups of `sample_qty`, average value, compute Cpk."""
    if df.empty:
        return pd.DataFrame(columns=["datetime", "avg_value", "cpk", "n"])

    df = df.reset_index(drop=True)
    df["group"] = df.index // sample_qty

    records = []
    for _, g in df.groupby("group"):
        vals = g["value"].dropna()
        if vals.empty:
            continue
        avg = vals.mean()
        std = vals.std(ddof=1) if len(vals) > 1 else np.nan
        if std is None or np.isnan(std) or std == 0:
            cpk = np.nan
        else:
            cpk = min((usl - avg) / (3 * std), (avg - lsl) / (3 * std))
        # x-axis point: use the datetime of the last sample in the group
        dt_series = g["datetime"].dropna()
        dt_point = dt_series.iloc[-1] if not dt_series.empty else pd.NaT
        records.append({
            "datetime": dt_point,
            "avg_value": avg,
            "cpk": cpk,
            "n": len(vals),
        })
    return pd.DataFrame(records)


# --------------------------------------------------------------------------
# Plotting
# --------------------------------------------------------------------------

def plot_value_chart(agg: pd.DataFrame, param: dict) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=agg["datetime"], y=agg["avg_value"],
        mode="lines+markers", name="Average value",
        line=dict(color="#1f77b4"),
    ))
    fig.add_hline(y=param["usl"], line=dict(color="red", dash="dash"),
                  annotation_text=f"USL {param['usl']}", annotation_position="top left")
    fig.add_hline(y=param["lsl"], line=dict(color="red", dash="dash"),
                  annotation_text=f"LSL {param['lsl']}", annotation_position="bottom left")
    fig.update_layout(
        title=f"{param['title']} — Value vs Time",
        xaxis_title="Time", yaxis_title="Value",
        height=380, margin=dict(t=60, b=40),
    )
    return fig


def plot_cpk_chart(agg: pd.DataFrame, param: dict) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=agg["datetime"], y=agg["cpk"],
        mode="lines+markers", name="Cpk",
        line=dict(color="#2ca02c"),
    ))
    fig.add_hline(y=CPK_TARGET_LINE, line=dict(color="orange", dash="dash"),
                  annotation_text=f"Target {CPK_TARGET_LINE}", annotation_position="top left")
    fig.add_hline(y=1.0, line=dict(color="red", dash="dot"),
                  annotation_text="Min 1.0", annotation_position="bottom left")
    fig.update_layout(
        title=f"{param['title']} — Cpk vs Time",
        xaxis_title="Time", yaxis_title="Cpk",
        height=380, margin=dict(t=60, b=40),
    )
    return fig


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="SPC Quality Dashboard", layout="wide")
    st.title("📊 SPC / Quality Dashboard")

    with st.sidebar:
        st.header("Filters")

        line_name = st.selectbox("Line", list(LINES.keys()))
        spreadsheet_id = LINES[line_name]

        with st.spinner("Loading tab list..."):
            tabs = list_tabs(spreadsheet_id)
        if not tabs:
            st.warning("No tabs found in this sheet.")
            st.stop()
        tab_name = st.selectbox("Sheet / Tab", tabs)

        with st.spinner("Loading sheet data..."):
            grid = get_tab_values(spreadsheet_id, tab_name)

        params = discover_parameters(grid)
        if not params:
            st.warning("No numeric parameter columns found on this tab "
                       "(check that row 4/6/7/8 are filled in).")
            st.stop()

        param_titles = [p["title"] for p in params]
        selected_titles = st.multiselect(
            "Parameter(s)", param_titles, default=param_titles[: min(3, len(param_titles))]
        )

        # Build a combined date range from all data (based on first selected
        # parameter, since date/time columns are shared across the tab).
        sample_df = build_raw_dataframe(grid, params[0]) if params else pd.DataFrame()
        valid_dates = sample_df["datetime"].dropna() if not sample_df.empty else pd.Series(dtype="datetime64[ns]")
        if not valid_dates.empty:
            min_date = valid_dates.min().date()
            max_date = valid_dates.max().date()
            date_range = st.date_input(
                "Date range", value=(min_date, max_date),
                min_value=min_date, max_value=max_date,
            )
        else:
            date_range = None
            st.info("No parseable dates found in column A for this tab.")

        st.caption(f"{len(params)} numeric parameter(s) detected on this tab.")

    if not selected_titles:
        st.info("Select at least one parameter from the sidebar to see charts.")
        return

    selected_params = [p for p in params if p["title"] in selected_titles]

    for param in selected_params:
        raw_df = build_raw_dataframe(grid, param)

        if isinstance(date_range, tuple) and len(date_range) == 2 and not raw_df.empty:
            start, end = date_range
            mask = (raw_df["datetime"].dt.date >= start) & (raw_df["datetime"].dt.date <= end)
            raw_df = raw_df[mask | raw_df["datetime"].isna()]

        agg = group_and_aggregate(raw_df, param["sample_qty"], param["usl"], param["lsl"])

        st.subheader(param["title"])
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("USL", param["usl"])
        c2.metric("LSL", param["lsl"])
        c3.metric("Sample qty / point", param["sample_qty"])
        c4.metric("Points plotted", len(agg))

        if agg.empty:
            st.warning("No data available for the selected date range.")
            st.divider()
            continue

        col1, col2 = st.columns(2)
        with col1:
            st.plotly_chart(plot_value_chart(agg, param), use_container_width=True)
        with col2:
            st.plotly_chart(plot_cpk_chart(agg, param), use_container_width=True)

        with st.expander("Show data table"):
            st.dataframe(agg, use_container_width=True)

        st.divider()


if __name__ == "__main__":
    main()
