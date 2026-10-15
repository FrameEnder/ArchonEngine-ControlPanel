/* Progressive enhancement only: every panel is visible without this file, so
   nothing is ever hidden from a reader who has JavaScript turned off. */
(function () {
  "use strict";

  function setupScrubber(root) {
    var panels = root.querySelectorAll("[data-scrub-panel]");
    if (panels.length < 2) { return; }

    var label = root.querySelector("[data-scrub-label]");
    var count = root.querySelector("[data-scrub-count]");
    var prev = root.querySelector("[data-scrub-prev]");
    var next = root.querySelector("[data-scrub-next]");
    var index = 0;

    function show(target) {
      index = (target + panels.length) % panels.length;
      for (var i = 0; i < panels.length; i++) {
        panels[i].hidden = i !== index;
      }
      if (label) { label.textContent = panels[index].getAttribute("data-title") || ""; }
      if (count) { count.textContent = (index + 1) + " of " + panels.length; }
    }

    if (prev) { prev.addEventListener("click", function () { show(index - 1); }); }
    if (next) { next.addEventListener("click", function () { show(index + 1); }); }
    root.classList.add("scrub-ready");
    show(0);
  }

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
    var roots = document.querySelectorAll("[data-scrub]");
    for (var i = 0; i < roots.length; i++) { setupScrubber(roots[i]); }

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
  const done = (label) => {
    const old = button.textContent;
    button.textContent = label;
    setTimeout(() => { button.textContent = old; }, 1200);
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
