"""
SPC / Quality Dashboard
Reads QAP-style data from Google Sheets (public "anyone with link can view")
and plots:
  - Value vs Time + Cpk vs Time for every NUMERIC parameter column
  - OK vs NOK bar chart (green/red) for every ATTRIBUTE (pass/fail) column
grouped by the "Sampling Qty" cell for each column.

Sheet layout this app expects (row numbers, 1-indexed, same for every tab):
    Row 4  -> Parameter title
    Row 6  -> USL
    Row 7  -> LSL
    Row 8  -> Sampling Qty (group size used to average points on the graph)
    Row 9+ -> Data. Col A = Date, Col B = Time, Col C = sample counter,
              Col D onward = one column per parameter.

HOW A COLUMN IS CLASSIFIED (data-driven, not header-driven):
For every column from D onward with a non-empty title in row 4, the app
reads the actual data starting at row 9 in THAT column. If it finds
OK / NOK - type text values (OK, NG, NOT OK, PASS, FAIL, etc.) in that
column's data, the column is treated as an ATTRIBUTE column -> a green/red
bar chart over time. If it finds numeric values instead, it's a NUMERIC
column -> Value vs Time + Cpk vs Time charts using USL/LSL from rows 6/7.
This matches columns anywhere in the sheet (e.g. G, I, J, K, M, N, O, P,
S, V, W, X, Y, Z, ...) regardless of what's in the header rows.

Configure your lines (line name -> Google Sheet ID) in LINES below.
Requires a Google Sheets API key stored in Streamlit secrets as
GOOGLE_API_KEY (see README.md).
"""

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
    "Line 2 - Crankcase Master Metal VSD Long Leg": "1AfTbwyK7e8ftAxSXZyZvgvuEgC9E9CivZsUMkeLudOI",
}

TITLE_ROW = 4          # row with parameter name / graph title
USL_ROW = 6             # row with USL
LSL_ROW = 7             # row with LSL
SAMPLE_QTY_ROW = 8      # row with sampling quantity (group size)
DATA_START_ROW = 9      # first row of actual data
DATE_COL = 0             # column A (0-indexed)
TIME_COL = 1             # column B (0-indexed)
FIRST_PARAM_COL = 3      # column D (0-indexed) -> first parameter column
MAX_COLS = 80            # how many columns wide to scan
MAX_ROWS = 2000          # how many rows deep to scan for data
EMPTY_ROW_STOP = 5       # stop scanning a column after this many fully-empty rows in a row

CPK_TARGET_LINE = 1.33   # common minimum-acceptable Cpk reference line
DECIMALS = 3             # USL/LSL/values are rounded & displayed to this many places

PASS_VALUES = {"OK", "OKAY", "PASS", "PASSED", "GOOD", "ACCEPT", "ACCEPTED"}
FAIL_VALUES = {"NG", "NOT OK", "NOTOK", "NOK", "FAIL", "FAILED", "REJECT", "REJECTED", "NO"}

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
def list_tabs(spreadsheet_id: str) -> list:
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
def get_tab_values(spreadsheet_id: str, tab_name: str) -> list:
    """Return the raw grid values (list of rows) for a tab."""
    api_key = get_api_key()
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
    """Parse a cell into a float, or NaN if it isn't numeric. Rounded to a
    few extra decimal places beyond DECIMALS to kill binary floating-point
    noise like 15.866999999999 that should really be 15.867."""
    if val is None:
        return np.nan
    if isinstance(val, (int, float)):
        return round(float(val), DECIMALS + 3)
    s = str(val).strip()
    if s == "":
        return np.nan
    s2 = s.replace(",", "")
    try:
        return round(float(s2), DECIMALS + 3)
    except ValueError:
        return np.nan


def classify_status(val):
    """Classify a raw cell as 'OK', 'NOK', or None (blank/unrecognized/numeric)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return None
    s = str(val).strip().upper()
    if s == "":
        return None
    if s in PASS_VALUES:
        return "OK"
    if s in FAIL_VALUES:
        return "NOK"
    return None


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


def fmt(val):
    """Format a number to exactly DECIMALS places for display."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "-"
    return f"{val:.{DECIMALS}f}"


# --------------------------------------------------------------------------
# Core: scan one column's data (row 9+), then decide its type from what's
# actually in the data — this is the key fix: classification is data-driven,
# not based on what's in the USL/LSL header rows.
# --------------------------------------------------------------------------

