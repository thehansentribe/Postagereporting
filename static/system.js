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

  async function load() {
    showError("");
    showBanner("");
    const r = await fetch("/api/system/flats-retail");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load flats retail rates");
    buildTable(j.rows || []);
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
    if (j.seeded) showBanner(`Seeded ${j.rows_inserted || 0} rows.`);
    else showBanner("Seed skipped (table already has rows).");
  }

  async function save() {
    showError("");
    showBanner("Saving…");
    const rows = collectRowsFromUi();
    const r = await fetch("/api/system/flats-retail", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Save failed");
    buildTable(j.rows || []);
    showBanner(`Saved ${j.rows_upserted || 0} rows.`);
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

