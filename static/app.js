(function () {
  const $ = (id) => document.getElementById(id);

  const state = {
    postageData: null,
    parcelData: null,
    parcelZoneSummary: null,
    /** Last `/api/summary` JSON for Import Summary customer tables + checkbox filters */
    lastSummary: null,
    /** Last `/api/profit/flats` JSON payload, keyed by query string */
    lastProfitFlats: { key: null, payload: null },
    /** Selected Postage row key for edit workflow */
    selectedPostage: null,
    /** Selected Parcels row key for edit workflow */
    selectedParcel: null,
    sortPostage: { key: "date", dir: 1 },
    sortParcel: { key: "date", dir: 1 },
    consolidatedDefaultsTouched: { removeZeros: false },
    /** @type {{ parent: { customer_number: number; customer_name: string; kind: string }[]; child: ...[]; standalone: ...[] }} */
    accountsByKind: { parent: [], child: [], standalone: [] },
    /** Customer numbers for combined Profit tab scope (optional; empty = toolbar account). */
    profitAccountIds: [],
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

  function parseParcelDiscount() {
    const discRaw = ($("parcelDiscount") && $("parcelDiscount").value) || "";
    let discount = discRaw.trim() === "" ? 0.25 : parseFloat(discRaw);
    if (Number.isNaN(discount)) discount = 0.25;
    return discount;
  }

  /** Flats discount to EFD (reseller) for profit report export (Summary B8). */
  function parseResellerDiscount() {
    const discRaw = ($("resellerDiscount") && $("resellerDiscount").value) || "";
    let discount = discRaw.trim() === "" ? 0.23 : parseFloat(discRaw);
    if (Number.isNaN(discount)) discount = 0.23;
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
    // Default to previous business day: yesterday, except Monday -> previous Friday.
    // Use local time at noon to avoid DST/offset boundary issues.
    const d = new Date();
    d.setHours(12, 0, 0, 0);
    const dayOfWeek = d.getDay(); // 0=Sun ... 1=Mon ... 6=Sat
    const deltaDays = dayOfWeek === 1 ? 3 : 1;
    d.setDate(d.getDate() - deltaDays);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const iso = `${y}-${m}-${day}`;
    return { start: iso, end: iso };
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
    p.set("kc_presort", $("kcPresort") && $("kcPresort").checked ? "true" : "false");
    p.set("efd", $("efd") && $("efd").checked ? "true" : "false");
    p.set(
      "allocate_presort_rejects",
      $("allocatePresortRejects") && $("allocatePresortRejects").checked ? "true" : "false",
    );
    p.set("consolidate", $("consolidate").checked ? "true" : "false");
    p.set("remove_zeros", $("removeZeros").checked ? "true" : "false");
    p.set("hide_costs", $("hideCosts").checked ? "true" : "false");
    p.set("hide_savings", "false");
    return p;
  }

  function parseProfitParcelFee() {
    const raw = ($("parcelFee") && $("parcelFee").value) || "";
    let v = raw.trim() === "" ? 1.25 : parseFloat(raw);
    if (Number.isNaN(v) || v < 0) v = 1.25;
    return v;
  }

  function parseEfdParcelFee() {
    const raw = ($("efdParcelFee") && $("efdParcelFee").value) || "";
    let v = raw.trim() === "" ? 1.25 : parseFloat(raw);
    if (Number.isNaN(v) || v < 0) v = 1.25;
    return v;
  }

  /** Keep in sync with ``db.MAX_PROFIT_ACCOUNT_IDS`` (profit_accounts / profit_account_ids cap). */
  const PROFIT_ACCOUNT_IDS_MAX = 2000;
  const PROFIT_IDS_POST_MIN_COUNT = 80;
  const PROFIT_IDS_POST_MIN_CSV_CHARS = 2048;

  function sortedProfitAccountIds() {
    return [...new Set((state.profitAccountIds || []).map((x) => Math.trunc(Number(x))))]
      .filter((n) => Number.isFinite(n) && !Number.isNaN(n))
      .sort((a, b) => a - b);
  }

  function profitAccountsUsePost() {
    const ids = sortedProfitAccountIds();
    if (ids.length === 0) return false;
    if (ids.length > PROFIT_IDS_POST_MIN_COUNT) return true;
    const csv = ids.join(",");
    if (csv.length > PROFIT_IDS_POST_MIN_CSV_CHARS) return true;
    return false;
  }

  function profitPostJsonBody(extra) {
    return Object.assign(
      {
        start_date: $("startDate").value,
        end_date: $("endDate").value,
        show_parents: $("showParents").checked,
        show_main: $("showMain").checked,
        profit_account_ids: sortedProfitAccountIds(),
        parcel_fee: parseProfitParcelFee(),
        efd_parcel_fee: parseEfdParcelFee(),
      },
      extra || {}
    );
  }

  /**
   * Query string for Profit tab APIs and profit export: dates, show flags, optional
   * ``profit_accounts`` CSV, ``parcel_fee``, and ``efd_parcel_fee``; falls back to toolbar account when chips are empty.
   */
  function profitQueryParams() {
    const base = queryParams();
    const p = new URLSearchParams();
    for (const [k, v] of base.entries()) {
      if (k === "parent_number" || k === "customer_number") continue;
      p.append(k, v);
    }
    const ids = (state.profitAccountIds || [])
      .map((x) => Math.trunc(Number(x)))
      .filter((n) => Number.isFinite(n) && !Number.isNaN(n));
    if (ids.length > 0) {
      const sorted = [...new Set(ids)].sort((a, b) => a - b);
      p.set("profit_accounts", sorted.join(","));
    } else {
      if (base.has("parent_number")) p.set("parent_number", base.get("parent_number"));
      if (base.has("customer_number")) p.set("customer_number", base.get("customer_number"));
    }
    p.set("parcel_fee", String(parseProfitParcelFee()));
    p.set("efd_parcel_fee", String(parseEfdParcelFee()));
    return p;
  }

  function invalidateProfitCache() {
    state.lastProfitFlats = { key: null, payload: null };
  }

  function profitTabIsActive() {
    const t = document.querySelector('.tab[data-tab="profitFlats"]');
    return !!(t && t.classList.contains("active"));
  }

  function onProfitScopeChanged() {
    invalidateProfitCache();
    if (profitTabIsActive()) {
      Promise.all([loadProfitFlats(), loadProfitParcels()]).catch(() => {});
    }
  }

  function accountRowByCustomerNumber(customerNumber, kind) {
    const n = Math.trunc(Number(customerNumber));
    if (Number.isNaN(n)) return null;
    const buckets = kind ? [kind] : ["parent", "child", "standalone"];
    for (const bk of buckets) {
      const row = (state.accountsByKind[bk] || []).find((r) => Math.trunc(Number(r.customer_number)) === n);
      if (row) return { ...row, kind: row.kind || bk };
    }
    return null;
  }

  /**
   * Customer numbers to add for profit scope: a parent row expands to that parent # plus every child #.
   */
  function profitAccountIdsForRow(row) {
    if (!row || row.customer_number == null) return [];
    const n = Math.trunc(Number(row.customer_number));
    if (Number.isNaN(n)) return [];
    const kind = row.kind || "";
    if (kind !== "parent") return [n];
    const children = state.accountsByKind.child || [];
    const childIds = children
      .filter((c) => c.parent_number != null && Math.trunc(Number(c.parent_number)) === n)
      .map((c) => Math.trunc(Number(c.customer_number)))
      .filter((x) => !Number.isNaN(x));
    return [...new Set([n, ...childIds])].sort((a, b) => a - b);
  }

  function mergeProfitAccountIds(ids) {
    for (const id of ids) {
      const m = Math.trunc(Number(id));
      if (Number.isNaN(m)) continue;
      if (!state.profitAccountIds.includes(m)) state.profitAccountIds.push(m);
    }
    renderProfitAccountChips();
    onProfitScopeChanged();
  }

  function renderProfitAccountChips() {
    const box = $("profitAccountChips");
    if (!box) return;
    const sorted = [...new Set((state.profitAccountIds || []).map((x) => Math.trunc(Number(x))))]
      .filter((n) => Number.isFinite(n) && !Number.isNaN(n))
      .sort((a, b) => a - b);
    state.profitAccountIds = sorted;
    const capWarn = $("profitScopeCapWarning");
    if (capWarn) {
      if (sorted.length > PROFIT_ACCOUNT_IDS_MAX) {
        capWarn.textContent = `Too many accounts selected (${sorted.length}). Maximum is ${PROFIT_ACCOUNT_IDS_MAX} for profit load and export. Remove some accounts.`;
        capWarn.classList.remove("hidden");
      } else if (sorted.length === PROFIT_ACCOUNT_IDS_MAX) {
        capWarn.textContent = `You are at the maximum of ${PROFIT_ACCOUNT_IDS_MAX} accounts for profit scope.`;
        capWarn.classList.remove("hidden");
      } else {
        capWarn.textContent = "";
        capWarn.classList.add("hidden");
      }
    }
    let h = "";
    if (!sorted.length) {
      h = `<span class="profit-chip-empty">Using toolbar / search account scope.</span>`;
    } else {
      for (const id of sorted) {
        h += `<span class="profit-chip"><span class="profit-chip-label">${escapeHtml(String(id))}</span>`;
        h += `<button type="button" class="profit-chip-remove" data-id="${id}" aria-label="Remove account ${id}">&times;</button></span>`;
      }
    }
    box.innerHTML = h;
    box.querySelectorAll(".profit-chip-remove").forEach((btn) => {
      btn.addEventListener("click", () => {
        const n = parseInt(btn.getAttribute("data-id"), 10);
        if (Number.isNaN(n)) return;
        state.profitAccountIds = state.profitAccountIds.filter((x) => Math.trunc(x) !== n);
        renderProfitAccountChips();
        onProfitScopeChanged();
      });
    });
  }

  /** Same grouping / search filter as ``#selectAccount`` (see ``populateAccountSelect``). */
  function populateProfitAccountPick(filterRaw) {
    const sel = $("profitAccountPick");
    if (!sel) return;
    sel.innerHTML = "";
    const z = document.createElement("option");
    z.value = "";
    z.textContent = "Add account…";
    sel.appendChild(z);

    const qLower = String(filterRaw || "")
      .trim()
      .toLowerCase();
    const qDigits = qLower.replace(/\D/g, "");
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
    const discount = parseFlatsDiscount();
    const parts = [
      `<span>Total Records: ${fmtInt(postage.total_records)}</span>`,
      `<span>Total Pieces: ${fmtInt(postage.total_pieces)}</span>`,
    ];
    if (!hideCosts) {
      const mc = postage.total_metered_cost != null ? postage.total_metered_cost : postage.total_cost;
      if (mc != null) parts.push(`<span>Metered cost: ${fmtMoney(mc)}</span>`);
      if (postage.total_retail_cost != null) {
        parts.push(`<span>Retail cost: ${fmtMoney(postage.total_retail_cost)}</span>`);
      }
    }
    if (!hideCosts && postage.total_pieces != null) {
      const flatsSavings = (Number(postage.total_pieces) || 0) * discount;
      parts.push(`<span>Flats savings: ${fmtMoney(flatsSavings)}</span>`);
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

  function renderPostageAndParcelSummaryBar(el, postage, parcels) {
    if (!postage) {
      el.textContent = "";
      return;
    }
    const hideCosts = $("hideCosts").checked;
    const flatsDisc = parseFlatsDiscount();
    const parcelDisc = parseParcelDiscount();
    const parts = [
      `<span>Total Records: ${fmtInt(postage.total_records)}</span>`,
      `<span>Total Pieces: ${fmtInt(postage.total_pieces)}</span>`,
    ];
    if (!hideCosts) {
      const mc = postage.total_metered_cost != null ? postage.total_metered_cost : postage.total_cost;
      if (mc != null) parts.push(`<span>Metered cost: ${fmtMoney(mc)}</span>`);
      if (postage.total_retail_cost != null) {
        parts.push(`<span>Retail cost: ${fmtMoney(postage.total_retail_cost)}</span>`);
      }
    }
    if (!hideCosts) {
      const flatsSavings = (Number(postage.total_pieces) || 0) * flatsDisc;
      parts.push(`<span>Flats savings: ${fmtMoney(flatsSavings)}</span>`);
      const parcelPieces = parcels ? Number(parcels.total_pieces) || 0 : 0;
      const parcelSavings = parcelPieces * parcelDisc;
      parts.push(`<span>Parcel savings: ${fmtMoney(parcelSavings)}</span>`);
    }
    el.innerHTML = parts.join("");
  }

  function postageStickyWidths(hideCustomerNumbers) {
    return hideCustomerNumbers ? [90, 140, 140, 100] : [90, 140, 140, 70, 100];
  }
  function parcelStickyWidths(hideCustomerNumbers) {
    return hideCustomerNumbers
      ? [90, 140, 140, 120, 55]
      : [90, 140, 140, 70, 120, 55];
  }

  function stickyLeft(index, widths) {
    let x = 0;
    for (let i = 0; i < index; i++) x += widths[i];
    return x;
  }

  /** Trailing non-oz columns after total_qty: 1 or 3 (Metered + Retail when costs shown). */
  function postageTrailingColumnCount(hideCosts) {
    return hideCosts ? 1 : 3;
  }

  /** Shared column layout for Postage grid (no savings). */
  function postageTableColumns(hideCosts, hideCustomerNumbers) {
    const ozKeys = [];
    for (let i = 0; i <= 12; i++) ozKeys.push(`oz_${i}`);
    ozKeys.push("oz_13", "oz_13plus");
    const headerKeys = ["date", "parent_name", "child_name"];
    const headers = ["Date", "Parent Name", "Child Name"];
    if (!hideCustomerNumbers) {
      headerKeys.push("child_number");
      headers.push("Child Number");
    }
    headerKeys.push("mail_class", ...ozKeys, "total_qty");
    headers.push(
      "Class",
      ...Array.from({ length: 13 }, (_, i) => `${i} oz`),
      "13 oz",
      "13+ oz",
      "Total Qty"
    );
    if (!hideCosts) {
      headerKeys.push("metered_cost", "retail_cost");
      headers.push("Metered cost", "Retail cost");
    }
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

  function parcelTableColumns(hideCosts, hideCustomerNumbers) {
    const lbKeys = [];
    for (let i = 1; i <= 10; i++) lbKeys.push(`lb_${i}`);
    lbKeys.push("lb_10plus");
    const headerKeys = ["date", "parent_name", "child_name"];
    const headers = ["Date", "Parent Name", "Child Name"];
    if (!hideCustomerNumbers) {
      headerKeys.push("child_number");
      headers.push("Child Number");
    }
    headerKeys.push("mail_class", "zone", ...lbKeys, "total_qty");
    headers.push(
      "Mail Class",
      "Zone",
      ...Array.from({ length: 10 }, (_, i) => `${i + 1} lb`),
      "10+ lb",
      "Total Qty"
    );
    if (!hideCosts) {
      headerKeys.push("total_retail");
      headers.push("Retail Cost");
    }
    return { headerKeys, headers };
  }

  function parcelCsvColumns(costsHidden, hideCustomerNumbers) {
    const lbKeys = [];
    for (let i = 1; i <= 10; i++) lbKeys.push(`lb_${i}`);
    lbKeys.push("lb_10plus");
    const headerKeys = ["date", "parent_name", "child_name"];
    const headers = ["Date", "Parent Name", "Child Name"];
    if (!hideCustomerNumbers) {
      headerKeys.push("child_number");
      headers.push("Child Number");
    }
    headerKeys.push(...lbKeys, "total_qty");
    headers.push(
      ...Array.from({ length: 10 }, (_, i) => `${i + 1} lb`),
      "10+ lb",
      "Total Qty"
    );
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
    if (k === "child_number") return fmtId(v);
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
    if (!hit) return { priority: null, efd: null, count: 0 };
    const rw = hit.block.rows[ri];
    const c = hit.side === "a" ? rw.zone_a : rw.zone_b;
    return { priority: c.priority, efd: c.efd, count: c.count || 0 };
  }

  function zoneLineRetailTotal(priority, count, hide) {
    if (hide || priority == null || priority === "") return null;
    const p = Number(priority);
    const n = Number(count) || 0;
    if (Number.isNaN(p)) return null;
    return Math.round(p * n * 100) / 100;
  }

  function zoneLineSavingsTotal(priority, discountUnit, count, hide) {
    if (hide || priority == null || priority === "" || discountUnit == null || discountUnit === "")
      return null;
    const p = Number(priority);
    const d = Number(discountUnit);
    const n = Number(count) || 0;
    if (Number.isNaN(p) || Number.isNaN(d)) return null;
    const per = Math.max(0, p - d);
    return Math.round(per * n * 100) / 100;
  }

  /**
   * Lines (including header) for the parcels counts CSV, or null if no rows.
   * @param {object|null} data parcel API payload
   * @param {boolean} includeMoney when true, append Retail Cost column
   */
  function buildParcelsCsvLinesFromData(data, includeMoney, hideCustomerNumbers) {
    if (!data || !(data.rows || []).length) return null;
    const aggregated = aggregateParcelRowsForCsv(data.rows || []);
    const hideNums = hideCustomerNumbers ?? $("hideCustomerNumbers")?.checked ?? true;
    const { headerKeys, headers } = parcelCsvColumns(!includeMoney, hideNums);
    const lines = [headers.map((h) => csvEscape(h)).join(",")];
    for (const row of aggregated) {
      lines.push(headerKeys.map((k) => csvEscape(parcelCsvCell(row, k))).join(","));
    }
    return lines;
  }

  function buildParcelsCsvLines() {
    return buildParcelsCsvLinesFromData(
      state.parcelData,
      !$("hideCosts").checked,
      $("hideCustomerNumbers")?.checked ?? true
    );
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

  function fmtDisplayDate(isoDate) {
    if (!isoDate) return "";
    const d = new Date(String(isoDate).trim() + "T12:00:00");
    if (Number.isNaN(d.getTime())) return String(isoDate).trim();
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function formatMissingDates(isoDates, maxShow = 5) {
    const list = isoDates || [];
    const shown = list.slice(0, maxShow).map(fmtDisplayDate);
    let s = shown.join(", ");
    const rest = list.length - maxShow;
    if (rest > 0) s += (s ? ", " : "") + `and ${rest} more`;
    return s;
  }

  function formatReadinessRangeLabel(start, end, count) {
    if (!count) return "";
    if (start === end) {
      return `${count} business day: ${fmtDisplayDate(start)}`;
    }
    return `${count} business days: ${fmtDisplayDate(start)} – ${fmtDisplayDate(end)}`;
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
    p.set("hide_customer_numbers", $("hideCustomerNumbers")?.checked ? "true" : "false");
    if (!state.consolidatedDefaultsTouched.removeZeros) {
      p.set("remove_zeros", "true");
    }
    // hide_costs comes from queryParams() / "Hide Costs" checkbox only. Do not override:
    // a previous bug forced hide_costs=true whenever the user had never toggled Hide Costs,
    // which hid Summary + sheet money even when Hide Costs was unchecked.
    p.set("account_scope", exportScopeLabel());
    p.set("parcel_discount", String(parseParcelDiscount()));
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
    const hideCustomerNumbers = $("hideCustomerNumbers")?.checked ?? true;
    const stickyWidths = postageStickyWidths(hideCustomerNumbers);
    const { headerKeys, headers } = postageTableColumns(hideCosts, hideCustomerNumbers);
    const trailingCols = postageTrailingColumnCount(hideCosts);
    if (!headerKeys.includes(state.sortPostage.key)) state.sortPostage.key = "date";

    const discount = parseFlatsDiscount();
    const rowsWithSavings = (data.rows || []).map((r) => ({
      ...r,
      savings: (Number(r.total_qty) || 0) * discount,
    }));
    const rows = sortRows(rowsWithSavings, state.sortPostage.key, state.sortPostage.dir);

    const nSticky = stickyWidths.length;
    let html = "<table class='data-table'><thead><tr>";
    headers.forEach((h, i) => {
      const k = headerKeys[i];
      const cl = i < nSticky ? "sticky-col" : "";
      const sortClass =
        state.sortPostage.key === k ? (state.sortPostage.dir === 1 ? "sort-asc" : "sort-desc") : "";
      const st =
        i < nSticky
          ? ` style="left:${stickyLeft(i, stickyWidths)}px;min-width:${stickyWidths[i]}px;width:${stickyWidths[i]}px"`
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
              ? ` style="left:${stickyLeft(i, stickyWidths)}px;min-width:${stickyWidths[i]}px;width:${stickyWidths[i]}px"`
              : "";
          let inner;
          if (k === "metered_cost" || k === "retail_cost")
            inner = v != null && v !== "" ? fmtMoney(v) : "\u2014";
          else if (k === "child_number") inner = fmtId(v);
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
        if (mailClass === "Presort rejects" || mailClass === "Rejects") return;
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
        "<p class='empty-state'>No zone summary for this range (load data or select accounts with parcel billing in zones 1–9).</p>";
      footerEl.textContent = "";
      return;
    }
    const hide = data.hide_costs === true;
    bannerEl.innerHTML = `<div class="parcel-zone-banner"><span class="parcel-zone-date">${escapeHtml(
      data.report_date || ""
    )}</span><span class="parcel-zone-title">${escapeHtml(data.title_name || "")}</span></div>`;
    const rowIdx = parcelZoneSummaryRowIndices();
    let html = "";
    for (let z = 1; z <= 9; z++) {
      if (!findBlockSideForZone(data.blocks, z)) continue;
      html += `<div class="parcel-zone-stack">`;
      html += `<div class="parcel-zone-stack-title">Zone ${z}</div>`;
      html +=
        "<table class='parcel-zone-summary parcel-zone-summary-stacked'><thead><tr>" +
        "<th class='pz-w'>Weight</th>" +
        `<th class='pz-pri'>Retail Z${z}</th>` +
        `<th class='pz-pri'>Discount Z${z}</th>` +
        `<th class='pz-cnt'>Count Z${z}</th>` +
        "<th class='pz-line'>Retail total</th>" +
        "<th class='pz-line'>Savings</th>" +
        "</tr></thead><tbody>";
      for (const ri of rowIdx) {
        const wl = data.blocks[0].rows[ri].weight_label;
        const { priority, efd, count } = zoneCellForRow(data.blocks, z, ri);
        const line = zoneLineRetailTotal(priority, count, hide);
        const sav = zoneLineSavingsTotal(priority, efd, count, hide);
        html += `<tr><td class='pz-w'>${escapeHtml(wl)}</td>`;
        html += `<td class='pz-pri num'>${hide ? "\u2014" : fmtCellMoney(priority)}</td>`;
        html += `<td class='pz-pri num'>${hide ? "\u2014" : fmtCellMoney(efd)}</td>`;
        html += `<td class='pz-cnt num'>${fmtInt(count || 0)}</td>`;
        html += `<td class='pz-line num'>${
          hide || line == null ? "\u2014" : fmtMoney(line)
        }</td></tr>`;
        html += `<td class='pz-line num'>${hide || sav == null ? "\u2014" : fmtMoney(sav)}</td></tr>`;
      }
      html += "</tbody></table></div>";
    }
    container.innerHTML = html;

    const tp = data.total_pieces != null ? fmtInt(data.total_pieces) : "\u2014";
    if (hide) {
      footerEl.innerHTML = `<span class="parcel-zone-total-pieces">Total pieces: <strong>${tp}</strong></span>`;
    } else {
      const tc = data.total_cost != null ? fmtMoney(data.total_cost) : "\u2014";
      const ts = data.total_savings != null ? fmtMoney(data.total_savings) : "\u2014";
      footerEl.innerHTML = `<span class="parcel-zone-total-pieces">Total pieces: <strong>${tp}</strong></span>
        <span class="parcel-zone-total-cost">Total cost: <strong>${tc}</strong></span>
        <span class="parcel-zone-total-cost">Total savings: <strong>${ts}</strong></span>`;
    }
  }

  function buildParcelTable(container, data) {
    const hide = $("hideCosts").checked;
    const hideCustomerNumbers = $("hideCustomerNumbers")?.checked ?? true;
    const stickyWidths = parcelStickyWidths(hideCustomerNumbers);
    const { headerKeys, headers } = parcelTableColumns(hide, hideCustomerNumbers);

    if (!headerKeys.includes(state.sortParcel.key)) state.sortParcel.key = "date";

    const rows = sortRows(data.rows || [], state.sortParcel.key, state.sortParcel.dir);

    const nParcSticky = stickyWidths.length;
    let html = "<table class='data-table'><thead><tr>";
    headers.forEach((h, i) => {
      const k = headerKeys[i];
      const cl = i < nParcSticky ? "sticky-col" : "";
      const sortClass =
        state.sortParcel.key === k ? (state.sortParcel.dir === 1 ? "sort-asc" : "sort-desc") : "";
      const st =
        i < nParcSticky
          ? ` style="left:${stickyLeft(i, stickyWidths)}px;min-width:${stickyWidths[i]}px;width:${stickyWidths[i]}px"`
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
        const rowKey = {
          date: row.date,
          child_number: row.child_number,
          mail_class: row.mail_class,
          zone: row.zone != null ? String(row.zone) : "",
        };
        const isSel =
          state.selectedParcel &&
          state.selectedParcel.date === rowKey.date &&
          String(state.selectedParcel.child_number) === String(rowKey.child_number) &&
          state.selectedParcel.mail_class === rowKey.mail_class &&
          state.selectedParcel.zone === rowKey.zone;
        const trCls = isSel ? " class='row-selected'" : "";
        html += `<tr${trCls} data-date="${escapeHtml(row.date)}" data-child-number="${escapeHtml(
          String(row.child_number ?? "")
        )}" data-mail-class="${escapeHtml(row.mail_class || "")}" data-zone="${escapeHtml(
          rowKey.zone
        )}">`;
        headerKeys.forEach((k, i) => {
          const v = row[k];
          const isLb = k.startsWith("lb_");
          const n = Number(v) || 0;
          const muted = isLb && n === 0 ? " muted" : "";
          const cellClass = i < nParcSticky ? "sticky-col" : "";
          const st =
            i < nParcSticky
              ? ` style="left:${stickyLeft(i, stickyWidths)}px;min-width:${stickyWidths[i]}px;width:${stickyWidths[i]}px"`
              : "";
          let inner;
          if (k === "total_retail") inner = fmtMoney(v);
          else if (k === "child_number") inner = fmtId(v);
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

    container.querySelectorAll("tbody tr[data-date]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const date = tr.getAttribute("data-date") || "";
        const childNumRaw = tr.getAttribute("data-child-number") || "";
        const mailClass = tr.getAttribute("data-mail-class") || "";
        const zone = tr.getAttribute("data-zone") || "";
        if (!date || date === "Combined") return;
        const cn = parseInt(childNumRaw, 10);
        if (Number.isNaN(cn) || !mailClass) return;
        state.selectedParcel = { date, child_number: cn, mail_class: mailClass, zone };
        const btn = $("btnEditParcel");
        if (btn) btn.disabled = false;
        buildParcelTable(container, data);
      });
    });

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
    const searchVal = $("searchCustomer").value;
    populateAccountSelect(searchVal);
    populateProfitAccountPick(searchVal);
    renderProfitAccountChips();
  }

  async function loadAll() {
    $("errorBanner").classList.add("hidden");
    state.selectedPostage = null;
    state.selectedParcel = null;
    if ($("btnEditPostage")) $("btnEditPostage").disabled = true;
    if ($("btnEditParcel")) $("btnEditParcel").disabled = true;
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
      renderPostageAndParcelSummaryBar($("summaryPostage"), jp, jv);
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
      refreshReportReadiness();
      refreshNoclassNotice();
    }
  }

  async function refreshNoclassNotice() {
    const notice = $("noclassNotice");
    if (!notice) return;

    const sel = $("selectAccount");
    const opt = sel && sel.selectedOptions && sel.selectedOptions[0];
    // Only flag for a specific selected account, not "All Accounts".
    if (!opt || !opt.value) {
      notice.classList.add("hidden");
      notice.textContent = "";
      return;
    }

    try {
      const r = await fetch("/api/postage/noclass?" + queryParams().toString());
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "NOCLASS check failed");

      const records = j.records || [];
      if (!records.length) {
        notice.classList.add("hidden");
        notice.textContent = "";
        return;
      }

      const lines = records.map(
        (rec) =>
          `${rec.customer_name || rec.account_code} \u2014 ${rec.mail_class}: ${fmtDisplayDate(
            rec.file_date
          )} (${fmtInt(rec.pieces)} pcs)`
      );
      notice.innerHTML = "";
      const heading = document.createElement("strong");
      heading.textContent = `Non-class postage found (${records.length}):`;
      notice.appendChild(heading);
      const ul = document.createElement("ul");
      lines.forEach((line) => {
        const li = document.createElement("li");
        li.textContent = line;
        ul.appendChild(li);
      });
      notice.appendChild(ul);
      notice.classList.remove("hidden");
    } catch {
      notice.classList.add("hidden");
      notice.textContent = "";
    }
  }

  const _dailyReportsInFlight = new Set();
  const _dailyReportsAttempted = new Set();

  async function autoCreateDailyReports(startDate, endDate) {
    const key = `${startDate}|${endDate}`;
    // Fire at most once per range per session to avoid loops when a ready day
    // still can't produce a complete set.
    if (_dailyReportsInFlight.has(key) || _dailyReportsAttempted.has(key)) return;
    _dailyReportsInFlight.add(key);
    try {
      const r = await fetch("/api/export/daily-reports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start_date: startDate, end_date: endDate }),
      });
      if (r.ok) {
        _dailyReportsAttempted.add(key);
        refreshReportReadiness();
      }
    } catch {
      // Non-fatal; readiness stays "All reports ready" until next refresh.
    } finally {
      _dailyReportsInFlight.delete(key);
    }
  }

  function updateDailyReportsNotice(j) {
    const el = $("dailyReportsNotice");
    if (!el) return;
    const count = j.business_day_count || 0;
    const entry = (j.daily_reports || [])[0];
    if (count !== 1 || !entry) {
      el.classList.add("hidden");
      el.classList.remove("generated", "pending");
      el.textContent = "";
      return;
    }
    const dateLabel = fmtDisplayDate(entry.date);
    const skipped = entry.skipped || [];
    const failed = entry.failed || [];
    const notes = [
      ...skipped.map((s) => `${s.label}: ${s.error || "no mail for this day"}`),
      ...failed.map((f) => `${f.label}: ${f.error || "error"}`),
    ];
    const notesSuffix = notes.length
      ? ` (ran with exceptions - ${notes.join("; ")})`
      : "";
    el.classList.remove("hidden", "generated", "pending");
    if (entry.complete) {
      el.classList.add("generated");
      el.textContent = `Daily report files generated for ${dateLabel} - saved to ${entry.folder_relative}${notesSuffix}`;
    } else {
      el.classList.add("pending");
      el.textContent = `Daily reports not generated yet for ${dateLabel}${notesSuffix}`;
    }
  }

  async function refreshReportReadiness() {
    const dot = $("reportReadinessDot");
    const label = $("reportReadinessLabel");
    if (!dot || !label) return;
    try {
      const r = await fetch("/api/reports/readiness?" + queryParams().toString());
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Readiness check failed");

      const count = j.business_day_count || 0;
      updateDailyReportsNotice(j);
      if (count === 0) {
        dot.classList.remove("ready");
        label.textContent = "No business days in this date range (weekends only).";
        return;
      }

      if (j.ready) {
        dot.classList.add("ready");
        const rangeLabel = formatReadinessRangeLabel(j.start_date, j.end_date, count);
        if (j.reports_created) {
          label.textContent = `Reports created (${rangeLabel})`;
        } else {
          label.textContent = `All reports ready (${rangeLabel})`;
          autoCreateDailyReports(j.start_date, j.end_date);
        }
        return;
      }

      dot.classList.remove("ready");
      const parts = [];
      const missing = j.missing || {};
      if ((missing.postage || []).length) {
        parts.push(`Postage (${formatMissingDates(missing.postage)})`);
      }
      if ((missing.parcel || []).length) {
        parts.push(`Parcel (${formatMissingDates(missing.parcel)})`);
      }
      if ((missing.ws3_presort || []).length) {
        parts.push(`WS3 presort (${formatMissingDates(missing.ws3_presort)})`);
      }
      label.textContent = parts.length
        ? `Missing: ${parts.join("; ")}`
        : "Reports incomplete for this date range.";
    } catch {
      dot.classList.remove("ready");
      label.textContent = "Report readiness unavailable";
      const notice = $("dailyReportsNotice");
      if (notice) {
        notice.classList.add("hidden");
        notice.classList.remove("generated", "pending");
        notice.textContent = "";
      }
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
    $("tabProfitFlats").classList.toggle("hidden", name !== "profitFlats");
    if (name === "profitFlats") {
      Promise.all([loadProfitFlats(), loadProfitParcels()]).catch(() => {});
    }
  }

  function buildParcelProfitLines(container, lines) {
    const headers = ["Line", "Description", "Value"];
    let h =
      "<table class='data-table'><thead><tr>" +
      `<th>${headers[0]}</th><th>${headers[1]}</th><th class='num'>${headers[2]}</th>` +
      "</tr></thead><tbody>";
    if (!lines || !lines.length) {
      h += `<tr><td colspan="3" class="empty-state">No parcel profit data.</td></tr>`;
    } else {
      for (const ln of lines) {
        const n = ln.line_no;
        const label = ln.label || "";
        const kind = ln.kind || "money";
        const v = ln.value;
        let disp = "\u2014";
        if (kind === "int") disp = fmtInt(Number(v) || 0);
        else disp = fmtMoney(v);
        h += `<tr><td>${escapeHtml(String(n))}</td><td>${escapeHtml(label)}</td><td class="num">${disp}</td></tr>`;
      }
    }
    h += "</tbody></table>";
    container.innerHTML = h;
  }

  async function loadProfitParcels() {
    const container = $("parcelProfitBlock");
    if (!container) return;
    if (sortedProfitAccountIds().length > PROFIT_ACCOUNT_IDS_MAX) {
      container.innerHTML = `<p class='empty-state'>Too many profit accounts (max ${PROFIT_ACCOUNT_IDS_MAX}).</p>`;
      return;
    }
    const p = profitQueryParams();
    const key = p.toString();
    try {
      let r;
      if (profitAccountsUsePost()) {
        r = await fetch("/api/profit/parcels", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(profitPostJsonBody({})),
        });
      } else {
        r = await fetch("/api/profit/parcels?" + key);
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "Parcel profit failed");
      buildParcelProfitLines(container, j.lines || []);
    } catch (e) {
      // Non-fatal: keep the rest of the Profit tab usable.
      container.innerHTML = `<p class='empty-state'>${escapeHtml(String(e.message || e))}</p>`;
    }
  }

  function profitSummaryHtml(meta, totals) {
    const parts = [];
    if (meta) {
      parts.push(`<span>Retail rate: ${fmtMoney(meta.retail_rate)}</span>`);
      parts.push(`<span>Flats discount: ${fmtMoney(meta.discount)}</span>`);
      parts.push(`<span>Sell-to rate: ${fmtMoney(meta.sell_to_rate)}</span>`);
      if (meta.profit_accounts && meta.profit_accounts.length) {
        parts.push(
          `<span>Profit accounts: ${meta.profit_accounts.map((x) => fmtId(x)).join(", ")}</span>`
        );
      }
      if (meta.parcel_fee != null && meta.parcel_fee !== "") {
        parts.push(`<span>Parcel Reseller: ${fmtMoney(meta.parcel_fee)}/pc</span>`);
      }
      if (meta.efd_parcel_fee != null && meta.efd_parcel_fee !== "") {
        parts.push(`<span>Parcel fee to EFD: ${fmtMoney(meta.efd_parcel_fee)}/pc</span>`);
      }
    }
    if (totals) {
      parts.push(`<span>Run days: ${fmtInt(totals.run_days || 0)}</span>`);
      parts.push(`<span>Total pieces: ${fmtInt(totals.total_pieces || 0)}</span>`);
      parts.push(`<span>Total profit: ${fmtMoney(totals.total_profit || 0)}</span>`);
    }
    return parts.join("");
  }

  function buildProfitRateTypeTable(container, rows) {
    const headers = [
      "Rate Type",
      "Pieces",
      "Avg USPS cost / pc",
      "Sell-to / pc",
      "Avg profit / pc",
      "Total profit",
    ];
    const keys = [
      "rate_type",
      "total_pieces",
      "avg_usps_cost_per_piece",
      "sell_to_rate",
      "avg_profit_per_piece",
      "total_profit",
    ];
    let h =
      "<table class='data-table'><thead><tr>" +
      headers.map((x, i) => `<th${i === 0 ? "" : " class='num'"}>${escapeHtml(x)}</th>`).join("") +
      "</tr></thead><tbody>";
    if (!rows || !rows.length) {
      h += `<tr><td colspan="${headers.length}" class="empty-state">No WS3 profit rows found.</td></tr>`;
    } else {
      for (const r of rows) {
        h += "<tr>";
        for (let i = 0; i < keys.length; i++) {
          const k = keys[i];
          const v = r[k];
          let inner = "";
          if (k === "total_pieces") inner = fmtInt(Number(v) || 0);
          else if (k === "total_profit") inner = fmtMoney(v);
          else if (k === "avg_usps_cost_per_piece" || k === "sell_to_rate" || k === "avg_profit_per_piece")
            inner = v == null || v === "" ? "\u2014" : fmtMoney(v);
          else inner = v != null ? escapeHtml(String(v)) : "";
          h += `<td${i === 0 ? "" : " class='num'"}>${inner}</td>`;
        }
        h += "</tr>";
      }
    }
    h += "</tbody></table>";
    container.innerHTML = h;
  }

  function buildProfitDetailTable(container, rows) {
    const headers = [
      "Mail Date",
      "Mail ID",
      "Profile",
      "Parent Account",
      "Customer Code",
      "Customer Name",
      "Rate Type",
      "Pieces",
      "Rejected",
      "Postage Claimed",
      "USPS cost / pc",
      "Sell-to / pc",
      "Profit / pc",
      "Total profit",
    ];
    const keys = [
      "mail_date",
      "mail_id",
      "profile_name",
      "parent_customer_name",
      "customer_code",
      "customer_name",
      "rate_type",
      "num_pieces",
      "pcs_rejected",
      "postage_claimed",
      "usps_cost_per_piece",
      "sell_to_rate",
      "profit_per_piece",
      "total_profit",
    ];
    let h =
      "<table class='data-table'><thead><tr>" +
      headers.map((x, i) => `<th${i < 7 ? "" : " class='num'"}>${escapeHtml(x)}</th>`).join("") +
      "</tr></thead><tbody>";
    if (!rows || !rows.length) {
      h += `<tr><td colspan="${headers.length}" class="empty-state">No WS3 detail rows found.</td></tr>`;
    } else {
      for (const r of rows) {
        h += "<tr>";
        for (let i = 0; i < keys.length; i++) {
          const k = keys[i];
          const v = r[k];
          let inner = "";
          if (k === "num_pieces" || k === "pcs_rejected") inner = fmtInt(Number(v) || 0);
          else if (k === "postage_claimed" || k === "total_profit") inner = fmtMoney(v);
          else if (k === "usps_cost_per_piece" || k === "sell_to_rate" || k === "profit_per_piece")
            inner = v == null || v === "" ? "\u2014" : fmtMoney(v);
          else inner = v != null ? escapeHtml(String(v)) : "";
          h += `<td${i < 7 ? "" : " class='num'"}>${inner}</td>`;
        }
        h += "</tr>";
      }
    }
    h += "</tbody></table>";
    container.innerHTML = h;
  }

  async function loadProfitFlats() {
    const err = $("profitFlatsError");
    const spin = $("spinProfitFlats");
    const sum = $("summaryProfitFlats");
    const tblSum = $("tableProfitFlatsRateType");
    const tblDet = $("tableProfitFlatsDetail");
    if (err) err.classList.add("hidden");
    if (spin) spin.classList.remove("hidden");

    const idCount = sortedProfitAccountIds().length;
    if (idCount > PROFIT_ACCOUNT_IDS_MAX) {
      if (err) {
        err.textContent = `Too many profit accounts (${idCount}). Maximum is ${PROFIT_ACCOUNT_IDS_MAX}.`;
        err.classList.remove("hidden");
      }
      if (sum) sum.innerHTML = "";
      if (tblSum) buildProfitRateTypeTable(tblSum, []);
      if (tblDet) buildProfitDetailTable(tblDet, []);
      if (spin) spin.classList.add("hidden");
      return;
    }

    const p = profitQueryParams();
    p.set("discount", String(parseFlatsDiscount()));
    const key = p.toString();
    try {
      if (state.lastProfitFlats.key === key && state.lastProfitFlats.payload) {
        const j = state.lastProfitFlats.payload;
        if (sum) sum.innerHTML = profitSummaryHtml(j.meta, j.totals);
        if (tblSum) buildProfitRateTypeTable(tblSum, j.rate_summary || []);
        if (tblDet) buildProfitDetailTable(tblDet, j.detail || []);
        return;
      }
      let r;
      if (profitAccountsUsePost()) {
        r = await fetch("/api/profit/flats", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(profitPostJsonBody({ discount: parseFlatsDiscount() })),
        });
      } else {
        r = await fetch("/api/profit/flats?" + key);
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = j.error || "Profit report failed";
        if (err) {
          err.textContent = msg;
          err.classList.remove("hidden");
        }
        if (sum) sum.innerHTML = profitSummaryHtml(j.meta, j.totals);
        if (tblSum) buildProfitRateTypeTable(tblSum, []);
        if (tblDet) buildProfitDetailTable(tblDet, []);
        return;
      }
      state.lastProfitFlats = { key, payload: j };
      if (sum) sum.innerHTML = profitSummaryHtml(j.meta, j.totals);
      if (tblSum) buildProfitRateTypeTable(tblSum, j.rate_summary || []);
      if (tblDet) buildProfitDetailTable(tblDet, j.detail || []);
    } catch (e) {
      if (err) {
        err.textContent = String(e.message || e);
        err.classList.remove("hidden");
      }
      if (tblSum) buildProfitRateTypeTable(tblSum, []);
      if (tblDet) buildProfitDetailTable(tblDet, []);
    } finally {
      if (spin) spin.classList.add("hidden");
    }
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
    if (!$("hideCosts")?.checked) {
      const mc = row.metered_cost != null ? row.metered_cost : row.total_cost;
      if (mc != null) parts.push(`<strong>Metered cost:</strong> ${fmtMoney(mc)}`);
      if (row.retail_cost != null) parts.push(`<strong>Retail cost:</strong> ${fmtMoney(row.retail_cost)}`);
    }
    return parts.join(" &nbsp;·&nbsp; ");
  }

  function renderWeightEditor(rows) {
    let h =
      "<table class='data-table'><thead><tr><th>Weight (oz)</th><th class='num'>Pieces</th><th class='num'>Metered cost</th></tr></thead><tbody>";
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

  function setParcelEditOpen(open) {
    const overlay = $("parcelEditOverlay");
    if (!overlay) return;
    overlay.classList.toggle("hidden", !open);
    overlay.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open) {
      $("parcelEditPreview")?.classList.add("hidden");
      if ($("btnApplyParcelEdit")) $("btnApplyParcelEdit").disabled = true;
      if ($("parcelEditBuckets")) $("parcelEditBuckets").innerHTML = "";
      if ($("parcelEditContext")) $("parcelEditContext").textContent = "";
      if ($("parcelEditAccount")) $("parcelEditAccount").value = "";
      if ($("parcelEditReason")) $("parcelEditReason").value = "";
    }
  }

  function selectedParcelRowFromState() {
    const sel = state.selectedParcel;
    if (!sel || !state.parcelData) return null;
    const rows = state.parcelData.rows || [];
    return rows.find(
      (r) =>
        r.date === sel.date &&
        String(r.child_number) === String(sel.child_number) &&
        r.mail_class === sel.mail_class &&
        (r.zone != null ? String(r.zone) : "") === sel.zone
    );
  }

  function parcelLbBucketLabel(lbBucket) {
    const b = Number(lbBucket);
    if (b === 11) return "10+ lb";
    if (b >= 1 && b <= 10) return `${b} lb`;
    return String(lbBucket);
  }

  function parcelEditContextHtml(row) {
    const parts = [
      `<strong>Date:</strong> ${escapeHtml(row.date || "")}`,
      `<strong>Child:</strong> ${escapeHtml(row.child_name || "")} (${fmtId(row.child_number)})`,
      `<strong>Class:</strong> ${escapeHtml(row.mail_class || "")}`,
      `<strong>Zone:</strong> ${escapeHtml(row.zone != null ? String(row.zone) : "")}`,
      `<strong>Total Qty:</strong> ${fmtInt(Number(row.total_qty) || 0)}`,
    ];
    if (!$("hideCosts")?.checked && row.total_retail != null) {
      parts.push(`<strong>Retail cost:</strong> ${fmtMoney(row.total_retail)}`);
    }
    return parts.join(" &nbsp;·&nbsp; ");
  }

  function renderParcelBucketEditor(rows) {
    let h =
      "<table class='data-table'><thead><tr><th>Lb bucket</th><th class='num'>Pieces</th><th class='num'>Billing amount</th></tr></thead><tbody>";
    if (!rows.length) {
      h += "<tr><td colspan='3' class='empty-state'>No underlying rows found.</td></tr>";
    } else {
      rows.forEach((r) => {
        const bucket = String(r.lb_bucket);
        const pieces = Number(r.pieces) || 0;
        const cost = r.billing_amount_sum != null ? fmtMoney(r.billing_amount_sum) : "\u2014";
        h += `<tr data-lb-bucket="${escapeHtml(bucket)}">
          <td>${escapeHtml(parcelLbBucketLabel(bucket))}</td>
          <td class="num"><input class="edit-piece-input" type="number" min="0" step="1" value="${pieces}" data-lb-bucket="${escapeHtml(
            bucket
          )}" /></td>
          <td class="num">${cost}</td>
        </tr>`;
      });
    }
    h += "</tbody></table>";
    return h;
  }

  async function openParcelEdit() {
    const row = selectedParcelRowFromState();
    if (!row) return;
    if (row.date === "Combined" || $("consolidate")?.checked) {
      alert('Disable "Consolidate Date & Mailclass" to edit a specific day.');
      return;
    }
    $("parcelEditContext").innerHTML = parcelEditContextHtml(row);
    setParcelEditOpen(true);
    $("parcelEditPreview")?.classList.add("hidden");
    if ($("btnApplyParcelEdit")) $("btnApplyParcelEdit").disabled = true;
    try {
      const p = new URLSearchParams();
      p.set("bill_date", row.date);
      p.set("account_code", String(row.child_number));
      p.set("mail_class", String(row.mail_class || ""));
      p.set("zone", row.zone != null ? String(row.zone) : "");
      const r = await fetch("/api/parcels/row-details?" + p.toString());
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Failed to load row details");
      const details = j.rows || [];
      $("parcelEditBuckets").innerHTML = renderParcelBucketEditor(details);
      const acct = $("parcelEditAccount");
      if (acct) acct.value = String(row.child_number);
    } catch (e) {
      alert(String(e.message || e));
      setParcelEditOpen(false);
    }
  }

  function readPiecesByBucketFromEditor() {
    const out = {};
    document.querySelectorAll("#parcelEditBuckets input[data-lb-bucket]").forEach((inp) => {
      const bucket = inp.getAttribute("data-lb-bucket");
      const v = inp.value;
      if (!bucket) return;
      const n = parseInt(String(v || "0"), 10);
      out[bucket] = Number.isNaN(n) ? 0 : Math.max(0, n);
    });
    return out;
  }

  function renderParcelEditPreview(preview) {
    const s = preview.summary || {};
    const from = state.selectedParcel?.child_number;
    const to = $("parcelEditAccount")?.value?.trim();
    const mc = state.selectedParcel?.mail_class;
    const z = state.selectedParcel?.zone;
    const dt = state.selectedParcel?.date;
    const msg = `Notice: You are about to reassign ${s.updated || 0} piece(s) for ${dt} (${mc}, zone ${z || ""}) from account ${from} → ${to}. Source billing rows: ${s.source_rows || 0}.`;
    return escapeHtml(msg);
  }

  $("btnPreviewParcelEdit")?.addEventListener("click", async () => {
    const row = selectedParcelRowFromState();
    if (!row) return;
    const toRaw = $("parcelEditAccount")?.value?.trim() || "";
    const to = parseInt(toRaw, 10);
    if (Number.isNaN(to)) {
      alert("Enter a valid new account number.");
      return;
    }
    const pieces_by_bucket = readPiecesByBucketFromEditor();
    const payload = {
      bill_date: row.date,
      from_account_code: row.child_number,
      to_account_code: to,
      mail_class: row.mail_class,
      zone: row.zone != null ? String(row.zone) : "",
      pieces_by_bucket,
    };
    try {
      const r = await fetch("/api/parcels/row-preview-update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Preview failed");
      const box = $("parcelEditPreview");
      if (box) {
        box.innerHTML = renderParcelEditPreview(j);
        box.classList.remove("hidden");
      }
      if ($("btnApplyParcelEdit")) $("btnApplyParcelEdit").disabled = false;
    } catch (e) {
      alert(String(e.message || e));
    }
  });

  $("btnApplyParcelEdit")?.addEventListener("click", async () => {
    const row = selectedParcelRowFromState();
    if (!row) return;
    const toRaw = $("parcelEditAccount")?.value?.trim() || "";
    const to = parseInt(toRaw, 10);
    if (Number.isNaN(to)) {
      alert("Enter a valid new account number.");
      return;
    }
    const pieces_by_bucket = readPiecesByBucketFromEditor();
    const payload = {
      bill_date: row.date,
      from_account_code: row.child_number,
      to_account_code: to,
      mail_class: row.mail_class,
      zone: row.zone != null ? String(row.zone) : "",
      pieces_by_bucket,
      reason: $("parcelEditReason")?.value || "",
    };
    try {
      const btn = $("btnApplyParcelEdit");
      if (btn) btn.disabled = true;
      const r = await fetch("/api/parcels/row-apply-update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Apply failed");
      setParcelEditOpen(false);
      await loadAll();
      alert(`Update applied. Reassigned: ${j.summary?.updated || 0} piece(s).`);
    } catch (e) {
      alert(String(e.message || e));
      if ($("btnApplyParcelEdit")) $("btnApplyParcelEdit").disabled = false;
    }
  });

  $("btnEditParcel")?.addEventListener("click", () => openParcelEdit());
  $("btnCloseParcelEdit")?.addEventListener("click", () => setParcelEditOpen(false));
  $("parcelEditOverlay")?.addEventListener("click", (ev) => {
    if (ev.target && ev.target.id === "parcelEditOverlay") setParcelEditOpen(false);
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
  $("startDate").addEventListener("change", () => refreshReportReadiness());
  $("endDate").addEventListener("change", () => refreshReportReadiness());
  function refreshPostageFromDiscount() {
    if (state.postageData) {
      renderPostageAndParcelSummaryBar($("summaryPostage"), state.postageData, state.parcelData);
      buildPostageTableFixed($("tablePostage"), state.postageData);
    }
  }

  /** Rebuild postage/parcel views when "Hide Costs" changes (no reload required). */
  function applyHideCostsToUi() {
    const hideBtn = $("btnExportFlatsSavings");
    if (hideBtn) hideBtn.hidden = $("hideCosts").checked;
    if (state.postageData) {
      renderPostageAndParcelSummaryBar($("summaryPostage"), state.postageData, state.parcelData);
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
  $("allocatePresortRejects")?.addEventListener("change", () => loadAll());
  $("removeZeros")?.addEventListener("change", () => loadAll());
  $("hideCosts")?.addEventListener("change", applyHideCostsToUi);
  $("hideCustomerNumbers")?.addEventListener("change", applyHideCostsToUi);
  $("btnScan").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/scan", { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error) {
        $("errorBanner").textContent = String(j.error || r.statusText || "Scan failed");
        $("errorBanner").classList.remove("hidden");
        await refreshWatcher();
        await refreshReportReadiness();
        return;
      }
      if (j.ok === false && (j.failed || []).length) {
        const parts = (j.failed || []).map((x) => `${x.file}: ${x.error}`);
        $("errorBanner").textContent = parts.join("\n");
        $("errorBanner").classList.remove("hidden");
      } else {
        $("errorBanner").classList.add("hidden");
      }
      await refreshWatcher();
      await loadAll();
    } catch (e) {
      $("errorBanner").textContent = String(e.message || e);
      $("errorBanner").classList.remove("hidden");
    }
  });

  $("btnRestart").addEventListener("click", async () => {
    if (!confirm("Restart the server now? In-flight work will be interrupted.")) return;
    const btn = $("btnRestart");
    btn.disabled = true;
    const label = $("watcherLabel");
    if (label) label.textContent = "Restarting server...";
    try {
      await fetch("/api/system/restart", { method: "POST" });
    } catch {
      // Expected: the process may die before the response returns.
    }
    const start = Date.now();
    const poll = async () => {
      try {
        const r = await fetch("/api/watcher/status", { cache: "no-store" });
        if (r.ok) {
          location.reload();
          return;
        }
      } catch {
        // Server still coming back up.
      }
      if (Date.now() - start > 30000) {
        btn.disabled = false;
        $("errorBanner").textContent =
          "Server did not come back within 30s. Check the terminal running the app.";
        $("errorBanner").classList.remove("hidden");
        return;
      }
      setTimeout(poll, 1000);
    };
    setTimeout(poll, 1500);
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
      p.set("hide_customer_numbers", $("hideCustomerNumbers")?.checked ? "true" : "false");
      const r = await fetch("/api/export/flats-grid-csv?" + p.toString());
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
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
        safeFilename(`${acctLabel} Flats Report ${endLabel}`) + ".csv";
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

  $("btnExportProfitReport")?.addEventListener("click", async () => {
    if (sortedProfitAccountIds().length > PROFIT_ACCOUNT_IDS_MAX) {
      alert(`Too many profit accounts (max ${PROFIT_ACCOUNT_IDS_MAX}). Remove some before exporting.`);
      return;
    }
    const q = profitQueryParams();
    q.set("discount", String(parseFlatsDiscount()));
    q.set("discount_efd", String(parseResellerDiscount()));
    const btn = $("btnExportProfitReport");
    setExportLoading(btn, true);
    try {
      let r;
      if (profitAccountsUsePost()) {
        r = await fetch("/api/export/profit-report-xlsx", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            profitPostJsonBody({
              discount: parseFlatsDiscount(),
              discount_efd: parseResellerDiscount(),
            })
          ),
        });
      } else {
        r = await fetch("/api/export/profit-report-xlsx?" + q.toString());
      }
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
        "profit_report.xlsx";
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
    q.set("hide_customer_numbers", $("hideCustomerNumbers")?.checked ? "true" : "false");
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

  $("btnExportConsolidatedParcelCsv").addEventListener("click", async () => {
    const q = queryParams();
    const btn = $("btnExportConsolidatedParcelCsv");
    setExportLoading(btn, true);
    try {
      const r = await fetch("/api/export/consolidated-parcel-csv?" + q.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
        "parcel_billing.csv";
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

  $("btnExportEfdParcelInvoice").addEventListener("click", async () => {
    const q = queryParams();
    q.set("efd_parcel_fee", String(parseEfdParcelFee()));
    const btn = $("btnExportEfdParcelInvoice");
    setExportLoading(btn, true);
    try {
      const r = await fetch("/api/export/efd-parcel-invoice-xlsx?" + q.toString());
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || "Export failed");
      }
      const blob = await r.blob();
      const name =
        filenameFromContentDisposition(r.headers.get("Content-Disposition") || "") ||
        "efd_parcel_invoice.xlsx";
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

  function efdWeeklyExportBaseQuery() {
    const q = queryParams();
    q.delete("parent_number");
    q.delete("customer_number");
    q.set("discount", String(parseResellerDiscount()));
    q.set("efd_parcel_fee", String(parseEfdParcelFee()));
    return q;
  }

  function efdWeeklyBundleRequestBody() {
    const q = efdWeeklyExportBaseQuery();
    q.set("postage_discount", String(parseFlatsDiscount()));
    q.set("parcel_discount", String(parseParcelDiscount()));
    const body = {};
    for (const [k, v] of q.entries()) body[k] = v;
    return body;
  }

  $("btnExportEfdWeeklyInvoice").addEventListener("click", async () => {
    const btn = $("btnExportEfdWeeklyInvoice");
    setExportLoading(btn, true);
    const totalExpected = 10;
    try {
      const r = await fetch("/api/export/efd-weekly-bundle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(efdWeeklyBundleRequestBody()),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        throw new Error(j.error || "Export failed");
      }
      const savedCount = (j.saved || []).length;
      const folder = j.folder_relative || j.folder || "PostageReports";
      const failed = j.failed || [];
      if (failed.length > 0) {
        const missing = failed.map((f) => f.label).join(", ");
        alert(
          `Saved ${savedCount} of ${totalExpected} reports to ${folder}. Failed: ${missing}.`
        );
      } else {
        alert(`Saved ${savedCount} reports to ${folder}`);
      }
    } catch (e) {
      alert(String(e.message || e));
    } finally {
      setExportLoading(btn, false);
    }
  });

  $("btnExportEfdDailyReport").addEventListener("click", async () => {
    const btn = $("btnExportEfdDailyReport");
    setExportLoading(btn, true);
    try {
      const start = $("startDate").value;
      const end = $("endDate").value;
      if (!start || !end) {
        alert("Choose a start and end date first.");
        return;
      }
      const r = await fetch("/api/export/daily-reports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start_date: start, end_date: end }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Daily report generation failed");
      const generated = j.generated || [];
      const skipped = j.skipped || [];
      if (!generated.length && !skipped.length) {
        alert("No business days in the selected range.");
        return;
      }
      const folders = generated.map((g) => g.folder_relative).filter(Boolean);
      const withMissing = generated.filter((g) => (g.failed || []).length > 0);
      let msg = `Saved daily reports for ${generated.length} day(s).`;
      if (folders.length) msg += `\nFolder(s): ${folders.join(", ")}`;
      if (skipped.length) msg += `\nSkipped (already complete): ${skipped.join(", ")}`;
      if (withMissing.length) {
        msg += `\nMissing reports logged for: ${withMissing
          .map((g) => g.report_date)
          .join(", ")}`;
      }
      alert(msg);
      refreshReportReadiness();
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

  $("removeZeros").addEventListener("change", () => {
    state.consolidatedDefaultsTouched.removeZeros = true;
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
    q.set("parcel_discount", String(parseParcelDiscount()));
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
    searchDebounce = setTimeout(() => {
      const v = $("searchCustomer").value;
      populateAccountSelect(v);
      populateProfitAccountPick(v);
    }, 150);
  });
  $("searchCustomer").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") loadAll();
  });

  $("profitAccountPick")?.addEventListener("change", () => {
    const sel = $("profitAccountPick");
    if (!sel || !sel.value) return;
    const opt = sel.selectedOptions[0];
    const n = parseInt(sel.value, 10);
    const kind = opt?.getAttribute("data-kind") || "";
    if (Number.isNaN(n)) return;
    const row = accountRowByCustomerNumber(n, kind);
    mergeProfitAccountIds(
      profitAccountIdsForRow(row || { customer_number: n, kind: kind || "standalone" })
    );
    sel.value = "";
  });

  $("btnProfitUseToolbar")?.addEventListener("click", () => {
    const acc = $("selectAccount");
    const opt = acc?.selectedOptions[0];
    if (!opt || !opt.value) return;
    const n = parseInt(opt.value, 10);
    const kind = opt.getAttribute("data-kind") || "";
    if (Number.isNaN(n)) return;
    const row = accountRowByCustomerNumber(n, kind);
    mergeProfitAccountIds(
      profitAccountIdsForRow(row || { customer_number: n, kind: kind || "standalone" })
    );
  });

  $("btnProfitClearAccounts")?.addEventListener("click", () => {
    state.profitAccountIds = [];
    renderProfitAccountChips();
    onProfitScopeChanged();
  });

  $("parcelFee")?.addEventListener("change", () => onProfitScopeChanged());
  $("efdParcelFee")?.addEventListener("change", () => onProfitScopeChanged());

  $("btnRunSummary").addEventListener("click", () => runSummary());
  $("sumCustUnmatchedOnly").addEventListener("change", () => refreshSummaryCustomerViews());
  $("sumParcelUnmatchedOnly").addEventListener("change", () => refreshSummaryCustomerViews());

  const d = defaultDates();
  $("startDate").value = d.start;
  $("endDate").value = d.end;
  $("sumStart").value = d.start;
  $("sumEnd").value = d.end;

  // Initial (no auto-load) state: show a lightweight hint instead of loading large tables.
  $("summaryPostage").textContent = "";
  $("summaryParcels").textContent = "";
  $("tablePostage").innerHTML = "<p class='empty-state'>Press <strong>Load All</strong> to load data.</p>";
  $("tableParcels").innerHTML = "<p class='empty-state'>Press <strong>Load All</strong> to load data.</p>";
  buildParcelZoneSummary($("parcelZoneBanner"), $("parcelZoneSummary"), $("parcelZoneFooter"), null);
  if ($("summaryProfitFlats")) $("summaryProfitFlats").textContent = "";
  if ($("tableProfitFlatsRateType"))
    $("tableProfitFlatsRateType").innerHTML =
      "<p class='empty-state'>Select the <strong>Profit Report</strong> tab to load.</p>";
  if ($("tableProfitFlatsDetail")) $("tableProfitFlatsDetail").innerHTML = "";
  if ($("parcelProfitBlock")) $("parcelProfitBlock").innerHTML = "";
  $("spinPostage").classList.add("hidden");
  $("spinParcels").classList.add("hidden");

  // On page refresh: populate account dropdown but don't auto-load the heavy tables.
  // Loading all accounts for the default date range can be slow; users can press "Load All" explicitly.
  loadCustomers().catch((e) => {
    $("errorBanner").textContent = String(e.message || e);
    $("errorBanner").classList.remove("hidden");
  });

  // Prefill pricing knobs from stored terms (System page); typed values still override per report.
  fetch("/api/system/pricing-terms")
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      const cur = j && j.current;
      if (!cur) return;
      const setKnob = (id, v) => {
        const el = $(id);
        if (el && v != null && !Number.isNaN(Number(v))) el.value = Number(v).toFixed(2);
      };
      setKnob("invoiceDiscount", cur.flats_customer_discount);
      setKnob("resellerDiscount", cur.flats_efd_discount);
      setKnob("parcelDiscount", cur.parcel_customer_discount);
      setKnob("parcelFee", cur.parcel_fee_per_piece);
      setKnob("efdParcelFee", cur.parcel_fee_per_piece);
    })
    .catch(() => {});

  setInterval(refreshWatcher, 30000);
  refreshWatcher();
  refreshReportReadiness();
})();
