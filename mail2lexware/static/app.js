// Kleine Interaktionen ohne Abhängigkeiten: Auswahl, Vorschau, Ladezustände, Statusabfrage.
(() => {
  "use strict";

  // Hinweise schließen
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-dismiss]");
    if (btn) btn.closest(".flash")?.remove();
  });

  // Ladezustand: Klasse statt disabled, damit name/value des Buttons mitgesendet wird
  document.addEventListener("submit", (e) => {
    const submitter = e.submitter;
    if (submitter && submitter.hasAttribute("data-loading")) {
      requestAnimationFrame(() => submitter.classList.add("is-loading"));
    }
  });
  // Zurück-Navigation aus dem Cache: Ladezustände zurücksetzen
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    document.querySelectorAll(".is-loading").forEach((b) => b.classList.remove("is-loading"));
    document.querySelectorAll(".upload-progress").forEach((el) => el.remove());
    document.querySelectorAll("[data-progress]").forEach((b) => (b.disabled = false));
    document.body.classList.remove("is-uploading");
  });

  // Einstellungen: nur die zum Zeitraum passenden Felder zeigen
  const period = document.querySelector("[data-period]");
  if (period) {
    const sync = () => document.querySelectorAll("[data-period-for]").forEach((el) => {
      el.hidden = el.dataset.periodFor !== period.value;
    });
    period.addEventListener("change", sync);
    sync();
  }

  // Postfach hinzufügen: Hinweis und Serverfelder passend zum Anbieter
  const providers = document.querySelectorAll("[data-provider]");
  if (providers.length) {
    const hint = document.querySelector("[data-provider-hint]");
    const custom = document.querySelector("[data-custom-only]");
    const sync = () => {
      const sel = [...providers].find((r) => r.checked);
      if (!sel) return;
      hint.textContent = sel.dataset.hint;
      hint.hidden = !sel.dataset.hint;
      custom.hidden = sel.dataset.custom !== "true";
    };
    providers.forEach((r) => r.addEventListener("change", sync));
    sync();
  }

  // Sicherheitsabfrage bei Formularen mit data-confirm
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset?.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Upload nach Lexware: Formular im Hintergrund senden, Fortschritt anzeigen, Verlassen abfangen
  const warnLeave = (e) => { e.preventDefault(); e.returnValue = ""; };
  const progressBox = () => {
    const box = document.createElement("div");
    box.className = "upload-progress";
    box.setAttribute("role", "status");
    box.innerHTML = '<div class="upload-progress-text"><span data-label>Upload wird vorbereitet …</span>' +
      '<span class="num" data-count></span></div>' +
      '<div class="upload-progress-track"><div class="upload-progress-fill" data-fill></div></div>' +
      '<div class="upload-progress-hint">Bitte die Seite nicht schließen, bis der Upload fertig ist.</div>';
    document.body.appendChild(box);
    requestAnimationFrame(() => box.classList.add("is-visible"));
    return box;
  };

  document.addEventListener("submit", async (e) => {
    const submitter = e.submitter;
    if (e.defaultPrevented || !submitter?.hasAttribute("data-progress")) return;
    e.preventDefault();
    const formEl = e.target;
    const body = new FormData(formEl);
    if (submitter.name) body.append(submitter.name, submitter.value);
    document.querySelectorAll("[data-progress]").forEach((b) => (b.disabled = true));

    document.body.classList.add("is-uploading");
    const box = progressBox();
    const label = box.querySelector("[data-label]");
    const count = box.querySelector("[data-count]");
    const fill = box.querySelector("[data-fill]");
    window.addEventListener("beforeunload", warnLeave);

    let finished = false;
    const poll = async () => {
      if (finished) return;
      try {
        const r = await fetch("/upload/progress", { headers: { Accept: "application/json" } });
        const p = r.ok ? await r.json() : null;
        if (!finished && p?.active && p.total) {
          label.textContent = "Lädt nach Lexware hoch …";
          count.textContent = `${p.done} von ${p.total}`;
          box.classList.add("is-determinate");
          fill.style.width = `${Math.round((p.done / p.total) * 100)}%`;
        }
      } catch (_) { /* nächster Versuch */ }
      if (!finished) setTimeout(poll, 600);
    };
    setTimeout(poll, 300);

    let target = location.href;
    try {
      // getAttribute statt formEl.action: die Buttons heißen name="action" und verdecken die Eigenschaft
      const url = formEl.getAttribute("action") || location.href;
      const r = await fetch(url, {
        method: "POST", body, credentials: "same-origin", headers: { "X-Upload-Progress": "1" },
      });
      const data = r.ok && (r.headers.get("Content-Type") || "").includes("json") ? await r.json() : null;
      target = data?.redirect || (r.redirected ? r.url : target);
    } catch (_) { /* Netzwerkfehler: Seite neu laden, die Meldungen zeigen den Stand */ }
    finished = true;
    box.classList.add("is-determinate");
    fill.style.width = "100%";
    label.textContent = "Fertig";
    window.removeEventListener("beforeunload", warnLeave);
    location.assign(target);
  });

  // Läuft gerade ein Durchlauf? Dann Status abfragen und danach neu laden.
  if (document.body.dataset.running === "true") {
    const poll = async () => {
      try {
        const r = await fetch("/status", { headers: { Accept: "application/json" } });
        if (r.ok && !(await r.json()).running) return location.reload();
      } catch (_) { /* Netzwerk kurz weg: einfach erneut versuchen */ }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 1500);
  }

  // ---- Prüfen-Ansicht ----
  const form = document.getElementById("review-form");
  if (!form) return;

  const rows = [...form.querySelectorAll(".doc")];
  const checks = rows.map((r) => r.querySelector('input[name="ids"]'));
  const all = form.querySelector("[data-select-all]");
  const bar = form.querySelector("[data-bulk]");
  const countEl = form.querySelector("[data-selected-count]");
  const frameBox = document.querySelector("[data-preview-frame]");
  const emptyEl = document.querySelector("[data-preview-empty]");
  const nameEl = document.querySelector("[data-preview-name]");
  const subEl = document.querySelector("[data-preview-sub]");
  const openEl = document.querySelector("[data-preview-open]");
  let current = null;

  const refresh = () => {
    const n = checks.filter((c) => c.checked).length;
    rows.forEach((r, i) => r.classList.toggle("is-checked", checks[i].checked));
    countEl.textContent = n;
    bar.classList.toggle("is-visible", n > 0);
    all.checked = n > 0 && n === checks.length;
    all.indeterminate = n > 0 && n < checks.length;
  };

  const preview = (row, { focus = false } = {}) => {
    if (current) current.classList.remove("is-previewed");
    current = row;
    row.classList.add("is-previewed");
    if (focus) row.focus({ preventScroll: false });
    nameEl.textContent = row.dataset.name;
    subEl.textContent = row.dataset.sub;
    openEl.href = row.dataset.url;
    openEl.hidden = false;

    frameBox.querySelector("iframe, img, .preview-pages, .preview-note")?.remove();
    const ext = row.dataset.ext;
    let el;
    if (["jpg", "jpeg", "png"].includes(ext)) {
      el = document.createElement("img");
      el.alt = "Vorschau von " + row.dataset.name;
      el.src = row.dataset.url;
    } else if (ext === "pdf") {
      // Seiten als Bilder vom Server: der PDF-Viewer des Browsers ist in Frames nicht überall verfügbar
      el = document.createElement("div");
      el.className = "preview-pages";
      el.setAttribute("aria-busy", "true");
      showPdfPages(row, el);
    } else {
      el = document.createElement("iframe");
      el.title = "Vorschau von " + row.dataset.name;
      el.src = row.dataset.url;
    }
    emptyEl.hidden = true;
    frameBox.appendChild(el);
  };

  const showPdfPages = async (row, box) => {
    let info = null;
    try {
      const r = await fetch(row.dataset.url + "/pages", { headers: { Accept: "application/json" } });
      if (r.ok) info = await r.json();
    } catch (_) { /* unten als Fehler behandelt */ }
    if (current !== row) return; // inzwischen anderer Beleg gewählt
    box.removeAttribute("aria-busy");
    if (!info || !info.shown) {
      const note = document.createElement("div");
      note.className = "preview-note";
      note.textContent = "Für dieses PDF gibt es keine Vorschau. Öffne es über den Pfeil oben rechts.";
      box.replaceWith(note);
      return;
    }
    for (let i = 0; i < info.shown; i++) {
      const img = document.createElement("img");
      img.src = `${row.dataset.url}/page/${i}`;
      img.alt = info.pages > 1 ? `Seite ${i + 1} von ${info.pages}` : "Vorschau von " + row.dataset.name;
      img.loading = i === 0 ? "eager" : "lazy";
      img.decoding = "async";
      // Platzhalter im A4-Format, danach echtes Seitenverhältnis (Querformat)
      img.addEventListener("load", () => img.classList.add("is-loaded"), { once: true });
      box.appendChild(img);
    }
    if (info.pages > info.shown) {
      const more = document.createElement("p");
      more.className = "preview-more";
      more.textContent = `Weitere ${info.pages - info.shown} Seiten im neuen Tab.`;
      box.appendChild(more);
    }
  };

  rows.forEach((row, i) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("input, a, button")) return;
      // Schmale Bildschirme haben kein Vorschaufenster: Beleg im neuen Tab öffnen
      if (!frameBox.offsetParent) return void window.open(row.dataset.url, "_blank", "noopener");
      preview(row);
    });
    checks[i].addEventListener("change", refresh);
    row.addEventListener("keydown", (e) => {
      if (e.target !== row) return;
      if (e.key === "ArrowDown" && rows[i + 1]) { e.preventDefault(); preview(rows[i + 1], { focus: true }); }
      else if (e.key === "ArrowUp" && rows[i - 1]) { e.preventDefault(); preview(rows[i - 1], { focus: true }); }
      else if (e.key === "Enter") { e.preventDefault(); preview(row); }
      else if (e.key === " " || e.key.toLowerCase() === "x") { e.preventDefault(); checks[i].checked = !checks[i].checked; refresh(); }
    });
  });

  all.addEventListener("change", () => { checks.forEach((c) => (c.checked = all.checked)); refresh(); });
  form.querySelector("[data-clear]").addEventListener("click", () => { checks.forEach((c) => (c.checked = false)); refresh(); });

  // Sobald das Vorschaufenster sichtbar ist, automatisch den ersten Beleg zeigen
  // (auch wenn das Fenster erst nachträglich breit genug gezogen wird)
  const autoPreview = () => { if (!current && rows.length && frameBox.offsetParent) preview(rows[0]); };
  autoPreview();
  window.addEventListener("resize", autoPreview, { passive: true });
  refresh();
})();
