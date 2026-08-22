"""
SPC / Quality Dashboard
Reads QAP-style data from Google Sheets (public "anyone with link can view")
and shows:
  - A DYNAMIC, CLICKABLE drill-down Summary Report:
        All Lines  --click a bar-->  Sheets/Tabs in that line
                   --click a bar-->  Parameters in that tab (fail % ranked)
                   --click a bar-->  Detail chart for that parameter
  - Value vs Time + Cpk vs Time for every NUMERIC parameter column
    (with USL / LSL / UCL / LCL reference lines)
  - OK vs NOK bar chart (green/red) for every ATTRIBUTE (pass/fail) column

Sheet layout this app expects (row numbers, 1-indexed, same for every tab):
    Row 4   -> Parameter title
    Row 6   -> USL
    Row 7   -> LSL
    Row 8   -> UCL
    Row 9   -> LCL
    Row 10  -> Sampling Qty (group size used to average points on the graph)
    Row 11+ -> Data. Col A = Date, Col B = Time, Col C = sample counter,
               Col D onward = one column per parameter.

HOW A COLUMN IS CLASSIFIED (data-driven, not header-driven):
For every column from D onward with a non-empty title in row 4, the app
reads the actual data starting at row 11 in THAT column. If it finds
OK / NOK - type text values (OK, NG, NOT OK, PASS, FAIL, etc.) in that
column's data, the column is treated as an ATTRIBUTE column -> a green/red
bar chart over time. If it finds numeric values instead, it's a NUMERIC
column -> Value vs Time (with USL/LSL/UCL/LCL lines) + Cpk vs Time charts.

"OUT OF LIMIT" for the summary report means:
  - Numeric parameters: raw value outside [LSL, USL] (the specification).
  - Attribute parameters: any NOK / fail result.

Configure your lines (line name -> Google Sheet ID) in LINES below.
Requires a Google Sheets API key stored in Streamlit secrets as
GOOGLE_API_KEY (see README.md).
"""

import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# --------------------------------------------------------------------------
# CONFIG — add / edit your production lines here.
# Key   = name shown in the dashboard
# Value = the Google Sheet ID (the long string in the sheet URL between
#          /d/ and /edit)
# --------------------------------------------------------------------------
LINES = {
    "Line 1 - Crankcase Master Metal VSD Short Leg": "1vfOOhvjS2yAix5wfutoKKQNdGPQp4lmlzqqRwHn0i84",
    "Line 2": "1AfTbwyK7e8ftAxSXZyZvgvuEgC9E9CivZsUMkeLudOI",
}

TITLE_ROW = 4           # row with parameter name / graph title
USL_ROW = 6              # row with USL
LSL_ROW = 7              # row with LSL
UCL_ROW = 8              # row with UCL
LCL_ROW = 9              # row with LCL
SAMPLE_QTY_ROW = 10      # row with sampling quantity (group size)
DATA_START_ROW = 11      # first row of actual data
DATE_COL = 0              # column A (0-indexed)
TIME_COL = 1              # column B (0-indexed)
FIRST_PARAM_COL = 3       # column D (0-indexed) -> first parameter column
MAX_COLS = 80             # how many columns wide to scan
MAX_ROWS = 3000           # how many rows deep to scan for data
EMPTY_ROW_STOP = 5        # stop scanning a column after this many fully-empty rows in a row

CPK_TARGET_LINE = 1.33    # common minimum-acceptable Cpk reference line
DECIMALS = 3              # USL/LSL/UCL/LCL/values are rounded & displayed to this many places

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

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def to_float(val):
    """Parse a cell into a float, or NaN if it isn't numeric. Rounded to a
    few extra decimal places beyond DECIMALS to kill binary floating-point
    noise like 15.866999999999 that should really be 15.867.

    Robust to stray formatting in cells like UCL/LCL that sometimes get
    typed with units, plus/minus signs, or extra spaces (e.g. "10.5 mm",
    "±0.05", " 9.7 "). Falls back to pulling the first numeric token out
    of the text if a direct float() parse fails."""
    if val is None:
        return np.nan
    if isinstance(val, bool):
        return np.nan
    if isinstance(val, (int, float)):
        return round(float(val), DECIMALS + 3)
    s = str(val).strip()
    if s == "":
        return np.nan
    s2 = s.replace(",", "").replace("\u00a0", " ").strip()
    try:
        return round(float(s2), DECIMALS + 3)
    except ValueError:
        pass
    match = _NUMBER_RE.search(s2)
    if match:
        try:
            return round(float(match.group()), DECIMALS + 3)
        except ValueError:
            return np.nan
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


