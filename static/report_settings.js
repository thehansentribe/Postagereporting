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
