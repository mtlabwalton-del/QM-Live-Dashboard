# SPC / Quality Dashboard (Streamlit + Google Sheets)

Reads QAP-style measurement data straight from Google Sheets and plots
**Value vs Time** and **Cpk vs Time** for every numeric parameter column,
grouped by the "Sampling Qty" cell for each column.

## How it reads your sheet

For every tab in a spreadsheet, for every column D onward that has a title:

| Row | Meaning |
|---|---|
| 4 | Parameter / graph title |
| 6 | USL |
| 7 | LSL |
| 8 | Sampling Qty (how many consecutive rows are averaged into 1 graph point) |
| 9+ | Data rows — Col A = Date, Col B = Time, Col D+ = measured values |

Column scanning stops automatically when it hits ~3 empty rows in a row.
Columns whose USL/LSL are not numbers (e.g. "Visual", "Ok/Not Ok" checks)
are automatically skipped since Cpk doesn't apply to them.

If a different line's sheet uses different row numbers, edit the constants
near the top of `app.py` (`TITLE_ROW`, `USL_ROW`, `LSL_ROW`, `SAMPLE_QTY_ROW`,
`DATA_START_ROW`, `FIRST_PARAM_COL`).

## 1. Get a free Google Sheets API key

You do **not** need OAuth or a service account since your sheets are shared
as "Anyone with the link -> Viewer". A simple API key is enough:

1. Go to https://console.cloud.google.com/
2. Create a project (or pick an existing one)
3. Go to **APIs & Services -> Library**, search **Google Sheets API**, click **Enable**
4. Go to **APIs & Services -> Credentials -> Create Credentials -> API key**
5. Copy the key. (Optional but recommended: click the key -> **Restrict key** ->
   Restrict to "Google Sheets API")

## 2. Add your lines

Open `app.py` and edit the `LINES` dictionary near the top:

```python
LINES = {
    "Line 1 - Crankcase Master Metal VSD Short Leg": "1vfOOhvjS2yAix5wfutoKKQNdGPQp4lmlzqqRwHn0i84",
    "Line 2": "1AfTbwyK7e8ftAxSXZyZvgvuEgC9E9CivZsUMkeLudOI",
}
```

The value is the Sheet ID — the long string in the URL between `/d/` and `/edit`.

## 3. Run locally (optional)

```bash
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml and paste your real API key
streamlit run app.py
```

## 4. Push to GitHub

```bash
git init
git add app.py requirements.txt README.md
git commit -m "SPC dashboard"
git branch -M main
git remote add origin <your-empty-github-repo-url>
git push -u origin main
```

**Do not commit your real `secrets.toml`** — it's meant to stay local /
be set inside Streamlit Cloud's settings instead. Only the `.example`
file should go to GitHub.

## 5. Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub
2. Click **New app**, pick your repo, branch `main`, main file `app.py`
3. Before/after deploying, go to **App settings -> Secrets** and paste:
   ```
   GOOGLE_API_KEY = "your_real_key_here"
   ```
4. Save — the app will restart and start pulling live data from your sheets

## Notes

- Data refreshes are cached for 5 minutes (`ttl=300` in `app.py`) so the app
  doesn't hammer the Sheets API — adjust if you need faster/slower refresh.
- The date filter in the sidebar is built from the first selected parameter's
  dates; since Date/Time (columns A/B) are shared across a tab this covers
  the whole tab.
- Cpk is computed per group as `min((USL-mean)/(3*std), (mean-LSL)/(3*std))`
  using the sample standard deviation of that group.
