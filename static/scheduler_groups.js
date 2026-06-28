(function () {
  const $ = (id) => document.getElementById(id);
  let editId = null;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function parseMembers() {
    return $("groupMembers").value
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  async function load() {
    const r = await fetch("/api/scheduler/recipient-groups");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Load failed");
    let html =
      "<table class='data-table'><thead><tr><th>Name</th><th>Members</th><th></th></tr></thead><tbody>";
    for (const g of j.groups || []) {
      html += `<tr>
        <td>${escapeHtml(g.group_name)}</td>
        <td>${g.member_count}</td>
        <td>
          <button type="button" class="btn btn-run btn-edit" data-id="${g.id}">Edit</button>
          <button type="button" class="btn btn-run btn-del" data-id="${g.id}">Delete</button>
        </td>
      </tr>`;
    }
    html += "</tbody></table>";
    $("groupsTable").innerHTML = html;

    document.querySelectorAll(".btn-edit").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        const rr = await fetch("/api/scheduler/recipient-groups");
        const jj = await rr.json();
        const g = (jj.groups || []).find((x) => String(x.id) === String(id));
        if (!g) return;
        editId = parseInt(id, 10);
        $("groupName").value = g.group_name;
        $("groupMembers").value = (g.members || []).join("\n");
        $("groupFormTitle").textContent = "Edit group";
      });
    });

    document.querySelectorAll(".btn-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        if (!confirm("Delete this group?")) return;
        const dr = await fetch(`/api/scheduler/recipient-groups/${id}`, { method: "DELETE" });
        const dj = await dr.json();
        if (!dr.ok) alert(dj.error || "Delete failed");
        else if (dj.referenced_by_jobs > 0) {
          alert(`Deleted. Was referenced by ${dj.referenced_by_jobs} job(s).`);
        }
        load().catch(() => {});
      });
    });
  }

  $("btnNewGroup").addEventListener("click", () => {
    editId = null;
    $("groupName").value = "";
    $("groupMembers").value = "";
    $("groupFormTitle").textContent = "New group";
  });

  $("btnSaveGroup").addEventListener("click", async () => {
    const body = { group_name: $("groupName").value, members: parseMembers() };
    const url = editId
      ? `/api/scheduler/recipient-groups/${editId}`
      : "/api/scheduler/recipient-groups";
    const method = editId ? "PUT" : "POST";
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Save failed");
    editId = j.id;
    $("groupFormTitle").textContent = "Edit group";
    load();
  });

  load().catch((e) => {
    $("groupsError").textContent = String(e.message || e);
    $("groupsError").classList.remove("hidden");
  });
})();
