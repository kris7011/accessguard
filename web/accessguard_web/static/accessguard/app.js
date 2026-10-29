// Live scan status: polls the status endpoint and reloads when the scan finishes.
// Progressive enhancement only; every page works without JavaScript.
(function () {
  "use strict";
  var root = document.querySelector("[data-scan-status-url]");
  if (!root) {
    return;
  }
  var url = root.getAttribute("data-scan-status-url");
  var bar = root.querySelector("[data-progress-bar]");
  var text = root.querySelector("[data-progress-text]");
  var label = root.querySelector("[data-status-label]");
  var failures = 0;

  function update() {
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        failures = 0;
        if (bar) {
          bar.value = data.percent;
        }
        if (text) {
          text.textContent = data.text + " · " + data.requests_used + " requests";
        }
        if (label) {
          label.textContent = data.status_label;
        }
        if (data.active) {
          window.setTimeout(update, 2000);
        } else {
          window.location.reload();
        }
      })
      .catch(function () {
        failures += 1;
        if (text && failures > 2) {
          text.textContent = "Lost connection to AccessGuard; retrying…";
        }
        window.setTimeout(update, Math.min(2000 * failures, 15000));
      });
  }
  window.setTimeout(update, 1500);
})();

// Copy buttons for runner tokens and commands.
(function () {
  "use strict";
  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-copy-target"));
      if (!target || !navigator.clipboard) {
        return;
      }
      navigator.clipboard.writeText(target.textContent.trim()).then(function () {
        button.textContent = "Copied";
      });
    });
  });
})();
