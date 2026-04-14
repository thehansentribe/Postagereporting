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
    const inputs = Array.from(document.querySelectorAll("input.system-money-input"));
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

  load().catch((e) => showError(String(e.message || e)));
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
  if (!tbl) return;

  let state = { profiles: [], assignment_accounts: [] };

  function buildAssignmentOptions(selected) {
    const accts = state.assignment_accounts || [];
    const parents = accts.filter((a) => a.kind === "parent");
    const mains = accts.filter((a) => a.kind === "main");
    let h = '<option value="">— None —</option>';
    function opts(rows) {
      let s = "";
      for (const p of rows) {
        const n = p.customer_number;
        const sel = n === selected ? " selected" : "";
        s += `<option value="${n}"${sel}>${escapeHtml(p.customer_name || "")} (${n})</option>`;
      }
      return s;
    }
    if (parents.length) {
      h += '<optgroup label="Parent accounts (have child accounts)">';
      h += opts(parents);
      h += "</optgroup>";
    }
    if (mains.length) {
      h += '<optgroup label="Main accounts (no child accounts)">';
      h += opts(mains);
      h += "</optgroup>";
    }
    return h;
  }

  function render() {
    if (!state.profiles || !state.profiles.length) {
      tbl.innerHTML =
        "<p class='empty-state'>No WS3 profiles yet. Import a WS3_FCFL_CustomerMailDetail file into input/.</p>";
      return;
    }
    let html =
      "<table class='data-table system-edit-table'><thead><tr><th>Profile</th><th>Rollup account</th><th class='num'>Reject fee</th><th></th></tr></thead><tbody>";
    for (const row of state.profiles) {
      const pid = row.id;
      const sel = row.parent_customer_number != null ? row.parent_customer_number : "";
      const fee =
        row.reject_fee != null && row.reject_fee !== ""
          ? String(row.reject_fee)
          : "";
      html += "<tr>";
      html += `<td>${escapeHtml(row.profile_name || "")}</td>`;
      html += `<td><select class="ws3-parent-select" data-profile-id="${pid}">${buildAssignmentOptions(
        sel
      )}</select></td>`;
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
        const sel = tbl.querySelector(`select[data-profile-id="${id}"]`);
        const feeIn = tbl.querySelector(`input.ws3-reject-fee[data-profile-id="${id}"]`);
        const raw = sel ? sel.value : "";
        const parent_customer_number = raw === "" ? null : parseInt(raw, 10);
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

  loadWs3().catch((e) => {
    tbl.innerHTML = `<p class="empty-state">${escapeHtml(String(e.message || e))}</p>`;
  });
})();

