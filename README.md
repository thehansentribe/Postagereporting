# Postage reporting dashboard

Flask + SQLite app for Pitney Bowes postage data and parcel billing imports, with a web dashboard and Excel exports.

## Requirements

- Python **3.12** recommended for parity with CI (see `.python-version`); 3.11+ often works locally.
- Optional: [LibreOffice](https://www.libreoffice.org/) (headless) for converting `BM_*.xls` and NetSort `WS3_FCFL_CustomerMailDetail*.xls` files — not needed if you only use `BM_*_report.csv` or supply `.xlsx`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt   # only needed for tests / development
```

See **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** for run commands, testing, and environment troubleshooting.

## Run

### Double-click launcher (recommended)

- **macOS:** double-click **`run.command`**.
- **Windows:** double-click **`run.bat`**.

The launcher creates/updates the `.venv`, installs dependencies, starts the server on port **8080** (served by [waitress](https://github.com/Pylons/waitress)), opens your browser, and prints the URLs to use.

The app is reachable from other computers on the same network at **`http://<this-computer-IP>:8080`** (the launcher prints the exact address). On first run, allow the firewall prompt (macOS "Allow incoming connections" / Windows "Allow access") so other computers can connect. For a stable address, give the host a static IP or a reserved DHCP lease.

> Access is unauthenticated and all users share one database — intended for a trusted, firewalled, internet-isolated internal network only.

### Manual run

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000). Override with env vars, e.g. `PORT=8080 python app.py`, or `HOST=127.0.0.1` to force local-only. If `waitress` is installed it is used automatically; otherwise the Flask dev server is used as a fallback.

On first run the app creates `postage.db` and the `watch/` folder layout. Drop import files in `watch/incoming/` (or `input/`); the background watcher polls about every 60 seconds, or use **Scan Now** in the UI.

NetSort **WS3_FCFL_CustomerMailDetail** presort reports (`.xls`/`.xlsx`) are imported into `postage.db`, a summary is written to `reports/mail_detail_export.csv`, and **presort reject** totals appear on the main postage table as rows with mail class **Presort rejects**. On the **System** page, map each WS3 profile to a parent account so those rejects roll up correctly.

On the **Import Summary** tab, parcel totals and tables use each piece’s **`Time Stamp`** (mailing date), not the file’s import time. Set the summary date range to include those piece dates to see parcel breakdowns.

## Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt   # first time, or after changing deps
python -m pytest tests/
```

You can also run **`scripts/check_env.sh`** for a quick import + subset check.
