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
    if (e.persisted) document.querySelectorAll(".is-loading").forEach((b) => b.classList.remove("is-loading"));
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

    frameBox.querySelector("iframe, img")?.remove();
    const ext = row.dataset.ext;
    let el;
    if (["jpg", "jpeg", "png"].includes(ext)) {
      el = document.createElement("img");
      el.alt = "Vorschau von " + row.dataset.name;
      el.src = row.dataset.url;
    } else {
      el = document.createElement("iframe");
      el.title = "Vorschau von " + row.dataset.name;
      el.src = row.dataset.url + (ext === "pdf" ? "#toolbar=0&navpanes=0&view=FitH" : "");
    }
    emptyEl.hidden = true;
    frameBox.appendChild(el);
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

  // Auf großen Bildschirmen direkt den ersten Beleg zeigen
  if (rows.length && window.matchMedia("(min-width: 1101px)").matches) preview(rows[0]);
  refresh();
})();
