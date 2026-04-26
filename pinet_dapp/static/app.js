(function () {
  "use strict";

  var endpointInput = document.getElementById("satellite-endpoint");
  var openDashboard = document.getElementById("open-dashboard");
  var bridgeStatus = document.getElementById("bridge-status");
  var DEFAULT_SATELLITE_PORT = "8080";
  var DEFAULT_ENDPOINT = window.location.protocol + "//" + window.location.hostname + ":" + DEFAULT_SATELLITE_PORT;

  function setStatus(element, state, text) {
    element.className = "status " + state;
    element.textContent = text;
  }

  function clearList(element) {
    while (element.firstChild) {
      element.removeChild(element.firstChild);
    }
  }

  function addRow(list, key, value) {
    var dt = document.createElement("dt");
    var dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = value === undefined || value === null || value === "" ? "—" : String(value);
    list.appendChild(dt);
    list.appendChild(dd);
  }

  function renderObject(listId, values) {
    var list = document.getElementById(listId);
    clearList(list);
    Object.keys(values).forEach(function (key) {
      addRow(list, key, values[key]);
    });
  }

  function endpoint() {
    return localStorage.getItem("zeddSatelliteEndpoint") || DEFAULT_ENDPOINT;
  }

  function normalizeEndpoint(value) {
    var candidate = (value || DEFAULT_ENDPOINT).trim();
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate) && !/^https?:\/\//i.test(candidate)) {
      return DEFAULT_ENDPOINT;
    }
    if (!/^https?:\/\//i.test(candidate)) {
      candidate = window.location.protocol + "//" + candidate;
    }
    try {
      var parsed = new URL(candidate);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        return DEFAULT_ENDPOINT;
      }
      parsed.hash = "";
      parsed.search = "";
      return parsed.href.replace(/\/+$/, "");
    } catch (error) {
      return DEFAULT_ENDPOINT;
    }
  }

  function setEndpoint(value) {
    var clean = normalizeEndpoint(value);
    localStorage.setItem("zeddSatelliteEndpoint", clean);
    endpointInput.value = clean;
    openDashboard.href = clean + "/";
  }

  function summarizeMinima(data) {
    if (!data || typeof data !== "object") {
      return { Status: "Received response", Response: JSON.stringify(data) };
    }
    return {
      Status: data.status || data.response || "Received response",
      Version: data.version || data.minima_version || "Unknown",
      Uptime: data.uptime || "Unknown",
      Chain: data.chain || data.network || "Unknown"
    };
  }

  function refreshPiNet() {
    setStatus(bridgeStatus, "pending", "Requesting PiNet host status");

    window.PiNetSdk.system.getStats()
      .then(function (stats) {
        setStatus(bridgeStatus, "ok", "Connected to PiNet OS bridge");
        renderObject("system-stats", {
          CPU: stats.cpu || stats.cpu_pct || "Unknown",
          RAM: stats.ram || stats.memory || "Unknown",
          Temperature: stats.temp || stats.cpu_temp_c || "Unknown",
          Disk: stats.disk || stats.disk_pct || "Unknown"
        });
      })
      .catch(function (error) {
        setStatus(bridgeStatus, "error", error.message);
        renderObject("system-stats", { Status: "Unavailable", Error: error.message });
      });

    window.PiNetSdk.minima.cmd("status")
      .then(function (result) {
        renderObject("minima-status", summarizeMinima(result));
      })
      .catch(function (error) {
        renderObject("minima-status", { Status: "Unavailable", Error: error.message });
      });
  }

  function refreshSatellite() {
    fetch(endpoint() + "/api/status", {
      headers: { "Accept": "application/json" },
      cache: "no-store"
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        var station = payload.station || {};
        var nextPass = payload.next_pass || {};
        renderObject("satellite-status", {
          Station: station.name || "Unnamed station",
          Captures: payload.capture_count,
          Images: payload.image_count,
          "Next pass": nextPass.satellite || "None scheduled",
          "Pass error": payload.pass_prediction_error || "None"
        });
      })
      .catch(function (error) {
        renderObject("satellite-status", {
          Status: "Open dashboard directly if API access is blocked",
          Error: error.message
        });
      });
  }

  document.getElementById("refresh-pinet").addEventListener("click", function () {
    refreshPiNet();
    refreshSatellite();
  });

  document.getElementById("notify").addEventListener("click", function () {
    window.PiNetSdk.notify("Zedd-Satellite", "PiNet OS bridge is connected")
      .catch(function (error) {
        setStatus(bridgeStatus, "error", error.message);
      });
  });

  document.getElementById("save-endpoint").addEventListener("click", function () {
    setEndpoint(endpointInput.value || DEFAULT_ENDPOINT);
    refreshSatellite();
  });

  setEndpoint(endpoint());
  refreshPiNet();
  refreshSatellite();
}());
