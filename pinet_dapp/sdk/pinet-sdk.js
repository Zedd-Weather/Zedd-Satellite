(function () {
  "use strict";

  var DEFAULT_TIMEOUT_MS = 30000;
  var RESPONSE_TYPE = "pinet-bridge-response";
  var REQUEST_TYPE = "pinet-bridge-request";
  var EVENT_TYPE = "pinet-bridge-event";
  var listeners = {};

  function requestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "zedd-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function call(method, params, options) {
    var id = requestId();
    var timeoutMs = options && options.timeoutMs ? options.timeoutMs : DEFAULT_TIMEOUT_MS;

    return new Promise(function (resolve, reject) {
      var timeout = window.setTimeout(function () {
        window.removeEventListener("message", handler);
        reject(new Error("PiNet bridge call timed out"));
      }, timeoutMs);

      function handler(event) {
        var data = event.data || {};
        if (data.type !== RESPONSE_TYPE || data.requestId !== id) {
          return;
        }
        window.clearTimeout(timeout);
        window.removeEventListener("message", handler);
        if (data.success) {
          resolve(data.data);
        } else {
          reject(new Error(data.error || "PiNet bridge call failed"));
        }
      }

      window.addEventListener("message", handler);
      window.parent.postMessage({
        type: REQUEST_TYPE,
        requestId: id,
        method: method,
        params: params || {}
      }, "*");
    });
  }

  function on(eventName, callback) {
    if (!listeners[eventName]) {
      listeners[eventName] = [];
    }
    listeners[eventName].push(callback);
    return function () {
      listeners[eventName] = (listeners[eventName] || []).filter(function (listener) {
        return listener !== callback;
      });
    };
  }

  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type !== EVENT_TYPE || !data.event) {
      return;
    }
    (listeners[data.event] || []).forEach(function (listener) {
      listener(data.data);
    });
  });

  window.PiNetSdk = {
    call: call,
    on: on,
    system: {
      getStats: function () {
        return call("system.getStats");
      }
    },
    minima: {
      cmd: function (command) {
        return call("minima.cmd", { command: command });
      }
    },
    notify: function (title, body) {
      return call("notify", { title: title, body: body });
    }
  };
}());
