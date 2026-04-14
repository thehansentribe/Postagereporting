(function () {
  const $ = (id) => document.getElementById(id);

  const state = {
    postageData: null,
    parcelData: null,
    parcelZoneSummary: null,
    /** Last `/api/summary` JSON for Import Summary customer tables + checkbox filters */
    lastSummary: null,
    /** Selected Postage row key for edit workflow */
    selectedPostage: null,
    sortPostage: { key: "date", dir: 1 },
    sortParcel: { key: "date", dir: 1 },
    /** @type {{ parent: { customer_number: number; customer_name: string; kind: string }[]; child: ...[]; standalone: ...[] }} */
    accountsByKind: { parent: [], child: [], standalone: [] },
  };

  function fmtMoney(n) {
    if (n == null || n === "") return "";
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);
  }

  /** Same normalization as postage invoice export (`btnExportPostage`). */
  function parseFlatsDiscount() {
    const discRaw = ($("invoiceDiscount") && $("invoiceDiscount").value) || "";
    let discount = discRaw.trim() === "" ? 0.1 : parseFloat(discRaw);
    if (Number.isNaN(discount)) discount = 0.1;
    return discount;
  }

  function fmtInt(n) {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n);
  }

  /** Account / customer ids without thousands separators */
  function fmtId(v) {
    if (v == null || v === "") return "";
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return String(Math.trunc(n));
  }

  /** Import Summary customer # column (em dash when no account code, e.g. NULL billing CAC) */
  function fmtSummaryAccountNum(v) {
    if (v == null || v === "") return "\u2014";
    return fmtId(v);
  }

  function defaultDates() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const today = `${y}-${m}-${day}`;
    return { start: today, end: today };
  }

  function queryParams() {
    const p = new URLSearchParams();
    p.set("start_date", $("startDate").value);
    p.set("end_date", $("endDate").value);
    const sel = $("selectAccount");
    const opt = sel.selectedOptions[0];
    if (opt && opt.value) {
      const kind = opt.getAttribute("data-kind");
      const n = parseInt(opt.value, 10);
      if (!Number.isNaN(n)) {
        if (kind === "parent") p.set("parent_number", String(n));
        else p.set("customer_number", String(n));
      }
    }
    const search = $("searchCustomer").value.trim();
    if (search) {
      const cn = parseInt(search, 10);
      if (!Number.isNaN(cn)) p.set("customer_number", String(cn));
    }
    p.set("show_parents", $("showParents").checked ? "true" : "false");
    p.set("show_main", $("showMain").checked ? "true" : "false");
    p.set("consolidate", $("consolidate").checked ? "true" : "false");
    p.set("remove_zeros", $("removeZeros").checked ? "true" : "false");
    p.set("hide_costs", $("hideCosts").checked ? "true" : "false");
    p.set("hide_savings", "false");
    return p;
  }

  /** Same as queryParams but force postage costs on (for consolidated flats column W). */
  function queryParamsWithPostageCosts() {
    const p = queryParams();
    p.set("hide_costs", "false");
    return p;
  }

  function sortRows(rows, key, dir) {
    const mul = dir;
    return [...rows].sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * mul;
      return String(va).localeCompare(String(vb), undefined, { numeric: true }) * mul;
    });
  }

  function renderSummaryBar(el, postage) {
    if (!postage) {
      el.textContent = "";
      return;
    }
    const hideCosts = $("hideCosts").checked;
    const parts = [
      `<span>Total Records: ${fmtInt(postage.total_records)}</span>`,
      `<span>Total Pieces: ${fmtInt(postage.total_pieces)}</span>`,
    ];
    if (!hideCosts && postage.total_cost != null) {
      parts.push(`<span>Total Cost: ${fmtMoney(postage.total_cost)}</span>`);
    }
    el.innerHTML = parts.join("");
  }

  function renderParcelSummaryBar(el, data) {
    if (!data) {
      el.textContent = "";
      return;
    }
    const hide = $("hideCosts").checked;
    const parts = [
      `<span>Total Records: ${fmtInt(data.total_records)}</span>`,
      `<span>Total Pieces: ${fmtInt(data.total_pieces)}</span>`,
    ];
    if (!hide && data.total_retail != null) {
      parts.push(`<span>Retail cost: ${fmtMoney(data.total_retail)}</span>`);
    }
    el.innerHTML = parts.join("");
  }

  const POSTAGE_STICKY_WIDTHS = [90, 140, 140, 100];
  const PARCEL_STICKY_WIDTHS = [90, 140, 140, 120, 55];

  function stickyLeft(index, widths) {
    let x = 0;
    for (let i = 0; i < index; i++) x += widths[i];
    return x;
  }

  /** Trailing non-oz columns after total_qty: 1 or 2 (Total Cost when costs shown). */
  function postageTrailingColumnCount(hideCosts) {
    return hideCosts ? 1 : 2;
  }

  /** Shared column layout for Postage grid (no parent/child #, no savings). */
  function postageTableColumns(hideCosts) {
    const ozKeys = [];
    for (let i = 0; i <= 12; i++) ozKeys.push(`oz_${i}`);
    ozKeys.push("oz_13", "oz_13plus");
    const headerKeys = [
      "date",
      "parent_name",
      "child_name",
      "mail_class",
      ...ozKeys,
      "total_qty",
    ];
    if (!hideCosts) headerKeys.push("total_cost");
    const headers = [
      "Date",
      "Parent Name",
      "Child Name",
      "Class",
      ...Array.from({ length: 13 }, (_, i) => `${i} oz`),
      "13 oz",
      "13+ oz",
      "Total Qty",
    ];
    if (!hideCosts) headers.push("Total Cost");
    return { headerKeys, headers };
  }

  function csvEscape(val) {
    if (val == null) return "";
    const s = String(val);
    if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  }

  /** Nearest cent for CSV cost/savings (e.g. 10.12). */
  function csvMoneyRounded(n) {
    const x = Number(n);
    if (Number.isNaN(x)) return "";
    return (Math.round(x * 100) / 100).toFixed(2);
  }

  /**
   * Parcel rows from the API are split by mail class and zone. For CSV export, roll up to one row
   * per date × parent × child with weight buckets, total qty, and summed total_retail (zone roll-up).
   */
  function aggregateParcelRowsForCsv(rawRows) {
    const map = new Map();
    for (const r of rawRows) {
      const key = [
        r.date,
        r.parent_name,
        r.parent_number,
        r.child_name,
        r.child_number,
      ].join("\t");
      if (!map.has(key)) {
        const o = {
          date: r.date,
          parent_name: r.parent_name,
          parent_number: r.parent_number,
          child_name: r.child_name,
          child_number: r.child_number,
        };
        for (let i = 1; i <= 10; i++) o[`lb_${i}`] = 0;
        o.lb_10plus = 0;
        o.total_qty = 0;
        o.total_retail = 0;
        map.set(key, o);
      }
      const a = map.get(key);
      for (let i = 1; i <= 10; i++) {
        a[`lb_${i}`] += Number(r[`lb_${i}`]) || 0;
      }
      a.lb_10plus += Number(r.lb_10plus) || 0;
      a.total_qty += Number(r.total_qty) || 0;
      a.total_retail += Number(r.total_retail) || 0;
    }
    const out = Array.from(map.values());
    out.sort((x, y) => {
      const d = String(x.date || "").localeCompare(String(y.date || ""));
      if (d !== 0) return d;
      const p = String(x.parent_name || "").localeCompare(String(y.parent_name || ""), undefined, {
        sensitivity: "base",
      });
      if (p !== 0) return p;
      const c = String(x.child_name || "").localeCompare(String(y.child_name || ""), undefined, {
        sensitivity: "base",
      });
      if (c !== 0) return c;
      const pn = Number(x.parent_number);
      const pn2 = Number(y.parent_number);
      if (!Number.isNaN(pn) && !Number.isNaN(pn2) && pn !== pn2) return pn - pn2;
      const cn = Number(x.child_number);
      const cn2 = Number(y.child_number);
      if (!Number.isNaN(cn) && !Number.isNaN(cn2) && cn !== cn2) return cn - cn2;
      return 0;
    });
    return out;
  }

  function parcelCsvColumns(costsHidden) {
    const lbKeys = [];
    for (let i = 1; i <= 10; i++) lbKeys.push(`lb_${i}`);
    lbKeys.push("lb_10plus");
    const headerKeys = [
      "date",
      "parent_name",
      "child_name",
      ...lbKeys,
      "total_qty",
    ];
    const headers = [
      "Date",
      "Parent Name",
      "Child Name",
      ...Array.from({ length: 10 }, (_, i) => `${i + 1} lb`),
      "10+ lb",
      "Total Qty",
    ];
    if (!costsHidden) {
      headerKeys.push("total_retail");
      headers.push("Retail Cost");
    }
    return { headerKeys, headers };
  }

  function parcelCsvCell(row, k) {
    const v = row[k];
    if (k === "total_retail") {
      if (v == null || v === "") return "";
      return csvMoneyRounded(v);
    }
    if (k.startsWith("lb_") || k === "total_qty") return String(Number(v) || 0);
    if (typeof v === "number") return String(v);
    return v != null ? String(v) : "";
  }

  function findBlockSideForZone(blocks, z) {
    for (const block of blocks) {
      if (block.zone_a === z) return { block, side: "a" };
      if (block.zone_b === z) return { block, side: "b" };
    }
    return null;
  }

  /** Row indices 0..9 for 1–10 lb, excluding 9 lb (index 8). */
  function parcelZoneSummaryRowIndices() {
    const out = [];
    for (let ri = 0; ri < 10; ri++) {
      if (ri === 8) continue;
      out.push(ri);
    }
    return out;
  }

  function zoneCellForRow(blocks, z, ri) {
    const hit = findBlockSideForZone(blocks, z);
    if (!hit) return { priority: null, count: 0 };
    const rw = hit.block.rows[ri];
    const c = hit.side === "a" ? rw.zone_a : rw.zone_b;
    return { priority: c.priority, count: c.count || 0 };
  }

  function zoneLineRetailTotal(priority, count, hide) {
    if (hide || priority == null || priority === "") return null;
    const p = Number(priority);
    const n = Number(count) || 0;
    if (Number.isNaN(p)) return null;
    return Math.round(p * n * 100) / 100;
  }

  /**
   * Lines (including header) for the parcels counts CSV, or null if no rows.
   * @param {object|null} data parcel API payload
   * @param {boolean} includeMoney when true, append Retail Cost column
   */
  function buildParcelsCsvLinesFromData(data, includeMoney) {
    if (!data || !(data.rows || []).length) return null;
    const aggregated = aggregateParcelRowsForCsv(data.rows || []);
    const { headerKeys, headers } = parcelCsvColumns(!includeMoney);
    const lines = [headers.map((h) => csvEscape(h)).join(",")];
    for (const row of aggregated) {
      lines.push(headerKeys.map((k) => csvEscape(parcelCsvCell(row, k))).join(","));
    }
    return lines;
  }

  function buildParcelsCsvLines() {
    return buildParcelsCsvLinesFromData(state.parcelData, !$("hideCosts").checked);
  }

  function exportScopeLabel() {
    const sel = $("selectAccount");
    const opt = sel && sel.selectedOptions && sel.selectedOptions[0];
    if (!opt || !opt.value) return "All Accounts";
    const t = (opt.textContent || "").trim();
    return t || "All Accounts";
  }

  function fmtFilenameDate(isoDate) {
    // Input is yyyy-mm-dd from <input type="date">. Output is m-d-yyyy.
    if (!isoDate) return "";
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate).trim());
    if (!m) return String(isoDate).trim();
    const y = m[1];
    const mm = String(parseInt(m[2], 10));
    const dd = String(parseInt(m[3], 10));
    return `${mm}-${dd}-${y}`;
  }

  function safeFilename(name) {
    return String(name || "")
      .replace(/[\\/:*?"<>|]/g, "-")
      .replace(/\s+/g, " ")
      .trim();
  }

  function selectedAccountRow() {
    const sel = $("selectAccount");
    const opt = sel && sel.selectedOptions && sel.selectedOptions[0];
    if (!opt || !opt.value) return null;
    const n = parseInt(opt.value, 10);
    if (Number.isNaN(n)) return null;
    for (const kind of ["parent", "child", "standalone"]) {
      const rows = state.accountsByKind[kind] || [];
      const hit = rows.find((r) => Number(r.customer_number) === n);
      if (hit) return hit;
    }
    return null;
  }

  function downloadParcelsCsv() {
    const lines = buildParcelsCsvLines();
    if (!lines) {
      alert("Load parcel data first (no rows to export).");
      return;
    }
    const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const start = $("startDate").value || "start";
    const end = $("endDate").value || "end";
    const name = `parcels_counts_${start}_${end}.csv`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function downloadConsolidatedVolumesXlsx() {
    const p = queryParams();
    p.set("account_scope", exportScopeLabel());
    const r = await fetch("/api/export/consolidated-volumes-xlsx?" + p.toString());
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || "Export failed");
    }
    const blob = await r.blob();
    const name =
      filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
      "volumes_flats_parcels.xlsx";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function buildPostageTableFixed(container, data) {
    const hideCosts = $("hideCosts").checked;
    const { headerKeys, headers } = postageTableColumns(hideCosts);
    const trailingCols = postageTrailingColumnCount(hideCosts);
    if (!headerKeys.includes(state.sortPostage.key)) state.sortPostage.key = "date";

    const discount = parseFlatsDiscount();
    const rowsWithSavings = (data.rows || []).map((r) => ({
      ...r,
      savings: (Number(r.total_qty) || 0) * discount,
    }));
    const rows = sortRows(rowsWithSavings, state.sortPostage.key, state.sortPostage.dir);

    const nSticky = POSTAGE_STICKY_WIDTHS.length;
    let html = "<table class='data-table'><thead><tr>";
    headers.forEach((h, i) => {
      const k = headerKeys[i];
      const cl = i < nSticky ? "sticky-col" : "";
      const sortClass =
        state.sortPostage.key === k ? (state.sortPostage.dir === 1 ? "sort-asc" : "sort-desc") : "";
      const st =
        i < nSticky
          ? ` style="left:${stickyLeft(i, POSTAGE_STICKY_WIDTHS)}px;min-width:${POSTAGE_STICKY_WIDTHS[i]}px;width:${POSTAGE_STICKY_WIDTHS[i]}px"`
          : i >= nSticky && i < headers.length - trailingCols
            ? " style='min-width:55px;width:55px'"
            : "";
      html += `<th class="${cl} ${sortClass}" data-key="${k}"${st}>${h}</th>`;
    });
    html += "</tr></thead><tbody>";

    if (!rows.length) {
      html += `<tr><td colspan="${headers.length}" class="empty-state">No data found for the selected date range and filters.</td></tr>`;
    } else {
      rows.forEach((row) => {
        const rowKey = {
          date: row.date,
          child_number: row.child_number,
          mail_class: row.mail_class,
        };
        const isSel =
          state.selectedPostage &&
          state.selectedPostage.date === rowKey.date &&
          String(state.selectedPostage.child_number) === String(rowKey.child_number) &&
          state.selectedPostage.mail_class === rowKey.mail_class;
        const trCls = isSel ? " class='row-selected'" : "";
        html += `<tr${trCls} data-date="${escapeHtml(row.date)}" data-child-number="${escapeHtml(
          String(row.child_number ?? "")
        )}" data-mail-class="${escapeHtml(row.mail_class || "")}">`;
        headerKeys.forEach((k, i) => {
          const v = row[k];
          const isOz = k.startsWith("oz_");
          const n = Number(v) || 0;
          const muted = isOz && n === 0 ? " muted" : "";
          const cellClass = i < nSticky ? "sticky-col" : "";
          const st =
            i < nSticky
              ? ` style="left:${stickyLeft(i, POSTAGE_STICKY_WIDTHS)}px;min-width:${POSTAGE_STICKY_WIDTHS[i]}px;width:${POSTAGE_STICKY_WIDTHS[i]}px"`
              : "";
          let inner;
          if (k === "total_cost") inner = fmtMoney(v);
          else if (k === "total_qty") inner = `<strong>${fmtInt(n)}</strong>`;
          else if (isOz || typeof v === "number") inner = fmtInt(Number(v) || 0);
          else inner = v != null ? String(v) : "";
          html += `<td class="${cellClass} num${muted}"${st}>${inner}</td>`;
        });
        html += "</tr>";
      });
    }
    html += "</tbody></table>";
    container.innerHTML = html;

    container.querySelectorAll("tbody tr[data-date]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const date = tr.getAttribute("data-date") || "";
        const childNumRaw = tr.getAttribute("data-child-number") || "";
        const mailClass = tr.getAttribute("data-mail-class") || "";
        if (!date || date === "Combined") return;
        if (mailClass === "Presort rejects") return;
        const cn = parseInt(childNumRaw, 10);
        if (Number.isNaN(cn) || !mailClass) return;
        state.selectedPostage = { date, child_number: cn, mail_class: mailClass };
        const btn = $("btnEditPostage");
        if (btn) btn.disabled = false;
        buildPostageTableFixed(container, data);
      });
    });

    container.querySelectorAll("thead th[data-key]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.getAttribute("data-key");
        if (state.sortPostage.key === key) state.sortPostage.dir *= -1;
        else {
          state.sortPostage.key = key;
          state.sortPostage.dir = 1;
        }
        buildPostageTableFixed(container, data);
      });
    });
  }

  function fmtCellMoney(v) {
    if (v == null || v === "") return "\u2014";
    return fmtMoney(v);
  }

  function buildParcelZoneSummary(bannerEl, container, footerEl, data) {
    if (!data || !data.blocks || !data.blocks.length) {
      bannerEl.innerHTML = "";
      container.innerHTML =
        "<p class='empty-state'>No zone summary for this range (load data or select accounts with parcel billing in zones 1–8).</p>";
      footerEl.textContent = "";
      return;
    }
    const hide = data.hide_costs === true;
    bannerEl.innerHTML = `<div class="parcel-zone-banner"><span class="parcel-zone-date">${escapeHtml(
      data.report_date || ""
    )}</span><span class="parcel-zone-title">${escapeHtml(data.title_name || "")}</span></div>`;
    const rowIdx = parcelZoneSummaryRowIndices();
    let html = "";
    for (let z = 1; z <= 8; z++) {
      if (!findBlockSideForZone(data.blocks, z)) continue;
      html += `<div class="parcel-zone-stack">`;
      html += `<div class="parcel-zone-stack-title">Zone ${z}</div>`;
      html +=
        "<table class='parcel-zone-summary parcel-zone-summary-stacked'><thead><tr>" +
        "<th class='pz-w'>Weight</th>" +
        `<th class='pz-pri'>Priority Z${z}</th>` +
        `<th class='pz-cnt'>Count Z${z}</th>` +
        "<th class='pz-line'>Line total</th>" +
        "</tr></thead><tbody>";
      for (const ri of rowIdx) {
        const wl = data.blocks[0].rows[ri].weight_label;
        const { priority, count } = zoneCellForRow(data.blocks, z, ri);
        const line = zoneLineRetailTotal(priority, count, hide);
        html += `<tr><td class='pz-w'>${escapeHtml(wl)}</td>`;
        html += `<td class='pz-pri num'>${hide ? "\u2014" : fmtCellMoney(priority)}</td>`;
        html += `<td class='pz-cnt num'>${fmtInt(count || 0)}</td>`;
        html += `<td class='pz-line num'>${
          hide || line == null ? "\u2014" : fmtMoney(line)
        }</td></tr>`;
      }
      html += "</tbody></table></div>";
    }
    container.innerHTML = html;

    const tp = data.total_pieces != null ? fmtInt(data.total_pieces) : "\u2014";
    if (hide) {
      footerEl.innerHTML = `<span class="parcel-zone-total-pieces">Total pieces: <strong>${tp}</strong></span>`;
    } else {
      const tc = data.total_cost != null ? fmtMoney(data.total_cost) : "\u2014";
      footerEl.innerHTML = `<span class="parcel-zone-total-pieces">Total pieces: <strong>${tp}</strong></span>
        <span class="parcel-zone-total-cost">Total cost: <strong>${tc}</strong></span>`;
    }
  }

  function buildParcelTable(container, data) {
    const hide = $("hideCosts").checked;
    const lbKeys = [];
    for (let i = 1; i <= 10; i++) lbKeys.push(`lb_${i}`);
    lbKeys.push("lb_10plus");
    const headerKeys = [
      "date",
      "parent_name",
      "child_name",
      "mail_class",
      "zone",
      ...lbKeys,
      "total_qty",
    ];
    if (!hide) headerKeys.push("total_retail");
    const headers = [
      "Date",
      "Parent Name",
      "Child Name",
      "Mail Class",
      "Zone",
      ...Array.from({ length: 10 }, (_, i) => `${i + 1} lb`),
      "10+ lb",
      "Total Qty",
    ];
    if (!hide) headers.push("Retail Cost");

    if (!headerKeys.includes(state.sortParcel.key)) state.sortParcel.key = "date";

    const rows = sortRows(data.rows || [], state.sortParcel.key, state.sortParcel.dir);

    const nParcSticky = PARCEL_STICKY_WIDTHS.length;
    let html = "<table class='data-table'><thead><tr>";
    headers.forEach((h, i) => {
      const k = headerKeys[i];
      const cl = i < nParcSticky ? "sticky-col" : "";
      const sortClass =
        state.sortParcel.key === k ? (state.sortParcel.dir === 1 ? "sort-asc" : "sort-desc") : "";
      const st =
        i < nParcSticky
          ? ` style="left:${stickyLeft(i, PARCEL_STICKY_WIDTHS)}px;min-width:${PARCEL_STICKY_WIDTHS[i]}px;width:${PARCEL_STICKY_WIDTHS[i]}px"`
          : i >= nParcSticky && i < headers.length - (hide ? 1 : 2)
            ? " style='min-width:55px;width:55px'"
            : "";
      html += `<th class="${cl} ${sortClass}" data-key="${k}"${st}>${h}</th>`;
    });
    html += "</tr></thead><tbody>";

    if (!rows.length) {
      html += `<tr><td colspan="${headers.length}" class="empty-state">No data found for the selected date range and filters.</td></tr>`;
    } else {
      rows.forEach((row) => {
        html += "<tr>";
        headerKeys.forEach((k, i) => {
          const v = row[k];
          const isLb = k.startsWith("lb_");
          const n = Number(v) || 0;
          const muted = isLb && n === 0 ? " muted" : "";
          const cellClass = i < nParcSticky ? "sticky-col" : "";
          const st =
            i < nParcSticky
              ? ` style="left:${stickyLeft(i, PARCEL_STICKY_WIDTHS)}px;min-width:${PARCEL_STICKY_WIDTHS[i]}px;width:${PARCEL_STICKY_WIDTHS[i]}px"`
              : "";
          let inner;
          if (k === "total_retail") inner = fmtMoney(v);
          else if (k === "total_qty") inner = `<strong>${fmtInt(n)}</strong>`;
          else if (isLb || typeof v === "number") inner = fmtInt(Number(v) || 0);
          else inner = v != null ? String(v) : "";
          html += `<td class="${cellClass} num${muted}"${st}>${inner}</td>`;
        });
        html += "</tr>";
      });
    }
    html += "</tbody></table>";
    container.innerHTML = html;

    container.querySelectorAll("thead th[data-key]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.getAttribute("data-key");
        if (state.sortParcel.key === key) state.sortParcel.dir *= -1;
        else {
          state.sortParcel.key = key;
          state.sortParcel.dir = 1;
        }
        buildParcelTable(container, data);
      });
    });
  }

  function accountRowLabel(row) {
    return `${row.customer_name} (${row.customer_number})`;
  }

  function filterAccountRows(rows, qLower, qDigits) {
    if (!qLower && !qDigits) return rows;
    return rows.filter((row) => {
      const text = accountRowLabel(row).toLowerCase();
      if (qLower && text.includes(qLower)) return true;
      if (qDigits && String(row.customer_number).includes(qDigits)) return true;
      return false;
    });
  }

  function populateAccountSelect(filterRaw) {
    const sel = $("selectAccount");
    const prev = sel.value;
    const prevKind = sel.selectedOptions[0]?.getAttribute("data-kind") || "";
    const qLower = String(filterRaw || "")
      .trim()
      .toLowerCase();
    const qDigits = qLower.replace(/\D/g, "");

    sel.innerHTML = "";
    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.textContent = "All Accounts";
    sel.appendChild(allOpt);

    const filterActive = qLower.length > 0 || qDigits.length > 0;
    const order = ["parent", "child", "standalone"];
    const labels = {
      parent: "Parent companies",
      child: "Child accounts",
      standalone: "Standalone (no parent company)",
    };

    if (filterActive) {
      const merged = order.flatMap((k) => state.accountsByKind[k] || []);
      const filtered = filterAccountRows(merged, qLower, qDigits);
      filtered.sort((a, b) =>
        a.customer_name.localeCompare(b.customer_name, undefined, { sensitivity: "base" })
      );
      filtered.forEach((row) => {
        const opt = document.createElement("option");
        opt.value = String(row.customer_number);
        opt.textContent = accountRowLabel(row);
        opt.setAttribute("data-kind", row.kind);
        sel.appendChild(opt);
      });
    } else {
      for (const kind of order) {
        const rows = state.accountsByKind[kind] || [];
        if (!rows.length) continue;
        const og = document.createElement("optgroup");
        og.label = labels[kind];
        rows.forEach((row) => {
          const opt = document.createElement("option");
          opt.value = String(row.customer_number);
          opt.textContent = accountRowLabel(row);
          opt.setAttribute("data-kind", row.kind);
          og.appendChild(opt);
        });
        sel.appendChild(og);
      }
    }

    const pick = (value, kind) => {
      const opts = Array.from(sel.options);
      const hit = opts.find(
        (o) => o.value === value && (!kind || o.getAttribute("data-kind") === kind)
      );
      if (hit) {
        sel.value = hit.value;
        return true;
      }
      return false;
    };
    if (prev === "") sel.value = "";
    else if (!pick(prev, prevKind) && qDigits) pick(qDigits, "");
  }

  async function loadCustomers() {
    const r = await fetch("/api/customers/list");
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Failed to load customers");
    state.accountsByKind = { parent: [], child: [], standalone: [] };
    data.forEach((row) => {
      const k = row.kind;
      if (state.accountsByKind[k]) state.accountsByKind[k].push(row);
    });
    populateAccountSelect($("searchCustomer").value);
  }

  async function loadAll() {
    $("errorBanner").classList.add("hidden");
    state.selectedPostage = null;
    if ($("btnEditPostage")) $("btnEditPostage").disabled = true;
    const q = queryParams();
    $("spinPostage").classList.remove("hidden");
    $("spinParcels").classList.remove("hidden");
    try {
      const [rp, rv] = await Promise.all([
        fetch("/api/postage?" + q.toString()),
        fetch("/api/parcels?" + q.toString()),
      ]);
      const jp = await rp.json();
      const jv = await rv.json();
      if (!rp.ok) throw new Error(jp.error || "Postage request failed");
      if (!rv.ok) throw new Error(jv.error || "Parcels request failed");
      state.postageData = jp;
      state.parcelData = jv;
      renderSummaryBar($("summaryPostage"), jp);
      renderParcelSummaryBar($("summaryParcels"), jv);
      buildPostageTableFixed($("tablePostage"), jp);
      buildParcelTable($("tableParcels"), jv);
      const rz = await fetch("/api/parcels/zone-summary?" + q.toString());
      const jz = await rz.json();
      if (!rz.ok) {
        state.parcelZoneSummary = null;
        buildParcelZoneSummary(
          $("parcelZoneBanner"),
          $("parcelZoneSummary"),
          $("parcelZoneFooter"),
          null
        );
      } else {
        state.parcelZoneSummary = jz;
        buildParcelZoneSummary(
          $("parcelZoneBanner"),
          $("parcelZoneSummary"),
          $("parcelZoneFooter"),
          jz
        );
      }
    } catch (e) {
      $("errorBanner").textContent = String(e.message || e);
      $("errorBanner").classList.remove("hidden");
      state.parcelZoneSummary = null;
      buildParcelZoneSummary(
        $("parcelZoneBanner"),
        $("parcelZoneSummary"),
        $("parcelZoneFooter"),
        null
      );
    } finally {
      $("spinPostage").classList.add("hidden");
      $("spinParcels").classList.add("hidden");
      if ($("btnExportFlatsSavings")) $("btnExportFlatsSavings").hidden = $("hideCosts").checked;
    }
  }

  async function refreshWatcher() {
    try {
      const r = await fetch("/api/watcher/status");
      const j = await r.json();
      const dot = $("watcherDot");
      const label = $("watcherLabel");
      if (j.active) {
        dot.classList.add("active");
        label.textContent = "Watcher Active";
      } else {
        dot.classList.remove("active");
        label.textContent = "Watcher Inactive";
      }
      if (j.last_scan) label.textContent += " · Last scan: " + j.last_scan;
    } catch {
      $("watcherDot").classList.remove("active");
      $("watcherLabel").textContent = "Watcher status unavailable";
    }
  }

  function refreshSummaryCustomerViews() {
    const j = state.lastSummary;
    if (!j) return;

    const onlyPost = $("sumCustUnmatchedOnly").checked;
    const pc = j.postage.by_customer || [];
    const visPost = onlyPost ? pc.filter((row) => row.unmatched === true) : pc;
    let h =
      "<table class='data-table'><thead><tr><th>Customer #</th><th>Customer Name</th><th class='num'>Pieces</th><th class='num'>Cost</th></tr></thead><tbody>";
    visPost.forEach((row) => {
      const trc = row.unmatched ? " class='row-unmatched'" : "";
      h += `<tr${trc}><td>${fmtSummaryAccountNum(row.customer_number)}</td><td>${escapeHtml(
        row.customer_name
      )}</td><td class='num'>${fmtInt(row.pieces)}</td><td class='num'>${fmtMoney(row.cost)}</td></tr>`;
    });
    h += "</tbody></table>";
    $("tableSumCust").innerHTML = h;
    const postP = visPost.reduce((s, x) => s + x.pieces, 0);
    const postC = visPost.reduce((s, x) => s + x.cost, 0);
    $("sumCustFooter").textContent = `Total: ${fmtInt(postP)} pieces, ${fmtMoney(postC)}`;

    const onlyPar = $("sumParcelUnmatchedOnly").checked;
    const pCust = (j.parcels && j.parcels.by_customer) || [];
    const visPar = onlyPar ? pCust.filter((row) => row.unmatched === true) : pCust;
    h =
      "<table class='data-table'><thead><tr><th>Customer #</th><th>Customer Name</th><th class='num'>Pieces</th><th class='num'>Billed</th></tr></thead><tbody>";
    visPar.forEach((row) => {
      const trc = row.unmatched ? " class='row-unmatched'" : "";
      h += `<tr${trc}><td>${fmtSummaryAccountNum(row.customer_number)}</td><td>${escapeHtml(
        row.customer_name
      )}</td><td class='num'>${fmtInt(row.pieces)}</td><td class='num'>${fmtMoney(row.cost)}</td></tr>`;
    });
    h += "</tbody></table>";
    $("tableSumParcelCust").innerHTML = h;
    const parP = visPar.reduce((s, x) => s + x.pieces, 0);
    const parB = visPar.reduce((s, x) => s + x.cost, 0);
    $("sumParcelCustFooter").textContent = `Total: ${fmtInt(parP)} pieces, ${fmtMoney(parB)}`;
  }

  async function runSummary() {
    const p = new URLSearchParams();
    p.set("start_date", $("sumStart").value);
    p.set("end_date", $("sumEnd").value);
    $("errorBanner").classList.add("hidden");
    try {
      const r = await fetch("/api/summary?" + p.toString());
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Summary failed");
      state.lastSummary = j;

      const pcl = j.postage.by_class || [];
      let h =
        "<table class='data-table'><thead><tr><th>Class</th><th class='num'>Pieces</th><th class='num'>Cost</th></tr></thead><tbody>";
      pcl.forEach((row) => {
        h += `<tr><td>${escapeHtml(row.mail_class)}</td><td class='num'>${fmtInt(
          row.pieces
        )}</td><td class='num'>${fmtMoney(row.cost)}</td></tr>`;
      });
      h += "</tbody></table>";
      $("tableSumClass").innerHTML = h;
      $("sumClassFooter").textContent = `Total: ${fmtInt(
        pcl.reduce((s, x) => s + x.pieces, 0)
      )} pieces, ${fmtMoney(pcl.reduce((s, x) => s + x.cost, 0))}`;

      refreshSummaryCustomerViews();

      const par = j.parcels || {};
      const pClass = par.by_class || [];

      h =
        "<table class='data-table'><thead><tr><th>Class</th><th class='num'>Pieces</th><th class='num'>Billed</th></tr></thead><tbody>";
      pClass.forEach((row) => {
        h += `<tr><td>${escapeHtml(row.mail_class)}</td><td class='num'>${fmtInt(
          row.pieces
        )}</td><td class='num'>${fmtMoney(row.cost)}</td></tr>`;
      });
      h += "</tbody></table>";
      $("tableSumParcelClass").innerHTML = h;
      $("sumParcelClassFooter").textContent = `Total: ${fmtInt(
        pClass.reduce((s, x) => s + x.pieces, 0)
      )} pieces, ${fmtMoney(pClass.reduce((s, x) => s + x.cost, 0))}`;

      const im = j.imports || [];
      h =
        "<table class='data-table'><thead><tr><th>File Name</th><th>Date</th><th class='num'>Rows</th><th>Imported At</th><th>Type</th></tr></thead><tbody>";
      im.forEach((row) => {
        const cls = row.type === "Billing" ? "row-billing" : "row-postage";
        h += `<tr class="${cls}"><td>${escapeHtml(row.file_name)}</td><td>${
          row.file_date || ""
        }</td><td class='num'>${fmtInt(row.row_count)}</td><td>${escapeHtml(
          String(row.imported_at)
        )}</td><td>${escapeHtml(row.type)}</td></tr>`;
      });
      h += "</tbody></table>";
      $("tableImports").innerHTML = h || "<p class='empty-state'>No imports in range.</p>";
    } catch (e) {
      $("errorBanner").textContent = String(e.message || e);
      $("errorBanner").classList.remove("hidden");
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setTab(name) {
    document.querySelectorAll(".tab").forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    $("tabPostage").classList.toggle("hidden", name !== "postage");
    $("tabParcels").classList.toggle("hidden", name !== "parcels");
    $("tabSummary").classList.toggle("hidden", name !== "summary");
  }

  function setPostageEditOpen(open) {
    const overlay = $("postageEditOverlay");
    if (!overlay) return;
    overlay.classList.toggle("hidden", !open);
    overlay.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open) {
      $("postageEditPreview")?.classList.add("hidden");
      if ($("btnApplyPostageEdit")) $("btnApplyPostageEdit").disabled = true;
      if ($("postageEditWeights")) $("postageEditWeights").innerHTML = "";
      if ($("postageEditContext")) $("postageEditContext").textContent = "";
      if ($("postageEditAccount")) $("postageEditAccount").value = "";
      if ($("postageEditReason")) $("postageEditReason").value = "";
    }
  }

  function selectedPostageRowFromState() {
    const sel = state.selectedPostage;
    if (!sel || !state.postageData) return null;
    const rows = state.postageData.rows || [];
    return rows.find(
      (r) =>
        r.date === sel.date &&
        String(r.child_number) === String(sel.child_number) &&
        r.mail_class === sel.mail_class
    );
  }

  function postageEditContextHtml(row) {
    const parts = [
      `<strong>Date:</strong> ${escapeHtml(row.date || "")}`,
      `<strong>Child:</strong> ${escapeHtml(row.child_name || "")} (${fmtId(row.child_number)})`,
      `<strong>Class:</strong> ${escapeHtml(row.mail_class || "")}`,
      `<strong>Total Qty:</strong> ${fmtInt(Number(row.total_qty) || 0)}`,
    ];
    if (!$("hideCosts")?.checked && row.total_cost != null) {
      parts.push(`<strong>Total Cost:</strong> ${fmtMoney(row.total_cost)}`);
    }
    return parts.join(" &nbsp;·&nbsp; ");
  }

  function renderWeightEditor(rows) {
    let h =
      "<table class='data-table'><thead><tr><th>Weight (oz)</th><th class='num'>Pieces</th><th class='num'>Total Cost</th></tr></thead><tbody>";
    if (!rows.length) {
      h += "<tr><td colspan='3' class='empty-state'>No underlying rows found.</td></tr>";
    } else {
      rows.forEach((r) => {
        const id = String(r.id);
        const w = r.weight_oz;
        const pieces = Number(r.pieces) || 0;
        const cost = r.total_cost != null ? fmtMoney(r.total_cost) : "\u2014";
        h += `<tr data-id="${escapeHtml(id)}" data-weight="${escapeHtml(String(w))}">
          <td>${escapeHtml(String(w))}</td>
          <td class="num"><input class="edit-piece-input" type="number" min="0" step="1" value="${pieces}" data-piece-id="${escapeHtml(
            id
          )}" /></td>
          <td class="num">${cost}</td>
        </tr>`;
      });
    }
    h += "</tbody></table>";
    return h;
  }

  async function openPostageEdit() {
    const row = selectedPostageRowFromState();
    if (!row) return;
    if (row.date === "Combined" || $("consolidate")?.checked) {
      alert('Disable "Consolidate Date & Mailclass" to edit a specific day.');
      return;
    }
    $("postageEditContext").innerHTML = postageEditContextHtml(row);
    setPostageEditOpen(true);
    $("postageEditPreview")?.classList.add("hidden");
    if ($("btnApplyPostageEdit")) $("btnApplyPostageEdit").disabled = true;
    try {
      const p = new URLSearchParams();
      p.set("file_date", row.date);
      p.set("account_code", String(row.child_number));
      p.set("mail_class", String(row.mail_class || ""));
      const r = await fetch("/api/postage/row-details?" + p.toString());
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Failed to load row details");
      const details = j.rows || [];
      $("postageEditWeights").innerHTML = renderWeightEditor(details);
      const acct = $("postageEditAccount");
      if (acct) acct.value = String(row.child_number);
    } catch (e) {
      alert(String(e.message || e));
      setPostageEditOpen(false);
    }
  }

  function readPiecesByIdFromEditor() {
    const out = {};
    document.querySelectorAll("#postageEditWeights input[data-piece-id]").forEach((inp) => {
      const id = inp.getAttribute("data-piece-id");
      const v = inp.value;
      if (!id) return;
      const n = parseInt(String(v || "0"), 10);
      out[id] = Number.isNaN(n) ? 0 : Math.max(0, n);
    });
    return out;
  }

  function renderPostageEditPreview(preview) {
    const s = preview.summary || {};
    const from = state.selectedPostage?.child_number;
    const to = $("postageEditAccount")?.value?.trim();
    const mc = state.selectedPostage?.mail_class;
    const dt = state.selectedPostage?.date;
    const msg = `Notice: You are about to update ${s.source_rows || 0} underlying rows for ${dt} (${mc}) from account ${from} → ${to}. Updated: ${s.updated || 0}. Merged: ${s.merged || 0}.`;
    return escapeHtml(msg);
  }

  $("btnPreviewPostageEdit")?.addEventListener("click", async () => {
    const row = selectedPostageRowFromState();
    if (!row) return;
    const toRaw = $("postageEditAccount")?.value?.trim() || "";
    const to = parseInt(toRaw, 10);
    if (Number.isNaN(to)) {
      alert("Enter a valid new account number.");
      return;
    }
    const pieces_by_id = readPiecesByIdFromEditor();
    const payload = {
      file_date: row.date,
      from_account_code: row.child_number,
      to_account_code: to,
      mail_class: row.mail_class,
      pieces_by_id,
    };
    try {
      const r = await fetch("/api/postage/row-preview-update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Preview failed");
      const box = $("postageEditPreview");
      if (box) {
        box.innerHTML = renderPostageEditPreview(j);
        box.classList.remove("hidden");
      }
      if ($("btnApplyPostageEdit")) $("btnApplyPostageEdit").disabled = false;
    } catch (e) {
      alert(String(e.message || e));
    }
  });

  $("btnApplyPostageEdit")?.addEventListener("click", async () => {
    const row = selectedPostageRowFromState();
    if (!row) return;
    const toRaw = $("postageEditAccount")?.value?.trim() || "";
    const to = parseInt(toRaw, 10);
    if (Number.isNaN(to)) {
      alert("Enter a valid new account number.");
      return;
    }
    const pieces_by_id = readPiecesByIdFromEditor();
    const payload = {
      file_date: row.date,
      from_account_code: row.child_number,
      to_account_code: to,
      mail_class: row.mail_class,
      pieces_by_id,
      reason: $("postageEditReason")?.value || "",
    };
    try {
      const btn = $("btnApplyPostageEdit");
      if (btn) btn.disabled = true;
      const r = await fetch("/api/postage/row-apply-update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Apply failed");
      setPostageEditOpen(false);
      await loadAll();
      alert(`Update applied. Updated: ${j.summary?.updated || 0}. Merged: ${j.summary?.merged || 0}.`);
    } catch (e) {
      alert(String(e.message || e));
      if ($("btnApplyPostageEdit")) $("btnApplyPostageEdit").disabled = false;
    }
  });

  $("btnEditPostage")?.addEventListener("click", () => openPostageEdit());
  $("btnClosePostageEdit")?.addEventListener("click", () => setPostageEditOpen(false));
  $("postageEditOverlay")?.addEventListener("click", (ev) => {
    if (ev.target && ev.target.id === "postageEditOverlay") setPostageEditOpen(false);
  });

  function filenameFromContentDisposition(cd) {
    if (!cd) return null;
    const mStar = cd.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
    if (mStar) return decodeURIComponent(mStar[1].replace(/["']/g, "").trim());
    const m = cd.match(/filename\s*=\s*("?)([^";\n]+)\1/i);
    return m ? m[2].trim() : null;
  }

  function setExportLoading(btn, loading) {
    btn.disabled = loading;
    const sp = btn.querySelector(".export-btn-spinner");
    const lb = btn.querySelector(".export-btn-label");
    if (sp) sp.classList.toggle("hidden", !loading);
    if (lb) lb.classList.toggle("muted", loading);
  }

  $("btnLoad").addEventListener("click", () => loadAll());
  function refreshPostageFromDiscount() {
    if (state.postageData) {
      renderSummaryBar($("summaryPostage"), state.postageData);
      buildPostageTableFixed($("tablePostage"), state.postageData);
    }
  }

  /** Rebuild postage/parcel views when "Hide Costs" changes (no reload required). */
  function applyHideCostsToUi() {
    const hideBtn = $("btnExportFlatsSavings");
    if (hideBtn) hideBtn.hidden = $("hideCosts").checked;
    if (state.postageData) {
      renderSummaryBar($("summaryPostage"), state.postageData);
      buildPostageTableFixed($("tablePostage"), state.postageData);
    }
    if (state.parcelData) {
      renderParcelSummaryBar($("summaryParcels"), state.parcelData);
      buildParcelTable($("tableParcels"), state.parcelData);
    }
    if (state.parcelZoneSummary) {
      buildParcelZoneSummary(
        $("parcelZoneBanner"),
        $("parcelZoneSummary"),
        $("parcelZoneFooter"),
        state.parcelZoneSummary
      );
    }
  }
  $("invoiceDiscount")?.addEventListener("input", refreshPostageFromDiscount);
  $("invoiceDiscount")?.addEventListener("change", refreshPostageFromDiscount);
  $("removeZeros")?.addEventListener("change", () => loadAll());
  $("hideCosts")?.addEventListener("change", applyHideCostsToUi);
  $("btnScan").addEventListener("click", async () => {
    try {
      await fetch("/api/scan", { method: "POST" });
      await refreshWatcher();
      await loadAll();
    } catch (e) {
      $("errorBanner").textContent = String(e.message || e);
      $("errorBanner").classList.remove("hidden");
    }
  });

  $("btnExportPostage").addEventListener("click", async () => {
    const sel = $("selectAccount");
    const opt = sel.selectedOptions[0];
    if (!opt || !opt.value || opt.getAttribute("data-kind") !== "parent") {
      alert(
        "Please select a parent company (under Parent companies) to export the postage invoice."
      );
      return;
    }
    const q = queryParams();
    q.set("discount", String(parseFlatsDiscount()));
    const btn = $("btnExportPostage");
    setExportLoading(btn, true);
    try {
      const r = await fetch("/api/export/postage-invoice?" + q.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") || "postage_invoice.xlsx";
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportFlatsSavings").addEventListener("click", async () => {
    if (!state.postageData || !(state.postageData.rows || []).length) {
      alert("Load postage data first (no rows to export).");
      return;
    }
    const btn = $("btnExportFlatsSavings");
    setExportLoading(btn, true);
    try {
      const p = queryParams();
      p.set("sort_key", state.sortPostage.key || "date");
      p.set("sort_dir", String(state.sortPostage.dir || 1));
      const r = await fetch("/api/export/flats-grid-xlsx?" + p.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const endLabel = fmtFilenameDate($("endDate").value) || "end";
      const acct = selectedAccountRow();
      const acctLabel = acct
        ? `${acct.customer_name} (${fmtId(acct.customer_number)})`
        : "All Accounts";
      const name = safeFilename(`${acctLabel} Flats Report ${endLabel}`) + ".xlsx";
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportParcelsCsv").addEventListener("click", () => {
    const btn = $("btnExportParcelsCsv");
    setExportLoading(btn, true);
    try {
      downloadParcelsCsv();
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportParcelsXlsx").addEventListener("click", async () => {
    const q = queryParams();
    q.set("hide_costs", "false");
    const btn = $("btnExportParcelsXlsx");
    setExportLoading(btn, true);
    try {
      const r = await fetch("/api/export/parcel-counts-xlsx?" + q.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
        "parcel_counts.xlsx";
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportConsolidatedVolumes").addEventListener("click", async () => {
    const btn = $("btnExportConsolidatedVolumes");
    setExportLoading(btn, true);
    try {
      await downloadConsolidatedVolumesXlsx();
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportParcel").addEventListener("click", async () => {
    const q = queryParams();
    const btn = $("btnExportParcel");
    setExportLoading(btn, true);
    try {
      const r = await fetch("/api/export/parcel-report?" + q.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") || "parcel_report.xlsx";
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportParcelZoneSummary").addEventListener("click", async () => {
    const q = queryParams();
    const btn = $("btnExportParcelZoneSummary");
    setExportLoading(btn, true);
    try {
      const r = await fetch("/api/export/parcel-zone-summary?" + q.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
        "parcel_invoice.xlsx";
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => setTab(t.dataset.tab));
  });

  let searchDebounce;
  $("searchCustomer").addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => populateAccountSelect($("searchCustomer").value), 150);
  });
  $("searchCustomer").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") loadAll();
  });

  $("btnRunSummary").addEventListener("click", () => runSummary());
  $("sumCustUnmatchedOnly").addEventListener("change", () => refreshSummaryCustomerViews());
  $("sumParcelUnmatchedOnly").addEventListener("change", () => refreshSummaryCustomerViews());

  const d = defaultDates();
  $("startDate").value = d.start;
  $("endDate").value = d.end;
  $("sumStart").value = d.start;
  $("sumEnd").value = d.end;

  loadCustomers()
    .then(() => loadAll())
    .catch((e) => {
      $("errorBanner").textContent = String(e.message || e);
      $("errorBanner").classList.remove("hidden");
    });

  setInterval(refreshWatcher, 30000);
  refreshWatcher();
})();
