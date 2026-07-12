(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  function showBanner(msg) {
    const el = $("banner");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function showError(msg) {
    const el = $("error");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function selectedDays() {
    return Array.from(document.querySelectorAll(".dow-cb"))
      .filter((cb) => cb.checked)
      .map((cb) => cb.value);
  }

  function setDays(days) {
    document.querySelectorAll(".dow-cb").forEach((cb) => {
      cb.checked = days.includes(cb.value);
    });
  }

  function parseRecipients() {
    return $("recipients")
      .value.split(/[\n,;]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function selectedGroupIds() {
    return Array.from($("recipientGroups").selectedOptions).map((o) =>
      parseInt(o.value, 10)
    );
  }

  async function loadGroups() {
    try {
      const r = await fetch("/api/scheduler/recipient-groups");
      const j = await r.json();
      if (!r.ok) return;
      const sel = $("recipientGroups");
      sel.textContent = "";
      for (const g of j.groups || []) {
        const opt = document.createElement("option");
        opt.value = g.id;
        opt.textContent = `${g.group_name} (${g.member_count})`;
        sel.appendChild(opt);
      }
    } catch (e) {
      /* non-fatal */
    }
  }

  async function checkFolder() {
    const path = $("attachmentFolder").value.trim();
    const box = $("folderPreview");
    if (!path) {
      showError("Enter or pick a folder first.");
      return;
    }
    showError("");
    try {
      const r = await fetch(
        "/api/scheduler/folder-files?path=" + encodeURIComponent(path)
      );
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Could not read folder");
      box.classList.remove("hidden");
      if (!j.count) {
        box.textContent = "No files in this folder right now.";
        return;
      }
      box.textContent =
        `${j.count} file(s) ready to send:\n` +
        j.files.map((f) => "  " + f.name).join("\n");
    } catch (e) {
      box.classList.add("hidden");
      showError(String(e.message || e));
    }
  }

  function collect() {
    return {
      name: $("reportName").value.trim(),
      enabled: true,
      schedule_type: "weekly",
      scheduled_time: $("scheduledTime").value,
      days_of_week_csv: selectedDays().join(","),
      attachment_folder: $("attachmentFolder").value.trim(),
      post_send_action: "archive",
      archive_subdir: "Sent",
      subject_template: $("subjectTemplate").value,
      body_template: $("bodyTemplate").value,
      recipients: parseRecipients(),
      recipient_group_ids: selectedGroupIds(),
      data_readiness_mode: "any_required",
    };
  }

  function validate(payload) {
    if (!payload.name) return "Report name is required.";
    if (!payload.attachment_folder) return "A folder to watch is required.";
    if (!payload.scheduled_time) return "A time of day is required.";
    if (!payload.days_of_week_csv) return "Pick at least one day of the week.";
    if (!payload.recipients.length && !payload.recipient_group_ids.length)
      return "Add at least one recipient or recipient group.";
    return null;
  }

  $("btnBrowseFolder").addEventListener("click", () =>
    window.openFolderPicker($("attachmentFolder"))
  );
  $("btnCheckFolder").addEventListener("click", checkFolder);
  $("btnWeekdays").addEventListener("click", () =>
    setDays(["Mon", "Tue", "Wed", "Thu", "Fri"])
  );
  $("btnEveryDay").addEventListener("click", () =>
    setDays(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
  );

  $("btnSave").addEventListener("click", async () => {
    showError("");
    const payload = collect();
    const err = validate(payload);
    if (err) {
      showError(err);
      return;
    }
    showBanner("Creating report\u2026");
    try {
      const r = await fetch("/api/scheduler/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Create failed");
      showBanner("Report created. Redirecting\u2026");
      window.location.href = "/scheduler/jobs";
    } catch (e) {
      showBanner("");
      showError(String(e.message || e));
    }
  });

  loadGroups();
})();
