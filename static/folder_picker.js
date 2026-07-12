(function () {
  "use strict";

  let overlay = null;
  let listEl = null;
  let currentInput = null;
  let currentPathBox = null;
  let upBtn = null;
  let errorEl = null;
  let state = { current: "", parent: null };

  function buildModal() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.className = "modal-overlay hidden";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-hidden", "true");
    overlay.innerHTML = [
      '<div class="modal" role="document" aria-labelledby="fpTitle">',
      '  <div class="modal-header">',
      '    <h2 class="modal-title" id="fpTitle">Choose a folder</h2>',
      '    <button type="button" class="btn btn-modal-close" data-fp="close" aria-label="Close">&#10005;</button>',
      "  </div>",
      '  <div class="modal-body">',
      '    <div class="folder-picker-path">',
      '      <input type="text" data-fp="current" placeholder="Type or paste a path and press Enter" />',
      '      <button type="button" class="btn" data-fp="go">Go</button>',
      '      <button type="button" class="btn" data-fp="up">&#8593; Up</button>',
      "    </div>",
      '    <div class="folder-picker-error error-banner hidden" data-fp="error" role="alert"></div>',
      '    <ul class="folder-list" data-fp="list"></ul>',
      "  </div>",
      '  <div class="modal-footer">',
      '    <button type="button" class="btn" data-fp="cancel">Cancel</button>',
      '    <button type="button" class="btn btn-primary" data-fp="use">Use this folder</button>',
      "  </div>",
      "</div>",
    ].join("");
    document.body.appendChild(overlay);

    listEl = overlay.querySelector('[data-fp="list"]');
    currentPathBox = overlay.querySelector('[data-fp="current"]');
    upBtn = overlay.querySelector('[data-fp="up"]');
    errorEl = overlay.querySelector('[data-fp="error"]');

    overlay.querySelector('[data-fp="close"]').addEventListener("click", close);
    overlay.querySelector('[data-fp="cancel"]').addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    overlay.querySelector('[data-fp="go"]').addEventListener("click", () => {
      fetchListing(currentPathBox.value.trim());
    });
    upBtn.addEventListener("click", () => {
      fetchListing(state.parent != null ? state.parent : "");
    });
    currentPathBox.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        fetchListing(currentPathBox.value.trim());
      }
    });
    overlay.querySelector('[data-fp="use"]').addEventListener("click", () => {
      const chosen = currentPathBox.value.trim() || state.current;
      if (currentInput && chosen) {
        currentInput.value = chosen;
        currentInput.dispatchEvent(new Event("change", { bubbles: true }));
      }
      close();
    });
  }

  function showError(msg) {
    if (!errorEl) return;
    errorEl.textContent = msg || "";
    errorEl.classList.toggle("hidden", !msg);
  }

  async function fetchListing(path) {
    showError("");
    try {
      const r = await fetch(
        "/api/scheduler/browse-folders?path=" + encodeURIComponent(path || "")
      );
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Unable to open folder");
      render(j);
    } catch (e) {
      showError(String(e.message || e));
    }
  }

  function render(data) {
    state.current = data.current || "";
    state.parent = data.parent != null ? data.parent : null;
    currentPathBox.value = state.current;
    upBtn.disabled = data.parent == null;
    listEl.textContent = "";
    const folders = data.folders || [];
    if (!folders.length) {
      const li = document.createElement("li");
      li.className = "folder-empty";
      li.textContent = "No sub-folders here.";
      listEl.appendChild(li);
      return;
    }
    for (const f of folders) {
      const li = document.createElement("li");
      li.className = "folder-item";
      li.textContent = "\uD83D\uDCC1 " + f.name;
      li.title = f.path;
      li.addEventListener("click", () => fetchListing(f.path));
      listEl.appendChild(li);
    }
  }

  function open(inputEl) {
    buildModal();
    currentInput =
      typeof inputEl === "string" ? document.getElementById(inputEl) : inputEl;
    showError("");
    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    const seed = (currentInput && currentInput.value) || "";
    fetchListing(seed);
  }

  function close() {
    if (!overlay) return;
    overlay.classList.add("hidden");
    overlay.setAttribute("aria-hidden", "true");
    currentInput = null;
  }

  window.openFolderPicker = open;
})();
