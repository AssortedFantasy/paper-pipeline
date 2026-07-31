(function () {
  "use strict";

  var source = null;

  function disconnect() {
    if (!source) return;
    source.close();
    source = null;
  }

  function trigger(name, detail) {
    htmx.trigger(document.body, name, detail || {});
  }

  function forward(event) {
    trigger("sse:" + event.type, {
      data: event.data,
      value: event.data
    });
  }

  function connect() {
    disconnect();

    var url = document.body.dataset.eventsUrl;
    if (!url) return;

    source = new EventSource(url, { withCredentials: true });
    source.addEventListener("state", forward);
    source.addEventListener("progress", forward);
    source.addEventListener("open", function () {
      trigger("htmx:sseOpen", { source: source });
    });
    source.addEventListener("error", function (event) {
      trigger("htmx:sseError", { error: event, source: source });
    });
  }

  window.addEventListener("pageshow", connect);
  window.addEventListener("pagehide", disconnect);
})();
