(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function query() {
    const p = new URLSearchParams();
    if ($("logStart").value) p.set("start_date", $("logStart").value);
    if ($("logEnd").value) p.set("end_date", $("logEnd").value);
    if ($("logStatus").value) p.set("status", $("logStatus").value);
    if ($("logSearch").value) p.set("q", $("logSearch").value);
    return p;
  }

  async function load() {
    const r = await fetch("/api/scheduler/execution-log?" + query().toString());
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Load failed");
    let html =
      "<table class='data-table'><thead><tr><th>Time</th><th>Job</th><th>Status</th><th>BaseName</th><th>Recipients</th><th></th></tr></thead><tbody>";
    for (const row of j.rows || []) {
      html += `<tr>
        <td>${escapeHtml(row.logged_at || "")}</td>
        <td>${escapeHtml(row.job_name || "")}</td>
        <td>${escapeHtml(row.status || "")}</td>
        <td>${escapeHtml(row.base_name || "")}</td>
        <td>${row.recipient_count != null ? row.recipient_count : ""}</td>
        <td><button type="button" class="btn btn-run btn-detail" data-id="${row.id}">Details</button></td>
      </tr>`;
    }
    html += "</tbody></table>";
    $("logTable").innerHTML = html;
    document.querySelectorAll(".btn-detail").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        const dr = await fetch(`/api/scheduler/execution-log/${id}`);
        const dj = await dr.json();
        $("logDetail").classList.remove("hidden");
        $("logDetail").textContent = JSON.stringify(dj, null, 2);
      });
    });
  }

  $("btnLoadLog").addEventListener("click", () => load().catch((e) => alert(e.message)));
  $("btnExportCsv").addEventListener("click", (ev) => {
    ev.preventDefault();
    window.location.href = "/api/scheduler/execution-log.csv?" + query().toString();
  });

  const d = new Date();
  $("logEnd").value = d.toISOString().slice(0, 10);
  d.setDate(d.getDate() - 7);
  $("logStart").value = d.toISOString().slice(0, 10);
  load().catch(() => {});
})();
