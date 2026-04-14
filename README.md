# Postage reporting dashboard

Flask + SQLite app for Pitney Bowes postage data and parcel billing imports, with a web dashboard and Excel exports.

## Requirements

- Python 3.11+
- Optional: [LibreOffice](https://www.libreoffice.org/) (headless) for converting `BM_*.xls` and NetSort `WS3_FCFL_CustomerMailDetail*.xls` files — not needed if you only use `BM_*_report.csv` or supply `.xlsx`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000). Override the port with `PORT=8080 python app.py`.

On first run the app creates `postage.db` and the `watch/` folder layout. Drop import files in `watch/incoming/` (or `input/`); the background watcher polls about every 60 seconds, or use **Scan Now** in the UI.

NetSort **WS3_FCFL_CustomerMailDetail** presort reports (`.xls`/`.xlsx`) are imported into `postage.db`, a summary is written to `reports/mail_detail_export.csv`, and **presort reject** totals appear on the main postage table as rows with mail class **Presort rejects**. On the **System** page, map each WS3 profile to a parent account so those rejects roll up correctly.

On the **Import Summary** tab, parcel totals and tables use each piece’s **`Time Stamp`** (mailing date), not the file’s import time. Set the summary date range to include those piece dates to see parcel breakdowns.

## Tests

```bash
pip install -r requirements.txt
pytest
```