def col_letter(col_idx: int) -> str:
    """0-indexed column number -> spreadsheet column letter (0->A, 3->D...)."""
    n = col_idx + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# --------------------------------------------------------------------------
# Core: scan one column's data (row 11+), then decide its type from what's
# actually in the data — classification is data-driven, not based on what's
# in the USL/LSL/UCL/LCL header rows.
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

        uid = col_letter(col_idx)

        if has_status:
            params.append({
                "col_idx": col_idx, "uid": uid, "title": str(title).strip(),
                "type": "attribute",
                "usl": None, "lsl": None, "ucl": None, "lcl": None,
                "sample_qty": sample_qty, "raw_df": raw_df,
            })
        elif has_numeric:
            usl = to_float(cell(grid, USL_ROW - 1, col_idx))
            lsl = to_float(cell(grid, LSL_ROW - 1, col_idx))
            ucl = to_float(cell(grid, UCL_ROW - 1, col_idx))
            lcl = to_float(cell(grid, LCL_ROW - 1, col_idx))
            params.append({
                "col_idx": col_idx, "uid": uid, "title": str(title).strip(),
                "type": "numeric",
                "usl": usl, "lsl": lsl, "ucl": ucl, "lcl": lcl,
                "sample_qty": sample_qty, "raw_df": raw_df,
            })
        # else: column has a title but no usable data at all -> skip
    return params


def param_label(p: dict) -> str:
    base = p["title"] if p["type"] == "numeric" else f"{p['title']} [OK/NOK]"
    return f"{base} ({p['uid']})"


def group_and_aggregate(df: pd.DataFrame, sample_qty: int, usl: float, lsl: float) -> pd.DataFrame:
    """Chunk rows into groups of `sample_qty`, average value, compute Cpk.
    Cpk uses USL/LSL (the specification), not UCL/LCL (control limits)."""
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
# Summary aggregation — Parameter level (per tab) / Tab level (per line) /
# Line level (across all lines). Each level rolls up "total checked" and
# "out of limit" counts from the level below it.
# --------------------------------------------------------------------------

def build_param_summary(params: list) -> pd.DataFrame:
    """One row per parameter on a tab: total checked, out-of-limit count,
    fail %. Numeric: value outside [LSL,USL]. Attribute: NOK result."""
    rows = []
    for p in params:
        df = p["raw_df"]
        if p["type"] == "numeric":
            vals = df["value"].dropna()
            total = len(vals)
            usl, lsl = p["usl"], p["lsl"]
            if total == 0 or usl is None or lsl is None or np.isnan(usl) or np.isnan(lsl):
                fail = 0
            else:
                fail = int(((vals < lsl) | (vals > usl)).sum())
        else:
            statuses = df["status"].dropna()
            total = len(statuses)
            fail = int((statuses == "NOK").sum()) if total else 0

        fail_pct = round(fail / total * 100, 2) if total else np.nan
        rows.append({
            "label": param_label(p), "uid": p["uid"], "Total": total,
            "Fail": fail, "Fail %": fail_pct,
        })
    return pd.DataFrame(rows)


def filter_params_by_date(params: list, date_range) -> list:
    """Return a new list of param dicts whose raw_df is filtered to
    [start, end] (inclusive). Rows with no parseable date are kept (so we
    never silently drop data just because its date/time didn't parse)."""
    if not date_range or not (isinstance(date_range, tuple) and len(date_range) == 2):
        return params
    start, end = date_range
    if start is None or end is None:
        return params
    out = []
    for p in params:
        df = p["raw_df"]
        if not df.empty:
            mask = (df["datetime"].dt.date >= start) & (df["datetime"].dt.date <= end)
            df = df[mask | df["datetime"].isna()]
        p2 = dict(p)
        p2["raw_df"] = df
        out.append(p2)
    return out


