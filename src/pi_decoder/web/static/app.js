/* Pi-Decoder — Web UI logic */

(function () {
  "use strict";

  // ── state ─────────────────────────────────────────────────────────

  let statusWs = null;
  let previewWs = null;
  let activeTab = 0;
  var pageClosing = false;
  var _logAutoTimer = null;
  var _logRawText = "";
  var _setupChecks = [];
  var _setupBannerDismissed = false;
  var _lastSetupKey = "";
  var _statusReconnectTimer = null;
  var _previewReconnectTimer = null;
  var _lastPreviewUrl = null;
  var _filterTimer = null;
  var _hotspotActive = false;

  window.addEventListener("beforeunload", function () { pageClosing = true; });

  // ── DOM element cache ──────────────────────────────────────────────
  var _el = {};
  function initElementCache() {
    var ids = [
      "headerBadge", "headerName", "statCpu", "statTemp", "statMem",
      "statUptime", "sysHostname", "netHostname", "mpvStatusLine",
      "overlayStatusLine", "previewImg", "previewPlaceholder",
      "statNetwork", "networkCard", "statTv", "tvStatusCard",
      "netType", "netIp", "netSsid", "netSignal", "netStatusCard",
      "setupBanner", "setupChecklist",
      "hostnameDisplay", "toastContainer", "hwdecCurrent",
      "mpvPerfLine", "mpvRes", "mpvFps", "mpvDrops",
    ];
    ids.forEach(function (id) { _el[id] = document.getElementById(id); });
  }

  // ── toast queue ───────────────────────────────────────────────────

  var _toastQueue = [];
  var MAX_TOASTS = 3;

  function toast(msg, type) {
    type = type || "success";
    var container = document.getElementById("toastContainer");
    var el = document.createElement("div");
    el.className = "toast-item " + type;
    el.textContent = msg;
    el.addEventListener("click", function () { dismissToast(el); });
    container.appendChild(el);
    _toastQueue.push(el);

    // enforce max visible
    while (_toastQueue.length > MAX_TOASTS) {
      dismissToast(_toastQueue[0]);
    }

    // auto-dismiss (errors persist until clicked)
    if (type !== "error") {
      setTimeout(function () { dismissToast(el); }, 3500);
    }
  }

  function dismissToast(el) {
    var idx = _toastQueue.indexOf(el);
    if (idx >= 0) _toastQueue.splice(idx, 1);
    el.classList.add("toast-exit");
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 300);
  }

  // ── custom confirm dialog ─────────────────────────────────────────

  function showConfirm(msg, confirmLabel) {
    return new Promise(function (resolve) {
      var overlay = document.getElementById("confirmOverlay");
      var msgEl = document.getElementById("confirmMsg");
      var okBtn = document.getElementById("confirmOk");
      var cancelBtn = document.getElementById("confirmCancel");
      msgEl.textContent = msg;
      okBtn.textContent = confirmLabel || "Confirm";
      overlay.style.display = "flex";
      var previousFocus = document.activeElement;

      // Focus trap: keep Tab within modal
      var dialog = overlay.querySelector(".modal-dialog");
      function onKeydown(e) {
        if (e.key === "Escape") { cleanup(false); return; }
        if (e.key === "Tab") {
          var focusable = dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
          if (!focusable.length) return;
          var first = focusable[0];
          var last = focusable[focusable.length - 1];
          if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
          } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        }
      }

      function cleanup(result) {
        overlay.style.display = "none";
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        overlay.removeEventListener("click", onOverlay);
        document.removeEventListener("keydown", onKeydown);
        if (previousFocus && previousFocus.focus) previousFocus.focus();
        resolve(result);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onOverlay(e) { if (e.target === overlay) cleanup(false); }

      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      overlay.addEventListener("click", onOverlay);
      document.addEventListener("keydown", onKeydown);
      cancelBtn.focus();
    });
  }

  // ── tabs ──────────────────────────────────────────────────────────
  // Tab 0: Dashboard, 1: Stream, 2: Overlay, 3: Network, 4: System, 5: Help

  function switchTab(idx) {
    activeTab = idx;
    try { localStorage.setItem("pi_decoder_activeTab", idx); } catch (e) {}
    document.querySelectorAll(".tab-bar button").forEach(function (btn, i) {
      btn.classList.toggle("active", i === idx);
      btn.setAttribute("aria-selected", i === idx ? "true" : "false");
      btn.setAttribute("tabindex", i === idx ? "0" : "-1");
    });
    document.querySelectorAll(".tab-panel").forEach(function (panel, i) {
      panel.classList.toggle("active", i === idx);
    });
    // move focus to active panel
    var panel = document.getElementById("panel-" + idx);
    if (panel) panel.focus();
    // connect/disconnect preview on Dashboard tab (index 0)
    if (idx === 0) {
      connectPreview();
    } else {
      disconnectPreview();
    }
    // load network data when opening Network tab (index 3)
    if (idx === 3) {
      loadSavedNetworks();
      window.loadSpeedTestResult();
    }
    // load logs + version when opening System tab (index 4)
    if (idx === 4) {
      loadLogs();
      loadVersion();
    }
  }

  // ── tab badges (setup) ────────────────────────────────────────────

  function updateTabBadges() {
    var badges = {};
    _setupChecks.forEach(function (c) {
      if (c.tab !== undefined) badges[c.tab] = true;
    });
    document.querySelectorAll(".tab-bar button").forEach(function (btn, i) {
      btn.classList.toggle("has-badge", !!badges[i]);
    });
  }

  // ── WebSocket: status ─────────────────────────────────────────────

  function connectStatus() {
    if (statusWs && statusWs.readyState <= 1) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    statusWs = new WebSocket(proto + "//" + location.host + "/ws/status");
    statusWs.onmessage = function (ev) {
      try {
        var d = JSON.parse(ev.data);
        updateStatusUI(d);
      } catch (err) {
        console.error("Status parse error:", err);
      }
    };
    statusWs.onclose = function () {
      if (!pageClosing) {
        var badge = _el.headerBadge;
        if (badge) {
          badge.textContent = "Reconnecting\u2026";
          badge.className = "status-badge offline";
        }
        if (_statusReconnectTimer) clearTimeout(_statusReconnectTimer);
        _statusReconnectTimer = setTimeout(function () {
          _statusReconnectTimer = null;
          connectStatus();
        }, 3000);
      }
    };
  }

  function updateStatusUI(d) {
    // header badge
    var badge = _el.headerBadge;
    if (badge) {
      if (d.mpv && d.mpv.playing) {
        badge.textContent = "Playing";
        badge.className = "status-badge online";
      } else {
        badge.textContent = "Idle";
        badge.className = "status-badge offline";
      }
    }

    // Update header name if changed
    if (d.name && _el.headerName) {
      if (_el.headerName.textContent !== d.name) {
        _el.headerName.textContent = d.name;
        document.title = d.name;
      }
    }

    // hostname display
    if (d.hostname) {
      var hn = d.hostname + ".local";
      if (_el.netHostname) _el.netHostname.textContent = hn;
      if (_el.sysHostname) {
        _el.sysHostname.textContent = hn;
        if (_el.hostnameDisplay) _el.hostnameDisplay.style.display = "block";
      }
    }

    // stat cards
    if (_el.statCpu) _el.statCpu.textContent = d.system.cpu_percent.toFixed(1) + "%";
    if (_el.statMem) _el.statMem.textContent = d.system.memory_percent.toFixed(1) + "%";
    if (_el.statTemp) _el.statTemp.textContent = d.system.temperature.toFixed(1) + "\u00b0C";
    if (_el.statUptime) _el.statUptime.textContent = d.system.uptime || "--";

    // network card on Dashboard
    if (d.network) {
      _hotspotActive = !!d.network.hotspot_active;
      updateNetworkCard(d.network);
      updateNetworkTab(d.network);
    }

    // CEC / TV status
    if (d.cec) {
      var tvEl = _el.statTv;
      var tvCard = _el.tvStatusCard;
      if (tvEl) {
        var ps = d.cec.power;
        if (ps === "on") {
          tvEl.textContent = "On";
          if (tvCard) tvCard.className = "stat-card";
        } else if (ps === "standby") {
          tvEl.textContent = "Standby";
          if (tvCard) tvCard.className = "stat-card stat-card-warning";
        } else {
          tvEl.textContent = "Unknown";
          if (tvCard) tvCard.className = "stat-card";
        }
      }
    }

    // mpv status
    var mpvLine = _el.mpvStatusLine;
    if (mpvLine) {
      if (d.mpv.playing) {
        mpvLine.innerHTML =
          '<span class="indicator green"></span>Playing: ' +
          escapeHtml(d.mpv.stream_url || "--");
      } else if (d.mpv.idle) {
        mpvLine.innerHTML = '<span class="indicator grey"></span>Idle';
      } else {
        mpvLine.innerHTML = '<span class="indicator red"></span>Stopped';
      }
    }

    // mpv performance stats
    if (_el.mpvPerfLine) {
      if (d.mpv.playing && d.mpv.fps) {
        _el.mpvPerfLine.style.display = "";
        _el.mpvRes.textContent = d.mpv.resolution || "--";
        _el.mpvFps.textContent = d.mpv.fps ? d.mpv.fps.toFixed(1) : "--";
        var totalDrops = (d.mpv.dropped_frames || 0) + (d.mpv.decoder_drops || 0);
        _el.mpvDrops.textContent = totalDrops;
        _el.mpvDrops.className = totalDrops > 10 ? "perf-drops-warn" : "";
      } else {
        _el.mpvPerfLine.style.display = "none";
      }
    }

    // hwdec current hint
    if (_el.hwdecCurrent && d.mpv) {
      _el.hwdecCurrent.textContent = d.mpv.hwdec_current || "--";
    }

    // setup banner (shown above tabs, dismissible)
    if (_el.setupBanner) {
      _setupChecks = [];
      if (!d.mpv.stream_url) _setupChecks.push({ msg: "Configure a stream URL", tab: 1 });
      if (d.overlay && d.overlay.enabled && !d.overlay.credentials_set) _setupChecks.push({ msg: "Add PCO credentials", tab: 2 });
      if (d.network && d.network.connection_type === "hotspot") _setupChecks.push({ msg: "Connect to WiFi", tab: 3 });
      var newKey = _setupChecks.map(function (c) { return c.tab; }).join(",");
      // Re-show if the set of issues changed since last dismiss
      if (newKey !== _lastSetupKey) _setupBannerDismissed = false;
      _lastSetupKey = newKey;
      if (_setupChecks.length && !_setupBannerDismissed) {
        var cl = _el.setupChecklist;
        cl.innerHTML = _setupChecks.map(function (c) {
          return '<li><a href="#" onclick="switchTab(' + c.tab + ');return false">' + escapeHtml(c.msg) + " &rarr; " + ["Dashboard", "Stream", "Overlay", "Network", "System", "Help"][c.tab] + " tab</a></li>";
        }).join("");
        _el.setupBanner.style.display = "flex";
      } else {
        _el.setupBanner.style.display = "none";
      }
      updateTabBadges();
    }

    // overlay
    var ovLine = _el.overlayStatusLine;
    if (ovLine) {
      if (d.overlay && d.overlay.enabled) {
        var parts = ["PCO Enabled"];
        if (d.overlay.is_live) parts.push("Live");
        if (d.overlay.countdown) {
          var suffix = d.overlay.timer_mode === "item" ? " to item end" : " to service end";
          parts.push(d.overlay.countdown + suffix);
        }
        if (d.overlay.message) parts.push(d.overlay.message);
        ovLine.innerHTML =
          '<span class="indicator green"></span>' + escapeHtml(parts.join(" \u2014 "));
      } else {
        ovLine.innerHTML = '<span class="indicator grey"></span>Disabled';
      }
    }
  }

  function updateNetworkCard(net) {
    var card = _el.networkCard;
    var el = _el.statNetwork;
    if (!el) return;

    var text = "";
    if (net.connection_type === "ethernet") {
      text = "Ethernet";
      if (card) card.className = "stat-card";
    } else if (net.connection_type === "wifi") {
      text = "WiFi" + (net.signal ? " " + net.signal + "%" : "");
      if (card) card.className = "stat-card";
    } else if (net.connection_type === "hotspot") {
      text = "Hotspot";
      if (card) card.className = "stat-card stat-card-warning";
    } else {
      text = "No Network";
      if (card) card.className = "stat-card stat-card-warning";
    }
    el.textContent = text;
  }

  function updateNetworkTab(net) {
    if (_el.netType) _el.netType.textContent = net.connection_type || "--";
    if (_el.netIp) _el.netIp.textContent = net.ip || "--";
    if (_el.netSsid) _el.netSsid.textContent = net.ssid || "--";

    // Visual signal indicator
    if (_el.netSignal) {
      if (net.signal) {
        _el.netSignal.innerHTML = buildSignalBars(net.signal) + ' <span style="font-size:14px">' + net.signal + '%</span>';
      } else {
        _el.netSignal.textContent = "--";
      }
    }

    // Style connection card
    if (_el.netStatusCard) {
      if (net.connection_type === "hotspot") {
        _el.netStatusCard.className = "stat-card stat-card-warning";
      } else {
        _el.netStatusCard.className = "stat-card";
      }
    }

  }

  // ── signal bars ───────────────────────────────────────────────────

  function buildSignalBars(signal) {
    var bars = 4;
    var filled = signal > 75 ? 4 : signal > 50 ? 3 : signal > 25 ? 2 : 1;
    var color = signal > 66 ? "var(--color-success)" : signal > 33 ? "var(--color-warning)" : "var(--color-danger)";
    var html = '<span class="signal-bars">';
    for (var i = 0; i < bars; i++) {
      var h = 6 + i * 4;
      var fill = i < filled ? color : "var(--color-border)";
      html += '<span class="signal-bar" style="height:' + h + 'px;background:' + fill + '"></span>';
    }
    html += '</span>';
    return html;
  }

  // ── WebSocket: preview ────────────────────────────────────────────

  function connectPreview() {
    if (previewWs && previewWs.readyState <= 1) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    previewWs = new WebSocket(proto + "//" + location.host + "/ws/preview");
    previewWs.binaryType = "blob";
    previewWs.onmessage = function (ev) {
      var img = document.getElementById("previewImg");
      if (img) {
        if (_lastPreviewUrl) URL.revokeObjectURL(_lastPreviewUrl);
        _lastPreviewUrl = URL.createObjectURL(ev.data);
        img.src = _lastPreviewUrl;
        img.style.display = "block";
        var ph = document.getElementById("previewPlaceholder");
        if (ph) ph.style.display = "none";
      }
    };
    previewWs.onclose = function () {
      // only reconnect if still on Dashboard tab
      if (!pageClosing && activeTab === 0) {
        if (_previewReconnectTimer) clearTimeout(_previewReconnectTimer);
        _previewReconnectTimer = setTimeout(function () {
          _previewReconnectTimer = null;
          connectPreview();
        }, 3000);
      }
    };
  }

  function disconnectPreview() {
    if (previewWs) {
      previewWs.close();
      previewWs = null;
    }
  }

  // ── API helpers ───────────────────────────────────────────────────

  function apiFetch(url, method, body) {
    var opts = { signal: AbortSignal.timeout(15000) };
    if (method === "POST") {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body || {});
    }
    var endpoint = url.replace(/^\/api\//, "");
    var statusMsg = {
      400: "Invalid request",
      403: "Not allowed",
      404: "Not found",
      409: "Already in progress",
      500: "Server error",
      502: "Service unavailable",
      503: "Service unavailable",
    };
    return fetch(url, opts).then(function (r) {
      if (!r.ok) toast(statusMsg[r.status] || "Request failed (" + r.status + ")", "error");
      return r.json();
    }).catch(function (err) {
      toast(err.name === "TimeoutError" ? "Request timed out" : "Network error — is the device reachable?", "error");
      return { ok: false, error: err.message };
    });
  }

  function apiPost(url, body) { return apiFetch(url, "POST", body); }
  function apiGet(url) { return apiFetch(url); }

  function withLoading(btn, promise) {
    btn.disabled = true;
    var orig = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> ' + btn.textContent.trim() + '...';
    return promise.finally(function () { btn.disabled = false; btn.innerHTML = orig; });
  }

  // ── CEC controls ──────────────────────────────────────────────────

  function pressAnim(ev) {
    var card = ev && ev.currentTarget;
    if (!card) return;
    card.classList.add("pressed");
    setTimeout(function () { card.classList.remove("pressed"); }, 300);
  }

  window.cecPowerOn = function (ev) { pressAnim(ev); apiPost("/api/cec/on").then(function () { toast("TV powering on..."); }); };
  window.cecStandby = function () {
    showConfirm("Turn off the TV?", "Turn Off").then(function (ok) {
      if (ok) apiPost("/api/cec/standby").then(function () { toast("TV turning off..."); });
    });
  };
  window.cecActiveSource = function (ev) { pressAnim(ev); apiPost("/api/cec/active-source").then(function () { toast("Switched to livestream source"); }); };
  window.cecVolumeUp = function (ev) { pressAnim(ev); apiPost("/api/cec/volume-up"); };
  window.cecVolumeDown = function (ev) { pressAnim(ev); apiPost("/api/cec/volume-down"); };
  window.cecMute = function (ev) { pressAnim(ev); apiPost("/api/cec/mute").then(function () { toast("Mute toggled"); }); };

  // ── stream config ─────────────────────────────────────────────────

  window.saveStreamConfig = function (btn) {
    var url = document.getElementById("streamUrl").value;
    var caching = parseInt(document.getElementById("networkCaching").value, 10);
    var hwdec = document.getElementById("streamHwdec").value;
    // Client-side validation
    if (url && !/^(rtmps?|srt|https?|rtp|udp):\/\//i.test(url)) {
      toast("Stream URL must start with rtmp://, srt://, http://, https://, etc.", "error");
      return;
    }
    if (isNaN(caching) || caching < 200 || caching > 30000) {
      toast("Buffer delay must be between 200 and 30000 ms", "error");
      return;
    }
    withLoading(btn, apiPost("/api/config/stream", { url: url, network_caching: caching, hwdec: hwdec }).then(
      function (d) {
        if (d.ok) {
          toast("Stream config saved");
          apiPost("/api/restart/video");
        } else toast("Error saving", "error");
      }
    ));
  };

  // ── overlay config ────────────────────────────────────────────────

  window.saveOverlayConfig = function (btn) {
    // Client-side validation
    var pollVal = parseInt(document.getElementById("pcoPollInterval").value, 10);
    if (!isNaN(pollVal) && (pollVal < 5 || pollVal > 300)) {
      toast("Poll interval must be between 5 and 300 seconds", "error");
      return;
    }
    // Save overlay + advanced PCO settings together
    var overlayPromise = apiPost("/api/config/overlay", {
      enabled: document.getElementById("overlayEnabled").checked,
      position: document.getElementById("overlayPosition").value,
      font_size: parseInt(document.getElementById("overlayFontSize").value, 10),
      font_size_title: parseInt(document.getElementById("overlayFontSizeTitle").value, 10),
      font_size_info: parseInt(document.getElementById("overlayFontSizeInfo").value, 10),
      transparency: parseFloat(document.getElementById("overlayTransparency").value),
      timer_mode: document.getElementById("timerMode").value,
      show_description: document.getElementById("showDescription").checked,
      show_service_end: document.getElementById("showServiceEnd").checked,
      timezone: document.getElementById("overlayTimezone").value,
    });

    var pcoAdvanced = apiPost("/api/config/pco", {
      search_mode: document.getElementById("pcoSearchMode").value,
      folder_id: document.getElementById("pcoFolderId").value,
      poll_interval: parseInt(document.getElementById("pcoPollInterval").value, 10),
    });

    withLoading(btn, Promise.all([overlayPromise, pcoAdvanced]).then(function (results) {
      if (results[0].ok) {
        toast("Overlay config saved");
        apiPost("/api/restart/overlay");
      } else toast("Error saving", "error");
    }));
  };

  // ── PCO config ────────────────────────────────────────────────────

  window.savePCOConfig = function (btn) {
    var appId = document.getElementById("pcoAppId").value;
    var secret = document.getElementById("pcoSecret").value;
    if ((appId && !secret) || (!appId && secret)) {
      toast("App ID and Secret must both be provided or both empty", "error");
      return;
    }
    withLoading(btn, apiPost("/api/config/pco", {
      app_id: appId,
      secret: secret,
      service_type_id: document.getElementById("pcoServiceType").value,
      search_mode: document.getElementById("pcoSearchMode").value,
      folder_id: document.getElementById("pcoFolderId").value,
    }).then(function (d) {
      if (d.ok) {
        toast("PCO config saved");
        document.getElementById("pcoAppId").value = "";
        document.getElementById("pcoSecret").value = "";
        apiPost("/api/restart/overlay");
      } else toast("Error saving", "error");
    }));
  };

  window.testPCO = function () {
    var appId = document.getElementById("pcoAppId").value;
    var secret = document.getElementById("pcoSecret").value;
    var stId = document.getElementById("pcoServiceType").value;
    var out = document.getElementById("testResults");
    out.style.display = "block";
    out.innerHTML = "Testing...";

    apiPost("/api/test-pco", {
      app_id: appId,
      secret: secret,
      service_type_id: stId,
    }).then(function (d) {
      if (d.success) {
        var html = '<p class="ok">Connection successful</p>';
        html += "<p>Service Types:</p><ul class='service-type-list'>";
        (d.service_types || []).forEach(function (st) {
          html +=
            "<li><strong>" +
            escapeHtml(st.name) +
            "</strong> &mdash; ID: " +
            escapeHtml(st.id) +
            "</li>";
        });
        html += "</ul>";
        html += '<p style="margin-top:12px"><button class="btn btn-primary btn-sm" onclick="savePCOConfig(this)">Save Now</button></p>';
        out.innerHTML = html;

        // populate dropdown
        populateServiceTypes(d.service_types);
      } else {
        out.innerHTML = '<p class="err">Failed: ' + escapeHtml(d.error || "Unknown") + "</p>";
      }
    });
  };

  window.loadServiceTypes = function () {
    apiGet("/api/service-types").then(function (d) {
      if (d.service_types && d.service_types.length) {
        populateServiceTypes(d.service_types);
      } else {
        var sel = document.getElementById("pcoServiceType");
        sel.innerHTML = '<option value="">Save credentials first</option>';
      }
    }).catch(function () {
      var sel = document.getElementById("pcoServiceType");
      sel.innerHTML = '<option value="">Save credentials first</option>';
    });
  };

  function populateServiceTypes(types) {
    var sel = document.getElementById("pcoServiceType");
    var current = sel.dataset.current || "";
    sel.innerHTML = '<option value="">Select...</option>';
    (types || []).forEach(function (st) {
      var opt = document.createElement("option");
      opt.value = st.id;
      opt.textContent = st.name;
      if (st.id === current) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  // ── PCO search mode toggle ──────────────────────────────────────

  window.onTimerModeChange = function () {
    var mode = document.getElementById("timerMode").value;
    if (mode === "service") {
      document.getElementById("showDescription").checked = false;
    }
  };

  window.toggleSearchMode = function () {
    var mode = document.getElementById("pcoSearchMode").value;
    var stGroup = document.getElementById("serviceTypeGroup");
    var folderGroup = document.getElementById("folderIdGroup");
    if (stGroup) stGroup.style.display = mode === "service_type" ? "block" : "none";
    if (folderGroup) folderGroup.style.display = mode === "folder" ? "block" : "none";
  };

  // ── actions ───────────────────────────────────────────────────────

  function confirmAction(msg, url, successMsg, confirmLabel) {
    showConfirm(msg, confirmLabel).then(function (ok) {
      if (ok) apiPost(url).then(function () { toast(successMsg); });
    });
  }

  window.restartVideo = function () { confirmAction("Restart video player?", "/api/restart/video", "Video restarting...", "Restart"); };
  window.restartOverlay = function () { confirmAction("Restart the overlay?", "/api/restart/overlay", "Overlay restarting...", "Restart"); };
  window.restartAll = function () { confirmAction("Restart all services?", "/api/restart/all", "All services restarting...", "Restart All"); };
  window.rebootSystem = function () {
    showConfirm("Reboot the system? It will be offline for ~90 seconds.", "Reboot").then(function (ok) {
      if (!ok) return;
      apiPost("/api/reboot").then(function () {
        pageClosing = true;
        if (statusWs) statusWs.close();
        if (previewWs) previewWs.close();
        var badge = _el.headerBadge;
        var remaining = 90;
        if (badge) {
          badge.textContent = "Rebooting\u2026 " + remaining + "s";
          badge.className = "status-badge offline";
        }
        var iv = setInterval(function () {
          remaining--;
          if (badge) badge.textContent = "Rebooting\u2026 " + remaining + "s";
          if (remaining <= 0) {
            clearInterval(iv);
            if (badge) badge.textContent = "Reconnecting\u2026";
            var retries = 0;
            var poll = setInterval(function () {
              retries++;
              fetch("/api/health", { signal: AbortSignal.timeout(3000) })
                .then(function (r) {
                  if (r.ok) { clearInterval(poll); location.reload(); }
                })
                .catch(function () {});
              if (retries >= 30) {
                clearInterval(poll);
                if (badge) badge.textContent = "Offline";
              }
            }, 3000);
          }
        }, 1000);
      });
    });
  };
  window.shutdownSystem = function () {
    showConfirm("Shut down the system? You will need physical access to the device to turn it back on.", "Shut Down").then(function (ok) {
      if (!ok) return;
      apiPost("/api/shutdown").then(function () {
        pageClosing = true;
        if (statusWs) statusWs.close();
        if (previewWs) previewWs.close();
        var badge = _el.headerBadge;
        if (badge) {
          badge.textContent = "Shut Down";
          badge.className = "status-badge offline";
        }
      });
    });
  };
  window.stopVideo = function () {
    apiPost("/api/stop/video").then(function (d) {
      if (d.ok) toast("Video stopped");
    });
  };

  // ── decoder name ──────────────────────────────────────────────────

  window.saveDecoderName = function (btn) {
    var name = document.getElementById("decoderName").value;
    withLoading(btn, apiPost("/api/config/general", { name: name }).then(function (d) {
      if (d.ok) {
        toast("Name saved");
        document.getElementById("headerName").textContent = name;
        document.title = name;
      } else toast("Error saving", "error");
    }));
  };

  // ── logs ──────────────────────────────────────────────────────────

  function loadLogs() {
    var service = document.getElementById("logService")
      ? document.getElementById("logService").value
      : "pi-decoder";
    var lines = document.getElementById("logLines")
      ? document.getElementById("logLines").value
      : "50";
    var viewer = document.getElementById("logViewer");
    if (viewer) viewer.textContent = "Loading...";
    apiGet("/api/logs?service=" + encodeURIComponent(service) + "&lines=" + lines).then(
      function (d) {
        _logRawText = d.logs || "No logs.";
        renderLogs();
        // auto-scroll to bottom
        if (viewer) viewer.scrollTop = viewer.scrollHeight;
      }
    );
  }

  function renderLogs() {
    var viewer = document.getElementById("logViewer");
    if (!viewer) return;
    var filterVal = (document.getElementById("logFilter") || {}).value || "";
    var lines = _logRawText.split("\n");
    if (filterVal) {
      var lf = filterVal.toLowerCase();
      lines = lines.filter(function (l) { return l.toLowerCase().indexOf(lf) >= 0; });
    }
    // Color-code log levels
    viewer.innerHTML = lines.map(function (line) {
      var cls = "";
      if (/\bERROR\b/i.test(line)) cls = "log-error";
      else if (/\bWARNING\b/i.test(line)) cls = "log-warning";
      if (cls) return '<span class="' + cls + '">' + escapeHtml(line) + '</span>';
      return escapeHtml(line);
    }).join("\n");
  }

  window.refreshLogs = loadLogs;

  window.filterLogs = function () {
    if (_filterTimer) clearTimeout(_filterTimer);
    _filterTimer = setTimeout(renderLogs, 300);
  };

  window.toggleLogAutoRefresh = function () {
    var on = document.getElementById("logAutoRefresh").checked;
    if (_logAutoTimer) { clearInterval(_logAutoTimer); _logAutoTimer = null; }
    if (on) {
      _logAutoTimer = setInterval(loadLogs, 5000);
    }
  };

  // ── system ────────────────────────────────────────────────────────

  function loadVersion() {
    apiGet("/api/version").then(function (d) {
      setText("currentVersion", d.version || "unknown");
    });
  }

  // ── config import ──────────────────────────────────────────────────

  window.importConfig = function () {
    var fileInput = document.getElementById("importFile");
    if (!fileInput.files || !fileInput.files.length) return;
    var file = fileInput.files[0];
    showConfirm("Import config from " + file.name + "? Current settings will be overwritten.").then(function (ok) {
      if (!ok) { fileInput.value = ""; return; }
      var formData = new FormData();
      formData.append("file", file);
      fetch("/api/config/import", { method: "POST", body: formData })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) {
            toast("Config imported — reloading...");
            setTimeout(function () { location.reload(); }, 1500);
          } else {
            toast(d.error || "Import failed", "error");
          }
        })
        .catch(function () { toast("Import failed", "error"); });
      fileInput.value = "";
    });
  };

  // ── network tab ───────────────────────────────────────────────────

  window.scanWifi = function () {
    var btn = document.getElementById("scanBtn");
    var list = document.getElementById("wifiList");
    var btnText = document.getElementById("scanBtnText");
    var spinner = document.getElementById("scanSpinner");
    btnText.textContent = "Scanning...";
    spinner.style.display = "inline-block";
    btn.disabled = true;
    list.innerHTML = '<div class="placeholder">Scanning...</div>';

    apiGet("/api/network/wifi-scan").then(function (d) {
      btnText.textContent = "Scan for Networks";
      spinner.style.display = "none";
      btn.disabled = false;
      var networks = d.networks || [];
      if (!networks.length) {
        list.innerHTML = '<div class="help" style="padding:12px">No networks found. Try scanning again.</div>';
        return;
      }
      var html = "";
      networks.forEach(function (n) {
        var inUse = n.in_use ? ' <span class="wifi-connected">Connected</span>' : "";
        html += '<button class="wifi-item" onclick="selectWifi(\'' + escapeAttr(n.ssid) + '\')">' +
          '<span class="wifi-name">' + escapeHtml(n.ssid) + inUse + '</span>' +
          '<span class="wifi-signal">' + buildSignalBars(n.signal) + ' ' + n.signal + '%</span>' +
          '<span class="wifi-security">' + escapeHtml(n.security) + '</span>' +
          '</button>';
      });
      list.innerHTML = html;
    }).catch(function () {
      btnText.textContent = "Scan for Networks";
      spinner.style.display = "none";
      btn.disabled = false;
      list.innerHTML = '<div class="placeholder">Scan failed</div>';
    });
  };

  window.selectWifi = function (ssid) {
    document.getElementById("wifiSsid").value = ssid;
    document.getElementById("wifiPassword").focus();
  };

  window.connectWifi = function () {
    var ssid = document.getElementById("wifiSsid").value.trim();
    var password = document.getElementById("wifiPassword").value;
    if (!ssid) {
      toast("Enter a network name", "error");
      return;
    }
    if (password && password.length < 8) {
      toast("WiFi password must be at least 8 characters", "error");
      return;
    }

    if (_hotspotActive) {
      var warning = document.getElementById("wifiConnectWarning");
      var cdEl = document.getElementById("connectCountdown");
      var cancelBtn = document.getElementById("cancelWifiConnect");
      if (warning) warning.style.display = "block";
      var remaining = 10;
      if (cdEl) cdEl.textContent = remaining;
      var iv = setInterval(function () {
        remaining--;
        if (cdEl) cdEl.textContent = remaining;
        if (remaining <= 0) {
          clearInterval(iv);
          if (cancelBtn) cancelBtn.onclick = null;
          if (warning) warning.style.display = "none";
          doConnect(ssid, password);
        }
      }, 1000);
      if (cancelBtn) cancelBtn.onclick = function () {
        clearInterval(iv);
        if (warning) warning.style.display = "none";
        toast("WiFi connection cancelled");
      };
    } else {
      doConnect(ssid, password);
    }
  };

  function doConnect(ssid, password) {
    toast("Connecting to " + ssid + "...");
    apiPost("/api/network/wifi-connect", { ssid: ssid, password: password }).then(
      function (d) {
        if (d.ok) {
          toast("Connected to " + ssid);
          document.getElementById("wifiPassword").value = "";
        } else toast("Connection failed: " + (d.error || ""), "error");
      }
    ).catch(function () {
      toast("Connection failed (network changed?)", "error");
    });
  }

  function loadSavedNetworks() {
    var el = document.getElementById("savedNetworks");
    apiGet("/api/network/wifi/saved").then(function (d) {
      var nets = d.networks || [];
      if (!nets.length) {
        el.innerHTML = '<div class="help" style="padding:12px">No saved networks.</div>';
        return;
      }
      var html = '<ul class="saved-network-list">';
      nets.forEach(function (name) {
        html += '<li><span>' + escapeHtml(name) + '</span>' +
          '<button class="btn btn-danger btn-sm" onclick="forgetNetwork(\'' + escapeAttr(name) + '\')">Forget</button></li>';
      });
      html += "</ul>";
      el.innerHTML = html;
    }).catch(function () {
      el.innerHTML = "<p>Could not load saved networks</p>";
    });
  }

  window.forgetNetwork = function (name) {
    showConfirm("Forget network '" + name + "'?", "Forget").then(function (ok) {
      if (!ok) return;
      apiPost("/api/network/wifi/forget", { name: name }).then(function (d) {
        if (d.ok) {
          toast("Forgot " + name);
          loadSavedNetworks();
        } else toast(d.error || "Failed to forget network", "error");
      }).catch(function () {
        toast("Failed to forget network", "error");
      });
    });
  };

  window.saveNetworkConfig = function (btn) {
    var hsPass = document.getElementById("hotspotPassword").value;
    if (hsPass && hsPass.length < 8) {
      toast("Hotspot password must be at least 8 characters", "error");
      return;
    }
    withLoading(btn, apiPost("/api/config/network", {
      hotspot_ssid: document.getElementById("hotspotSsid").value,
      hotspot_password: document.getElementById("hotspotPassword").value,
      ethernet_timeout: parseInt(document.getElementById("ethTimeout").value, 10),
      wifi_timeout: parseInt(document.getElementById("wifiTimeout").value, 10),
    }).then(function (d) {
      if (d.ok) toast("Network settings saved");
      else toast(d.error || "Error saving", "error");
    }));
  };

  // ── static IP configuration ──────────────────────────────────────

  window.toggleStaticIp = function (prefix) {
    var cb = document.getElementById(prefix + "StaticIp");
    var fields = document.getElementById(prefix + "StaticFields");
    if (fields) fields.style.display = cb && cb.checked ? "block" : "none";
  };

  function validateIpv4(ip) {
    if (!ip) return false;
    var parts = ip.split(".");
    if (parts.length !== 4) return false;
    for (var i = 0; i < 4; i++) {
      var n = parseInt(parts[i], 10);
      if (isNaN(n) || n < 0 || n > 255 || parts[i] !== String(n)) return false;
    }
    return true;
  }

  function subnetMaskToPrefix(mask) {
    if (!validateIpv4(mask)) return 0;
    var parts = mask.split(".");
    var num = ((parseInt(parts[0], 10) << 24) |
               (parseInt(parts[1], 10) << 16) |
               (parseInt(parts[2], 10) << 8) |
               parseInt(parts[3], 10)) >>> 0;
    if (num === 0) return 0;
    // Valid mask: all 1s followed by all 0s
    var inverted = (~num) >>> 0;
    if ((inverted & (inverted + 1)) !== 0) return 0;
    var bits = 0;
    while (num & 0x80000000) { bits++; num = (num << 1) >>> 0; }
    return bits;
  }

  function validateSubnetMask(mask) {
    return subnetMaskToPrefix(mask) > 0;
  }

  window.saveAndApplyStaticIp = function (btn) {
    var payload = {};
    var applyInterfaces = [];
    var prefixes = ["eth", "wifi"];

    // Validate all interfaces before saving anything
    for (var p = 0; p < prefixes.length; p++) {
      var prefix = prefixes[p];
      var cb = document.getElementById(prefix + "StaticIp");
      var isManual = cb && cb.checked;
      payload[prefix + "_ip_mode"] = isManual ? "manual" : "auto";

      if (isManual) {
        var ip = (document.getElementById(prefix + "IpAddress") || {}).value || "";
        var mask = (document.getElementById(prefix + "SubnetMask") || {}).value || "";
        var gw = (document.getElementById(prefix + "Gateway") || {}).value || "";
        var dns = (document.getElementById(prefix + "Dns") || {}).value || "";
        var label = prefix === "eth" ? "Ethernet" : "WiFi";

        if (!ip) { toast(label + " IP address is required", "error"); return; }
        if (!validateIpv4(ip)) { toast(label + " IP address is invalid", "error"); return; }
        if (!mask) { toast(label + " subnet mask is required", "error"); return; }
        if (!validateSubnetMask(mask)) { toast(label + " subnet mask is invalid (e.g. 255.255.255.0)", "error"); return; }
        var subnetPrefix = subnetMaskToPrefix(mask);
        if (!gw) { toast(label + " gateway is required", "error"); return; }
        if (!validateIpv4(gw)) { toast(label + " gateway is invalid", "error"); return; }
        if (dns) {
          var dnsEntries = dns.split(",");
          for (var i = 0; i < dnsEntries.length; i++) {
            var entry = dnsEntries[i].trim();
            if (entry && !validateIpv4(entry)) { toast(label + " DNS '" + entry + "' is invalid", "error"); return; }
          }
        }

        payload[prefix + "_ip_address"] = ip + "/" + subnetPrefix;
        payload[prefix + "_gateway"] = gw;
        payload[prefix + "_dns"] = dns;
        applyInterfaces.push(prefix === "eth" ? "ethernet" : "wifi");
      } else {
        payload[prefix + "_ip_address"] = "";
        payload[prefix + "_gateway"] = "";
        payload[prefix + "_dns"] = "";
      }
    }

    // Nothing to do if no interface has static IP enabled
    if (!applyInterfaces.length) {
      toast("No static IP configured — both interfaces set to DHCP");
      return;
    }

    // Save config first, then apply to interfaces with static IP
    withLoading(btn, apiPost("/api/config/network", payload).then(function (d) {
      if (!d.ok) {
        toast(d.error || "Error saving IP config", "error");
        return;
      }

      var promises = applyInterfaces.map(function (iface) {
        return apiPost("/api/network/apply-ip", { interface: iface }).then(function (r) {
          if (r.ok) {
            toast(iface + ": " + (r.message || "applied"));
          } else if (r.error && r.error.indexOf("No active") === -1) {
            toast(iface + ": " + r.error, "error");
          }
        });
      });
      return Promise.all(promises);
    }));
  };

  // ── password show/hide toggle ─────────────────────────────────────

  window.togglePasswordVis = function (btn) {
    var input = btn.parentElement.querySelector("input");
    if (input.type === "password") {
      input.type = "text";
      btn.setAttribute("aria-label", "Hide password");
    } else {
      input.type = "password";
      btn.setAttribute("aria-label", "Show password");
    }
  };

  // ── transparency slider display ───────────────────────────────────

  window.updateTransparencyDisplay = function () {
    var val = document.getElementById("overlayTransparency").value;
    document.getElementById("transparencyValue").textContent =
      Math.round(val * 100) + "%";
  };

  // ── network caching context help ──────────────────────────────────

  function updateCachingHelp() {
    var el = document.getElementById("networkCaching");
    var help = document.getElementById("cachingHelp");
    if (!el || !help) return;
    var val = parseInt(el.value, 10);
    var desc;
    if (val < 500) desc = "Ultra-low latency — may stutter";
    else if (val < 1500) desc = "Low latency — good for local networks";
    else if (val <= 3000) desc = "Balanced — stable for most setups";
    else if (val <= 5000) desc = "High buffer — very stable, more delay";
    else desc = "Maximum buffer — highest stability";
    help.textContent = desc;
  }

  // ── utilities ─────────────────────────────────────────────────────

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function escapeAttr(s) {
    return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ── speed test ──────────────────────────────────────────────────

  function relativeTime(isoStr) {
    if (!isoStr) return "--";
    var then = new Date(isoStr);
    var now = new Date();
    var diffS = Math.floor((now - then) / 1000);
    if (diffS < 60) return "just now";
    var diffM = Math.floor(diffS / 60);
    if (diffM < 60) return diffM + (diffM === 1 ? " minute ago" : " minutes ago");
    var diffH = Math.floor(diffM / 60);
    if (diffH < 24) return diffH + (diffH === 1 ? " hour ago" : " hours ago");
    var diffD = Math.floor(diffH / 24);
    return diffD + (diffD === 1 ? " day ago" : " days ago");
  }

  function renderSpeedTestResult(data) {
    var resultsDiv = document.getElementById("speedTestResults");
    if (!resultsDiv || !data) return;
    resultsDiv.style.display = "block";

    var dlEl = document.getElementById("speedDownload");
    var latEl = document.getElementById("speedLatency");
    var tsEl = document.getElementById("speedTimestamp");
    var dlCard = document.getElementById("speedDownloadCard");

    if (dlEl) dlEl.textContent = data.download_mbps + " Mbps";
    if (latEl) latEl.textContent = data.latency_ms + " ms";
    if (tsEl) tsEl.textContent = relativeTime(data.timestamp);

    // Color-code download speed
    if (dlCard) {
      if (data.download_mbps >= 5) {
        dlCard.className = "stat-card stat-card-good";
      } else if (data.download_mbps >= 2) {
        dlCard.className = "stat-card stat-card-warning";
      } else {
        dlCard.className = "stat-card stat-card-danger";
      }
    }

    // WiFi metadata line
    var metaEl = document.getElementById("speedWifiMeta");
    if (metaEl) {
      var parts = [];
      if (data.wifi_band) parts.push(data.wifi_band);
      if (data.avg_signal != null) parts.push("Signal " + data.avg_signal + "%");
      if (data.interface_type) parts.push(data.interface_type);
      if (parts.length) {
        metaEl.textContent = parts.join(" \u00b7 ");
        metaEl.style.display = "block";
      } else {
        metaEl.style.display = "none";
      }
    }
  }

  window.loadSpeedTestResult = function () {
    fetch("/api/network/speedtest", { signal: AbortSignal.timeout(10000) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok && d.result) renderSpeedTestResult(d.result);
      })
      .catch(function () {});
  };

  window.runSpeedTest = function (btn) {
    btn.disabled = true;
    var orig = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Testing... (up to 30s)';

    fetch("/api/network/speedtest", {
      method: "POST",
      signal: AbortSignal.timeout(35000),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        btn.innerHTML = orig;
        if (d.ok) {
          renderSpeedTestResult(d);
          toast("Speed test complete: " + d.download_mbps + " Mbps");
        } else {
          toast(d.error || "Speed test failed", "error");
        }
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.innerHTML = orig;
        toast(err.name === "TimeoutError" ? "Speed test timed out" : "Speed test failed", "error");
      });
  };

  // ── tab wiring ────────────────────────────────────────────────────

  window.switchTab = switchTab;

  window.dismissSetupBanner = function () {
    _setupBannerDismissed = true;
    if (_el.setupBanner) _el.setupBanner.style.display = "none";
  };

  // ── init ──────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    initElementCache();
    // Restore saved tab
    var saved = 0;
    try { saved = parseInt(localStorage.getItem("pi_decoder_activeTab"), 10) || 0; } catch (e) {}
    var tabCount = document.querySelectorAll(".tab-bar button").length;
    if (saved < 0 || saved >= tabCount) saved = 0;
    switchTab(saved);
    connectStatus();
    // try loading service types on page load
    window.loadServiceTypes();
    // initialize search mode toggle
    window.toggleSearchMode();
    // caching help
    var cachingInput = document.getElementById("networkCaching");
    if (cachingInput) {
      cachingInput.addEventListener("input", updateCachingHelp);
      updateCachingHelp();
    }
    // scroll-fade detector for tab bar
    var tabBar = document.querySelector(".tab-bar");
    var tabsEl = document.querySelector(".tabs");
    function checkTabScroll() {
      if (!tabBar || !tabsEl) return;
      var canScroll = tabBar.scrollWidth > tabBar.clientWidth + 2;
      var atEnd = tabBar.scrollLeft + tabBar.clientWidth >= tabBar.scrollWidth - 2;
      tabsEl.classList.toggle("has-scroll-fade", canScroll && !atEnd);
    }
    if (tabBar) {
      tabBar.addEventListener("scroll", checkTabScroll);
      window.addEventListener("resize", checkTabScroll);
      checkTabScroll();
      // Arrow key navigation for tabs (WAI-ARIA tabs pattern)
      tabBar.addEventListener("keydown", function (e) {
        var tabs = tabBar.querySelectorAll("button");
        var len = tabs.length;
        if (!len) return;
        var newIdx = activeTab;
        if (e.key === "ArrowRight") { newIdx = (activeTab + 1) % len; }
        else if (e.key === "ArrowLeft") { newIdx = (activeTab - 1 + len) % len; }
        else if (e.key === "Home") { newIdx = 0; }
        else if (e.key === "End") { newIdx = len - 1; }
        else { return; }
        e.preventDefault();
        switchTab(newIdx);
        tabs[newIdx].focus();
      });
    }
  });
})();
