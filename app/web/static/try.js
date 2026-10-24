// Interview mode panel: polls /try/<token>/status every 2 seconds (no websockets) and shows
// what happened to the visitor's lead. Text is set with textContent only (strict CSP).
(function () {
  "use strict";

  var root = document.getElementById("panel");
  if (!root) return;
  var URL_STATUS = root.getAttribute("data-status-url");
  var POLL_MS = 2000;
  var MAX_POLLS = 150; // about five minutes
  var polls = 0;
  var fmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  function when(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "" : fmt.format(d);
  }

  function render(step) {
    var li = root.querySelector('[data-step="' + step.key + '"]');
    if (!li) return;
    li.classList.toggle("is-done", !!step.done);
    li.classList.toggle("is-failed", !!step.failed);
    li.querySelector(".state").textContent = step.done ? "✓" : step.failed ? "✕" : "…";
    var parts = [];
    if (step.at) parts.push(when(step.at));
    if (step.key === "scored" && step.score !== null && step.score !== undefined) {
      parts.push("score " + step.score + (step.summary ? " · " + step.summary : ""));
    }
    if (step.detail) parts.push(step.detail);
    if (!step.done && !step.failed) parts.push("waiting…");
    var detail = li.querySelector(".detail");
    detail.textContent = parts.join(" · ");
    if (step.note) {
      var note = document.createElement("span");
      note.className = "note";
      note.textContent = step.note;
      detail.appendChild(note);
    }
  }

  function tick() {
    polls += 1;
    fetch(URL_STATUS, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        (data.steps || []).forEach(render);
        if (data.complete) polls = MAX_POLLS;
      })
      .catch(function () { /* keep polling; the free host may be waking up */ })
      .then(function () { if (polls < MAX_POLLS) window.setTimeout(tick, POLL_MS); });
  }

  tick();
})();