@st.cache_data(ttl=300, show_spinner=False)
def get_tab_rollup(spreadsheet_id: str, tab_name: str, start_date=None, end_date=None):
    """(total_checked, total_fail, fail_pct) for one tab, rolled up from
    every parameter on it, optionally restricted to [start_date, end_date]."""
    grid = get_tab_values(spreadsheet_id, tab_name)
    params = discover_parameters(grid)
    if start_date and end_date:
        params = filter_params_by_date(params, (start_date, end_date))
    psum = build_param_summary(params)
    total = int(psum["Total"].sum()) if not psum.empty else 0
    fail = int(psum["Fail"].sum()) if not psum.empty else 0
    pct = round(fail / total * 100, 2) if total else 0.0
    return total, fail, pct


@st.cache_data(ttl=300, show_spinner=False)
def get_line_summary_df(spreadsheet_id: str, start_date=None, end_date=None) -> pd.DataFrame:
    """One row per tab in a spreadsheet: total checked, fail count, fail %."""
    tabs = list_tabs(spreadsheet_id)
    rows = []
    for t in tabs:
        total, fail, pct = get_tab_rollup(spreadsheet_id, t, start_date, end_date)
        rows.append({"label": t, "Total": total, "Fail": fail, "Fail %": pct})
    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner=False)
def get_all_lines_summary_df(start_date=None, end_date=None) -> pd.DataFrame:
    """One row per configured line: total checked, fail count, fail %,
    rolled up across every tab in that line's spreadsheet."""
    rows = []
    for line_name, spreadsheet_id in LINES.items():
        line_df = get_line_summary_df(spreadsheet_id, start_date, end_date)
        total = int(line_df["Total"].sum()) if not line_df.empty else 0
        fail = int(line_df["Fail"].sum()) if not line_df.empty else 0
        pct = round(fail / total * 100, 2) if total else 0.0
        rows.append({"label": line_name, "Total": total, "Fail": fail, "Fail %": pct})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Date-range bounds (for the shared date_input widget on the Summary Report)
# --------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_tab_date_bounds(spreadsheet_id: str, tab_name: str):
    grid = get_tab_values(spreadsheet_id, tab_name)
    params = discover_parameters(grid)
    mn = mx = None
    for p in params:
        d = p["raw_df"]["datetime"].dropna()
        if d.empty:
            continue
        dmin, dmax = d.min().date(), d.max().date()
        mn = dmin if mn is None or dmin < mn else mn
        mx = dmax if mx is None or dmax > mx else mx
    return mn, mx


@st.cache_data(ttl=300, show_spinner=False)
def get_line_date_bounds(spreadsheet_id: str):
    mn = mx = None
    for t in list_tabs(spreadsheet_id):
        tmn, tmx = get_tab_date_bounds(spreadsheet_id, t)
        if tmn:
            mn = tmn if mn is None or tmn < mn else mn
        if tmx:
            mx = tmx if mx is None or tmx > mx else mx
    return mn, mx


@st.cache_data(ttl=300, show_spinner=False)
def get_all_lines_date_bounds():
    mn = mx = None
    for spreadsheet_id in LINES.values():
        lmn, lmx = get_line_date_bounds(spreadsheet_id)
        if lmn:
            mn = lmn if mn is None or lmn < mn else mn
        if lmx:
            mx = lmx if mx is None or lmx > mx else mx
    return mn, mx





def plot_fail_bar(df: pd.DataFrame, title: str, x_title: str) -> go.Figure:
    """Generic clickable fail-% bar chart used at every drill-down level."""
    d = df.dropna(subset=["Fail %"]).sort_values("Fail %", ascending=False)
    colors = ["#d62728" if v >= 5 else ("#ff7f0e" if v > 0 else "#2ca02c") for v in d["Fail %"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=d["label"], y=d["Fail %"],
        marker_color=colors,
        customdata=d[["Total", "Fail"]],
        hovertemplate="%{x}<br>Fail: %{y:.2f}%<br>Checked: %{customdata[0]}<br>Out of limit: %{customdata[1]}<extra></extra>",
    ))
    fig.update_layout(
        title=title, xaxis_title=x_title, yaxis_title="Fail %",
        height=440, margin=dict(t=60, b=140), xaxis_tickangle=-45,
    )
    return fig


