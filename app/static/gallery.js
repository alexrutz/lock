(() => {
  const SEL_KEY = "lock:selected";
  const MAX_PREVIEW = 4;

  // ---------- selection state (persists across pagination) ----------
  function loadSelection() {
    try {
      const v = JSON.parse(sessionStorage.getItem(SEL_KEY) || "[]");
      return new Set(Array.isArray(v) ? v.map(Number) : []);
    } catch (_) { return new Set(); }
  }
  function saveSelection(set) {
    sessionStorage.setItem(SEL_KEY, JSON.stringify([...set]));
  }
  const selection = loadSelection();

  const actionBar = document.getElementById("action-bar");
  const selCount = document.getElementById("sel-count");
  const btnPreview = document.getElementById("btn-preview");
  const btnExportSelected = document.getElementById("btn-export-selected");
  const btnClear = document.getElementById("btn-clear-selection");
  const btnSelectPage = document.getElementById("btn-select-page");
  const exportSelectedForm = document.getElementById("export-selected-form");

  function refreshActionBar() {
    const n = selection.size;
    selCount.textContent = `${n} selected`;
    actionBar.hidden = n === 0;
    btnPreview.disabled = n === 0;
    btnPreview.textContent = n > MAX_PREVIEW
      ? `Preview first ${MAX_PREVIEW}`
      : "Preview";
    btnExportSelected.disabled = n === 0;
  }

  // ---------- per-tile wiring ----------
  const tiles = [...document.querySelectorAll(".tile")];
  const pagePhotos = tiles.map((t) => ({
    id: Number(t.dataset.photoId),
    name: t.dataset.photoName,
    hidden: t.dataset.hidden === "1",
  }));

  tiles.forEach((tile) => {
    const id = Number(tile.dataset.photoId);
    const cb = tile.querySelector(".select-cb");

    if (selection.has(id)) {
      cb.checked = true;
      tile.classList.add("selected");
    }

    cb.addEventListener("change", () => {
      if (cb.checked) selection.add(id); else selection.delete(id);
      tile.classList.toggle("selected", cb.checked);
      saveSelection(selection);
      refreshActionBar();
    });
    tile.querySelector(".select-box").addEventListener("click", (e) => e.stopPropagation());

    const link = tile.querySelector(".tile-image-link");
    link.addEventListener("click", (e) => {
      e.preventDefault();
      openViewer([{ id, name: tile.dataset.photoName, hidden: tile.dataset.hidden === "1" }], 0);
    });

    wireStars(tile, id);
    wireTagRemove(tile, id);
    wireTagAdd(tile, id);
    wireHide(tile, id);
  });

  function wireHide(tile, id) {
    const btn = tile.querySelector(".hide-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const currentlyHidden = btn.dataset.hidden === "1";
      await postHide(id, !currentlyHidden);
      // Tile state depends on whether show_hidden is currently active;
      // a reload is the simplest correct outcome (the tile may need to
      // appear or disappear from the page).
      window.location.reload();
    });
  }

  async function postHide(id, hidden) {
    const fd = new FormData();
    fd.append("hidden", hidden ? "true" : "false");
    const res = await fetch(`/photo/${id}/hide`, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: fd,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  // ---------- top-level buttons ----------
  btnPreview.addEventListener("click", () => {
    const ids = [...selection].slice(0, MAX_PREVIEW);
    if (!ids.length) return;
    // Look up names from page tiles; fall back to id-only otherwise.
    const items = ids.map((id) => {
      const tile = document.querySelector(`.tile[data-photo-id="${id}"]`);
      return { id, name: tile ? tile.dataset.photoName : `#${id}` };
    });
    openViewer(items, 0, /*multi=*/items.length > 1);
  });

  btnExportSelected.addEventListener("click", () => {
    if (!selection.size) return;
    exportSelectedForm.innerHTML = "";
    for (const id of selection) {
      const inp = document.createElement("input");
      inp.type = "hidden";
      inp.name = "ids";
      inp.value = String(id);
      exportSelectedForm.appendChild(inp);
    }
    exportSelectedForm.submit();
  });

  btnClear.addEventListener("click", () => {
    selection.clear();
    saveSelection(selection);
    document.querySelectorAll(".tile.selected").forEach((t) => {
      t.classList.remove("selected");
      const cb = t.querySelector(".select-cb");
      if (cb) cb.checked = false;
    });
    refreshActionBar();
  });

  btnSelectPage.addEventListener("click", () => {
    const allChecked = tiles.every((t) => t.classList.contains("selected"));
    tiles.forEach((t) => {
      const id = Number(t.dataset.photoId);
      const cb = t.querySelector(".select-cb");
      if (allChecked) {
        selection.delete(id); cb.checked = false; t.classList.remove("selected");
      } else {
        selection.add(id); cb.checked = true; t.classList.add("selected");
      }
    });
    saveSelection(selection);
    refreshActionBar();
  });

  refreshActionBar();

  // ---------- viewer ----------
  const viewer = document.getElementById("viewer");
  const viewerGrid = document.getElementById("viewer-grid");
  const viewerCaption = document.getElementById("viewer-caption");
  const viewerClose = document.getElementById("viewer-close");
  const viewerPrev = document.getElementById("viewer-prev");
  const viewerNext = document.getElementById("viewer-next");
  const viewerHide = document.getElementById("viewer-hide");

  // Navigation state for *single*-image viewer mode:
  //   pagePhotos is the current page in render order; index points into it.
  let viewerMode = "single"; // "single" | "multi"
  let viewerIndex = 0;

  function openViewer(items, index, multi = false) {
    viewerMode = multi ? "multi" : "single";
    viewerIndex = index;
    renderViewer(items);
    viewer.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function renderViewer(items) {
    const n = Math.min(items.length, MAX_PREVIEW);
    viewerGrid.dataset.count = String(n);
    viewerGrid.innerHTML = "";
    for (const it of items.slice(0, n)) {
      const cell = document.createElement("div");
      cell.className = "viewer-cell";
      const img = document.createElement("img");
      img.src = `/photo/${it.id}`;
      img.alt = it.name || `#${it.id}`;
      img.draggable = false;
      cell.appendChild(img);
      const cap = document.createElement("span");
      cap.className = "viewer-cell-caption";
      cap.textContent = it.name || `#${it.id}`;
      cell.appendChild(cap);
      viewerGrid.appendChild(cell);
    }
    if (viewerMode === "single") {
      const cur = items[0];
      viewerCaption.textContent =
        `${viewerIndex + 1} / ${pagePhotos.length}  ·  ${cur ? cur.name : ""}`;
      viewerPrev.hidden = false;
      viewerNext.hidden = false;
      viewerHide.hidden = false;
      viewerHide.textContent = cur && cur.hidden ? "Unhide" : "Hide";
      viewerHide.dataset.photoId = cur ? String(cur.id) : "";
      viewerHide.dataset.hidden = cur && cur.hidden ? "1" : "0";
    } else {
      viewerCaption.textContent = `${n} photo${n === 1 ? "" : "s"}`;
      viewerPrev.hidden = true;
      viewerNext.hidden = true;
      viewerHide.hidden = true;
    }
  }

  function closeViewer() {
    viewer.hidden = true;
    viewerGrid.innerHTML = "";
    document.body.style.overflow = "";
  }

  function stepViewer(delta) {
    if (viewerMode !== "single" || !pagePhotos.length) return;
    viewerIndex = (viewerIndex + delta + pagePhotos.length) % pagePhotos.length;
    renderViewer([pagePhotos[viewerIndex]]);
  }

  viewerClose.addEventListener("click", closeViewer);
  viewerPrev.addEventListener("click", () => stepViewer(-1));
  viewerNext.addEventListener("click", () => stepViewer(1));
  viewerHide.addEventListener("click", async () => {
    const id = Number(viewerHide.dataset.photoId);
    if (!id) return;
    const currentlyHidden = viewerHide.dataset.hidden === "1";
    try {
      await postHide(id, !currentlyHidden);
      // Closing + reloading is the safest behavior — the photo may now
      // be excluded from the gallery (depending on show_hidden).
      closeViewer();
      window.location.reload();
    } catch (err) { console.error(err); }
  });

  // Click on the dimmed background closes (but not clicks on images).
  viewer.addEventListener("click", (e) => {
    if (e.target === viewer || e.target === viewerGrid) closeViewer();
  });

  document.addEventListener("keydown", (e) => {
    if (viewer.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); closeViewer(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepViewer(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepViewer(1); }
  });

  // ---------- rating / tags (unchanged API) ----------
  function wireStars(tile, id) {
    tile.querySelectorAll(".star").forEach((b) =>
      b.addEventListener("click", async () => {
        const value = parseInt(b.dataset.value, 10);
        const fd = new FormData();
        fd.append("rating", value);
        try {
          const res = await fetch(`/photo/${id}/rate`, {
            method: "POST",
            headers: { Accept: "application/json" },
            body: fd,
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = await res.json();
          tile.querySelectorAll(".star").forEach((s) => {
            const v = parseInt(s.dataset.value, 10);
            if (v === 0) return;
            s.classList.toggle("filled", v <= json.rating);
          });
          tile.dataset.rating = String(json.rating);
        } catch (err) { console.error(err); }
      })
    );
  }

  function wireTagRemove(tile, id) {
    tile.querySelectorAll(".tag-chip .tag-remove").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const chip = btn.closest(".tag-chip");
        const fd = new FormData();
        fd.append("tag_id", chip.dataset.tagId);
        try {
          const res = await fetch(`/photo/${id}/untag`, {
            method: "POST",
            headers: { Accept: "application/json" },
            body: fd,
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          chip.remove();
        } catch (err) { console.error(err); }
      });
    });
  }

  function wireTagAdd(tile, id) {
    const form = tile.querySelector(".tag-add");
    if (!form) return;
    const input = form.querySelector("input[name='name']");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) return;
      const fd = new FormData();
      fd.append("name", name);
      try {
        const res = await fetch(form.action, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: fd,
        });
        if (!res.ok) {
          let msg = `HTTP ${res.status}`;
          try { msg = (await res.json()).detail || msg; } catch (_) {}
          throw new Error(msg);
        }
        const json = await res.json();
        renderTags(tile, id, json.tags);
        input.value = "";
      } catch (err) {
        input.setCustomValidity(err.message);
        input.reportValidity();
        setTimeout(() => input.setCustomValidity(""), 2000);
      }
    });
  }

  function renderTags(tile, photoId, tags) {
    const list = tile.querySelector(".tag-list");
    list.innerHTML = "";
    for (const t of tags) {
      const li = document.createElement("li");
      li.className = "tag-chip";
      li.dataset.tagId = t.id;
      const span = document.createElement("span");
      span.textContent = t.name;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tag-remove";
      btn.setAttribute("aria-label", "Remove tag");
      btn.textContent = "×";
      li.appendChild(span);
      li.appendChild(btn);
      list.appendChild(li);
    }
    wireTagRemove(tile, photoId);
  }
})();
