(() => {
  const SEL_KEY = "lock:selected";
  const DENSITY_KEY = "lock:density";
  const MAX_PREVIEW = 4;

  // ---------- tile-density control (client-only, persisted) ----------
  // Multipliers on the base --tile-min set in CSS. 5 steps total; index
  // 2 is "comfortable" (1.0×). Smaller index = finer grid (more tiles
  // per row), larger index = coarser grid.
  const DENSITY_STEPS = [0.6, 0.78, 1.0, 1.3, 1.7];
  let densityIdx = (() => {
    const raw = parseInt(localStorage.getItem(DENSITY_KEY) || "2", 10);
    return Number.isFinite(raw) && raw >= 0 && raw < DENSITY_STEPS.length ? raw : 2;
  })();

  function applyDensity() {
    document.documentElement.style.setProperty("--density", String(DENSITY_STEPS[densityIdx]));
    const finer = document.getElementById("dens-finer");
    const coarser = document.getElementById("dens-coarser");
    if (finer)   finer.disabled   = densityIdx === 0;
    if (coarser) coarser.disabled = densityIdx === DENSITY_STEPS.length - 1;
  }
  applyDensity();
  document.getElementById("dens-finer")?.addEventListener("click", () => {
    if (densityIdx === 0) return;
    densityIdx--;
    localStorage.setItem(DENSITY_KEY, String(densityIdx));
    applyDensity();
    requestAnimationFrame(updateTightLayout);
  });
  document.getElementById("dens-coarser")?.addEventListener("click", () => {
    if (densityIdx === DENSITY_STEPS.length - 1) return;
    densityIdx++;
    localStorage.setItem(DENSITY_KEY, String(densityIdx));
    applyDensity();
    requestAnimationFrame(updateTightLayout);
  });

  // ---------- per-tile overlay toggle ----------
  // Hides the figcaption (name, stars, tags, delete) over the
  // thumbnail so more of the image is visible. Useful when running a
  // dense grid on the desktop.
  const OVERLAY_KEY = "lock:overlay";
  const overlayBtn = document.getElementById("overlay-toggle");
  function applyOverlay() {
    const off = localStorage.getItem(OVERLAY_KEY) === "off";
    if (off) document.body.dataset.overlay = "off";
    else delete document.body.dataset.overlay;
    if (overlayBtn) overlayBtn.classList.toggle("active", !off);
  }
  applyOverlay();
  overlayBtn?.addEventListener("click", () => {
    const off = localStorage.getItem(OVERLAY_KEY) === "off";
    localStorage.setItem(OVERLAY_KEY, off ? "on" : "off");
    applyOverlay();
  });

  // ---------- "tight layout" detection (Mobile + >2 columns) ----------
  // When the grid renders more than two columns on a phone, the figcaption
  // overlay starts to cover too much of each thumbnail. Hide it then.
  function updateTightLayout() {
    const grid = document.querySelector(".grid");
    if (!grid) {
      delete document.body.dataset.colsTight;
      return;
    }
    const cs = getComputedStyle(grid).gridTemplateColumns;
    const cols = cs ? cs.split(/\s+/).filter(Boolean).length : 1;
    if (cols > 2) document.body.dataset.colsTight = "1";
    else delete document.body.dataset.colsTight;
  }
  updateTightLayout();
  window.addEventListener("resize", () => requestAnimationFrame(updateTightLayout));

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
  const tileById = new Map(tiles.map((t) => [Number(t.dataset.photoId), t]));
  const pagePhotos = tiles.map((t) => ({
    id: Number(t.dataset.photoId),
    name: t.dataset.photoName,
    hidden: t.dataset.hidden === "1",
    rating: Number(t.dataset.rating) || 0,
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
      const idx = pagePhotos.findIndex((p) => p.id === id);
      const start = idx >= 0 ? idx : 0;
      // Render only the clicked photo, but remember its index so
      // Next/Prev step from the right position (not always from 0).
      openViewer([pagePhotos[start]], start);
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

  // Keep tile DOM, pagePhotos entry, and viewer stars in sync. Called
  // from any place that changes a rating (tile stars, viewer stars,
  // keyboard shortcut). The actual POST goes through wireStars / viewer
  // handlers; this is the local-state side only.
  function applyRatingLocally(id, rating) {
    const tile = tileById.get(id);
    if (tile) {
      tile.dataset.rating = String(rating);
      tile.querySelectorAll(".star").forEach((s) => {
        const v = parseInt(s.dataset.value, 10);
        if (v === 0) return;
        s.classList.toggle("filled", v <= rating);
      });
    }
    const entry = pagePhotos.find((p) => p.id === id);
    if (entry) entry.rating = rating;
    // If this is the photo currently in the viewer, repaint viewer stars.
    if (viewerHide.dataset.photoId === String(id)) paintViewerStars(rating);
  }

  async function postRating(id, rating) {
    const fd = new FormData();
    fd.append("rating", String(rating));
    const res = await fetch(`/photo/${id}/rate`, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: fd,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
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

  // Random-mode history so left-arrow / prev goes back to actual
  // previously-seen photos instead of rolling a new one.
  let randomHistory = [];
  let randomCursor = -1;

  const btnRandom = document.getElementById("btn-random");
  if (btnRandom) {
    btnRandom.addEventListener("click", async () => {
      const photo = await fetchRandom();
      if (!photo) return;
      randomHistory = [photo];
      randomCursor = 0;
      openViewer([photo], 0, /*multi=*/false, /*random=*/true);
    });
  }

  function currentFilterQS() {
    const q = new URLSearchParams(window.location.search);
    const out = new URLSearchParams();
    for (const k of ["min_rating", "tag", "show_hidden"]) {
      const v = q.get(k);
      if (v) out.set(k, v);
    }
    return out.toString();
  }

  async function fetchRandom() {
    const qs = currentFilterQS();
    try {
      const res = await fetch(`/api/random${qs ? "?" + qs : ""}`, {
        headers: { Accept: "application/json" },
      });
      if (res.status === 404) {
        alert("No photos match the current filters.");
        return null;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      console.error(e);
      return null;
    }
  }

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

  // ---------- EXIF reindex driver ----------
  const reindexBtn = document.getElementById("btn-reindex");
  const reindexStatus = document.getElementById("reindex-status");
  if (reindexBtn && reindexStatus) {
    reindexBtn.addEventListener("click", async () => {
      reindexBtn.disabled = true;
      let remaining = parseInt(
        document.querySelector(".reindex-note")?.dataset.reindexPending || "0", 10);
      while (remaining > 0) {
        try {
          const res = await fetch("/api/reindex-batch?limit=100", { method: "POST" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = await res.json();
          remaining = json.remaining;
          reindexStatus.textContent =
            remaining > 0 ? `${remaining} photos not yet searchable` : "done";
          if (!json.processed) break;
        } catch (e) {
          console.error(e);
          reindexStatus.textContent = "error — try again";
          break;
        }
      }
      if (remaining === 0) {
        reindexBtn.textContent = "Done — reload to search";
      } else {
        reindexBtn.disabled = false;
      }
    });
  }

  // ---------- viewer ----------
  const viewer = document.getElementById("viewer");
  const viewerGrid = document.getElementById("viewer-grid");
  const viewerCaption = document.getElementById("viewer-caption");
  const viewerClose = document.getElementById("viewer-close");
  const viewerPrev = document.getElementById("viewer-prev");
  const viewerNext = document.getElementById("viewer-next");
  const viewerHide = document.getElementById("viewer-hide");
  const viewerRating = document.getElementById("viewer-rating");

  function paintViewerStars(rating) {
    viewerRating.querySelectorAll(".vstar").forEach((s) => {
      const v = parseInt(s.dataset.value, 10);
      if (v === 0) return;  // skip the clear-button
      s.classList.toggle("filled", v <= rating);
    });
  }

  viewerRating.querySelectorAll(".vstar").forEach((b) => {
    b.addEventListener("click", async () => {
      const id = Number(viewerHide.dataset.photoId);
      if (!id) return;
      const value = parseInt(b.dataset.value, 10);
      const tile = tileById.get(id);
      const previous = tile ? (Number(tile.dataset.rating) || 0) : 0;
      applyRatingLocally(id, value);  // optimistic — feels instant
      try {
        const json = await postRating(id, value);
        if (json.rating !== value) applyRatingLocally(id, json.rating);
      } catch (err) {
        console.error(err);
        applyRatingLocally(id, previous);
      }
    });
  });

  // Navigation state. "single" steps through the rendered page,
  // "random" replaces the current photo with a fresh random pick on
  // each step, "multi" has no stepping.
  let viewerMode = "single"; // "single" | "multi" | "random"
  let viewerIndex = 0;

  function openViewer(items, index, multi = false, random = false) {
    viewerMode = random ? "random" : (multi ? "multi" : "single");
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
    if (viewerMode === "single" || viewerMode === "random") {
      const cur = items[0];
      viewerCaption.textContent = viewerMode === "random"
        ? `random  ·  ${cur ? cur.name : ""}`
        : `${viewerIndex + 1} / ${pagePhotos.length}  ·  ${cur ? cur.name : ""}`;
      viewerPrev.hidden = false;
      viewerNext.hidden = false;
      viewerHide.hidden = false;
      viewerHide.textContent = cur && cur.hidden ? "Unhide" : "Hide";
      viewerHide.dataset.photoId = cur ? String(cur.id) : "";
      viewerHide.dataset.hidden = cur && cur.hidden ? "1" : "0";
      viewerRating.hidden = false;
      paintViewerStars(cur ? (cur.rating || 0) : 0);
    } else {
      viewerCaption.textContent = `${n} photo${n === 1 ? "" : "s"}`;
      viewerPrev.hidden = true;
      viewerNext.hidden = true;
      viewerHide.hidden = true;
      viewerRating.hidden = true;
    }
  }

  function closeViewer() {
    viewer.hidden = true;
    viewerGrid.innerHTML = "";
    document.body.style.overflow = "";
  }

  async function stepViewer(delta) {
    if (viewerMode === "random") {
      let photo = null;
      if (delta > 0) {
        // Forward: advance in history if we already went back, else
        // roll a new random and push it on the stack.
        if (randomCursor + 1 < randomHistory.length) {
          randomCursor++;
          photo = randomHistory[randomCursor];
        } else {
          photo = await fetchRandom();
          if (photo) {
            randomHistory.push(photo);
            randomCursor = randomHistory.length - 1;
          }
        }
      } else if (randomCursor > 0) {
        // Backward: step into history, no network call.
        randomCursor--;
        photo = randomHistory[randomCursor];
      }
      if (photo) renderViewer([photo]);
      return;
    }
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
      if (viewerMode === "random") {
        // Stay in the viewer — just roll the next random photo. Don't
        // bother reloading the gallery, that interrupts the flow.
        const next = await fetchRandom();
        if (next) {
          // Drop anything after the cursor (we're branching from here)
          // and append the new pick.
          randomHistory = randomHistory.slice(0, randomCursor + 1);
          randomHistory.push(next);
          randomCursor = randomHistory.length - 1;
          renderViewer([next]);
        } else closeViewer();
        return;
      }
      // Single-mode: the photo may now be excluded from the current
      // page; close + reload is the simplest correct outcome.
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
    else if (/^[0-5]$/.test(e.key) && !viewerRating.hidden && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // 0 clears, 1..5 set the rating. Mirrors the visible star row.
      e.preventDefault();
      const id = Number(viewerHide.dataset.photoId);
      if (!id) return;
      const value = parseInt(e.key, 10);
      const tile = tileById.get(id);
      const previous = tile ? (Number(tile.dataset.rating) || 0) : 0;
      applyRatingLocally(id, value);
      postRating(id, value).catch((err) => {
        console.error(err);
        applyRatingLocally(id, previous);
      });
    }
  });

  // ---------- rating / tags (unchanged API) ----------
  function wireStars(tile, id) {
    tile.querySelectorAll(".star").forEach((b) =>
      b.addEventListener("click", async () => {
        const value = parseInt(b.dataset.value, 10);
        const previous = Number(tile.dataset.rating) || 0;
        applyRatingLocally(id, value);  // optimistic
        try {
          const json = await postRating(id, value);
          if (json.rating !== value) applyRatingLocally(id, json.rating);
        } catch (err) {
          console.error(err);
          applyRatingLocally(id, previous);  // revert on failure
        }
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
