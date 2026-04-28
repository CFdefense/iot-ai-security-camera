(function () {
  var cfgEl = document.getElementById("dashboard-ui-config");
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent.trim());
  const STREAM = cfg.mqtt_stream_url;
  const RECENT_EVENTS_URL = cfg.recent_events_url;
  const LS_KEY = "dashboard_mqtt_notifications_v1";

  const bellPanel = document.getElementById("bell-panel");
  const bellToggle = document.getElementById("bell-toggle");
  const bellList = document.getElementById("bell-list");
  const bellBadge = document.getElementById("bell-badge");
  const bellEmpty = document.getElementById("bell-empty");
  const toastStack = document.getElementById("toast-stack");

  if (!bellPanel || !bellToggle || !bellList || !bellBadge || !bellEmpty || !toastStack) {
    return;
  }

  let store = [];
  let unread = 0;
  let panelOpen = false;

  function topicShort(t) {
    if (!t || typeof t !== "string") return "";
    const parts = t.split("/");
    return parts[parts.length - 1] || t;
  }

  function summarize(entry) {
    const p = entry.payload || {};
    const kind = typeof p.event_type === "string" ? p.event_type : "mqtt";
    let detail = "";
    if (kind === "user_registered") {
      detail =
        typeof p.name === "string" ? p.name + " (#" + p.user_id + ")" : "(user_registered)";
    } else if (kind === "user_unregistered") {
      detail = typeof p.name === "string" ? p.name : "(user_unregistered)";
    } else if (kind === "access_granted") {
      detail =
        String(p.user || "") + " · " + (p.confidence != null ? Number(p.confidence).toFixed(2) : "");
    } else if (kind === "unknown_face_detected" || (entry.topic && entry.topic.indexOf("alert") !== -1)) {
      detail = typeof p.confidence === "number" ? "confidence " + String(p.confidence) : "";
    } else if (kind === "detection_toggle") {
      detail = p.enabled === true ? "on" : p.enabled === false ? "off" : "";
    } else detail = "";

    detail = detail.trim();
    return { kind: kind, detail: detail };
  }

  function renderBell() {
    bellList.innerHTML = "";
    if (store.length === 0) {
      bellEmpty.style.display = "block";
      return;
    }
    bellEmpty.style.display = "none";
    store.forEach(function (row) {
      var ssum = summarize(row);
      var kind = ssum.kind;
      var detail = ssum.detail;
      var ts = typeof row.payload.timestamp === "string" ? row.payload.timestamp : "";

      var li = document.createElement("li");

      var dEv = document.createElement("div");
      dEv.className = "ev";
      dEv.textContent = kind.replace(/_/g, " ") + (detail ? " — " + detail : "");

      li.appendChild(dEv);
      if (ts) {
        var dm = document.createElement("div");
        dm.className = "meta-sm";
        dm.textContent = formatTs(ts);
        li.appendChild(dm);
      }
      bellList.appendChild(li);
    });
  }

  function formatTs(ts) {
    var d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function saveStore(arr) {
    try {
      sessionStorage.setItem(LS_KEY, JSON.stringify(arr.slice(0, 80)));
    } catch (_e) {
      /* quota or private mode */
    }
  }

  function setBadge(n) {
    unread = Math.max(0, n);
    sessionStorage.setItem("dashboard_notif_unread", String(unread));
    if (unread > 0) {
      bellBadge.hidden = false;
      bellBadge.textContent = unread > 99 ? "99+" : String(unread);
    } else {
      bellBadge.hidden = true;
    }
  }

  function pushEntry(env, silent) {
    silent = !!silent;
    const received = Date.now();
    const row = Object.assign({}, env, { received: received });
    store.unshift(row);
    if (store.length > 80) store = store.slice(0, 80);
    saveStore(store);
    renderBell();
    if (!panelOpen && !silent) setBadge(unread + 1);
  }

  function loadRecent() {
    if (!RECENT_EVENTS_URL) return;
    fetch(RECENT_EVENTS_URL, { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : Promise.reject(new Error(String(r.status)));
      })
      .then(function (blob) {
        var events = blob && Array.isArray(blob.events) ? blob.events : [];
        for (var i = events.length - 1; i >= 0; i -= 1) {
          pushEntry(events[i], true);
        }
        setBadge(0);
      })
      .catch(function () {});
  }

  function isHeartbeatEvent(env) {
    const p = env.payload || {};
    if (p.event_type === "heartbeat") return true;
    const t = env.topic || "";
    return t.endsWith("/status") || t.indexOf("/status") !== -1;
  }

  function toast(kind, subtitle) {
    var isAlert = /alert|unknown/i.test(kind + " " + (subtitle || ""));
    var div = document.createElement("div");
    div.className = "toast" + (isAlert ? " alt" : "");

    var em = document.createElement("em");
    em.textContent = prettyEvent(kind);
    div.appendChild(em);

    if (subtitle) {
      var sd = document.createElement("div");
      sd.style.marginTop = ".35rem";
      sd.textContent = subtitle;
      div.appendChild(sd);
    }
    var dismissing = false;
    function dismissToast() {
      if (dismissing) return;
      dismissing = true;
      div.classList.add("closing");
      setTimeout(function () {
        div.remove();
      }, 280);
    }
    div.addEventListener("click", dismissToast);
    toastStack.appendChild(div);
    setTimeout(function () {
      dismissToast();
    }, 5600);
  }

  function prettyEvent(kind) {
    if (!kind || typeof kind !== "string") return "Notification";
    var words = kind.replace(/_/g, " ").trim().split(/\s+/);
    return words
      .map(function (w) {
        var up = w.toUpperCase();
        if (up === "API" || up === "MQTT") return up;
        return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
      })
      .join(" ");
  }

  function initAsyncForms() {
    var registerForm = document.getElementById("register-user-form");
    var detectionForm = document.getElementById("detection-toggle-form");
    var detectionStateEl = document.getElementById("detection-service-state");
    var usersCountEl = document.getElementById("registered-users-count");

    function setBusy(form, busy) {
      var btn = form ? form.querySelector('button[type="submit"]') : null;
      if (!btn) return;
      btn.disabled = !!busy;
      btn.style.opacity = busy ? "0.75" : "";
    }

    function submitFormAsync(form, onSuccess) {
      if (!form) return;
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        setBusy(form, true);
        fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json,text/html",
          },
        })
          .then(function (r) {
            if (!r.ok) throw new Error("request_failed");
            return r;
          })
          .then(function () {
            if (typeof onSuccess === "function") onSuccess();
          })
          .catch(function () {
            toast("error", "Action failed. Please try again.");
          })
          .finally(function () {
            setBusy(form, false);
          });
      });
    }

    submitFormAsync(registerForm, function () {
      var nameInput = registerForm ? registerForm.querySelector('input[name="name"]') : null;
      if (nameInput) nameInput.value = "";
    });

    submitFormAsync(detectionForm, function () {
      if (!detectionForm) return;
      var hidden = detectionForm.querySelector('input[name="enabled"]');
      var btn = detectionForm.querySelector('button[type="submit"]');
      if (!hidden || !btn) return;
      if (hidden.value === "off") {
        hidden.value = "on";
        btn.textContent = "Turn detection on";
        btn.classList.remove("on");
        btn.classList.add("off");
        if (detectionStateEl) {
          detectionStateEl.textContent = "Paused";
          detectionStateEl.classList.remove("active");
          detectionStateEl.classList.add("inactive");
        }
      } else {
        hidden.value = "off";
        btn.textContent = "Turn detection off";
        btn.classList.remove("off");
        btn.classList.add("on");
        if (detectionStateEl) {
          detectionStateEl.textContent = "Online";
          detectionStateEl.classList.remove("inactive");
          detectionStateEl.classList.add("active");
        }
      }
    });

    document.querySelectorAll(".unregister-user-form").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        if (!window.confirm("Unregister this user?")) return;
        setBusy(form, true);
        fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json,text/html",
          },
        })
          .then(function (r) {
            if (!r.ok) throw new Error("request_failed");
            return r.json();
          })
          .then(function () {
            var card = form.closest(".user-card");
            var userNameEl = card ? card.querySelector(".alert-main") : null;
            var userName = userNameEl && userNameEl.textContent ? userNameEl.textContent.trim() : "User";
            if (card) card.remove();
            if (usersCountEl) {
              var current = Number(usersCountEl.textContent || "0");
              if (Number.isFinite(current)) usersCountEl.textContent = String(Math.max(0, current - 1));
            }
          })
          .catch(function () {
            toast("error", "Unable to unregister user.");
          })
          .finally(function () {
            setBusy(form, false);
          });
      });
    });
  }

  document.addEventListener("dash:close-bell", function () {
    if (!panelOpen) return;
    panelOpen = false;
    bellPanel.classList.remove("open");
    bellToggle.setAttribute("aria-expanded", "false");
  });

  bellToggle.addEventListener("click", function (e) {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent("dash:close-status"));
    panelOpen = !panelOpen;
    bellPanel.classList.toggle("open", panelOpen);
    bellToggle.setAttribute("aria-expanded", panelOpen ? "true" : "false");
    if (panelOpen) {
      renderBell();
      setBadge(0);
    }
  });
  document.body.addEventListener("click", function () {
    if (panelOpen) {
      panelOpen = false;
      bellPanel.classList.remove("open");
      bellToggle.setAttribute("aria-expanded", "false");
    }
  });
  bellPanel.addEventListener("click", function (e) {
    e.stopPropagation();
  });

  const es = new EventSource(STREAM);
  es.onmessage = function (ev) {
    try {
      var env = JSON.parse(ev.data);
      var hb = isHeartbeatEvent(env);
      pushEntry(env, hb);
      if (hb) return;
      var p = env.payload || {};
      var evt = typeof p.event_type === "string" ? p.event_type : topicShort(env.topic);
      var sum = summarize({ topic: env.topic, payload: p });
      toast(evt, sum.detail);
    } catch (_e) {
      /* ignore malformed chunks */
    }
  };
  es.onerror = function () {
    /* browser auto-reconnects EventSource */
  };

  renderBell();
  setBadge(0);
  loadRecent();
  initAsyncForms();
})();

