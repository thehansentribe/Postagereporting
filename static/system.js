(function () {
  const $ = (id) => document.getElementById(id);

  function showBanner(msg) {
    const el = $("systemBanner");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function showError(msg) {
    const el = $("systemError");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtWeight(w) {
    const n = Number(w);
    if (Number.isNaN(n)) return String(w);
    return String(n % 1 === 0 ? Math.trunc(n) : n);
  }

  function fmtRateInput(v) {
    if (v == null || v === "") return "";
    const n = Number(v);
    if (Number.isNaN(n)) return "";
    return n.toFixed(2);
  }

  function parseMoneyInput(s) {
    const raw = String(s || "").trim();
    if (!raw) return null;
    const cleaned = raw.replace(/[$,\s]/g, "");
    const n = parseFloat(cleaned);
    if (Number.isNaN(n)) return NaN;
    return n;
  }

  function buildTable(rows) {
    const container = $("flatsRetailTable");
    if (!rows || !rows.length) {
      container.innerHTML =
        "<p class='empty-state'>No flats retail rates found in the database. Click “System Update (seed flats retail)” to load the default 1–13 oz tiers.</p>";
      return;
    }

    let html =
      "<table class='data-table system-edit-table'><thead><tr><th>Weight Not Over (oz.)</th><th class='num'>Retail</th></tr></thead><tbody>";
    for (const r of rows) {
      const w = r.weight_not_over_oz;
      const rate = r.rate_retail;
      html += "<tr>";
      html += `<td>${escapeHtml(fmtWeight(w))}</td>`;
      html += `<td class="num"><input class="system-money-input" inputmode="decimal" data-weight="${escapeHtml(
        String(w)
      )}" value="${escapeHtml(fmtRateInput(rate))}" /></td>`;
      html += "</tr>";
    }
    html += "</tbody></table>";
    container.innerHTML = html;
  }

  function setPresortRejectInput(v) {
    const el = $("presortRejectUnitCost");
    if (!el) return;
    if (v == null || v === "") {
      el.value = "";
      return;
    }
    const n = Number(v);
    el.value = Number.isNaN(n) ? "" : n.toFixed(2);
  }

  async function load() {
    showError("");
    showBanner("");
    const r = await fetch("/api/system/flats-retail");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load flats retail rates");
    buildTable(j.rows || []);
    setPresortRejectInput(j.presort_reject_unit_cost);
    if (j.empty) showBanner("Database has no flats retail rates yet. You can seed the defaults.");
  }

  function collectRowsFromUi() {
    const tbl = $("flatsRetailTable");
    const scope = tbl && tbl.querySelectorAll ? tbl : document;
    const inputs = Array.from(scope.querySelectorAll("input.system-money-input"));
    const out = [];
    for (const el of inputs) {
      const w = parseFloat(el.getAttribute("data-weight") || "");
      if (Number.isNaN(w)) continue;
      const v = parseMoneyInput(el.value);
      if (v !== null && Number.isNaN(v)) {
        el.focus();
        throw new Error("Invalid retail value (must be a number).");
      }
      if (v != null && v < 0) {
        el.focus();
        throw new Error("Retail must be non-negative.");
      }
      out.push({ weight_not_over_oz: w, rate_retail: v });
    }
    return out;
  }

  async function seedDefaults() {
    showError("");
    showBanner("Seeding defaults…");
    const r = await fetch("/api/system/flats-retail/seed", { method: "POST" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Seed failed");
    buildTable(j.rows || []);
    setPresortRejectInput(j.presort_reject_unit_cost);
    if (j.seeded) showBanner(`Seeded ${j.rows_inserted || 0} rows.`);
    else showBanner("Seed skipped (table already has rows).");
  }

  async function save() {
    showError("");
    showBanner("Saving…");
    const rows = collectRowsFromUi();
    const prEl = $("presortRejectUnitCost");
    let presort_reject_unit_cost = null;
    if (prEl) {
      const t = String(prEl.value || "").trim();
      if (t !== "") {
        const n = parseMoneyInput(t);
        if (n !== null && Number.isNaN(n)) {
          prEl.focus();
          throw new Error("Presort reject charge must be a number.");
        }
        if (n != null && n < 0) {
          prEl.focus();
          throw new Error("Presort reject charge must be non-negative.");
        }
        presort_reject_unit_cost = n;
      }
    }
    const body = { rows };
    if (presort_reject_unit_cost != null) body.presort_reject_unit_cost = presort_reject_unit_cost;
    const r = await fetch("/api/system/flats-retail", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Save failed");
    buildTable(j.rows || []);
    setPresortRejectInput(j.presort_reject_unit_cost);
    showBanner(`Saved ${j.rows_upserted || 0} flat rate row(s) and postage options.`);
  }

  function fmtRateDisplay(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (Number.isNaN(n)) return "—";
    return n.toFixed(3).replace(/\.?0+$/, (m) => (m === "." ? "" : m));
  }

  function fmtRateDisplayMoney(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (Number.isNaN(n)) return "—";
    return n.toFixed(2);
  }

  function formatQueriedMeta(j) {
    const parts = [];
    if (j.queried_at) {
      const d = new Date(j.queried_at);
      parts.push(`Queried at ${Number.isNaN(d.getTime()) ? j.queried_at : d.toLocaleString()}`);
    }
    if (j.as_of_date) parts.push(`as-of ${j.as_of_date}`);
    if (j.tariff_effective_date) parts.push(`tariff effective ${j.tariff_effective_date}`);
    else if (j.tariff_effective_date === null) parts.push("no tariff on file");
    return parts.join(" · ");
  }

  function buildFlatsRatesViewTable(rows) {
    const container = $("flatsRatesViewTable");
    if (!container) return;
    if (!rows || !rows.length) {
      container.innerHTML =
        "<p class='empty-state'>No flats rates found for today. Import a Notice 123 rate case first.</p>";
      return;
    }
    let html =
      "<table class='data-table'><thead><tr>" +
      "<th>Weight (oz)</th><th class='num'>Retail</th><th class='num'>5-Digit</th>" +
      "<th class='num'>3-Digit</th><th class='num'>AADC</th><th class='num'>Mixed ADC</th>" +
      "<th class='num'>Machinable Presort</th></tr></thead><tbody>";
    for (const r of rows) {
      html += "<tr>";
      html += `<td>${escapeHtml(fmtWeight(r.weight_not_over_oz))}</td>`;
      html += `<td class="num">${escapeHtml(fmtRateDisplayMoney(r.rate_retail))}</td>`;
      html += `<td class="num">${escapeHtml(fmtRateDisplay(r.rate_5digit))}</td>`;
      html += `<td class="num">${escapeHtml(fmtRateDisplay(r.rate_3digit))}</td>`;
      html += `<td class="num">${escapeHtml(fmtRateDisplay(r.rate_aadc))}</td>`;
      html += `<td class="num">${escapeHtml(fmtRateDisplay(r.rate_mixed_adc))}</td>`;
      html += `<td class="num">${escapeHtml(fmtRateDisplay(r.rate_machinable_pres))}</td>`;
      html += "</tr>";
    }
    html += "</tbody></table>";
    container.innerHTML = html;
  }

  function buildParcelRatesViewTable(matrix, flatRateItems) {
    const container = $("parcelRatesViewTable");
    if (!container) return;
    if ((!matrix || !matrix.length) && (!flatRateItems || !flatRateItems.length)) {
      container.innerHTML =
        "<p class='empty-state'>No Priority Mail rates found for today. Import a Notice 123 rate case first.</p>";
      return;
    }
    let html = "";
    if (matrix && matrix.length) {
      html +=
        "<table class='data-table'><thead><tr><th>Lb</th>" +
        [1, 2, 3, 4, 5, 6, 7, 8, 9].map((z) => `<th class='num'>Zone ${z}</th>`).join("") +
        "</tr></thead><tbody>";
      for (const row of matrix) {
        html += `<tr><td>${escapeHtml(String(row.lb))}</td>`;
        const zones = row.zones || {};
        for (let z = 1; z <= 9; z += 1) {
          const p = zones[String(z)];
          html += `<td class="num">${escapeHtml(p != null ? fmtRateDisplayMoney(p) : "—")}</td>`;
        }
        html += "</tr>";
      }
      html += "</tbody></table>";
    }
    if (flatRateItems && flatRateItems.length) {
      html +=
        "<h3 class='system-section-title' style='margin-top:1rem'>Flat-rate envelopes &amp; boxes</h3>" +
        "<table class='data-table'><thead><tr><th>Item</th><th class='num'>Price</th></tr></thead><tbody>";
      for (const item of flatRateItems) {
        html += "<tr>";
        html += `<td>${escapeHtml(item.label || "")}</td>`;
        html += `<td class="num">${escapeHtml(fmtRateDisplayMoney(item.price))}</td>`;
        html += "</tr>";
      }
      html += "</tbody></table>";
    }
    container.innerHTML = html;
  }

  $("btnSeedFlatsRetail").addEventListener("click", () => {
    seedDefaults().catch((e) => {
      showBanner("");
      showError(String(e.message || e));
    });
  });

  $("btnSaveFlatsRetail").addEventListener("click", () => {
    save().catch((e) => {
      showBanner("");
      showError(String(e.message || e));
    });
  });

  const btnNotice123 = $("btnUploadNotice123");
  if (btnNotice123) {
    btnNotice123.addEventListener("click", async () => {
      showError("");
      const effEl = $("notice123EffectiveDate");
      const fileEl = $("notice123ZipFile");
      const eff = effEl && effEl.value ? String(effEl.value).trim() : "";
      const file = fileEl && fileEl.files && fileEl.files[0] ? fileEl.files[0] : null;
      if (!eff) {
        showError("Choose an effective date.");
        return;
      }
      if (!file) {
        showError("Choose a Notice 123 .zip file.");
        return;
      }
      showBanner("Importing Notice 123 rate case…");
      const fd = new FormData();
      fd.append("effective_date", eff);
      fd.append("file", file, file.name);
      try {
        const r = await fetch("/api/import/notice123-rate-case", { method: "POST", body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "Import failed");
        const pmRows = (j.priority_mail && j.priority_mail.rows_imported) || 0;
        const flatRows = (j.flats && j.flats.rows_imported) || 0;
        showBanner(
          `Imported PM ${pmRows} row(s), flats ${flatRows} tier(s); effective ${j.effective_date || eff}.`
        );
        if (fileEl) fileEl.value = "";
      } catch (e) {
        showBanner("");
        showError(String(e.message || e));
      }
    });
  }

  const btnFlatsView = $("btnLoadFlatsRatesView");
  if (btnFlatsView) {
    btnFlatsView.addEventListener("click", async () => {
      showError("");
      const meta = $("flatsRatesMeta");
      const table = $("flatsRatesViewTable");
      if (table) table.innerHTML = "<p class='empty-state'>Loading…</p>";
      btnFlatsView.disabled = true;
      try {
        const r = await fetch("/api/system/rates/flats");
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "Failed to load flats rates");
        if (meta) {
          meta.textContent = formatQueriedMeta(j);
          meta.classList.remove("hidden");
        }
        buildFlatsRatesViewTable(j.rows || []);
      } catch (e) {
        if (table) table.innerHTML = `<p class='empty-state'>${escapeHtml(String(e.message || e))}</p>`;
      } finally {
        btnFlatsView.disabled = false;
      }
    });
  }

  const btnParcelView = $("btnLoadParcelRatesView");
  if (btnParcelView) {
    btnParcelView.addEventListener("click", async () => {
      showError("");
      const meta = $("parcelRatesMeta");
      const table = $("parcelRatesViewTable");
      if (table) table.innerHTML = "<p class='empty-state'>Loading…</p>";
      btnParcelView.disabled = true;
      try {
        const r = await fetch("/api/system/rates/parcels");
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "Failed to load parcel rates");
        if (meta) {
          meta.textContent = formatQueriedMeta(j);
          meta.classList.remove("hidden");
        }
        buildParcelRatesViewTable(j.matrix || [], j.flat_rate_items || []);
      } catch (e) {
        if (table) table.innerHTML = `<p class='empty-state'>${escapeHtml(String(e.message || e))}</p>`;
      } finally {
        btnParcelView.disabled = false;
      }
    });
  }

  const btnRawExport = $("btnImportRawExportCustomers");
  if (btnRawExport) {
    btnRawExport.addEventListener("click", async () => {
      showError("");
      const fileEl = $("rawExportCustomersFile");
      const file = fileEl && fileEl.files && fileEl.files[0] ? fileEl.files[0] : null;
      if (!file) {
        showError("Choose a Raw Export .xlsx file.");
        return;
      }
      showBanner("Importing customers from Raw Export…");
      const fd = new FormData();
      fd.append("file", file, file.name);
      try {
        const r = await fetch("/api/import/customers-raw-export", { method: "POST", body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "Import failed");
        let msg = `Imported ${j.rows_imported || 0} customer row(s).`;
        const w = j.warnings;
        if (w && w.length) {
          msg += ` ${w.length} warning(s); see server log or response for details. First: ${w[0]}`;
        }
        showBanner(msg);
        if (fileEl) fileEl.value = "";
      } catch (e) {
        showBanner("");
        showError(String(e.message || e));
      }
    });
  }

  function renderPricingTermsTable(revisions) {
    const container = $("pricingTermsTable");
    if (!container) return;
    if (!revisions || !revisions.length) {
      container.innerHTML = "<p class='empty-state'>No pricing terms stored yet.</p>";
      return;
    }
    let html =
      "<table class='data-table'><thead><tr>" +
      "<th>Effective date</th><th class='num'>Flats: customer disc.</th>" +
      "<th class='num'>Flats: EFD disc.</th><th class='num'>Parcels: customer disc.</th>" +
      "<th class='num'>Parcel fee / pc</th><th>Notes</th><th></th>" +
      "</tr></thead><tbody>";
    for (const t of revisions) {
      const canDelete = t.effective_date !== "1900-01-01";
      html += "<tr>";
      html += `<td>${escapeHtml(t.effective_date)}</td>`;
      html += `<td class='num'>${escapeHtml(fmtRateInput(t.flats_customer_discount))}</td>`;
      html += `<td class='num'>${escapeHtml(fmtRateInput(t.flats_efd_discount))}</td>`;
      html += `<td class='num'>${escapeHtml(fmtRateInput(t.parcel_customer_discount))}</td>`;
      html += `<td class='num'>${escapeHtml(fmtRateInput(t.parcel_fee_per_piece))}</td>`;
      html += `<td>${escapeHtml(t.notes || "")}</td>`;
      html += `<td>${
        canDelete
          ? `<button type='button' class='btn btn-run' data-del-terms='${escapeHtml(t.effective_date)}'>Delete</button>`
          : ""
      }</td>`;
      html += "</tr>";
    }
    html += "</tbody></table>";
    container.innerHTML = html;
    for (const btn of container.querySelectorAll("button[data-del-terms]")) {
      btn.addEventListener("click", async () => {
        const eff = btn.getAttribute("data-del-terms");
        if (!window.confirm(`Delete pricing terms revision ${eff}?`)) return;
        showError("");
        try {
          const r = await fetch(
            `/api/system/pricing-terms?effective_date=${encodeURIComponent(eff)}`,
            { method: "DELETE" }
          );
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || "Delete failed");
          applyPricingTerms(j);
          showBanner(`Deleted pricing terms revision ${eff}.`);
        } catch (e) {
          showError(String(e.message || e));
        }
      });
    }
  }

  function applyPricingTerms(j) {
    const cur = j.current || {};
    const set = (id, v) => {
      const el = $(id);
      if (el) el.value = fmtRateInput(v);
    };
    set("pricingFlatsCustomerDiscount", cur.flats_customer_discount);
    set("pricingFlatsEfdDiscount", cur.flats_efd_discount);
    set("pricingParcelCustomerDiscount", cur.parcel_customer_discount);
    set("pricingParcelFeePerPiece", cur.parcel_fee_per_piece);
    const dateEl = $("pricingTermsEffectiveDate");
    if (dateEl && !dateEl.value) dateEl.value = new Date().toISOString().slice(0, 10);
    renderPricingTermsTable(j.revisions || []);
  }

  async function loadPricingTerms() {
    const r = await fetch("/api/system/pricing-terms");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load pricing terms");
    applyPricingTerms(j);
  }

  async function savePricingTerms() {
    showError("");
    const dateEl = $("pricingTermsEffectiveDate");
    const eff = dateEl ? String(dateEl.value || "").trim() : "";
    if (!eff) throw new Error("Choose an effective date for these pricing terms.");
    const fields = {
      flats_customer_discount: "pricingFlatsCustomerDiscount",
      flats_efd_discount: "pricingFlatsEfdDiscount",
      parcel_customer_discount: "pricingParcelCustomerDiscount",
      parcel_fee_per_piece: "pricingParcelFeePerPiece",
    };
    const body = { effective_date: eff };
    for (const [key, id] of Object.entries(fields)) {
      const el = $(id);
      const n = parseMoneyInput(el ? el.value : "");
      if (n == null || Number.isNaN(n)) {
        if (el) el.focus();
        throw new Error("All pricing terms must be numbers.");
      }
      if (n < 0) {
        if (el) el.focus();
        throw new Error("Pricing terms must be non-negative.");
      }
      body[key] = n;
    }
    const notesEl = $("pricingTermsNotes");
    if (notesEl && String(notesEl.value || "").trim()) body.notes = String(notesEl.value).trim();
    showBanner("Saving pricing terms…");
    const r = await fetch("/api/system/pricing-terms", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Save failed");
    applyPricingTerms(j);
    showBanner(`Saved pricing terms effective ${eff}.`);
  }

  const btnSaveTerms = $("btnSavePricingTerms");
  if (btnSaveTerms) {
    btnSaveTerms.addEventListener("click", () => {
      savePricingTerms().catch((e) => {
        showBanner("");
        showError(String(e.message || e));
      });
    });
  }

  load().catch((e) => showError(String(e.message || e)));
  loadPricingTerms().catch((e) => showError(String(e.message || e)));
})();

(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const tbl = $("ws3ProfilesTable");
  const btnLoadWs3 = $("btnLoadWs3Profiles");
  const ws3Dl = $("systemWs3RollupList");
  if (!tbl || !btnLoadWs3) return;

  let state = { profiles: [], assignment_accounts: [] };

  function populateWs3RollupDatalist() {
    const dl = ws3Dl;
    const accts = state.assignment_accounts || [];
    if (!dl) return;
    let opts = "";
    for (const a of accts) {
      const n = a.customer_number;
      const nm = (a.customer_name || "").trim() || `Account ${n}`;
      const kind = a.kind === "parent" ? "Parent" : "Main";
      opts += `<option value="${escapeHtml(String(n))}">${escapeHtml(nm)} (${n}) · ${kind}</option>`;
    }
    dl.innerHTML = opts;
  }

  function render() {
    if (!state.profiles || !state.profiles.length) {
      tbl.innerHTML =
        "<p class='empty-state'>No WS3 profiles yet. Import a WS3_FCFL_CustomerMailDetail file into input/.</p>";
      return;
    }
    populateWs3RollupDatalist();
    let html =
      "<table class='data-table system-edit-table'><thead><tr><th>Profile</th><th>Rollup account</th><th class='num'>Reject fee</th><th></th></tr></thead><tbody>";
    for (const row of state.profiles) {
      const pid = row.id;
      const sel =
        row.parent_customer_number != null ? String(row.parent_customer_number).trim() : "";
      const fee =
        row.reject_fee != null && row.reject_fee !== ""
          ? String(row.reject_fee)
          : "";
      html += "<tr>";
      html += `<td>${escapeHtml(row.profile_name || "")}</td>`;
      html += `<td><input type="text" class="ws3-parent-input input-ws3-rollup" data-profile-id="${pid}" list="systemWs3RollupList" value="${escapeHtml(
        sel
      )}" autocomplete="off" placeholder="Acct # …" /></td>`;
      html += `<td class="num"><input type="text" class="system-money-input ws3-reject-fee" inputmode="decimal" placeholder="—" data-profile-id="${pid}" value="${escapeHtml(
        fee
      )}" /></td>`;
      html += `<td><button type="button" class="btn btn-primary ws3-save" data-profile-id="${pid}">Save</button></td>`;
      html += "</tr>";
    }
    html += "</tbody></table>";
    tbl.innerHTML = html;

    tbl.querySelectorAll("button.ws3-save").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = parseInt(btn.getAttribute("data-profile-id"), 10);
        const inp = tbl.querySelector(`input.ws3-parent-input[data-profile-id="${id}"]`);
        const feeIn = tbl.querySelector(`input.ws3-reject-fee[data-profile-id="${id}"]`);
        const raw = inp ? String(inp.value || "").trim() : "";
        let parent_customer_number = null;
        if (raw !== "") {
          parent_customer_number = parseInt(raw, 10);
          if (Number.isNaN(parent_customer_number)) {
            alert("Rollup account must be a numeric customer number (pick from suggestions).");
            if (inp) inp.focus();
            return;
          }
        }
        let reject_fee = null;
        if (feeIn) {
          const t = String(feeIn.value || "").trim();
          if (t !== "") {
            const n = parseFloat(t.replace(/[$,\s]/g, ""));
            if (Number.isNaN(n) || n < 0) {
              alert("Reject fee must be a non-negative number.");
              feeIn.focus();
              return;
            }
            reject_fee = n;
          }
        }
        btn.disabled = true;
        try {
          const r = await fetch("/api/system/ws3-profiles", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ profile_id: id, parent_customer_number, reject_fee }),
          });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || "Save failed");
          state.profiles = j.profiles || state.profiles;
          render();
        } catch (e) {
          alert(String(e.message || e));
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  async function loadWs3() {
    const r = await fetch("/api/system/ws3-profiles");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load WS3 profiles");
    state.profiles = j.profiles || [];
    state.assignment_accounts = j.assignment_accounts || [];
    render();
  }

  tbl.innerHTML =
    "<p class='empty-state'>Click “Load WS3 assignments” above. (Assignment lists are large — loaded on demand.)</p>";

  btnLoadWs3.addEventListener("click", async () => {
    btnLoadWs3.disabled = true;
    tbl.innerHTML = "<p class='empty-state'>Loading…</p>";
    try {
      await loadWs3();
      const hw = $("ws3ProfilesLoadHint");
      if (hw) hw.textContent = "";
    } catch (e) {
      tbl.innerHTML = `<p class='empty-state'>${escapeHtml(String(e.message || e))}</p>`;
    } finally {
      btnLoadWs3.disabled = false;
    }
  });
})();

(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function moneyCell(n) {
    const v = Number(n || 0);
    if (Number.isNaN(v)) return "0.00";
    return v.toFixed(2);
  }

  const tbl = $("unmatchedAccountsTable");
  const btnUn = $("btnLoadUnmatched");
  const unmatchedDl = $("systemUnmatchedParentList");
  if (!tbl || !btnUn) return;

  let state = { unmatched: [], parent_options: [] };

  function populateUnmatchedParentDatalist() {
    const dl = unmatchedDl;
    const rows = (state.parent_options || []).filter((r) =>
      ["parent", "standalone"].includes(r.kind)
    );
    if (!dl) return;
    let opts = '<option value=""></option>';
    for (const r of rows) {
      const n = r.customer_number;
      const nm = (r.customer_name || "").trim() || `Account ${n}`;
      const tag = r.kind === "parent" ? "Parent" : "Standalone";
      opts += `<option value="${escapeHtml(String(n))}">${escapeHtml(nm)} (${n}) · ${tag}</option>`;
    }
    dl.innerHTML = opts;
  }

  function render() {
    const rows = state.unmatched || [];
    if (!rows.length) {
      tbl.innerHTML = "<p class='empty-state'>No unmatched accounts found.</p>";
      return;
    }

    populateUnmatchedParentDatalist();

    let html =
      "<table class='data-table system-edit-table'><thead><tr>" +
      "<th>Account #</th>" +
      "<th>Sources</th>" +
      "<th class='num'>Postage pieces</th>" +
      "<th class='num'>Postage cost</th>" +
      "<th class='num'>Parcel pieces</th>" +
      "<th class='num'>Parcel cost</th>" +
      "<th>Customer name</th>" +
      "<th>Parent</th>" +
      "<th></th>" +
      "</tr></thead><tbody>";

    for (const r of rows) {
      const code = r.account_code;
      html += "<tr>";
      html += `<td class="num">${escapeHtml(String(code))}</td>`;
      html += `<td>${escapeHtml(r.sources || "")}</td>`;
      html += `<td class="num">${escapeHtml(String(r.postage_pieces || 0))}</td>`;
      html += `<td class="num">${escapeHtml(moneyCell(r.postage_cost))}</td>`;
      html += `<td class="num">${escapeHtml(String(r.parcel_pieces || 0))}</td>`;
      html += `<td class="num">${escapeHtml(moneyCell(r.parcel_cost))}</td>`;
      html += `<td><input type="text" class="unmatched-name" data-account="${escapeHtml(
        String(code)
      )}" placeholder="Enter name…" /></td>`;
      html += `<td><input type="text" class="unmatched-parent-input" data-account="${escapeHtml(
        String(code)
      )}" list="systemUnmatchedParentList" placeholder="Parent acct #" autocomplete="off" /></td>`;
      html += `<td><button type="button" class="btn btn-primary unmatched-save" data-account="${escapeHtml(
        String(code)
      )}">Save</button></td>`;
      html += "</tr>";
    }

    html += "</tbody></table>";
    tbl.innerHTML = html;

    tbl.querySelectorAll("button.unmatched-save").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const code = parseInt(btn.getAttribute("data-account") || "", 10);
        const nameEl = tbl.querySelector(`input.unmatched-name[data-account="${code}"]`);
        const parentEl = tbl.querySelector(`input.unmatched-parent-input[data-account="${code}"]`);
        const customer_name = String((nameEl && nameEl.value) || "").trim();
        const rawParent = parentEl ? String(parentEl.value || "").trim() : "";
        let parent_number = null;
        if (rawParent !== "") {
          parent_number = parseInt(rawParent, 10);
          if (Number.isNaN(parent_number)) {
            alert("Parent must be a numeric account number (use suggestions).");
            if (parentEl) parentEl.focus();
            btn.disabled = false;
            return;
          }
        }

        if (!customer_name) {
          alert("Customer name is required.");
          if (nameEl) nameEl.focus();
          return;
        }

        btn.disabled = true;
        try {
          const r = await fetch("/api/system/unmatched-accounts/assign", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ customer_number: code, customer_name, parent_number }),
          });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || "Save failed");
          state.unmatched = j.unmatched || [];
          state.parent_options = j.parent_options || state.parent_options;
          render();
        } catch (e) {
          alert(String(e.message || e));
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  async function loadUnmatched() {
    const r = await fetch("/api/system/unmatched-accounts");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load unmatched accounts");
    state.unmatched = j.unmatched || [];
    state.parent_options = j.parent_options || [];
    render();
  }

  tbl.innerHTML =
    "<p class=\"empty-state\">Click “Load unmatched accounts” above if you need this list. Scanning billing and postage data waits until then.</p>";

  btnUn.addEventListener("click", async () => {
    btnUn.disabled = true;
    tbl.innerHTML = "<p class=\"empty-state\">Loading…</p>";
    try {
      await loadUnmatched();
      const hu = $("unmatchedLoadHint");
      if (hu) hu.textContent = "";
    } catch (e) {
      tbl.innerHTML = `<p class="empty-state">${escapeHtml(String(e.message || e))}</p>`;
    } finally {
      btnUn.disabled = false;
    }
  });

  // --- Backup & Restore ------------------------------------------------------
  const btnDownloadBackup = $("btnDownloadBackup");
  if (btnDownloadBackup) {
    btnDownloadBackup.addEventListener("click", () => {
      showError("");
      const includeArchives = $("backupIncludeArchives");
      const flag = includeArchives && includeArchives.checked ? "1" : "0";
      showBanner("Preparing backup… the download will start shortly.");
      // Let the browser handle the download (backups can be large).
      window.location.href = `/api/system/backup?include_archives=${flag}`;
    });
  }

  const btnRestoreBackup = $("btnRestoreBackup");
  if (btnRestoreBackup) {
    btnRestoreBackup.addEventListener("click", async () => {
      showError("");
      const fileEl = $("restoreFile");
      const file = fileEl && fileEl.files && fileEl.files[0] ? fileEl.files[0] : null;
      if (!file) {
        showError("Choose a backup .zip file to restore.");
        return;
      }
      const ok = window.confirm(
        "Restore from this backup? It will replace the current database and report " +
          "data when the app restarts. The current database is saved as a " +
          "postage.db.bak-restore-* file first."
      );
      if (!ok) return;
      showBanner("Uploading and validating backup…");
      const fd = new FormData();
      fd.append("file", file, file.name);
      try {
        const r = await fetch("/api/system/restore", { method: "POST", body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "Restore failed");
        showBanner(
          "Backup staged successfully. Restart the app (or the Windows service) to apply the restored data."
        );
        if (fileEl) fileEl.value = "";
      } catch (e) {
        showBanner("");
        showError(String(e.message || e));
      }
    });
  }
})();

