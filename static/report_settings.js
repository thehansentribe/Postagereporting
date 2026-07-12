(function () {
  const $ = (id) => document.getElementById(id);

  const FIELDS = [
    "reportSourcePath",
    "pollingIntervalSeconds",
    "logRetentionDays",
    "defaultExpirationHours",
    "timezone",
    "emailRootPath",
    "adminNotificationEmail",
  ];

  function showBanner(msg) {
    const el = $("settingsBanner");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function showError(msg) {
    const el = $("settingsError");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  async function load() {
    showError("");
    const r = await fetch("/api/scheduler/settings");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load settings");
    for (const k of FIELDS) {
      const el = $(k);
      if (el && j[k] != null) el.value = j[k];
    }
  }

  function collect() {
    const out = {};
    for (const k of FIELDS) {
      const el = $(k);
      if (!el) continue;
      if (k === "pollingIntervalSeconds" || k === "logRetentionDays" || k === "defaultExpirationHours") {
        out[k] = parseInt(el.value, 10);
      } else {
        out[k] = el.value;
      }
    }
    return out;
  }

  $("btnSaveSettings").addEventListener("click", async () => {
    showError("");
    showBanner("Saving…");
    try {
      const r = await fetch("/api/scheduler/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collect()),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Save failed");
      showBanner("Settings saved.");
    } catch (e) {
      showBanner("");
      showError(String(e.message || e));
    }
  });

  const picker = {
    targetId: null,
    current: "",
    parent: null,
  };

  function showPickerError(msg) {
    const el = $("folderPickerError");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function openPicker(targetId) {
    picker.targetId = targetId;
    showPickerError("");
    const overlay = $("folderPickerOverlay");
    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    const seed = ($(targetId) && $(targetId).value) || "";
    fetchListing(seed);
  }

  function closePicker() {
    const overlay = $("folderPickerOverlay");
    overlay.classList.add("hidden");
    overlay.setAttribute("aria-hidden", "true");
    picker.targetId = null;
  }

  async function fetchListing(path) {
    showPickerError("");
    try {
      const r = await fetch("/api/scheduler/browse-folders?path=" + encodeURIComponent(path || ""));
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Unable to open folder");
      renderListing(j);
    } catch (e) {
      showPickerError(String(e.message || e));
    }
  }

  function renderListing(data) {
    picker.current = data.current || "";
    picker.parent = data.parent || null;
    $("folderPickerCurrent").value = picker.current;
    $("btnFolderPickerUp").disabled = data.parent == null;

    const list = $("folderPickerList");
    list.textContent = "";
    const folders = data.folders || [];
    if (!folders.length) {
      const li = document.createElement("li");
      li.className = "folder-empty";
      li.textContent = "No sub-folders here.";
      list.appendChild(li);
      return;
    }
    for (const f of folders) {
      const li = document.createElement("li");
      li.className = "folder-item";
      li.textContent = "\uD83D\uDCC1 " + f.name;
      li.title = f.path;
      li.addEventListener("click", () => fetchListing(f.path));
      list.appendChild(li);
    }
  }

  document.querySelectorAll(".btn-browse").forEach((btn) => {
    btn.addEventListener("click", () => openPicker(btn.dataset.target));
  });

  $("btnCloseFolderPicker").addEventListener("click", closePicker);
  $("btnFolderPickerCancel").addEventListener("click", closePicker);
  $("folderPickerOverlay").addEventListener("click", (e) => {
    if (e.target === $("folderPickerOverlay")) closePicker();
  });

  $("btnFolderPickerUp").addEventListener("click", () => {
    if (picker.parent != null) fetchListing(picker.parent);
    else fetchListing("");
  });

  $("btnFolderPickerGo").addEventListener("click", () => {
    fetchListing($("folderPickerCurrent").value.trim());
  });

  $("folderPickerCurrent").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      fetchListing($("folderPickerCurrent").value.trim());
    }
  });

  $("btnFolderPickerUse").addEventListener("click", () => {
    const target = picker.targetId && $(picker.targetId);
    const chosen = $("folderPickerCurrent").value.trim() || picker.current;
    if (target && chosen) target.value = chosen;
    closePicker();
  });

  $("btnTestEmailRoot").addEventListener("click", async () => {
    showError("");
    showBanner("Testing email root…");
    try {
      const r = await fetch("/api/scheduler/settings/test-email-root", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emailRootPath: $("emailRootPath").value }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Test failed");
      showBanner("Connection test succeeded.");
    } catch (e) {
      showBanner("");
      showError(String(e.message || e));
    }
  });

  load().catch((e) => showError(String(e.message || e)));
})();
