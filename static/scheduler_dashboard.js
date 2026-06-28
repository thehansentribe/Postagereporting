(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function statusLabel(st) {
    const map = {
      pending: "Pending",
      waiting: "Waiting for Data",
      ready: "Ready",
      sent: "Sent",
      expired: "Expired / Failed",
    };
    return map[st] || st;
  }

  async function load() {
    const r = await fetch("/api/scheduler/dashboard");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Dashboard load failed");

    $("summaryCards").innerHTML = `
      <div class="scheduler-card"><span class="scheduler-card-label">Active jobs</span><strong>${j.total_active_jobs || 0}</strong></div>
      <div class="scheduler-card"><span class="scheduler-card-label">Pending today</span><strong>${j.pending_today || 0}</strong></div>
      <div class="scheduler-card"><span class="scheduler-card-label">Completed today</span><strong>${j.completed_today || 0}</strong></div>
      <div class="scheduler-card"><span class="scheduler-card-label">Failed / waiting</span><strong>${j.failed_waiting_today || 0}</strong></div>`;

    const rows = j.timeline || [];
    let html =
      "<table class='data-table'><thead><tr><th>Job</th><th>Schedule</th><th>Time</th><th>Status</th><th>Details</th><th></th></tr></thead><tbody>";
    if (!rows.length) {
      html += "<tr><td colspan='6' class='empty-state'>No jobs scheduled for today.</td></tr>";
    } else {
      for (const row of rows) {
        let details = "";
        if (row.status === "waiting" && row.missing_files && row.missing_files.length) {
          details = "Missing: " + row.missing_files.map(escapeHtml).join(", ");
        } else if (row.status === "sent") {
          details = `BaseName: ${escapeHtml(row.base_name || "")}`;
        }
        html += `<tr>
          <td>${escapeHtml(row.job_name)}</td>
          <td>${escapeHtml(row.schedule_type || "")}</td>
          <td>${escapeHtml(row.scheduled_time || "—")}</td>
          <td>${escapeHtml(statusLabel(row.status))}</td>
          <td class="scheduler-details">${details}</td>
          <td>
            <button type="button" class="btn btn-run btn-run-now" data-id="${row.job_id}">Run Now</button>
            <a class="btn" href="/scheduler/jobs/${row.job_id}">Edit</a>
          </td>
        </tr>`;
      }
    }
    html += "</tbody></table>";
    $("timelineTable").innerHTML = html;

    document.querySelectorAll(".btn-run-now").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        if (!confirm("Send this job now?")) return;
        const rr = await fetch(`/api/scheduler/jobs/${id}/run-now`, { method: "POST" });
        const jj = await rr.json();
        if (!rr.ok) alert(jj.error || "Send failed");
        else {
          alert(`Sent. BaseName: ${jj.baseName || ""}`);
          load().catch(() => {});
        }
      });
    });
  }

  load().catch((e) => {
    $("dashError").textContent = String(e.message || e);
    $("dashError").classList.remove("hidden");
  });

  setInterval(() => load().catch(() => {}), 60000);
})();