def scan_column(grid: list, col_idx: int) -> pd.DataFrame:
    """Read every data row (from DATA_START_ROW) for one column, returning
    datetime + raw + value(numeric-or-NaN) + status(OK/NOK-or-None)."""
    rows = []
    row_idx = DATA_START_ROW - 1
    empty_streak = 0
    while row_idx < len(grid) and row_idx < MAX_ROWS:
        date_val = cell(grid, row_idx, DATE_COL)
        time_val = cell(grid, row_idx, TIME_COL)
        raw_val = cell(grid, row_idx, col_idx)

        if date_val is None and time_val is None and raw_val is None:
            empty_streak += 1
            if empty_streak >= EMPTY_ROW_STOP:
                break
            row_idx += 1
            continue
        empty_streak = 0

        dt = parse_datetime(date_val, time_val)
        rows.append({
            "datetime": dt,
            "raw": raw_val,
            "value": to_float(raw_val),
            "status": classify_status(raw_val),
        })
        row_idx += 1

    return pd.DataFrame(rows, columns=["datetime", "raw", "value", "status"])


def discover_parameters(grid: list) -> list:
    """Scan every column from D onward that has a title in row 4. For each,
    scan its data and decide numeric vs attribute based on the DATA itself:
      - any recognized OK/NOK text in the column's data -> 'attribute'
      - otherwise any numeric value in the column's data -> 'numeric'
      - otherwise (no usable data at all) -> column is skipped
    """
    if not grid:
        return []
    header_len = max((len(r) for r in grid[:TITLE_ROW + 2]), default=0)
    n_cols = min(max(header_len, MAX_COLS), MAX_COLS)

    params = []
    for col_idx in range(FIRST_PARAM_COL, n_cols):
        title = cell(grid, TITLE_ROW - 1, col_idx)
        if title is None or str(title).strip() == "":
            continue

        raw_df = scan_column(grid, col_idx)
        if raw_df.empty:
            continue

        has_status = raw_df["status"].notna().any()
        has_numeric = raw_df["value"].notna().any()

        sample_qty = to_float(cell(grid, SAMPLE_QTY_ROW - 1, col_idx))
        sample_qty = int(sample_qty) if not np.isnan(sample_qty) and sample_qty >= 1 else 1

        if has_status:
            params.append({
                "col_idx": col_idx,
                "title": str(title).strip(),
                "type": "attribute",
                "usl": None,
                "lsl": None,
                "sample_qty": sample_qty,
                "raw_df": raw_df,
            })
        elif has_numeric:
            usl = to_float(cell(grid, USL_ROW - 1, col_idx))
            lsl = to_float(cell(grid, LSL_ROW - 1, col_idx))
            params.append({
                "col_idx": col_idx,
                "title": str(title).strip(),
                "type": "numeric",
                "usl": usl,
                "lsl": lsl,
                "sample_qty": sample_qty,
                "raw_df": raw_df,
            })
        # else: column has a title but no usable data at all -> skip
    return params


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
        avg = round(vals.mean(), DECIMALS + 3)
        std = vals.std(ddof=1) if len(vals) > 1 else np.nan
        if std is None or np.isnan(std) or std == 0 or usl is None or lsl is None or np.isnan(usl) or np.isnan(lsl):
            cpk = np.nan
        else:
            cpk = min((usl - avg) / (3 * std), (avg - lsl) / (3 * std))
        dt_series = g["datetime"].dropna()
        dt_point = dt_series.iloc[-1] if not dt_series.empty else pd.NaT
        records.append({"datetime": dt_point, "avg_value": avg, "cpk": cpk, "n": len(vals)})
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
        hovertemplate="%{x}<br>Value: %{y:.3f}<extra></extra>",
    ))
    if param["usl"] is not None and not np.isnan(param["usl"]):
        fig.add_hline(y=param["usl"], line=dict(color="red", dash="dash"),
                      annotation_text=f"USL {fmt(param['usl'])}", annotation_position="top left")
    if param["lsl"] is not None and not np.isnan(param["lsl"]):
        fig.add_hline(y=param["lsl"], line=dict(color="red", dash="dash"),
                      annotation_text=f"LSL {fmt(param['lsl'])}", annotation_position="bottom left")
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
        hovertemplate="%{x}<br>Cpk: %{y:.3f}<extra></extra>",
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