(function () {
  var cfgEl = document.getElementById("dashboard-ui-config");
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent.trim());
  var statusUrl = cfg.status_json_url;
  var statusToggle = document.getElementById("status-toggle");
  var statusPanel = document.getElementById("status-panel");
  var statusTsLine = document.getElementById("status-ts-line");
  var statusPayloadExpl = document.getElementById("status-payload-expl");
  var statusComponents = document.getElementById("status-components");
  var pulseTpl = document.getElementById("pulse-icon-tpl");
  if (!statusUrl || !statusToggle || !statusPanel) {
    return;
  }
  var statusPanelOpen = false;

  function clonePulseIcon() {
    if (!pulseTpl) return null;
    return document.importNode(pulseTpl.content, true);
  }

  if (pulseTpl && statusToggle) {
    var wrap = document.createElement("span");
    wrap.className = "heartbeat-mark";
    wrap.setAttribute("aria-hidden", "true");
    wrap.appendChild(clonePulseIcon());
    statusToggle.appendChild(wrap);
  }

  function applyPayload(data) {
    var ts = data && data.last_status_at ? data.last_status_at : "";
    var payload = data && data.status_payload && typeof data.status_payload === "object" ? data.status_payload : {};
    var uptime = typeof payload.uptime_sec === "number" ? String(payload.uptime_sec) + "s" : "n/a";
    var sensorId = typeof payload.sensor_id === "string" && payload.sensor_id ? payload.sensor_id : "n/a";
    var components = payload && typeof payload.components === "object" ? payload.components : {};
    if (statusTsLine) {
      statusTsLine.textContent = ts || "—";
    }
    if (statusPayloadExpl) {
      statusPayloadExpl.textContent = "Sensor " + sensorId + " · uptime " + uptime;
    }
    if (statusComponents) {
      statusComponents.innerHTML = "";
      function addRow(name, state, mode) {
        var row = document.createElement("div");
        row.className = "status-row";
        var nm = document.createElement("span");
        nm.className = "status-name";
        nm.textContent = name;
        var pill = document.createElement("span");
        pill.className = "status-pill " + mode;
        pill.textContent = state;
        row.appendChild(nm);
        row.appendChild(pill);
        statusComponents.appendChild(row);
      }
      function componentState(componentName) {
        var entry = components ? components[componentName] : null;
        if (typeof entry === "string") return entry.toLowerCase();
        if (entry && typeof entry === "object" && typeof entry.state === "string") {
          return entry.state.toLowerCase();
        }
        if (componentName === "mqtt") {
          if (payload.connected === true) return "up";
          if (payload.connected === false) return "down";
        }
        return "down";
      }
      function renderComponent(label, key) {
        var state = componentState(key);
        if (state === "up") addRow(label, "UP", "ok");
        else addRow(label, "DOWN", "down");
      }

      renderComponent("API", "api");
      renderComponent("MQTT", "mqtt");
      renderComponent("Camera", "camera");
      renderComponent("Sensor", "sensor");
    }
  }

  function refreshStatus() {
    fetch(statusUrl, { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : Promise.reject(new Error(String(r.status)));
      })
      .then(applyPayload)
      .catch(function () {});
  }

  document.addEventListener("dash:close-status", function () {
    if (!statusPanelOpen) return;
    statusPanelOpen = false;
    statusPanel.classList.remove("open");
    statusToggle.setAttribute("aria-expanded", "false");
  });

  statusToggle.addEventListener("click", function (e) {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent("dash:close-bell"));
    statusPanelOpen = !statusPanelOpen;
    statusPanel.classList.toggle("open", statusPanelOpen);
    statusToggle.setAttribute("aria-expanded", statusPanelOpen ? "true" : "false");
    if (statusPanelOpen) refreshStatus();
  });
  document.body.addEventListener("click", function () {
    if (statusPanelOpen) {
      statusPanelOpen = false;
      statusPanel.classList.remove("open");
      statusToggle.setAttribute("aria-expanded", "false");
    }
  });
  statusPanel.addEventListener("click", function (e) {
    e.stopPropagation();
  });

  refreshStatus();
  setInterval(refreshStatus, 15000);
})();
