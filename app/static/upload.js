(() => {
  const CONCURRENCY = 4;

  const form = document.getElementById("upload-form");
  const input = document.getElementById("upload-input");
  const dropzone = document.getElementById("dropzone");
  const list = document.getElementById("upload-list");
  const summary = document.getElementById("upload-summary");
  if (!form || !input || !dropzone) return;

  // The native submit button is hidden when JS is enabled.
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (input.files && input.files.length) start(input.files);
  });
  input.addEventListener("change", () => {
    if (input.files && input.files.length) start(input.files);
  });

  ["dragenter", "dragover"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-dragging");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-dragging");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) start(files);
  });

  let inFlight = 0;
  let queue = [];
  let totals = { ok: 0, failed: 0, done: 0, total: 0 };

  function start(fileList) {
    const files = Array.from(fileList);
    // Filter non-images on the client too, so the user sees them rejected
    // immediately instead of after a round-trip.
    const accepted = files.filter((f) => /^image\//.test(f.type) || /\.(jpe?g|png|gif|webp)$/i.test(f.name));
    const rejected = files.length - accepted.length;
    if (!accepted.length) {
      flash(`No image files selected${rejected ? ` (${rejected} skipped)` : ""}`);
      return;
    }

    list.innerHTML = "";
    summary.hidden = true;
    totals = { ok: 0, failed: rejected, done: 0, total: accepted.length };

    queue = accepted.map((file) => {
      const li = document.createElement("li");
      li.className = "upload-item pending";
      li.innerHTML = `
        <span class="ui-name"></span>
        <span class="ui-status">queued</span>
        <span class="ui-bar"><span class="ui-bar-fill"></span></span>
      `;
      li.querySelector(".ui-name").textContent = file.name;
      list.appendChild(li);
      return { file, li };
    });

    pump();
  }

  function pump() {
    while (inFlight < CONCURRENCY && queue.length) {
      const job = queue.shift();
      inFlight++;
      uploadOne(job).finally(() => {
        inFlight--;
        if (queue.length) {
          pump();
        } else if (inFlight === 0) {
          finish();
        }
      });
    }
  }

  function uploadOne({ file, li }) {
    return new Promise((resolve) => {
      const fd = new FormData();
      fd.append("files", file, file.name);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/upload");
      xhr.setRequestHeader("Accept", "application/json");

      const status = li.querySelector(".ui-status");
      const fill = li.querySelector(".ui-bar-fill");
      li.classList.remove("pending");
      li.classList.add("uploading");
      status.textContent = "uploading…";

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          const pct = (e.loaded / e.total) * 100;
          fill.style.width = pct.toFixed(1) + "%";
        }
      });

      xhr.addEventListener("load", () => {
        li.classList.remove("uploading");
        totals.done++;
        if (xhr.status >= 200 && xhr.status < 300) {
          let detail = null;
          try {
            const json = JSON.parse(xhr.responseText);
            // Single-file POST → results array of length 1.
            detail = json.results && json.results[0];
          } catch (_) { /* noop */ }
          if (detail && detail.ok === false) {
            mark(li, "failed", detail.error || "failed");
            totals.failed++;
          } else {
            mark(li, "done", "encrypted");
            fill.style.width = "100%";
            totals.ok++;
          }
        } else {
          let msg = `HTTP ${xhr.status}`;
          try {
            const json = JSON.parse(xhr.responseText);
            if (json.detail) msg = json.detail;
          } catch (_) { /* noop */ }
          mark(li, "failed", msg);
          totals.failed++;
        }
        updateSummary();
        resolve();
      });

      xhr.addEventListener("error", () => {
        li.classList.remove("uploading");
        mark(li, "failed", "network error");
        totals.done++;
        totals.failed++;
        updateSummary();
        resolve();
      });

      xhr.send(fd);
    });
  }

  function mark(li, cls, text) {
    li.classList.add(cls);
    li.querySelector(".ui-status").textContent = text;
  }

  function updateSummary() {
    summary.hidden = false;
    const remaining = totals.total - totals.done;
    summary.textContent =
      `Uploaded ${totals.ok}/${totals.total}` +
      (totals.failed ? `  ·  ${totals.failed} failed` : "") +
      (remaining ? `  ·  ${remaining} pending` : "");
  }

  function finish() {
    updateSummary();
    if (totals.ok > 0) {
      // Reload to show the new photos in the gallery; keep current page.
      const url = new URL(window.location.href);
      setTimeout(() => window.location.assign(url.toString()), 600);
    }
  }

  function flash(msg) {
    summary.hidden = false;
    summary.textContent = msg;
  }
})();
