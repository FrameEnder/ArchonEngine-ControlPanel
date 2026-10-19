/* Progressive enhancement only: every panel is visible without this file, so
   nothing is ever hidden from a reader who has JavaScript turned off. */
(function () {
  "use strict";

  /* ── image viewer ─────────────────────────────────────────────────
     Opens in place rather than a new tab. The markup is plain links, so
     with this file absent an image still opens normally. */
  var viewer = null;
  var shots = [];
  var at = 0;
  var opener = null;

  function buildViewer() {
    var box = document.createElement("div");
    box.className = "lightbox";
    box.hidden = true;
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Image viewer");
    box.innerHTML =
      '<button type="button" class="lb-close" aria-label="Close image viewer">\u2715</button>' +
      '<button type="button" class="lb-step prev" aria-label="Previous image">\u2039</button>' +
      '<img alt="">' +
      '<button type="button" class="lb-step next" aria-label="Next image">\u203a</button>' +
      '<span class="lb-count"></span>';

    box.addEventListener("click", function (event) {
      // Clicking the backdrop closes; clicking the picture itself does not.
      if (event.target === box) { close(); }
    });
    box.querySelector(".lb-close").addEventListener("click", close);
    box.querySelector(".prev").addEventListener("click", function () { step(-1); });
    box.querySelector(".next").addEventListener("click", function () { step(1); });
    document.body.appendChild(box);
    return box;
  }

  function render() {
    var image = viewer.querySelector("img");
    image.src = shots[at];
    var many = shots.length > 1;
    viewer.querySelector(".prev").hidden = !many;
    viewer.querySelector(".next").hidden = !many;
    var count = viewer.querySelector(".lb-count");
    count.hidden = !many;
    count.textContent = many ? (at + 1) + " / " + shots.length : "";
  }

  function step(delta) {
    at = (at + delta + shots.length) % shots.length;
    render();
  }

  function open(list, index, trigger) {
    if (!viewer) { viewer = buildViewer(); }
    shots = list;
    at = index;
    opener = trigger || null;
    render();
    viewer.hidden = false;
    document.body.classList.add("lb-open");
    viewer.querySelector(".lb-close").focus();
  }

  function close() {
    if (!viewer || viewer.hidden) { return; }
    viewer.hidden = true;
    viewer.querySelector("img").src = "";
    document.body.classList.remove("lb-open");
    if (opener) { opener.focus(); }
  }

  function setupGallery(gallery) {
    var links = gallery.querySelectorAll("a.shot");
    var urls = [];
    for (var i = 0; i < links.length; i++) { urls.push(links[i].getAttribute("href")); }
    for (var j = 0; j < links.length; j++) {
      (function (link, index) {
        link.addEventListener("click", function (event) {
          if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) { return; }
          event.preventDefault();
          open(urls, index, link);
        });
      })(links[j], j);
    }
  }

  function setupFileLabel(input) {
    var label = input.parentNode.querySelector("[data-filecount-label]");
    if (!label) { return; }
    var original = label.textContent;
    input.addEventListener("change", function () {
      var n = input.files ? input.files.length : 0;
      label.textContent = n === 0 ? original
        : (n === 1 ? "1 image selected" : n + " images selected");
    });
  }

  function init() {
    var galleries = document.querySelectorAll("[data-gallery]");
    for (var g = 0; g < galleries.length; g++) { setupGallery(galleries[g]); }

    var files = document.querySelectorAll("[data-filecount]");
    for (var f = 0; f < files.length; f++) { setupFileLabel(files[f]); }

    document.addEventListener("keydown", function (event) {
      if (!viewer || viewer.hidden) { return; }
      if (event.key === "Escape") { close(); }
      else if (event.key === "ArrowLeft" && shots.length > 1) { step(-1); }
      else if (event.key === "ArrowRight" && shots.length > 1) { step(1); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

// Copy buttons: <button data-copy="text">. Clipboard needs a secure context
// (https or localhost); over plain http on the LAN fall back to selecting it.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const text = button.dataset.copy;
  // Only the label changes: an icon inside the button stays, and a second click
  // while it says "Copied" doesn't make "Copied" the label for good.
  const done = (label) => {
    if (!button.dataset.copyLabel) {
      button.dataset.copyLabel = button.innerHTML;
      button.setAttribute("aria-live", "polite");
    }
    clearTimeout(button._copyTimer);
    const icon = button.querySelector("svg");
    button.textContent = "";
    if (icon) button.append(icon.cloneNode(true));
    button.append(label);
    button._copyTimer = setTimeout(() => {
      button.innerHTML = button.dataset.copyLabel;
      delete button.dataset.copyLabel;
    }, 1200);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => done("Copied"), () => done("Failed"));
  } else {
    window.prompt("Copy this:", text);
  }
});

// Select-all checkboxes: <input type="checkbox" data-check-all="name">.
document.addEventListener("change", (event) => {
  const box = event.target.closest("[data-check-all]");
  if (!box) return;
  const form = box.closest("form") || document;
  form.querySelectorAll(`input[type=checkbox][name="${box.dataset.checkAll}"]`)
      .forEach((input) => { input.checked = box.checked; });
});

// Shop: picking a character submits straight away.
document.querySelectorAll("select[data-autosubmit]").forEach((select) => {
    select.addEventListener("change", () => select.form.submit());
});

// Phones: the dashboard's section menu and the top menu scroll sideways; keep the current one in view.
document.querySelectorAll(".subnav a.on, .topbar .mainnav a.on").forEach((link) => {
  const row = link.parentElement;
  if (row.scrollWidth > row.clientWidth) {
    row.scrollLeft = link.offsetLeft - (row.clientWidth - link.offsetWidth) / 2;
  }
});

// Live previews: a form with data-preview-form copies its [data-preview-X] fields
// into the page's [data-preview-X-out] elements as you type (Announcements).
document.querySelectorAll("[data-preview-form]").forEach((form) => {
  form.querySelectorAll("input, textarea").forEach((field) => {
    const key = [...field.attributes].map((a) => a.name).find((n) => n.startsWith("data-preview-"));
    if (!key) return;
    const outs = document.querySelectorAll(`[${key}-out]`);
    const fallback = outs.length ? outs[0].textContent : "";
    const update = () => outs.forEach((out) => {
      out.textContent = field.value || (key.endsWith("title") ? fallback : "");
    });
    field.addEventListener("input", update);
    update();
  });
});

/* ── shop pages ── */
// Shop: the big picture on a listing opens the image viewer at its place in the
// gallery strip (app.js wires the strip's a.shot links to the viewer). Without
// JavaScript it is a plain link to the picture.
document.querySelectorAll("[data-shop-open]").forEach((hero) => {
  hero.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    const strip = hero.parentElement.querySelector("[data-gallery]");
    const shot = strip && strip.querySelectorAll("a.shot")[Number(hero.dataset.shopOpen) || 0];
    if (!shot) return;
    event.preventDefault();
    shot.click();
  });
});

