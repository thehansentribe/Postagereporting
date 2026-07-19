"""Flask application: postage reporting dashboard API and UI."""

from __future__ import annotations

from datetime import date, datetime
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, after_this_request, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import backup_restore
import db
import exports
import exports_consolidated_volumes
import importer
import watcher
from scheduler_api import register_scheduler
import scheduler as report_scheduler

app = Flask(__name__, template_folder="templates", static_folder="static")
register_scheduler(app)

_watcher_started = False
_watcher_lock = threading.Lock()
_scheduler_started = False


# Keep this string identical to the XLSX export's no-data guidance (exports.py).
_WS3_FLATS_PROFIT_NO_DATA_MSG = (
    "No WS3 flats profit rows found for this date range/account scope. "
    "Import the WS3 Customer Mail Detail file for these dates (Scan Now), then re-export."
)


def _ensure_watcher() -> None:
    global _watcher_started, _scheduler_started
    with _watcher_lock:
        if not _watcher_started:
            t = threading.Thread(target=watcher.watch_loop, kwargs={"interval_sec": 60}, daemon=True)
            t.start()
            _watcher_started = True
        if not _scheduler_started:
            report_scheduler.ensure_scheduler()
            _scheduler_started = True


def _bool_param(name: str, default: bool = False) -> bool:
    v = request.args.get(name)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def _stored_pricing_terms(end_date: str | None) -> dict:
    """
    Stored pricing terms in effect at ``end_date``.

    Report knobs absent from the request resolve from these; explicit request
    parameters always win.
    """
    conn = db.get_connection()
    try:
        return db.get_pricing_terms(conn, as_of_date=end_date)
    finally:
        conn.close()


def _resolve_knob(explicit: float | None, terms: dict, field: str) -> tuple[float, str]:
    """Resolved knob value plus its source ("request" or "stored")."""
    if explicit is not None:
        return float(explicit), "request"
    return float(terms[field]), "stored"


def _profit_request_extras(conn) -> tuple[list[int] | None, float | None, str | None]:
    """
    Parse optional ``profit_accounts`` CSV and ``parcel_fee`` from the query string.

    Returns ``(profit_account_ids, parcel_fee, error_message)`` where ``error_message``
    is set on validation failure; ``parcel_fee`` is None when absent (callers resolve
    it from stored pricing terms).
    """
    raw_pa = request.args.get("profit_accounts")
    profit_ids: list[int] | None = None
    if raw_pa and str(raw_pa).strip():
        try:
            profit_ids = db.parse_profit_accounts_csv(conn, raw_pa)
        except ValueError as e:
            return None, None, str(e)
    pf = request.args.get("parcel_fee", type=float)
    if pf is not None and pf < 0:
        return None, None, "parcel_fee must be non-negative"
    return profit_ids, (float(pf) if pf is not None else None), None


def _efd_parcel_fee_pair(
    efd_explicit: float | None,
    parcel_fee_fallback: float | None,
    default: float = 1.25,
) -> tuple[float, str | None]:
    """
    Price-to-EFD adder for invoice column Y and Summary B10 on profit export.

    Prefer ``efd_explicit``; if absent, use ``parcel_fee_fallback`` (legacy query
    strings); else ``default`` (the stored parcel_fee_per_piece term).
    """
    if efd_explicit is not None:
        if efd_explicit < 0:
            return float(default), "efd_parcel_fee must be non-negative"
        return float(efd_explicit), None
    if parcel_fee_fallback is not None:
        if parcel_fee_fallback < 0:
            return float(default), "parcel_fee must be non-negative"
        return float(parcel_fee_fallback), None
    return float(default), None


def _efd_parcel_fee_from_post_body(body: dict) -> tuple[float | None, str | None]:
    """If key ``efd_parcel_fee`` is present, parse non-negative float; else (None, None)."""
    if "efd_parcel_fee" not in body:
        return None, None
    raw = body["efd_parcel_fee"]
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None, "efd_parcel_fee must be a number"
    if f < 0:
        return None, "efd_parcel_fee must be non-negative"
    return float(f), None


def _profit_request_extras_post(conn, data: dict) -> tuple[list[int] | None, float | None, str | None]:
    """
    Parse ``profit_account_ids`` JSON array and ``parcel_fee`` from a POST JSON body.

    ``parcel_fee`` is None when absent (callers resolve it from stored pricing terms).
    """
    profit_ids: list[int] | None = None
    j = data.get("profit_account_ids")
    if j is not None:
        if not isinstance(j, list):
            return None, None, "profit_account_ids must be an array"
        try:
            profit_ids = db.parse_profit_account_ids_from_json_list(conn, j)
        except ValueError as e:
            return None, None, str(e)
    pf = data.get("parcel_fee")
    if pf is not None:
        try:
            pf = float(pf)
        except (TypeError, ValueError):
            return None, None, "parcel_fee must be a number"
        if pf < 0:
            return None, None, "parcel_fee must be non-negative"
    return profit_ids, (float(pf) if pf is not None else None), None


@app.before_request
def _before() -> None:
    _ensure_watcher()


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/customers")
def customers_hierarchy() -> str:
    return render_template("customers.html")


@app.route("/system")
def system_page() -> str:
    return render_template("system.html")