def get_click(event) -> str:
    """Extract the clicked bar's x-axis category label from a plotly_chart
    selection event, or None if nothing was clicked."""
    if not event:
        return None
    sel = event.get("selection") if isinstance(event, dict) else getattr(event, "selection", None)
    if not sel:
        return None
    points = sel.get("points") if isinstance(sel, dict) else getattr(sel, "points", None)
    if not points:
        return None
    return points[0].get("x") if isinstance(points[0], dict) else getattr(points[0], "x", None)


# --------------------------------------------------------------------------
# Detail-chart plotting (Value/Cpk for numeric, Bar for attribute)
# --------------------------------------------------------------------------

def plot_value_chart(agg: pd.DataFrame, param: dict) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=agg["datetime"], y=agg["avg_value"],
        mode="lines+markers", name="Average value",
        line=dict(color="#1f77b4"),
        hovertemplate="%{x}<br>Value: %{y:.3f}<extra></extra>",
    ))

    def hline(val, color, dash, label, pos):
        if val is not None and not np.isnan(val):
            fig.add_hline(y=val, line=dict(color=color, dash=dash),
                          annotation_text=f"{label} {fmt(val)}", annotation_position=pos)

    hline(param["usl"], "red", "dash", "USL", "top left")
    hline(param["lsl"], "red", "dash", "LSL", "bottom left")
    hline(param.get("ucl"), "purple", "dot", "UCL", "top right")
    hline(param.get("lcl"), "purple", "dot", "LCL", "bottom right")

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


def render_param_detail(param: dict, date_range, key_prefix: str = ""):
    """Render the Value/Cpk (numeric) or OK-NOK bar (attribute) detail
    chart(s) for one parameter, with metrics above.

    key_prefix must be unique per calling context (e.g. "summary" vs
    "dash") since this can now be called from both the Summary Report and
    the Dashboard section on the SAME page render — without a prefix their
    widget keys (based only on column uid) would collide."""
    raw_df = param["raw_df"]
    uid = param["uid"]
    kp = f"{key_prefix}_" if key_prefix else ""

    if isinstance(date_range, tuple) and len(date_range) == 2 and not raw_df.empty:
        start, end = date_range
        mask = (raw_df["datetime"].dt.date >= start) & (raw_df["datetime"].dt.date <= end)
        raw_df = raw_df[mask | raw_df["datetime"].isna()]

    st.subheader(f"{param['title']}  (col {uid})")

    if param["type"] == "numeric":
        agg = group_and_aggregate(raw_df, param["sample_qty"], param["usl"], param["lsl"])

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("USL", fmt(param["usl"]))
        c2.metric("LSL", fmt(param["lsl"]))
        c3.metric("UCL", fmt(param.get("ucl")))
        c4.metric("LCL", fmt(param.get("lcl")))
        c5.metric("Sample qty / point", param["sample_qty"])
        c6.metric("Points plotted", len(agg))

        if agg.empty:
            st.warning("No data available for the selected date range.")
            return

        col1, col2 = st.columns(2)
        with col1:
            st.plotly_chart(plot_value_chart(agg, param), use_container_width=True,
                             key=f"{kp}chart_value_{uid}")
        with col2:
            st.plotly_chart(plot_cpk_chart(agg, param), use_container_width=True,
                             key=f"{kp}chart_cpk_{uid}")

        with st.expander("Show data table"):
            st.dataframe(agg.assign(
                avg_value=agg["avg_value"].round(DECIMALS),
                cpk=agg["cpk"].round(DECIMALS),
            ), use_container_width=True, key=f"{kp}table_{uid}")

    else:  # attribute / OK-NOK parameter
        if raw_df.empty or raw_df["status"].dropna().empty:
            st.warning("No OK/NOK data available for the selected date range.")
            return

        ok_count = (raw_df["status"] == "OK").sum()
        nok_count = (raw_df["status"] == "NOK").sum()
        total = ok_count + nok_count
        c1, c2, c3 = st.columns(3)
        c1.metric("OK", int(ok_count))
        c2.metric("NOK", int(nok_count))
        c3.metric("NOK rate", f"{(nok_count/total*100):.1f}%" if total else "-")

        st.plotly_chart(plot_attribute_chart(raw_df, param), use_container_width=True,
                         key=f"{kp}chart_attr_{uid}")

        with st.expander("Show data table"):
            st.dataframe(raw_df[["datetime", "raw", "status"]], use_container_width=True,
                         key=f"{kp}table_{uid}")