/* ── people pages ── */
/* people: the profile's full-stats pop-up. Without this file it is a :target
   overlay opened and closed by plain links; this adds Escape, backdrop click,
   focus handling, a scroll lock, and closes it without leaving a #hash. */
(function () {
  "use strict";
  var dialogs = document.querySelectorAll(".pf-dialog[role=dialog]");
  if (!dialogs.length) { return; }
  document.documentElement.classList.add("pf-js");

  var current = null;
  var opener = null;
  var FOCUSABLE = 'a[href]:not([tabindex="-1"]), button:not([disabled]), input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex="-1"])';

  function cleanUrl() {
    return location.pathname + location.search;
  }

  function open(dialog, trigger) {
    if (current && current !== dialog) { close(false); }
    current = dialog;
    opener = trigger || document.querySelector('[data-pf-open="' + dialog.id + '"]');
    dialog.classList.add("is-open");
    document.body.classList.add("pf-locked");
    history.replaceState(history.state, "", cleanUrl() + "#" + dialog.id);
    var panel = dialog.querySelector(".pf-dialog-panel");
    (panel || dialog).focus({ preventScroll: true });
    dialog.scrollTop = 0;
  }

  function close(restore) {
    if (!current) { return; }
    current.classList.remove("is-open");
    document.body.classList.remove("pf-locked");
    history.replaceState(history.state, "", cleanUrl());
    current = null;
    if (restore !== false && opener) { opener.focus(); }
    opener = null;
  }

  document.addEventListener("click", function (event) {
    var openLink = event.target.closest("[data-pf-open]");
    if (openLink) {
      var target = document.getElementById(openLink.getAttribute("data-pf-open"));
      if (target && !(event.metaKey || event.ctrlKey || event.shiftKey)) {
        event.preventDefault();
        open(target, openLink);
      }
      return;
    }
    if (!current) { return; }
    if (event.target.closest("[data-pf-close]") || event.target.classList.contains("pf-dialog-backdrop")
        || event.target === current) {
      event.preventDefault();
      close();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (!current) { return; }
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key !== "Tab") { return; }
    // Keep Tab inside the pop-up while it is open.
    var items = Array.prototype.filter.call(current.querySelectorAll(FOCUSABLE), function (el) {
      return el.offsetParent !== null;
    });
    if (!items.length) { return; }
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || !current.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });

  // Hash links elsewhere on the page (or Back/Forward) still work.
  window.addEventListener("hashchange", function () {
    var target = location.hash ? document.getElementById(location.hash.slice(1)) : null;
    if (target && target.classList.contains("pf-dialog")) { open(target); }
    else if (current) { close(false); }
  });

  // A shared link such as /profile/bob?tab=characters#char-1001 opens it at load.
  if (location.hash) {
    var initial = document.getElementById(location.hash.slice(1));
    if (initial && initial.classList.contains("pf-dialog")) { open(initial); }
  }
})();

// Phones: the settings menu scrolls sideways; keep the current section in view.
(function () {
  var on = document.querySelector(".pf-snav a.on");
  if (!on) { return; }
  var row = on.parentElement;
  if (row.scrollWidth > row.clientWidth) {
    row.scrollLeft = on.offsetLeft - (row.clientWidth - on.offsetWidth) / 2;
  }
})();
