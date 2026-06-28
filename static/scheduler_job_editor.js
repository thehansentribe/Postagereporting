(function () {
  const $ = (id) => document.getElementById(id);
  const jobId = window.SCHEDULER_JOB_ID;
  let lastFocus = null;
  let groupIds = [];

  const DOW_VALUES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const DOW_ALIASES = {
    mon: "Mon",
    monday: "Mon",
    tue: "Tue",
    tues: "Tue",
    tuesday: "Tue",
    wed: "Wed",
    wednesday: "Wed",
    thu: "Thu",
    thur: "Thu",
    thurs: "Thu",
    thursday: "Thu",
    fri: "Fri",
    friday: "Fri",
    sat: "Sat",
    saturday: "Sat",
    sun: "Sun",
    sunday: "Sun",
  };

  document.querySelectorAll("[data-token-target]").forEach((el) => {
    el.addEventListener("focus", () => {
      lastFocus = el;
    });
  });

  document.querySelectorAll(".token-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tok = btn.getAttribute("data-token");
      const el = lastFocus || $("subjectTemplate");
      if (!el) return;
      const start = el.selectionStart ?? el.value.length;
      const end = el.selectionEnd ?? el.value.length;
      el.value = el.value.slice(0, start) + tok + el.value.slice(end);
      el.focus();
    });
  });

  function normalizeDowToken(raw) {
    const t = String(raw || "").trim().toLowerCase();
    if (!t) return null;
    if (DOW_ALIASES[t]) return DOW_ALIASES[t];
    const cap = t.charAt(0).toUpperCase() + t.slice(1, 3).toLowerCase();
    return DOW_VALUES.includes(cap) ? cap : null;
  }

  function collectDaysOfWeek() {
    const checked = Array.from(document.querySelectorAll(".dow-cb:checked"))
      .map((cb) => cb.value)
      .filter((v) => DOW_VALUES.includes(v));
    return checked.length ? checked.join(",") : null;
  }

  function loadDaysOfWeek(csv) {
    const set = new Set();
    String(csv || "")
      .split(",")
      .forEach((part) => {
        const n = normalizeDowToken(part);
        if (n) set.add(n);
      });
    document.querySelectorAll(".dow-cb").forEach((cb) => {
      cb.checked = set.has(cb.value);
    });
  }

  function formatOneTimeForDb(datetimeLocal) {
    if (!datetimeLocal) return null;
    const v = String(datetimeLocal).trim();
    if (!v) return null;
    const normalized = v.replace("T", " ");
    if (/:\d{2}:\d{2}$/.test(normalized)) return normalized;
    if (/:\d{2}$/.test(normalized)) return `${normalized}:00`;
    return normalized;
  }

  function parseOneTimeToLocal(dbValue) {
    if (!dbValue) return "";
    const s = String(dbValue).trim().replace(" ", "T");
    return s.slice(0, 16);
  }

  function timeFromDatetimeLocal(datetimeLocal) {
    if (!datetimeLocal) return null;
    const m = String(datetimeLocal).match(/T(\d{2}:\d{2})/);
    return m ? m[1] : null;
  }

  function updateScheduleVisibility() {
    const oneTime = $("oneTimeReport").checked;
    const type = $("scheduleType").value;
    const untilCanceled = $("effectiveUntilCanceled").checked;

    $("oneTimeFields").classList.toggle("hidden", !oneTime);
    $("repeatScheduleFields").classList.toggle("hidden", oneTime);
    $("weeklyFields").classList.toggle("hidden", oneTime || type !== "weekly");
    $("monthlyFields").classList.toggle("hidden", oneTime || type !== "monthly");

    const showTime = !oneTime && type !== "data-only";
    $("timeFieldWrap").classList.toggle("hidden", !showTime);

    $("effectiveEndWrap").classList.toggle("hidden", untilCanceled);
    const endInput = $("effectiveEnd");
    if (untilCanceled) {
      endInput.value = "";
      endInput.disabled = true;
    } else {
      endInput.disabled = false;
    }
  }

  function bindScheduleControls() {
    $("oneTimeReport").addEventListener("change", updateScheduleVisibility);
    $("scheduleType").addEventListener("change", updateScheduleVisibility);
    $("effectiveUntilCanceled").addEventListener("change", updateScheduleVisibility);
  }

  function collectScheduleFields() {
    const oneTime = $("oneTimeReport").checked;
    if (oneTime) {
      const ot = $("oneTimeAt").value;
      return {
        schedule_type: "one-time",
        scheduled_time: timeFromDatetimeLocal(ot),
        days_of_week_csv: null,
        day_of_month: null,
        one_time_at: formatOneTimeForDb(ot),
      };
    }
    const type = $("scheduleType").value;
    return {
      schedule_type: type,
      scheduled_time: type === "data-only" ? null : $("scheduledTime").value || null,
      days_of_week_csv: type === "weekly" ? collectDaysOfWeek() : null,
      day_of_month:
        type === "monthly" && $("dayOfMonth").value
          ? parseInt($("dayOfMonth").value, 10)
          : null,
      one_time_at: null,
    };
  }

  function loadScheduleFields(j) {
    const isOneTime = j.schedule_type === "one-time";
    $("oneTimeReport").checked = isOneTime;

    if (isOneTime) {
      $("oneTimeAt").value = parseOneTimeToLocal(j.one_time_at);
      if (j.scheduled_time) $("scheduledTime").value = j.scheduled_time;
    } else {
      const validTypes = ["daily", "weekly", "monthly", "data-only"];
      const st = j.schedule_type || "daily";
      $("scheduleType").value = validTypes.includes(st) ? st : "daily";
      $("scheduledTime").value = j.scheduled_time || "";
      loadDaysOfWeek(j.days_of_week_csv);
      $("dayOfMonth").value = j.day_of_month ?? "";
    }

    $("effectiveStart").value = j.effective_start_date || "";
    const hasEnd = !!(j.effective_end_date && String(j.effective_end_date).trim());
    $("effectiveUntilCanceled").checked = !hasEnd;
    $("effectiveEnd").value = hasEnd ? j.effective_end_date : "";
    updateScheduleVisibility();
  }

  function addPatternRow(containerId, placeholder) {
    const div = document.createElement("div");
    div.className = "pattern-row";
    div.innerHTML = `<input type="text" class="pattern-input grow" placeholder="${placeholder}" />
      <button type="button" class="btn btn-run btn-remove">Remove</button>`;
    div.querySelector(".btn-remove").addEventListener("click", () => div.remove());
    $(containerId).appendChild(div);
    return div.querySelector("input");
  }

  function collectPatterns(containerId) {
    return Array.from(document.querySelectorAll(`#${containerId} .pattern-input`))
      .map((i) => i.value.trim())
      .filter(Boolean)
      .map((p, i) => ({ file_path_pattern: p, sort_order: i }));
  }

  function renderPatterns(containerId, items, placeholder) {
    $(containerId).innerHTML = "";
    (items || []).forEach((it) => {
      const inp = addPatternRow(containerId, placeholder);
      inp.value = typeof it === "string" ? it : it.file_path_pattern || "";
    });
  }

  function collectPayload() {
    const schedule = collectScheduleFields();
    const untilCanceled = $("effectiveUntilCanceled").checked;
    return {
      name: $("jobName").value,
      description: $("jobDescription").value,
      enabled: $("jobEnabled").checked,
      ...schedule,
      effective_start_date: $("effectiveStart").value || null,
      effective_end_date: untilCanceled ? null : $("effectiveEnd").value || null,
      subject_template: $("subjectTemplate").value,
      body_template: $("bodyTemplate").value,
      data_readiness_mode: $("dataReadinessMode").value,
      stale_file_threshold_minutes: $("staleThreshold").value
        ? parseInt($("staleThreshold").value, 10)
        : null,
      expiration_hours: $("expirationHours").value
        ? parseInt($("expirationHours").value, 10)
        : null,
      send_failure_notification: $("sendFailureNotification").checked,
      retry_count: parseInt($("retryCount").value, 10) || 0,
      retry_delay_seconds: parseInt($("retryDelay").value, 10) || 60,
      required_files: collectPatterns("requiredFilesList"),
      attachments: collectPatterns("attachmentsList"),
      recipients: Array.from(document.querySelectorAll(".recipient-input"))
        .map((i) => i.value.trim())
        .filter(Boolean),
      recipient_group_ids: groupIds,
    };
  }

  async function loadGroups() {
    const r = await fetch("/api/scheduler/recipient-groups");
    const j = await r.json();
    const sel = $("recipientGroupPick");
    sel.innerHTML = "<option value=''>Select group…</option>";
    (j.groups || []).forEach((g) => {
      const o = document.createElement("option");
      o.value = String(g.id);
      o.textContent = `${g.group_name} (${g.member_count})`;
      sel.appendChild(o);
    });
  }

  function renderAttachedGroups(namesById) {
    $("attachedGroups").innerHTML = groupIds
      .map(
        (id) =>
          `<span class="profit-chip">${namesById[id] || id} <button type="button" data-gid="${id}" class="profit-chip-remove">&times;</button></span>`
      )
      .join("");
    document.querySelectorAll("#attachedGroups .profit-chip-remove").forEach((b) => {
      b.addEventListener("click", () => {
        const gid = parseInt(b.getAttribute("data-gid"), 10);
        groupIds = groupIds.filter((x) => x !== gid);
        renderAttachedGroups(namesById);
      });
    });
  }

  async function loadJob() {
    if (!jobId) {
      $("editorTitle").textContent = "New job";
      addPatternRow("requiredFilesList", "\\\\server\\path\\{YYYY}{MM}{DD}_file.csv");
      loadScheduleFields({
        schedule_type: "daily",
        days_of_week_csv: "",
        effective_end_date: null,
      });
      return;
    }
    const r = await fetch(`/api/scheduler/jobs/${jobId}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Load failed");
    $("editorTitle").textContent = j.name;
    $("jobName").value = j.name;
    $("jobDescription").value = j.description || "";
    $("jobEnabled").checked = j.enabled;
    loadScheduleFields(j);
    $("subjectTemplate").value = j.subject_template;
    $("bodyTemplate").value = j.body_template;
    $("dataReadinessMode").value = j.data_readiness_mode || "all_required";
    $("staleThreshold").value = j.stale_file_threshold_minutes ?? "";
    $("expirationHours").value = j.expiration_hours ?? "";
    $("sendFailureNotification").checked = j.send_failure_notification;
    $("retryCount").value = j.retry_count;
    $("retryDelay").value = j.retry_delay_seconds;
    renderPatterns("requiredFilesList", j.required_files, "");
    renderPatterns("attachmentsList", j.attachments, "");
    $("recipientsList").innerHTML = "";
    (j.recipients || []).forEach((em) => {
      const row = document.createElement("div");
      row.className = "pattern-row";
      row.innerHTML = `<input type="email" class="recipient-input grow" value="${em}" /><button type="button" class="btn btn-run btn-remove">Remove</button>`;
      row.querySelector(".btn-remove").addEventListener("click", () => row.remove());
      $("recipientsList").appendChild(row);
    });
    groupIds = j.recipient_group_ids || [];
    const gr = await fetch("/api/scheduler/recipient-groups");
    const gj = await gr.json();
    const map = {};
    (gj.groups || []).forEach((g) => {
      map[g.id] = g.group_name;
    });
    renderAttachedGroups(map);
  }

  async function preview() {
    const payload = collectPayload();
    const url = jobId
      ? `/api/scheduler/jobs/${jobId}/preview`
      : "/api/scheduler/jobs/preview-draft";
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Preview failed");
    $("previewPane").classList.remove("hidden");
    $("previewPane").textContent = JSON.stringify(j, null, 2);
    let fp = "<h3>File readiness (today)</h3><ul>";
    (j.required_files || []).forEach((f) => {
      fp += `<li>${f.present ? "✓" : "✗"} ${f.path} ${f.size ? `(${f.size} bytes)` : ""}</li>`;
    });
    fp += "</ul>";
    $("filePreview").innerHTML = fp;
  }

  $("btnAddRequiredFile").addEventListener("click", () =>
    addPatternRow("requiredFilesList", "path pattern")
  );
  $("btnAddAttachment").addEventListener("click", () =>
    addPatternRow("attachmentsList", "attachment pattern")
  );
  $("btnAddRecipient").addEventListener("click", () => {
    const row = document.createElement("div");
    row.className = "pattern-row";
    row.innerHTML =
      '<input type="email" class="recipient-input grow" /><button type="button" class="btn btn-run btn-remove">Remove</button>';
    row.querySelector(".btn-remove").addEventListener("click", () => row.remove());
    $("recipientsList").appendChild(row);
  });
  $("btnAddGroup").addEventListener("click", async () => {
    const gid = parseInt($("recipientGroupPick").value, 10);
    if (Number.isNaN(gid)) return;
    if (!groupIds.includes(gid)) groupIds.push(gid);
    const gr = await fetch("/api/scheduler/recipient-groups");
    const gj = await gr.json();
    const map = {};
    (gj.groups || []).forEach((g) => {
      map[g.id] = g.group_name;
    });
    renderAttachedGroups(map);
  });

  $("btnSaveJob").addEventListener("click", async () => {
    const payload = collectPayload();
    const url = jobId ? `/api/scheduler/jobs/${jobId}` : "/api/scheduler/jobs";
    const method = jobId ? "PUT" : "POST";
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Save failed");
    if (!jobId) window.location.href = `/scheduler/jobs/${j.id}`;
    else {
      $("editorBanner").textContent = "Saved.";
      $("editorBanner").classList.remove("hidden");
    }
  });

  $("btnPreviewJob").addEventListener("click", () => preview().catch((e) => alert(e.message)));
  $("btnSendNow").addEventListener("click", async () => {
    if (!jobId) {
      alert("Save the job first.");
      return;
    }
    const prev = await fetch(`/api/scheduler/jobs/${jobId}/preview`, { method: "POST" });
    const pj = await prev.json();
    const n = pj.recipient_count || 0;
    if (!confirm(`This will send immediately to ${n} recipient(s). Continue?`)) return;
    const r = await fetch(`/api/scheduler/jobs/${jobId}/run-now`, { method: "POST" });
    const j = await r.json();
    if (!r.ok) alert(j.error || "Send failed");
    else alert(`Sent. BaseName: ${j.baseName || ""}`);
  });

  bindScheduleControls();

  Promise.all([loadGroups(), loadJob()]).catch((e) => {
    $("editorError").textContent = String(e.message || e);
    $("editorError").classList.remove("hidden");
  });
})();
