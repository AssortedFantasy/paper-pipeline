(function () {
  "use strict";

  var wrappedColumns = {
    paper: false,
    authors: false,
    citekey: false
  };
  var workOptionStates = {};
  var columnResize = null;

  function applyPaperWrap() {
    var table = document.querySelector(".papers-table");
    Object.keys(wrappedColumns).forEach(function (column) {
      var active = wrappedColumns[column];
      if (table) table.classList.toggle("wrap-" + column + "-text", active);
      var control = document.querySelector("[data-paper-wrap='" + column + "']");
      if (!control) return;
      control.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setColumnWidth(column, width) {
    var results = document.getElementById("paper-results");
    if (!results) return;
    results.style.setProperty("--" + column + "-column-width", Math.round(width) + "px");
  }

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-paper-sort]");
    if (!control) return;
    document.getElementById("paper-sort").value = control.dataset.paperSort;
    document.getElementById("paper-direction").value = control.dataset.paperDirection;
  }, true);

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-paper-wrap]");
    if (!control) return;
    var column = control.dataset.paperWrap;
    wrappedColumns[column] = !wrappedColumns[column];
    applyPaperWrap();
  });

  document.addEventListener("pointerdown", function (event) {
    var control = event.target.closest("[data-paper-resize]");
    if (!control) return;
    var header = control.closest("th");
    if (!header) return;
    columnResize = {
      column: control.dataset.paperResize,
      minWidth: Number(control.dataset.minWidth),
      startWidth: header.getBoundingClientRect().width,
      startX: event.clientX
    };
    document.body.classList.add("is-resizing-column");
    event.preventDefault();
  });

  document.addEventListener("pointermove", function (event) {
    if (!columnResize) return;
    setColumnWidth(
      columnResize.column,
      Math.max(columnResize.minWidth, columnResize.startWidth + event.clientX - columnResize.startX)
    );
  });

  function finishColumnResize() {
    columnResize = null;
    document.body.classList.remove("is-resizing-column");
  }

  document.addEventListener("pointerup", finishColumnResize);
  document.addEventListener("pointercancel", finishColumnResize);

  document.addEventListener("keydown", function (event) {
    var control = event.target.closest("[data-paper-resize]");
    if (!control || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
    var header = control.closest("th");
    if (!header) return;
    var delta = event.key === "ArrowRight" ? 16 : -16;
    setColumnWidth(
      control.dataset.paperResize,
      Math.max(Number(control.dataset.minWidth), header.getBoundingClientRect().width + delta)
    );
    event.preventDefault();
  });

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-paper-selection]");
    if (!control) return;
    var mode = control.dataset.paperSelection;
    document.querySelectorAll("#paper-selection tbody input[name='citekeys']").forEach(function (input) {
      var bulkSelectable = input.dataset.bulkSelectable === "true";
      input.checked = bulkSelectable && (mode === "all" ||
        (mode === "pending" && input.dataset.conversionPending === "true") ||
        (mode === "pages" && input.dataset.pagesPending === "true"));
    });
    updateWorkControls();
  });

  function optionState(option) {
    return workOptionStates[option] || "off";
  }

  function applyWorkConfiguration() {
    var form = document.getElementById("paper-selection");
    if (!form) return;
    form.querySelectorAll("[data-work-option]").forEach(function (control) {
      var option = control.dataset.workOption;
      var state = optionState(option);
      control.dataset.workState = state;
      control.setAttribute(
        "aria-checked",
        state === "overwrite" ? "mixed" : state === "run" ? "true" : "false"
      );
      var name = "mode_" + option;
      var input = form.querySelector("input[type='hidden'][name='" + name + "']");
      if (state === "off") {
        if (input) input.remove();
        return;
      }
      if (!input) {
        input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        form.appendChild(input);
      }
      input.value = state;
    });
    updateWorkControls();
  }

  function updateWorkControls() {
    var form = document.getElementById("paper-selection");
    if (!form) return;
    var selectedPapers = form.querySelectorAll(
      "tbody input[name='citekeys']:checked"
    ).length;
    var configured = Object.keys(workOptionStates).filter(function (option) {
      return optionState(option) !== "off";
    }).length;
    form.querySelectorAll("[data-selected-paper-count]").forEach(function (count) {
      count.textContent = String(selectedPapers);
    });
    form.querySelectorAll("[data-configured-count]").forEach(function (count) {
      count.textContent = String(configured);
    });
    var queue = form.querySelector(".queue-work-button");
    if (queue) queue.disabled = selectedPapers === 0 || configured === 0;
  }

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-work-option]");
    if (!control) return;
    var option = control.dataset.workOption;
    var state = optionState(option);
    workOptionStates[option] =
      state === "off" ? "run" : state === "run" ? "overwrite" : "off";
    applyWorkConfiguration();
  });

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-work-config-open]");
    if (!control) return;
    var dialog = document.getElementById("work-config-dialog");
    if (dialog && !dialog.open) dialog.showModal();
  });

  document.addEventListener("click", function (event) {
    var control = event.target.closest("[data-work-config-close]");
    if (!control) return;
    var dialog = control.closest("dialog");
    if (dialog) dialog.close();
  });

  document.addEventListener("click", function (event) {
    if (event.target.matches(".work-config-dialog")) event.target.close();
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches("#paper-selection tbody input[name='citekeys']")) {
      updateWorkControls();
    }
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
  document.addEventListener("htmx:load", function () {
    applyPaperWrap();
    applyWorkConfiguration();
  });
  applyPaperWrap();
  applyWorkConfiguration();
})();