# --------------------------------------------------------------------------
# Streamlit UI — cascading Summary Report (all 3 levels on one page)
# --------------------------------------------------------------------------

def init_state():
    st.session_state.setdefault("drill_line", None)   # user-clicked focus line (None = auto/worst)
    st.session_state.setdefault("drill_tab", None)     # user-clicked focus tab  (None = auto/worst)
    st.session_state.setdefault("drill_param_uid", None)  # user-clicked focus parameter


def worst_label(df: pd.DataFrame, fallback: str = None) -> str:
    """Return the label with the highest Fail % in df, or `fallback` if
    nothing usable is found."""
    d = df.dropna(subset=["Fail %"])
    if d.empty:
        return fallback
    return d.sort_values("Fail %", ascending=False)["label"].iloc[0]


def render_summary_report():
    """Cascading Summary Report, all on one page:
      1) Fail % by Line
      2) Fail % by Sheet/Tab, for the line focused above (worst by default)
      3) Fail % by Parameter, for the tab focused above (worst by default)
    Clicking a bar in chart 1 changes the focus line (and resets tab/param
    focus below it); clicking in chart 2 changes the focus tab (and resets
    param focus); clicking in chart 3 shows that parameter's detail chart.
    A single date-range filter at the top applies to all three charts and
    to the detail chart, so you can zoom into a specific time window to see
    which line/sheet/parameter had problems in that window.
    """
    init_state()
    st.header("📋 Summary Report")

    # ---------------- Shared date-range filter ----------------
    with st.spinner("Loading available date range..."):
        overall_min, overall_max = get_all_lines_date_bounds()

    start_date = end_date = None
    if overall_min and overall_max:
        picked = st.date_input(
            "📅 Date range (applies to all charts & the detail view below)",
            value=(overall_min, overall_max),
            min_value=overall_min, max_value=overall_max,
            key="summary_date_range",
        )
        if isinstance(picked, tuple) and len(picked) == 2:
            start_date, end_date = picked
        else:
            start_date, end_date = overall_min, overall_max
    else:
        st.info("No parseable dates found yet across the configured lines.")

    if st.button("🔁 Reset focus to worst offenders", key="btn_reset_focus"):
        st.session_state.drill_line = None
        st.session_state.drill_tab = None
        st.session_state.drill_param_uid = None
        st.rerun()

    st.divider()

    # ---------------- 1) Fail % by Line ----------------
    with st.spinner("Loading line-level summary..."):
        df_lines = get_all_lines_summary_df(start_date, end_date)

    if df_lines.empty or df_lines["Total"].sum() == 0:
        st.info("No data found across the configured lines for this date range.")
        return

    t1, t2, t3 = st.columns(3)
    t1.metric("Lines", len(df_lines))
    t2.metric("Total values checked", int(df_lines["Total"].sum()))
    overall = round(df_lines["Fail"].sum() / df_lines["Total"].sum() * 100, 2) if df_lines["Total"].sum() else 0
    t3.metric("Overall out-of-limit %", f"{overall}%")

    st.subheader("1️⃣ Fail % by Line")
    event1 = st.plotly_chart(
        plot_fail_bar(df_lines, "Fail % by Line (click a bar to focus)", "Line"),
        use_container_width=True, on_select="rerun", key="chart_lines",
    )
    clicked1 = get_click(event1)
    if clicked1 and clicked1 in LINES:
        st.session_state.drill_line = clicked1
        st.session_state.drill_tab = None
        st.session_state.drill_param_uid = None
        st.rerun()

    current_line = st.session_state.drill_line or worst_label(df_lines, list(LINES.keys())[0])
    auto_tag = "" if st.session_state.drill_line else " _(auto: highest fail %)_"
    st.caption(f"Focused line: **{current_line}**{auto_tag}")
    spreadsheet_id = LINES[current_line]

    st.divider()

    # ---------------- 2) Fail % by Sheet/Tab within the focused line ----------------
    with st.spinner(f"Loading sheet/tab summary for {current_line}..."):
        df_tabs = get_line_summary_df(spreadsheet_id, start_date, end_date)

    if df_tabs.empty or df_tabs["Total"].sum() == 0:
        st.info(f"No data found on {current_line}'s sheets for this date range.")
        return

    t1, t2, t3 = st.columns(3)
    t1.metric("Sheets / Tabs", len(df_tabs))
    t2.metric("Total values checked", int(df_tabs["Total"].sum()))
    overall2 = round(df_tabs["Fail"].sum() / df_tabs["Total"].sum() * 100, 2) if df_tabs["Total"].sum() else 0
    t3.metric(f"{current_line} out-of-limit %", f"{overall2}%")

    st.subheader(f"2️⃣ Fail % by Sheet/Tab — {current_line}")
    event2 = st.plotly_chart(
        plot_fail_bar(df_tabs, f"Fail % by Sheet/Tab in {current_line} (click a bar to focus)", "Sheet / Tab"),
        use_container_width=True, on_select="rerun", key="chart_tabs",
    )
    clicked2 = get_click(event2)
    if clicked2 and clicked2 in df_tabs["label"].values:
        st.session_state.drill_tab = clicked2
        st.session_state.drill_param_uid = None
        st.rerun()

    current_tab = st.session_state.drill_tab or worst_label(df_tabs, df_tabs["label"].iloc[0])
    auto_tag2 = "" if st.session_state.drill_tab else " _(auto: highest fail %)_"
    st.caption(f"Focused sheet/tab: **{current_tab}**{auto_tag2}")

    st.divider()

    # ---------------- 3) Fail % by Parameter within the focused tab ----------------
    with st.spinner(f"Loading parameters for {current_tab}..."):
        grid = get_tab_values(spreadsheet_id, current_tab)
        params = discover_parameters(grid)
        if start_date and end_date:
            params = filter_params_by_date(params, (start_date, end_date))

    if not params:
        st.warning("No parameter columns with data found on this tab for this date range.")
        return

    psum = build_param_summary(params)
    total3 = int(psum["Total"].sum())
    fail3 = int(psum["Fail"].sum())
    pct3 = round(fail3 / total3 * 100, 2) if total3 else 0.0

    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Parameters", len(params))
    t2.metric("Total values checked", total3)
    t3.metric("Out of limit", fail3)
    t4.metric(f"{current_tab} out-of-limit %", f"{pct3}%")

    st.subheader(f"3️⃣ Fail % by Parameter — {current_tab}")
    event3 = st.plotly_chart(
        plot_fail_bar(psum, f"Fail % by Parameter in {current_tab} (click a bar for detail)", "Parameter"),
        use_container_width=True, on_select="rerun", key="chart_params",
    )
    clicked3 = get_click(event3)
    uid_by_label = {row["label"]: row["uid"] for _, row in psum.iterrows()}
    if clicked3 and clicked3 in uid_by_label:
        st.session_state.drill_param_uid = uid_by_label[clicked3]
        st.rerun()

    with st.expander("Show parameter summary table"):
        st.dataframe(psum.drop(columns=["uid"]), use_container_width=True, key="table_params")

    st.divider()

    # ---------------- Parameter detail (shown after clicking chart 3) ----------------
    label_by_uid = {p["uid"]: param_label(p) for p in params}
    label_to_param = {param_label(p): p for p in params}
    date_range = (start_date, end_date) if start_date and end_date else None

    if st.session_state.drill_param_uid and st.session_state.drill_param_uid in label_by_uid:
        focused_label = label_by_uid[st.session_state.drill_param_uid]
        st.subheader(f"🔎 Detail: {focused_label}")
        render_param_detail(label_to_param[focused_label], date_range, key_prefix="summary_focus")
    else:
        st.info("👆 Click a bar in the Parameter chart above to see its detail chart here.")

    with st.expander("➕ Compare more parameters from this tab"):
        extra_labels = st.multiselect(
            "Add parameter(s) to compare alongside the focused one above",
            [l for l in label_to_param if l != label_by_uid.get(st.session_state.drill_param_uid)],
            key="sel_extra_params",
        )
        for lbl in extra_labels:
            render_param_detail(label_to_param[lbl], date_range, key_prefix="summary_extra")
            st.divider()


