# Development and testing

Use a dedicated virtual environment so global Python packages do not mix with this project.

## Supported Python

CI targets **Python 3.12** (see `.python-version` for pyenv). Newer versions often work locally; match CI for production-like behavior.

## One-time setup

```bash
cd /path/to/Postagereporting
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Production servers should install **only** `requirements.txt` (not `requirements-dev.txt`).

Always prefer **`python -m pip`** and **`python -m pytest`** inside the activated venv.

## Run the app

[`app.py`](../app.py) listens on `0.0.0.0` and reads **`PORT`** (default **5000**).

```bash
source .venv/bin/activate
PORT=8080 python app.py
```

Without activating:

```bash
PORT=8080 .venv/bin/python app.py
```

## Run tests

```bash
source .venv/bin/activate
python -m pytest tests/
```

Quick smoke after dependency changes:

```bash
python -c "import app"
python -m pytest tests/test_exports.py -q
```

Or run [`scripts/check_env.sh`](../scripts/check_env.sh) (uses `.venv/bin/python` automatically when present).

## Continuous integration

On GitHub, **`.github/workflows/test.yml`** runs `pytest` on every push and pull request using **Python 3.12** (see `.python-version`). Match that version locally for results closest to CI.

## Virtual environment hygiene

- Do **not** delete or recreate `.venv` as part of normal test runs—only when repairing a broken environment or changing Python version.
- If `pip` or imports inside `.venv` are corrupted, recreate the venv and reinstall from the requirement files above.