def plot_attribute_chart(raw_df: pd.DataFrame, param: dict) -> go.Figure:
    """Bar chart over time for an OK/NOK (attribute) parameter.
    Green bar = OK, red bar = NOK. Rows with unrecognized/blank status are skipped."""
    df = raw_df.dropna(subset=["status"]).copy().reset_index(drop=True)
    has_dt = df["datetime"].notna().any()
    x_vals = df["datetime"] if has_dt else df.index.astype(str)

    colors = df["status"].map({"OK": "#2ca02c", "NOK": "#d62728"})
    ok_n = int((df["status"] == "OK").sum())
    nok_n = int((df["status"] == "NOK").sum())

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x_vals, y=[1] * len(df),
        marker_color=colors,
        customdata=df[["status", "raw"]].astype(str),
        hovertemplate="%{x}<br>Result: %{customdata[0]}<extra></extra>",
        name="Result",
    ))
    fig.update_layout(
        title=f"{param['title']} — OK / NOK vs Time (OK: {ok_n}, NOK: {nok_n})",
        xaxis_title="Time", yaxis=dict(showticklabels=False, title=""),
        height=320, margin=dict(t=60, b=40), showlegend=False, bargap=0.15,
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

        with st.spinner("Scanning columns..."):
            params = discover_parameters(grid)

        if not params:
            st.warning("No parameter columns with data found on this tab.")
            st.stop()

        numeric_count = sum(1 for p in params if p["type"] == "numeric")
        attr_count = sum(1 for p in params if p["type"] == "attribute")

        def label(p):
            return p["title"] if p["type"] == "numeric" else f"{p['title']}  [OK/NOK]"

        param_labels = [label(p) for p in params]
        selected_labels = st.multiselect(
            "Parameter(s)", param_labels, default=param_labels[: min(3, len(param_labels))]
        )

        # Date range, based on whichever parameter has the most dates
        # (Date/Time columns A/B are shared across the whole tab).
        best_dates = pd.Series(dtype="datetime64[ns]")
        for p in params:
            d = p["raw_df"]["datetime"].dropna()
            if len(d) > len(best_dates):
                best_dates = d
        if not best_dates.empty:
            min_date = best_dates.min().date()
            max_date = best_dates.max().date()
            date_range = st.date_input(
                "Date range", value=(min_date, max_date),
                min_value=min_date, max_value=max_date,
            )
        else:
            date_range = None
            st.info("No parseable dates found in column A for this tab.")

        st.caption(f"{numeric_count} numeric + {attr_count} OK/NOK parameter(s) detected.")
        if st.button("🔄 Refresh data"):
            list_tabs.clear()
            get_tab_values.clear()
            st.rerun()

    if not selected_labels:
        st.info("Select at least one parameter from the sidebar to see charts.")
        return

    label_to_param = {label(p): p for p in params}
    selected_params = [label_to_param[l] for l in selected_labels]

    for param in selected_params:
        raw_df = param["raw_df"]

        if isinstance(date_range, tuple) and len(date_range) == 2 and not raw_df.empty:
            start, end = date_range
            mask = (raw_df["datetime"].dt.date >= start) & (raw_df["datetime"].dt.date <= end)
            raw_df = raw_df[mask | raw_df["datetime"].isna()]

        st.subheader(param["title"])

        if param["type"] == "numeric":
            agg = group_and_aggregate(raw_df, param["sample_qty"], param["usl"], param["lsl"])

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("USL", fmt(param["usl"]))
            c2.metric("LSL", fmt(param["lsl"]))
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
                st.dataframe(agg.assign(
                    avg_value=agg["avg_value"].round(DECIMALS),
                    cpk=agg["cpk"].round(DECIMALS),
                ), use_container_width=True)

        else:  # attribute / OK-NOK parameter
            if raw_df.empty or raw_df["status"].dropna().empty:
                st.warning("No OK/NOK data available for the selected date range.")
                st.divider()
                continue

            ok_count = (raw_df["status"] == "OK").sum()
            nok_count = (raw_df["status"] == "NOK").sum()
            total = ok_count + nok_count
            c1, c2, c3 = st.columns(3)
            c1.metric("OK", int(ok_count))
            c2.metric("NOK", int(nok_count))
            c3.metric("NOK rate", f"{(nok_count/total*100):.1f}%" if total else "-")

            st.plotly_chart(plot_attribute_chart(raw_df, param), use_container_width=True)

            with st.expander("Show data table"):
                st.dataframe(raw_df[["datetime", "raw", "status"]], use_container_width=True)

        st.divider()


if __name__ == "__main__":
    main()