def render_dashboard():
    """The original sidebar-filtered browsing view: pick a Line, a Sheet/Tab,
    then one or more Parameters, and see their charts. Full manual control,
    no clicking required."""
    with st.sidebar:
        st.header("Dashboard Filters")

        line_name = st.selectbox("Line", list(LINES.keys()), key="dash_sel_line")
        spreadsheet_id = LINES[line_name]

        with st.spinner("Loading tab list..."):
            tabs = list_tabs(spreadsheet_id)
        if not tabs:
            st.warning("No tabs found in this sheet.")
            st.stop()
        tab_name = st.selectbox("Sheet / Tab", tabs, key="dash_sel_tab")

        with st.spinner("Loading sheet data..."):
            grid = get_tab_values(spreadsheet_id, tab_name)
        with st.spinner("Scanning columns..."):
            params = discover_parameters(grid)

        if not params:
            st.warning("No parameter columns with data found on this tab.")
            st.stop()

        numeric_count = sum(1 for p in params if p["type"] == "numeric")
        attr_count = sum(1 for p in params if p["type"] == "attribute")

        param_labels = [param_label(p) for p in params]
        selected_labels = st.multiselect(
            "Parameter(s)", param_labels,
            default=param_labels[: min(3, len(param_labels))],
            key="dash_sel_params",
        )

        best_dates = pd.Series(dtype="datetime64[ns]")
        for p in params:
            d = p["raw_df"]["datetime"].dropna()
            if len(d) > len(best_dates):
                best_dates = d
        if not best_dates.empty:
            min_date, max_date = best_dates.min().date(), best_dates.max().date()
            date_range = st.date_input(
                "Date range", value=(min_date, max_date),
                min_value=min_date, max_value=max_date,
                key="dash_sel_date_range",
            )
        else:
            date_range = None
            st.info("No parseable dates found in column A for this tab.")

        st.caption(f"{numeric_count} numeric + {attr_count} OK/NOK parameter(s) detected.")

    if not selected_labels:
        st.info("Select at least one parameter from the sidebar to see charts.")
        return

    label_to_param = {param_label(p): p for p in params}
    for lbl in selected_labels:
        render_param_detail(label_to_param[lbl], date_range, key_prefix="dash")
        st.divider()


def main():
    st.set_page_config(page_title="SPC Quality Dashboard", layout="wide")
    st.title("📊 SPC / Quality Dashboard")

    with st.sidebar:
        if st.button("🔄 Refresh all data", key="btn_refresh"):
            list_tabs.clear()
            get_tab_values.clear()
            get_tab_rollup.clear()
            get_line_summary_df.clear()
            get_all_lines_summary_df.clear()
            get_tab_date_bounds.clear()
            get_line_date_bounds.clear()
            get_all_lines_date_bounds.clear()
            st.rerun()
        st.caption(
            "Summary Report: click any bar to drill down — "
            "Line → Sheet/Tab → Parameter → detail chart."
        )
        st.divider()

    # Summary Report first (click-linked drill-down across all lines/sheets)
    render_summary_report()

    st.divider()
    st.divider()

    # Regular Dashboard below it (manual Line/Tab/Parameter browsing via sidebar)
    st.header("📈 Dashboard")
    render_dashboard()


if __name__ == "__main__":
    main()
