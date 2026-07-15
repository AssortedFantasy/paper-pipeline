(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-paper-sort]");
    if (!control) return;
    document.getElementById("paper-sort").value = control.dataset.paperSort;
    document.getElementById("paper-direction").value = control.dataset.paperDirection;
  }, true);

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-paper-selection]");
    if (!control) return;
    var mode = control.dataset.paperSelection;
    document.querySelectorAll("#paper-selection tbody input[name='citekeys']").forEach(function (input) {
      input.checked = mode === "all" ||
        (mode === "pending" && input.dataset.conversionPending === "true");
    });
  });

  function payload(event) {
    var detail = event.detail || {};
    var raw = detail.data || detail.value || detail;
    try {
      return typeof raw === "string" ? JSON.parse(raw) : null;
    } catch (_error) {
      return null;
    }
  }

  function updateLive(event) {
    var job = payload(event);
    if (!job || !job.citekey) return;
    var row = document.querySelector("tr[data-citekey='" + CSS.escape(job.citekey) + "']");
    if (!row) return;
    if (["succeeded", "failed", "cancelled"].indexOf(job.state) >= 0) {
      htmx.ajax("GET", "/papers/row/" + encodeURIComponent(job.citekey), {
        target: row,
        swap: "outerHTML"
      });
      return;
    }
    var cell = row.querySelector(".live-job-status");
    if (!cell) return;
    var message = job.progress || job.label || "Waiting";
    cell.innerHTML = "<span class='badge badge-" + job.state + "'>" + job.state +
      "</span><small></small>";
    cell.querySelector("small").textContent = message;
  }

  document.addEventListener("sse:state", updateLive);
  document.addEventListener("sse:progress", updateLive);
})();
