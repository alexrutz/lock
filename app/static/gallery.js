(() => {
  document.querySelectorAll(".tile").forEach((tile) => {
    const id = tile.dataset.photoId;
    wireStars(tile, id);
    wireTagRemove(tile, id);
    wireTagAdd(tile, id);
  });

  function wireStars(tile, id) {
    const buttons = tile.querySelectorAll(".star");
    buttons.forEach((b) =>
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
          paintStars(tile, json.rating);
          tile.dataset.rating = String(json.rating);
        } catch (e) {
          console.error(e);
        }
      })
    );
  }

  function paintStars(tile, rating) {
    tile.querySelectorAll(".star").forEach((s) => {
      const v = parseInt(s.dataset.value, 10);
      if (v === 0) return;  // skip the clear-button
      s.classList.toggle("filled", v <= rating);
    });
  }

  function wireTagRemove(tile, id) {
    tile.querySelectorAll(".tag-chip .tag-remove").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const chip = btn.closest(".tag-chip");
        const tagId = chip.dataset.tagId;
        const fd = new FormData();
        fd.append("tag_id", tagId);
        try {
          const res = await fetch(`/photo/${id}/untag`, {
            method: "POST",
            headers: { Accept: "application/json" },
            body: fd,
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          chip.remove();
        } catch (e) {
          console.error(e);
        }
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
      } catch (e) {
        input.setCustomValidity(e.message);
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
