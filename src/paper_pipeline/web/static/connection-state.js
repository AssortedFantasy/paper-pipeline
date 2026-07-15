(function () {
  "use strict";

  function swapState(templateId) {
    var target = document.getElementById("connection-status");
    var template = document.getElementById(templateId);
    if (target && template) {
      htmx.swap(target, template.innerHTML, { swapStyle: "innerHTML" });
    }
  }

  document.addEventListener("htmx:sseOpen", function () {
    swapState("live-connected-template");
  });
  document.addEventListener("htmx:sseError", function () {
    swapState("live-disconnected-template");
  });
})();
