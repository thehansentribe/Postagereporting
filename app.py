"""Flask application: postage reporting dashboard API and UI."""

from __future__ import annotations

from datetime import datetime
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import db
import exports
import exports_consolidated_volumes
import importer
import watcher

app = Flask(__name__, template_folder="templates", static_folder="static")

_watcher_started = False
_watcher_lock = threading.Lock()


def _ensure_watcher() -> None:
    global _watcher_started
    with _watcher_lock:
        if not _watcher_started:
            t = threading.Thread(target=watcher.watch_loop, kwargs={"interval_sec": 60}, daemon=True)
            t.start()
            _watcher_started = True


def _bool_param(name: str, default: bool = False) -> bool:
    v = request.args.get(name)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


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
        conn = db.get_connection()
        data = db.query_postage(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
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
    if mail_class == db.WS3_REJECT_MAIL_CLASS:
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


@app.route("/api/parcels")
def api_parcels():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "start_date and end_date required"}), 400
    try:
        pn = request.args.get("parent_number", type=int)
        cn = request.args.get("customer_number", type=int)
        conn = db.get_connection()
        data = db.query_parcels(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
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
        conn = db.get_connection()
        data = db.query_parcel_zone_summary(
            conn,
            start,
            end,
            parent_number=pn,
            customer_number=cn,
            show_parents=_bool_param("show_parents", True),
            show_main=_bool_param("show_main", True),
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


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        watcher.scan_once()
        return jsonify({"ok": True})
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
        discount = 0.10
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
        # Friendly download name: "Customer (1234) Postage invoice M-D-YYYY.xlsx"
        conn = db.get_connection()
        row = conn.execute(
            "SELECT customer_name FROM customers WHERE customer_number = ?",
            (int(pn),),
        ).fetchone()
        conn.close()
        cust_name = (row["customer_name"] if row else f"Account {pn}").strip()
        dt = datetime.strptime(end, "%Y-%m-%d")
        end_label = f"{dt.month}-{dt.day}-{dt.year}"
        name = f"{cust_name} ({pn}) Postage invoice {end_label}.xlsx"
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
    remove_zeros = _bool_param("remove_zeros", False)
    hide_summary_money = _bool_param("hide_costs", False)
    scope = (request.args.get("account_scope") or "All Accounts").strip() or "All Accounts"
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
            account_scope_label=scope,
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
        )
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
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


db.init_db()
watcher.ensure_dirs()

if __name__ == "__main__":
    main()
