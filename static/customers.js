(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtAccount(name, num) {
    return `${escapeHtml(name)} <span class="hierarchy-num">(${escapeHtml(String(num))})</span>`;
  }

  function renderParents(listEl, parents) {
    listEl.innerHTML = "";
    parents.forEach((p) => {
      const li = document.createElement("li");
      li.className = "hierarchy-parent";
      const hasKids = (p.children && p.children.length > 0) || p.child_count > 0;
      const row = document.createElement("div");
      row.className = "hierarchy-parent-row";

      if (hasKids) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "hierarchy-toggle";
        btn.setAttribute("aria-expanded", "false");
        btn.setAttribute("aria-label", `Show children for ${p.customer_name}`);
        btn.textContent = "\u25b6";
        row.appendChild(btn);

        const label = document.createElement("span");
        label.className = "hierarchy-label";
        label.innerHTML = fmtAccount(p.customer_name, p.customer_number);
        row.appendChild(label);

        const ul = document.createElement("ul");
        ul.className = "hierarchy-children";
        ul.hidden = true;
        (p.children || []).forEach((ch) => {
          const cli = document.createElement("li");
          cli.className = "hierarchy-child";
          cli.innerHTML = fmtAccount(ch.customer_name, ch.customer_number);
          ul.appendChild(cli);
        });
        li.appendChild(row);
        li.appendChild(ul);
      } else {
        const label = document.createElement("span");
        label.className = "hierarchy-label hierarchy-label-noexpand";
        label.innerHTML = fmtAccount(p.customer_name, p.customer_number);
        row.appendChild(label);
        li.appendChild(row);
      }
      listEl.appendChild(li);
    });
  }

  function renderStandalone(listEl, emptyEl, rows) {
    listEl.innerHTML = "";
    if (!rows.length) {
      emptyEl.classList.remove("hidden");
      return;
    }
    emptyEl.classList.add("hidden");
    rows.forEach((r) => {
      const li = document.createElement("li");
      li.className = "hierarchy-standalone";
      li.innerHTML = fmtAccount(r.customer_name, r.customer_number);
      listEl.appendChild(li);
    });
  }

  document.getElementById("parentList").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".hierarchy-toggle");
    if (!btn || !document.getElementById("parentList").contains(btn)) return;
    const li = btn.closest(".hierarchy-parent");
    if (!li) return;
    const childUl = li.querySelector(".hierarchy-children");
    if (!childUl) return;
    const open = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", open ? "false" : "true");
    btn.setAttribute("aria-label", open ? `Show children` : `Hide children`);
    childUl.hidden = open;
    btn.textContent = open ? "\u25b6" : "\u25bc";
  });

  async function load() {
    $("hierarchyError").classList.add("hidden");
    try {
      const r = await fetch("/api/customers/hierarchy");
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Failed to load");
      renderParents($("parentList"), j.parents || []);
      renderStandalone($("standaloneList"), $("standaloneEmpty"), j.standalone || []);
    } catch (e) {
      $("hierarchyError").textContent = String(e.message || e);
      $("hierarchyError").classList.remove("hidden");
    }
  }

  load();
})();