@app.route("/api/system/backup")
def api_system_backup():
    include_archives = _bool_param("include_archives", False)
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmp = Path(tempfile.gettempdir()) / f"postage-backup-{stamp}.zip"
        backup_restore.create_backup(tmp, include_archives=include_archives)

        @after_this_request
        def _cleanup(resp):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return resp

        return send_file(
            tmp,
            as_attachment=True,
            download_name=tmp.name,
            mimetype="application/zip",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/restore", methods=["POST"])
def api_system_restore():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify({"error": "Restore requires a .zip backup file"}), 400
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = Path(tempfile.gettempdir()) / f"postage-restore-{stamp}-{secure_filename(f.filename)}"
    try:
        f.save(tmp)
        result = backup_restore.stage_restore(tmp)
        tmp.unlink(missing_ok=True)
        if not result.get("ok"):
            return jsonify({"error": result.get("error") or "Invalid backup"}), 400
        return jsonify(result)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/flats-retail")
def api_system_flats_retail():
    try:
        conn = db.get_connection()
        rows = db.list_flat_retail_rates(conn)
        presort_reject_unit_cost = db.get_presort_reject_unit_cost(conn)
        conn.close()
        return jsonify(
            {
                "rows": rows,
                "empty": len(rows) == 0,
                "presort_reject_unit_cost": presort_reject_unit_cost,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/flats-retail/seed", methods=["POST"])
def api_system_flats_retail_seed():
    try:
        conn = db.get_connection()
        with conn:
            result = db.seed_flat_retail_rates_if_empty(conn)
        rows = db.list_flat_retail_rates(conn)
        presort_reject_unit_cost = db.get_presort_reject_unit_cost(conn)
        conn.close()
        return jsonify(
            {"ok": True, **result, "rows": rows, "presort_reject_unit_cost": presort_reject_unit_cost}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/flats-retail", methods=["PUT"])
def api_system_flats_retail_update():
    try:
        payload = request.get_json(silent=True) or {}
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            return jsonify({"error": "rows must be a list"}), 400
        pr_raw = payload.get("presort_reject_unit_cost")
        conn = db.get_connection()
        with conn:
            result = db.upsert_flat_retail_rates(conn, rows)
            if pr_raw is not None and pr_raw != "":
                try:
                    pr = float(pr_raw)
                except (TypeError, ValueError):
                    conn.close()
                    return jsonify({"error": "presort_reject_unit_cost must be a number"}), 400
                db.set_presort_reject_unit_cost(conn, pr)
        out = db.list_flat_retail_rates(conn)
        presort_reject_unit_cost = db.get_presort_reject_unit_cost(conn)
        conn.close()
        return jsonify(
            {
                "ok": True,
                **result,
                "rows": out,
                "presort_reject_unit_cost": presort_reject_unit_cost,
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/pricing-terms")
def api_system_pricing_terms():
    try:
        as_of = request.args.get("as_of")
        conn = db.get_connection()
        try:
            current = db.get_pricing_terms(conn, as_of_date=as_of)
            revisions = db.list_pricing_terms(conn)
        finally:
            conn.close()
        return jsonify({"current": current, "revisions": revisions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/pricing-terms", methods=["PUT"])
def api_system_pricing_terms_update():
    try:
        payload = request.get_json(silent=True) or {}
        conn = db.get_connection()
        try:
            with conn:
                db.upsert_pricing_terms(
                    conn,
                    payload.get("effective_date"),
                    flats_customer_discount=payload.get("flats_customer_discount"),
                    flats_efd_discount=payload.get("flats_efd_discount"),
                    parcel_customer_discount=payload.get("parcel_customer_discount"),
                    parcel_fee_per_piece=payload.get("parcel_fee_per_piece"),
                    notes=payload.get("notes"),
                )
            current = db.get_pricing_terms(conn)
            revisions = db.list_pricing_terms(conn)
        finally:
            conn.close()
        return jsonify({"ok": True, "current": current, "revisions": revisions})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/pricing-terms", methods=["DELETE"])
def api_system_pricing_terms_delete():
    try:
        payload = request.get_json(silent=True) or {}
        eff = request.args.get("effective_date") or payload.get("effective_date")
        if not eff:
            return jsonify({"error": "effective_date required"}), 400
        conn = db.get_connection()
        try:
            with conn:
                deleted = db.delete_pricing_terms(conn, eff)
            current = db.get_pricing_terms(conn)
            revisions = db.list_pricing_terms(conn)
        finally:
            conn.close()
        if not deleted:
            return jsonify({"error": f"No pricing-terms revision at {eff}"}), 404
        return jsonify({"ok": True, "current": current, "revisions": revisions})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/rates/flats")
def api_system_rates_flats():
    try:
        conn = db.get_connection()
        view = db.get_flat_rate_costs(conn)
        conn.close()
        return jsonify(
            {
                "queried_at": datetime.now().isoformat(timespec="seconds"),
                "as_of_date": view["as_of_date"],
                "tariff_effective_date": view["tariff_effective_date"],
                "rows": view["rows"],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/rates/parcels")
def api_system_rates_parcels():
    try:
        conn = db.get_connection()
        view = db.get_priority_mail_retail_tariff_view(conn)
        conn.close()
        return jsonify(
            {
                "queried_at": datetime.now().isoformat(timespec="seconds"),
                "as_of_date": view["as_of_date"],
                "tariff_effective_date": view["tariff_effective_date"],
                "matrix": view["matrix"],
                "flat_rate_items": view["flat_rate_items"],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/ws3-profiles")
def api_system_ws3_profiles():
    try:
        conn = db.get_connection()
        profiles = db.list_ws3_profiles(conn)
        assignment_accounts = db.list_ws3_assignment_accounts(conn)
        conn.close()
        return jsonify(
            {
                "profiles": profiles,
                "assignment_accounts": assignment_accounts,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/ws3-profiles", methods=["PUT"])
def api_system_ws3_profiles_update():
    payload = request.get_json(silent=True) or {}
    profile_id = payload.get("profile_id")
    parent_raw = payload.get("parent_customer_number")
    reject_raw = payload.get("reject_fee")
    if profile_id is None:
        return jsonify({"error": "profile_id required"}), 400
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id must be an integer"}), 400
    parent_num: int | None
    if parent_raw is None or parent_raw == "":
        parent_num = None
    else:
        try:
            parent_num = int(parent_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "parent_customer_number must be an integer or null"}), 400

    reject_fee: float | None
    if reject_raw is None or reject_raw == "":
        reject_fee = None
    else:
        try:
            reject_fee = float(reject_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "reject_fee must be a number or empty"}), 400
    try:
        conn = db.get_connection()
        with conn:
            out = db.update_ws3_profile(conn, pid, parent_num, reject_fee)
        profiles = db.list_ws3_profiles(conn)
        conn.close()
        return jsonify({"ok": True, **out, "profiles": profiles})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/unmatched-accounts")
def api_system_unmatched_accounts():
    try:
        conn = db.get_connection()
        unmatched = db.list_unmatched_accounts_all_time(conn)
        parent_options = [
            r
            for r in db.list_customers_dropdown(conn)
            if r.get("kind") in ("parent", "standalone")
        ]
        conn.close()
        return jsonify({"unmatched": unmatched, "parent_options": parent_options})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/unmatched-accounts/assign", methods=["POST"])
def api_system_unmatched_accounts_assign():
    payload = request.get_json(silent=True) or {}
    customer_number = payload.get("customer_number")
    customer_name = payload.get("customer_name")
    parent_number = payload.get("parent_number")
    try:
        conn = db.get_connection()
        with conn:
            out = db.upsert_customer(conn, customer_number, str(customer_name or ""), parent_number)
        unmatched = db.list_unmatched_accounts_all_time(conn)
        parent_options = [
            r
            for r in db.list_customers_dropdown(conn)
            if r.get("kind") in ("parent", "standalone")
        ]
        conn.close()
        return jsonify({"ok": True, **out, "unmatched": unmatched, "parent_options": parent_options})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/customers/hierarchy")
def api_customers_hierarchy():
    try:
        conn = db.get_connection()
        data = db.query_customer_hierarchy(conn)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/customers")
def api_customers():
    try:
        conn = db.get_connection()
        parents = db.list_parent_customers(conn)
        conn.close()
        out = [{"customer_number": None, "customer_name": "All Accounts", "child_count": 0}]
        out.extend(parents)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/customers/list")
def api_customers_list():
    try:
        conn = db.get_connection()
        rows = db.list_customers_dropdown(conn)
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/postage")
def api_postage():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        kc_presort = _bool_param("kc_presort", False)
        efd = _bool_param("efd", False)
        allocate_presort_rejects = _bool_param("allocate_presort_rejects", False)
        conn = db.get_connection()
        data = db.query_postage(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
            kc_presort=kc_presort,
            efd=efd,
            allocate_presort_rejects=allocate_presort_rejects,
            consolidate=_bool_param("consolidate", False),
            remove_zeros=_bool_param("remove_zeros", False),
            hide_costs=_bool_param("hide_costs", False),
        )
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/postage/row-details")
def api_postage_row_details():
    file_date = request.args.get("file_date")
    account_code = request.args.get("account_code", type=int)
    mail_class = request.args.get("mail_class")
    if not file_date or account_code is None or not mail_class:
        return jsonify({"error": "file_date, account_code, mail_class required"}), 400
    if file_date == "Combined":
        return jsonify({"error": "Cannot edit a consolidated row"}), 400
    if mail_class in (db.WS3_REJECT_MAIL_CLASS, db.WS3_REJECT_ALLOCATED_MAIL_CLASS):
        return jsonify({"error": "Presort rejects are not editable"}), 400
    try:
        conn = db.get_connection()
        rows = db.get_postage_row_details(conn, file_date, account_code, mail_class)
        conn.close()
        return jsonify({"rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/postage/row-preview-update", methods=["POST"])
def api_postage_row_preview_update():
    payload = request.get_json(silent=True) or {}
    file_date = payload.get("file_date")
    from_account = payload.get("from_account_code")
    to_account = payload.get("to_account_code")
    mail_class = payload.get("mail_class")
    pieces_by_id = payload.get("pieces_by_id") or {}
    if not file_date or file_date == "Combined":
        return jsonify({"error": "file_date required"}), 400
    if from_account is None or to_account is None or not mail_class:
        return jsonify({"error": "from_account_code, to_account_code, mail_class required"}), 400
    try:
        conn = db.get_connection()
        out = db.preview_postage_row_update(
            conn,
            file_date=str(file_date),
            from_account_code=int(from_account),
            mail_class=str(mail_class),
            to_account_code=int(to_account),
            pieces_by_id=pieces_by_id,
        )
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/postage/row-apply-update", methods=["POST"])
def api_postage_row_apply_update():
    payload = request.get_json(silent=True) or {}
    file_date = payload.get("file_date")
    from_account = payload.get("from_account_code")
    to_account = payload.get("to_account_code")
    mail_class = payload.get("mail_class")
    pieces_by_id = payload.get("pieces_by_id") or {}
    reason = payload.get("reason")
    if not file_date or file_date == "Combined":
        return jsonify({"error": "file_date required"}), 400
    if from_account is None or to_account is None or not mail_class:
        return jsonify({"error": "from_account_code, to_account_code, mail_class required"}), 400
    try:
        conn = db.get_connection()
        with conn:
            out = db.apply_postage_row_update(
                conn,
                file_date=str(file_date),
                from_account_code=int(from_account),
                mail_class=str(mail_class),
                to_account_code=int(to_account),
                pieces_by_id=pieces_by_id,
                reason=str(reason) if reason is not None else None,
            )
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/parcels/row-details")
def api_parcels_row_details():
    bill_date = request.args.get("bill_date")
    account_code = request.args.get("account_code", type=int)
    mail_class = request.args.get("mail_class")
    zone = request.args.get("zone")
    if zone is None:
        zone = ""
    if not bill_date or account_code is None or not mail_class:
        return jsonify({"error": "bill_date, account_code, mail_class required"}), 400
    if bill_date == "Combined":
        return jsonify({"error": "Cannot edit a consolidated row"}), 400
    try:
        conn = db.get_connection()
        rows = db.get_billing_row_details(
            conn, bill_date, account_code, mail_class, zone
        )
        conn.close()
        return jsonify({"rows": rows})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/parcels/row-preview-update", methods=["POST"])
def api_parcels_row_preview_update():
    payload = request.get_json(silent=True) or {}
    bill_date = payload.get("bill_date")
    from_account = payload.get("from_account_code")
    to_account = payload.get("to_account_code")
    mail_class = payload.get("mail_class")
    zone = payload.get("zone")
    if zone is None:
        zone = ""
    pieces_by_bucket = payload.get("pieces_by_bucket") or {}
    if not bill_date or bill_date == "Combined":
        return jsonify({"error": "bill_date required"}), 400
    if from_account is None or to_account is None or not mail_class:
        return jsonify({"error": "from_account_code, to_account_code, mail_class required"}), 400
    try:
        conn = db.get_connection()
        out = db.preview_billing_row_update(
            conn,
            bill_date=str(bill_date),
            from_account_code=int(from_account),
            mail_class=str(mail_class),
            zone=str(zone),
            to_account_code=int(to_account),
            pieces_by_bucket=pieces_by_bucket,
        )
        conn.close()
        return jsonify(out)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/parcels/row-apply-update", methods=["POST"])
def api_parcels_row_apply_update():
    payload = request.get_json(silent=True) or {}
    bill_date = payload.get("bill_date")
    from_account = payload.get("from_account_code")
    to_account = payload.get("to_account_code")
    mail_class = payload.get("mail_class")
    zone = payload.get("zone")
    if zone is None:
        zone = ""
    pieces_by_bucket = payload.get("pieces_by_bucket") or {}
    reason = payload.get("reason")
    if not bill_date or bill_date == "Combined":
        return jsonify({"error": "bill_date required"}), 400
    if from_account is None or to_account is None or not mail_class:
        return jsonify({"error": "from_account_code, to_account_code, mail_class required"}), 400
    try:
        conn = db.get_connection()
        with conn:
            out = db.apply_billing_row_update(
                conn,
                bill_date=str(bill_date),
                from_account_code=int(from_account),
                mail_class=str(mail_class),
                zone=str(zone),
                to_account_code=int(to_account),
                pieces_by_bucket=pieces_by_bucket,
                reason=str(reason) if reason is not None else None,
            )
        conn.close()
        return jsonify(out)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/parcels")
def api_parcels():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        kc_presort = _bool_param("kc_presort", False)
        efd = _bool_param("efd", False)
        conn = db.get_connection()
        data = db.query_parcels(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
            kc_presort=kc_presort,
            efd=efd,
            consolidate=_bool_param("consolidate", False),
            remove_zeros=_bool_param("remove_zeros", False),
            hide_costs=_bool_param("hide_costs", False),
        )
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/parcels/zone-summary")
def api_parcels_zone_summary():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        kc_presort = _bool_param("kc_presort", False)
        efd = _bool_param("efd", False)
        conn = db.get_connection()
        data = db.query_parcel_zone_summary(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
            kc_presort=kc_presort,
            efd=efd,
            hide_costs=_bool_param("hide_costs", False),
        )
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/parcels/over-10lb-lines")
def api_parcels_over_10lb_lines():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        conn = db.get_connection()
        rows = db.query_parcel_over_10lb_lines(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
        )
        conn.close()
        return jsonify({"rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary")
def api_summary():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        conn = db.get_connection()
        data = db.query_summary(conn, start, end)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports/readiness")
def api_reports_readiness():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        conn = db.get_connection()
        data = db.query_report_readiness(conn, start, end)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/postage/noclass")
def api_postage_noclass():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        conn = db.get_connection()
        records = db.query_noclass_records(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
        )
        conn.close()
        return jsonify({"records": records})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watcher/status")
def api_watcher_status():
    try:
        watcher.ensure_dirs()
        log_path = watcher.LOG_FILE
        lines: list[str] = []
        if log_path.is_file():
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-10:]
        lines = [ln.rstrip("\n\r") for ln in lines]
        return jsonify(
            {
                "active": True,
                "last_scan": watcher._last_scan_ts,
                "last_log_lines": lines,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    """Restart the server process in place (os.execv).

    Replies immediately, then re-execs after a short delay so the response can
    flush. The re-exec releases DB locks/connections and restarts the watcher.
    """

    def _reexec() -> None:
        time.sleep(0.75)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_reexec, daemon=True).start()
    return jsonify({"ok": True, "restarting": True})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        result = watcher.scan_once()
        failed = result.get("failed") or []
        ok = len(failed) == 0
        return jsonify({"ok": ok, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/import/customers", methods=["POST"])
def api_import_customers():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    path = Path(watcher.INCOMING) / secure_filename(f.filename)
    watcher.ensure_dirs()
    f.save(path)
    try:
        result = importer.import_customers_csv(str(path), db.DB_PATH)
        path.unlink(missing_ok=True)
        return jsonify(result)
    except Exception as e:
        path.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/import/customers-raw-export", methods=["POST"])
def api_import_customers_raw_export():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    if not f.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Raw Export import requires an .xlsx file"}), 400
    path = Path(watcher.INCOMING) / secure_filename(f.filename)
    watcher.ensure_dirs()
    f.save(path)
    try:
        result = importer.import_customers_raw_export_xlsx(str(path), db.DB_PATH)
        path.unlink(missing_ok=True)
        return jsonify(result)
    except Exception as e:
        path.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/import/flatrates", methods=["POST"])
def api_import_flatrates():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    path = Path(watcher.INCOMING) / secure_filename(f.filename)
    watcher.ensure_dirs()
    f.save(path)
    try:
        result = importer.import_flat_rate_costs(str(path), db.DB_PATH)
        path.unlink(missing_ok=True)
        return jsonify(result)
    except Exception as e:
        path.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/import/notice123-rate-case", methods=["POST"])
def api_import_notice123_rate_case():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify({"error": "file must be a .zip archive"}), 400
    eff = (request.form.get("effective_date") or "").strip()
    if not eff:
        return jsonify({"error": "effective_date required (YYYY-MM-DD)"}), 400
    try:
        date.fromisoformat(eff)
    except ValueError:
        return jsonify({"error": "effective_date must be YYYY-MM-DD"}), 400

    tmp = Path(tempfile.mkdtemp(prefix="notice123-"))
    zip_path = tmp / secure_filename(f.filename)
    try:
        f.save(zip_path)
        result = importer.import_notice123_rate_case(
            zip_path, db.DB_PATH, effective_date=eff
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.route("/api/export/postage-invoice")
def api_export_postage_invoice():
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not pn or not start or not end:
        return jsonify({"error": "parent_number, start_date, end_date required"}), 400
    discount = request.args.get("discount", type=float)
    if discount is None:
        discount = _stored_pricing_terms(end)["flats_customer_discount"]
    if discount < 0:
        return jsonify({"error": "discount must be non-negative"}), 400
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    remove_zeros = _bool_param("remove_zeros", False)
    hide_costs = _bool_param("hide_costs", False)
    hide_savings = _bool_param("hide_savings", False)
    try:
        out = exports.export_postage_invoice(
            pn,
            start,
            end,
            discount=discount,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            remove_zeros=remove_zeros,
            hide_costs=hide_costs,
            hide_savings=hide_savings,
        )
        conn = db.get_connection()
        row = conn.execute(
            "SELECT customer_name FROM customers WHERE customer_number = ?",
            (int(pn),),
        ).fetchone()
        conn.close()
        cust_name = (row["customer_name"] if row else f"Account {pn}").strip()
        name = exports.postage_invoice_download_name(
            title_name=cust_name,
            parent_number=int(pn),
            end_date=end,
        )
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/flats-grid-xlsx")
def api_export_flats_grid_xlsx():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    consolidate = _bool_param("consolidate", False)
    remove_zeros = _bool_param("remove_zeros", False)
    hide_costs = _bool_param("hide_costs", False)
    hide_customer_numbers = _bool_param("hide_customer_numbers", True)
    allocate_presort_rejects = _bool_param("allocate_presort_rejects", False)
    sort_key = request.args.get("sort_key") or "date"
    sort_dir = request.args.get("sort_dir", type=int)
    if sort_dir is None:
        sort_dir = 1
    if sort_dir not in (-1, 1):
        sort_dir = 1
    try:
        out = exports.export_flats_data_grid_xlsx(
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            consolidate=consolidate,
            remove_zeros=remove_zeros,
            hide_costs=hide_costs,
            hide_customer_numbers=hide_customer_numbers,
            allocate_presort_rejects=allocate_presort_rejects,
            sort_key=sort_key,
            sort_dir=sort_dir,
        )
        name = f"Flats_Invoice_{start}_{end}.xlsx"
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/flats-grid-csv")
def api_export_flats_grid_csv():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    consolidate = _bool_param("consolidate", False)
    remove_zeros = _bool_param("remove_zeros", False)
    hide_costs = _bool_param("hide_costs", False)
    hide_customer_numbers = _bool_param("hide_customer_numbers", True)
    allocate_presort_rejects = _bool_param("allocate_presort_rejects", False)
    sort_key = request.args.get("sort_key") or "date"
    sort_dir = request.args.get("sort_dir", type=int)
    if sort_dir is None:
        sort_dir = 1
    if sort_dir not in (-1, 1):
        sort_dir = 1
    try:
        out = exports.export_flats_data_grid_csv(
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            consolidate=consolidate,
            remove_zeros=remove_zeros,
            hide_costs=hide_costs,
            hide_customer_numbers=hide_customer_numbers,
            allocate_presort_rejects=allocate_presort_rejects,
            sort_key=sort_key,
            sort_dir=sort_dir,
        )
        efd_weekly_bundle = _bool_param("efd_weekly_bundle", False)
        dt = datetime.strptime(end, "%Y-%m-%d")
        end_label = f"{dt.month}-{dt.day}-{dt.year}"
        scope = "All Accounts"
        cust_name = None
        if pn is not None:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT customer_name FROM customers WHERE customer_number = ?",
                (int(pn),),
            ).fetchone()
            conn.close()
            cust_name = (row["customer_name"] if row else f"Account {pn}").strip()
            scope = f"{cust_name} ({pn})"
        elif cn is not None:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT customer_name FROM customers WHERE customer_number = ?",
                (int(cn),),
            ).fetchone()
            conn.close()
            cust_name = (row["customer_name"] if row else f"Account {cn}").strip()
            scope = f"{cust_name} ({cn})"
        if efd_weekly_bundle and pn is not None:
            name = exports.efd_account_report_download_name(
                title_name=cust_name,
                parent_number=int(pn),
                report_label="Flats Report",
                start_date=start,
                end_date=end,
                ext="csv",
            )
        else:
            name = f"{scope} Flats Report {end_label}.csv"
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="text/csv",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/ground-advantage-zone-pricing-csv")
def api_export_ground_advantage_zone_pricing_csv():
    """Download current Ground Advantage retail zone matrix (from SQLite, last import)."""
    try:
        out = exports.export_ground_advantage_zone_pricing_csv()
        name = exports.ground_advantage_zone_pricing_download_name()
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="text/csv",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/profit-report-xlsx", methods=["GET", "POST"])
def api_export_profit_report_xlsx():
    body: dict = {}
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        start = body.get("start_date")
        end = body.get("end_date")
        pn = body.get("parent_number")
        cn = body.get("customer_number")
        if pn is not None:
            pn = int(pn)
        if cn is not None:
            cn = int(cn)
        show_parents = bool(body.get("show_parents", True))
        show_main = bool(body.get("show_main", True))
        discount = body.get("discount")
        discount_efd = body.get("discount_efd")
        try:
            discount = float(discount) if discount is not None else None
            discount_efd = float(discount_efd) if discount_efd is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "discount and discount_efd must be numbers"}), 400
    else:
        start = request.args.get("start_date")
        end = request.args.get("end_date")
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        show_parents = _bool_param("show_parents", True)
        show_main = _bool_param("show_main", True)
        discount = request.args.get("discount", type=float)
        discount_efd = request.args.get("discount_efd", type=float)
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    terms = _stored_pricing_terms(end)
    discount, _src = _resolve_knob(discount, terms, "flats_customer_discount")
    discount_efd, _src = _resolve_knob(discount_efd, terms, "flats_efd_discount")
    if discount < 0:
        return jsonify({"error": "discount must be non-negative"}), 400
    if discount_efd < 0:
        return jsonify({"error": "discount_efd must be non-negative"}), 400
    try:
        conn_pf = db.get_connection()
        try:
            if request.method == "POST":
                profit_ids, parcel_fee, perr = _profit_request_extras_post(conn_pf, body)
            else:
                profit_ids, parcel_fee, perr = _profit_request_extras(conn_pf)
        finally:
            conn_pf.close()
        if perr:
            return jsonify({"error": perr}), 400
        if request.method == "POST":
            efd_raw, efd_err = _efd_parcel_fee_from_post_body(body)
            if efd_err:
                return jsonify({"error": efd_err}), 400
            efd_q = efd_raw
        else:
            efd_q = request.args.get("efd_parcel_fee", type=float)
            if efd_q is not None and efd_q < 0:
                return jsonify({"error": "efd_parcel_fee must be non-negative"}), 400
        fee_efd, fe_err = _efd_parcel_fee_pair(
            efd_q, parcel_fee, default=terms["parcel_fee_per_piece"]
        )
        if fe_err:
            return jsonify({"error": fe_err}), 400
        out = exports.export_profit_report_xlsx(
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            flats_discount=float(discount),
            flats_discount_efd=float(discount_efd),
            efd_parcel_fee=float(fee_efd),
            profit_account_ids=profit_ids,
        )
        dt = datetime.strptime(end, "%Y-%m-%d")
        end_label = f"{dt.month}-{dt.day}-{dt.year}"
        scope = "All Accounts"
        if pn is not None:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT customer_name FROM customers WHERE customer_number = ?",
                (int(pn),),
            ).fetchone()
            conn.close()
            cust_name = (row["customer_name"] if row else f"Account {pn}").strip()
            scope = f"{cust_name} ({pn})"
        elif cn is not None:
            scope = f"Account {cn}"
        name = f"{scope} Profit Report {end_label}.xlsx"
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profit/flats", methods=["GET", "POST"])
def api_profit_flats():
    body: dict = {}
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        start = body.get("start_date")
        end = body.get("end_date")
        pn = body.get("parent_number")
        cn = body.get("customer_number")
        if pn is not None:
            pn = int(pn)
        if cn is not None:
            cn = int(cn)
        show_parents = bool(body.get("show_parents", True))
        show_main = bool(body.get("show_main", True))
        discount = body.get("discount")
        discount_efd = body.get("discount_efd")
        try:
            discount = float(discount) if discount is not None else None
            discount_efd = float(discount_efd) if discount_efd is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "discount and discount_efd must be numbers"}), 400
    else:
        start = request.args.get("start_date")
        end = request.args.get("end_date")
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        show_parents = _bool_param("show_parents", True)
        show_main = _bool_param("show_main", True)
        discount = request.args.get("discount", type=float)
        discount_efd = request.args.get("discount_efd", type=float)
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    terms = _stored_pricing_terms(end)
    discount, discount_src = _resolve_knob(discount, terms, "flats_customer_discount")
    discount_efd, discount_efd_src = _resolve_knob(discount_efd, terms, "flats_efd_discount")
    if discount < 0:
        return jsonify({"error": "discount must be non-negative"}), 400
    if discount_efd < 0:
        return jsonify({"error": "discount_efd must be non-negative"}), 400

    try:
        conn = db.get_connection()
        retail_view = db.get_flats_retail_rate(conn, as_of_date=end)
        retail_rate = retail_view["rate"]
        sell_to_rate = round(float(retail_rate) - float(discount), 4)
        if request.method == "POST":
            profit_ids, parcel_fee, perr = _profit_request_extras_post(conn, body)
        else:
            profit_ids, parcel_fee, perr = _profit_request_extras(conn)
        if perr:
            conn.close()
            return jsonify({"error": perr}), 400
        parcel_fee, parcel_fee_src = _resolve_knob(parcel_fee, terms, "parcel_fee_per_piece")
        totals = db.query_ws3_flats_profit_totals(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            customer_discount=discount,
            efd_discount=discount_efd,
            profit_account_ids=profit_ids,
        )
        rate_summary = db.query_ws3_flats_profit_rate_type_summary(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            customer_discount=discount,
            efd_discount=discount_efd,
            profit_account_ids=profit_ids,
        )
        detail = db.query_ws3_flats_profit_detail(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            customer_discount=discount,
            efd_discount=discount_efd,
            profit_account_ids=profit_ids,
        )
        conn.close()

        if not rate_summary and not detail:
            return (
                jsonify(
                    {
                        "error": _WS3_FLATS_PROFIT_NO_DATA_MSG,
                        "empty": True,
                        "meta": {
                            "start_date": start,
                            "end_date": end,
                            "retail_rate": retail_rate,
                            "tariff_effective_date": retail_view["tariff_effective_date"],
                            "discount": float(discount),
                            "discount_efd": float(discount_efd),
                            "sell_to_rate": sell_to_rate,
                            "profit_accounts": profit_ids,
                            "parcel_fee": float(parcel_fee),
                            "terms_effective_date": terms["effective_date"],
                            "terms_source": {
                                "discount": discount_src,
                                "discount_efd": discount_efd_src,
                                "parcel_fee": parcel_fee_src,
                            },
                        },
                        "totals": totals,
                        "rate_summary": [],
                        "detail": [],
                    }
                ),
                404,
            )

        return jsonify(
            {
                "meta": {
                    "start_date": start,
                    "end_date": end,
                    "retail_rate": retail_rate,
                    "tariff_effective_date": retail_view["tariff_effective_date"],
                    "discount": float(discount),
                    "discount_efd": float(discount_efd),
                    "sell_to_rate": sell_to_rate,
                    "profit_accounts": profit_ids,
                    "parcel_fee": float(parcel_fee),
                    "terms_effective_date": terms["effective_date"],
                    "terms_source": {
                        "discount": discount_src,
                        "discount_efd": discount_efd_src,
                        "parcel_fee": parcel_fee_src,
                    },
                },
                "totals": totals,
                "rate_summary": rate_summary,
                "detail": detail,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profit/parcels", methods=["GET", "POST"])
def api_profit_parcels():
    body: dict = {}
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        start = body.get("start_date")
        end = body.get("end_date")
        pn = body.get("parent_number")
        cn = body.get("customer_number")
        if pn is not None:
            pn = int(pn)
        if cn is not None:
            cn = int(cn)
        show_parents = bool(body.get("show_parents", True))
        show_main = bool(body.get("show_main", True))
    else:
        start = request.args.get("start_date")
        end = request.args.get("end_date")
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        show_parents = _bool_param("show_parents", True)
        show_main = _bool_param("show_main", True)
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400

    try:
        terms = _stored_pricing_terms(end)
        conn = db.get_connection()
        if request.method == "POST":
            profit_ids, parcel_fee, perr = _profit_request_extras_post(conn, body)
        else:
            profit_ids, parcel_fee, perr = _profit_request_extras(conn)
        if perr:
            conn.close()
            return jsonify({"error": perr}), 400
        parcel_fee, parcel_fee_src = _resolve_knob(parcel_fee, terms, "parcel_fee_per_piece")
        if request.method == "POST":
            efd_raw, efd_err = _efd_parcel_fee_from_post_body(body)
            if efd_err:
                conn.close()
                return jsonify({"error": efd_err}), 400
            efd_q = efd_raw
        else:
            efd_q = request.args.get("efd_parcel_fee", type=float)
            if efd_q is not None and efd_q < 0:
                conn.close()
                return jsonify({"error": "efd_parcel_fee must be non-negative"}), 400
        fee_efd, fe_err = _efd_parcel_fee_pair(
            efd_q, parcel_fee, default=terms["parcel_fee_per_piece"]
        )
        if fe_err:
            conn.close()
            return jsonify({"error": fe_err}), 400
        raw = db.query_parcel_profit_totals(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            profit_account_ids=profit_ids,
        )
        conn.close()

        pp = db.parcel_profit_from_raw(raw, parcel_fee_per_piece=float(parcel_fee))

        return jsonify(
            {
                "meta": {
                    "start_date": start,
                    "end_date": end,
                    "profit_accounts": profit_ids,
                    "parcel_fee": float(parcel_fee),
                    "efd_parcel_fee": float(fee_efd),
                    "terms_effective_date": terms["effective_date"],
                    "terms_source": {"parcel_fee": parcel_fee_src},
                },
                "raw": raw,
                "computed": pp["computed"],
                "lines": pp["lines"],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/parcel-report")
def api_export_parcel_report():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    try:
        out = exports.export_parcel_report(
            start,
            end,
            pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
        )
        if pn is None:
            name = exports.parcel_report_download_name(start, end, pn, cn)
        else:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT customer_name FROM customers WHERE customer_number = ?",
                (int(pn),),
            ).fetchone()
            conn.close()
            cust_name = (row["customer_name"] if row else f"Account {pn}").strip()
            dt = datetime.strptime(end, "%Y-%m-%d")
            end_label = f"{dt.month}-{dt.day}-{dt.year}"
            name = f"{cust_name} ({pn}) Parcel Invoice {end_label}.xlsx"
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/parcel-counts-xlsx")
def api_export_parcel_counts_xlsx():
    """PARCELS (COUNTS) grid with Retail Cost column; always loads amounts from DB."""
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    consolidate = _bool_param("consolidate", False)
    remove_zeros = _bool_param("remove_zeros", False)
    hide_customer_numbers = _bool_param("hide_customer_numbers", True)
    try:
        out = exports.export_parcel_counts_report_xlsx(
            start,
            end,
            pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            consolidate=consolidate,
            remove_zeros=remove_zeros,
            hide_costs=False,
            hide_customer_numbers=hide_customer_numbers,
        )
        name = exports.parcel_counts_download_name(start, end, pn, cn)
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/consolidated-parcel-csv")
def api_export_consolidated_parcel_csv():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    try:
        out = exports.export_parcel_billing_csv(
            start,
            end,
            pn,
            cn,
            show_parents=show_parents,
            show_main=show_main,
        )
        scope = exports.parcel_report_scope_label(pn, cn)
        name = f"Parcel_Billing_{scope}_{start}_{end}.csv"
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="text/csv",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/efd-parcel-invoice-xlsx")
def api_export_efd_parcel_invoice_xlsx():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    # Prefer efd_parcel_fee; fall back to parcel_fee for older bookmarked URLs.
    efd_q = request.args.get("efd_parcel_fee", type=float)
    if efd_q is not None and efd_q < 0:
        return jsonify({"error": "efd_parcel_fee must be non-negative"}), 400
    parcel_fb = request.args.get("parcel_fee", type=float)
    if parcel_fb is not None and parcel_fb < 0:
        return jsonify({"error": "parcel_fee must be non-negative"}), 400
    fee_efd, fe_err = _efd_parcel_fee_pair(
        efd_q, parcel_fb, default=_stored_pricing_terms(end)["parcel_fee_per_piece"]
    )
    if fe_err:
        return jsonify({"error": fe_err}), 400
    try:
        out, title = exports.export_efd_parcel_invoice_xlsx(
            start,
            end,
            pn,
            cn,
            show_parents=show_parents,
            show_main=show_main,
            efd_parcel_fee=float(fee_efd),
        )
        name = exports.efd_parcel_invoice_download_name(
            title, parent_number=pn, customer_number=cn, end_date=end
        )
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/efd-weekly-invoice-xlsx")
def api_export_efd_weekly_invoice_xlsx():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    terms = _stored_pricing_terms(end)
    discount = request.args.get("discount", type=float)
    if discount is None:
        discount = terms["flats_customer_discount"]
    if discount < 0:
        return jsonify({"error": "discount must be non-negative"}), 400
    efd_q = request.args.get("efd_parcel_fee", type=float)
    if efd_q is not None and efd_q < 0:
        return jsonify({"error": "efd_parcel_fee must be non-negative"}), 400
    parcel_fb = request.args.get("parcel_fee", type=float)
    if parcel_fb is not None and parcel_fb < 0:
        return jsonify({"error": "parcel_fee must be non-negative"}), 400
    fee_efd, fe_err = _efd_parcel_fee_pair(
        efd_q, parcel_fb, default=terms["parcel_fee_per_piece"]
    )
    if fe_err:
        return jsonify({"error": fe_err}), 400
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    remove_zeros = _bool_param("remove_zeros", False)
    hide_costs = _bool_param("hide_costs", False)
    hide_savings = _bool_param("hide_savings", False)
    pn = request.args.get("parent_number", type=int)
    try:
        out = exports.export_efd_weekly_invoice_xlsx(
            start,
            end,
            discount=float(discount),
            efd_parcel_fee=float(fee_efd),
            show_parents=show_parents,
            show_main=show_main,
            remove_zeros=remove_zeros,
            hide_costs=hide_costs,
            hide_savings=hide_savings,
            parent_number=pn,
        )
        if pn is not None:
            name = exports.efd_weekly_account_download_name(
                exports.efd_weekly_summary_label(int(pn)), start, end
            )
        else:
            name = exports.efd_weekly_invoice_download_name(start, end)
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _bool_from_payload(payload: dict, name: str, default: bool = False) -> bool:
    if name not in payload:
        return default
    v = payload[name]
    return str(v).lower() in ("1", "true", "yes", "on")


@app.route("/api/export/efd-weekly-bundle", methods=["POST"])
def api_export_efd_weekly_bundle():
    body = request.get_json(silent=True) or {}
    start = body.get("start_date") or request.args.get("start_date")
    end = body.get("end_date") or request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    terms = _stored_pricing_terms(end)
    discount = body.get("discount", request.args.get("discount", type=float))
    if discount is None:
        discount = terms["flats_customer_discount"]
    else:
        discount = float(discount)
    if discount < 0:
        return jsonify({"error": "discount must be non-negative"}), 400
    efd_q = body.get("efd_parcel_fee", request.args.get("efd_parcel_fee"))
    parcel_fb = body.get("parcel_fee", request.args.get("parcel_fee"))
    efd_q_f = float(efd_q) if efd_q is not None else None
    parcel_fb_f = float(parcel_fb) if parcel_fb is not None else None
    if efd_q_f is not None and efd_q_f < 0:
        return jsonify({"error": "efd_parcel_fee must be non-negative"}), 400
    if parcel_fb_f is not None and parcel_fb_f < 0:
        return jsonify({"error": "parcel_fee must be non-negative"}), 400
    fee_efd, fe_err = _efd_parcel_fee_pair(
        efd_q_f, parcel_fb_f, default=terms["parcel_fee_per_piece"]
    )
    if fe_err:
        return jsonify({"error": fe_err}), 400
    parcel_discount = body.get("parcel_discount", request.args.get("parcel_discount"))
    if parcel_discount is None:
        parcel_discount = terms["parcel_customer_discount"]
    else:
        parcel_discount = float(parcel_discount)
    if parcel_discount < 0:
        return jsonify({"error": "parcel_discount must be non-negative"}), 400
    postage_discount = body.get("postage_discount", request.args.get("postage_discount"))
    if postage_discount is None:
        postage_discount = terms["flats_customer_discount"]
    else:
        postage_discount = float(postage_discount)
    if postage_discount < 0:
        return jsonify({"error": "postage_discount must be non-negative"}), 400
    try:
        result = exports.save_efd_weekly_bundle(
            start,
            end,
            discount=discount,
            postage_discount=postage_discount,
            efd_parcel_fee=float(fee_efd),
            parcel_discount=parcel_discount,
            show_parents=_bool_from_payload(body, "show_parents", True),
            show_main=_bool_from_payload(body, "show_main", True),
            remove_zeros=_bool_from_payload(body, "remove_zeros", False),
            hide_costs=_bool_from_payload(body, "hide_costs", False),
            hide_savings=_bool_from_payload(body, "hide_savings", False),
        )
        if not result.get("saved"):
            err = (
                result["failed"][0]["error"]
                if result.get("failed")
                else "No reports were generated"
            )
            return jsonify({"error": err, **result}), 400
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/consolidated-volumes-xlsx")
def api_export_consolidated_volumes_xlsx():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    consolidate = _bool_param("consolidate", False)
    remove_zeros = _bool_param("remove_zeros", True)
    hide_summary_money = _bool_param("hide_costs", True)
    hide_customer_numbers = _bool_param("hide_customer_numbers", True)
    scope = (request.args.get("account_scope") or "All Accounts").strip() or "All Accounts"
    parcel_discount = request.args.get("parcel_discount", type=float)
    if parcel_discount is None:
        parcel_discount = _stored_pricing_terms(end)["parcel_customer_discount"]
    if parcel_discount < 0:
        return jsonify({"error": "parcel_discount must be non-negative"}), 400
    try:
        out = exports_consolidated_volumes.export_consolidated_volumes_xlsx(
            start,
            end,
            pn,
            cn,
            show_parents=show_parents,
            show_main=show_main,
            consolidate=consolidate,
            remove_zeros=remove_zeros,
            hide_costs_summary=hide_summary_money,
            hide_customer_numbers=hide_customer_numbers,
            account_scope_label=scope,
            parcel_discount=float(parcel_discount),
        )
        name = exports_consolidated_volumes.consolidated_volumes_download_name(scope, end)
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/efd-daily-volumes-xlsx")
def api_export_efd_daily_volumes_xlsx():
    """Consolidated volumes XLS for one EFD parent (3901, 3899, or 3900)."""
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    if pn is None:
        return jsonify({"error": "parent_number required"}), 400
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    consolidate = _bool_param("consolidate", False)
    remove_zeros = _bool_param("remove_zeros", True)
    hide_summary_money = _bool_param("hide_costs", True)
    hide_customer_numbers = _bool_param("hide_customer_numbers", True)
    parcel_discount = request.args.get("parcel_discount", type=float)
    if parcel_discount is None:
        parcel_discount = _stored_pricing_terms(end)["parcel_customer_discount"]
    if parcel_discount < 0:
        return jsonify({"error": "parcel_discount must be non-negative"}), 400
    try:
        scope = exports.efd_report_scope_label(int(pn))
        out = exports_consolidated_volumes.export_consolidated_volumes_xlsx(
            start,
            end,
            int(pn),
            None,
            show_parents=show_parents,
            show_main=show_main,
            consolidate=consolidate,
            remove_zeros=remove_zeros,
            hide_costs_summary=hide_summary_money,
            hide_customer_numbers=hide_customer_numbers,
            account_scope_label=scope,
            parcel_discount=float(parcel_discount),
        )
        name = exports_consolidated_volumes.consolidated_volumes_download_name(scope, end)
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/daily-reports", methods=["POST"])
def api_export_daily_reports():
    """Generate and save the daily report set to disk for one day or a range.

    Accepts ``report_date`` (single) or ``start_date`` + ``end_date`` (range).
    Idempotent: business days whose folder is already complete are skipped.
    """
    payload = request.get_json(silent=True) or {}

    def _arg(name: str) -> str | None:
        val = payload.get(name)
        if val is None:
            val = request.args.get(name) or request.form.get(name)
        return str(val).strip() if val else None

    report_date = _arg("report_date")
    start = _arg("start_date")
    end = _arg("end_date")

    if report_date:
        dates = [report_date]
    elif start and end:
        dates = db._business_days_in_range(start, end)
    else:
        return jsonify({"error": "report_date or start_date+end_date required"}), 400

    if not dates:
        return jsonify({"error": "no business days in range"}), 400

    try:
        generated: list[dict] = []
        skipped: list[str] = []
        for d in dates:
            if exports.daily_report_set_complete(d):
                skipped.append(d)
                continue
            generated.append(exports.save_daily_report_set(d))
        return jsonify({"generated": generated, "skipped": skipped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/parcel-zone-summary")
def api_export_parcel_zone_summary():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    pn = request.args.get("parent_number", type=int)
    cn = request.args.get("customer_number", type=int)
    show_parents = _bool_param("show_parents", True)
    show_main = _bool_param("show_main", True)
    parcel_discount = request.args.get("parcel_discount", type=float)
    if parcel_discount is None:
        parcel_discount = _stored_pricing_terms(end)["parcel_customer_discount"]
    if parcel_discount < 0:
        return jsonify({"error": "parcel_discount must be non-negative"}), 400
    try:
        conn = db.get_connection()
        summary = db.query_parcel_zone_summary(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            hide_costs=False,
            parcel_discount=float(parcel_discount),
        )
        conn.close()
        out = exports.export_parcel_zone_summary_xlsx(
            summary,
            start_date=start,
            end_date=end,
            parent_number=pn,
            customer_number=cn,
            show_parents=show_parents,
            show_main=show_main,
            parcel_discount=float(parcel_discount),
        )
        efd_weekly_bundle = _bool_param("efd_weekly_bundle", False)
        if efd_weekly_bundle and pn is not None:
            name = exports.efd_account_report_download_name(
                title_name=summary.get("title_name"),
                parent_number=int(pn),
                report_label="Parcel invoice",
                start_date=start,
                end_date=end,
                ext="xlsx",
            )
        else:
            name = exports.parcel_invoice_download_name(
                title_name=summary.get("title_name"),
                parent_number=pn,
                end_date=end,
            )
        return send_file(
            out,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    try:
        from waitress import serve

        print(f"Serving on http://{host}:{port} (waitress)")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, use_reloader=False)


# Apply a staged restore (if any) before opening the DB, so postage.db is never
# replaced while a connection holds it open (required on Windows).
backup_restore.apply_pending_restore()
db.init_db()
watcher.ensure_dirs()

if __name__ == "__main__":
    main()
