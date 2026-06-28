(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  let allJobs = [];

  function scheduleSummary(j) {
    const st = j.schedule_type;
    if (st === "daily") return `Daily ${j.scheduled_time || ""}`;
    if (st === "weekly") return `Weekly ${j.days_of_week_csv || ""} ${j.scheduled_time || ""}`;
    if (st === "monthly") return `Monthly day ${j.day_of_month || ""} ${j.scheduled_time || ""}`;
    if (st === "one-time") return `Once ${j.one_time_at || ""}`;
    if (st === "data-only") return "Data only";
    return st;
  }

  function render(filter) {
    const q = (filter || "").trim().toLowerCase();
    const rows = allJobs.filter((j) => !q || j.name.toLowerCase().includes(q));
    let html =
      "<table class='data-table'><thead><tr><th><input type='checkbox' id='chkAll' /></th><th>Name</th><th>Schedule</th><th>Enabled</th><th></th></tr></thead><tbody>";
    for (const j of rows) {
      html += `<tr>
        <td><input type="checkbox" class="job-chk" data-id="${j.id}" /></td>
        <td><a href="/scheduler/jobs/${j.id}">${escapeHtml(j.name)}</a></td>
        <td>${escapeHtml(scheduleSummary(j))}</td>
        <td>${j.enabled ? "Yes" : "No"}</td>
        <td><button type="button" class="btn btn-run btn-toggle" data-id="${j.id}" data-enabled="${j.enabled ? "1" : "0"}">${j.enabled ? "Disable" : "Enable"}</button></td>
      </tr>`;
    }
    html += "</tbody></table>";
    $("jobsTable").innerHTML = html;

    const chkAll = document.getElementById("chkAll");
    if (chkAll) {
      chkAll.addEventListener("change", () => {
        document.querySelectorAll(".job-chk").forEach((c) => {
          c.checked = chkAll.checked;
        });
      });
    }

    document.querySelectorAll(".btn-toggle").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        const en = btn.getAttribute("data-enabled") === "1";
        await fetch(`/api/scheduler/jobs/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !en }),
        });
        load();
      });
    });
  }

  async function bulkSet(enabled) {
    const ids = Array.from(document.querySelectorAll(".job-chk:checked")).map((c) =>
      c.getAttribute("data-id")
    );
    for (const id of ids) {
      await fetch(`/api/scheduler/jobs/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    }
    load();
  }

  async function load() {
    const r = await fetch("/api/scheduler/jobs");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load jobs");
    allJobs = j.jobs || [];
    render($("jobSearch").value);
  }

  $("jobSearch").addEventListener("input", () => render($("jobSearch").value));
  $("btnBulkEnable").addEventListener("click", () => bulkSet(true));
  $("btnBulkDisable").addEventListener("click", () => bulkSet(false));

  load().catch((e) => {
    $("jobsError").textContent = String(e.message || e);
    $("jobsError").classList.remove("hidden");
  });
})();
