(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Состояние на клиенте
  // ---------------------------------------------------------------------
  let routes = [];
  let running = false;
  let logCursor = 0;
  let logPollTimer = null;
  let statusPollTimer = null;

  let browseMode = null;       // 'source' | 'destination'
  let browseCurrentPath = null;
  let browseSelectedEntry = null; // {name, path, is_dir}

  const routeStatusTimers = {}; // routeId -> timeout id

  // ---------------------------------------------------------------------
  // Элементы
  // ---------------------------------------------------------------------
  const el = {
    statusPill: document.getElementById("statusPill"),
    statusText: document.getElementById("statusText"),
    toggleRunBtn: document.getElementById("toggleRunBtn"),
    addRouteBtn: document.getElementById("addRouteBtn"),
    routeList: document.getElementById("routeList"),
    emptyState: document.getElementById("emptyState"),
    intervalInput: document.getElementById("intervalInput"),
    skipUnchangedInput: document.getElementById("skipUnchangedInput"),
    threadsInput: document.getElementById("threadsInput"),
    terminal: document.getElementById("terminal"),
    clearLogBtn: document.getElementById("clearLogBtn"),

    routeModalOverlay: document.getElementById("routeModalOverlay"),
    routeModalTitle: document.getElementById("routeModalTitle"),
    routeNameInput: document.getElementById("routeNameInput"),
    sourceInput: document.getElementById("sourceInput"),
    destinationInput: document.getElementById("destinationInput"),
    routeHint: document.getElementById("routeHint"),
    closeRouteModal: document.getElementById("closeRouteModal"),
    cancelRouteBtn: document.getElementById("cancelRouteBtn"),
    saveRouteBtn: document.getElementById("saveRouteBtn"),
    routeCustomToggle: document.getElementById("routeCustomToggle"),
    routeCustomFields: document.getElementById("routeCustomFields"),
    routeIntervalInput: document.getElementById("routeIntervalInput"),
    routeSkipSelect: document.getElementById("routeSkipSelect"),
    routeThreadsInput: document.getElementById("routeThreadsInput"),

    logoutBtn: document.getElementById("logoutBtn"),
    apiKeyInput: document.getElementById("apiKeyInput"),
    toggleKeyVisibility: document.getElementById("toggleKeyVisibility"),
    copyKeyBtn: document.getElementById("copyKeyBtn"),
    regenerateKeyBtn: document.getElementById("regenerateKeyBtn"),

    browseModalOverlay: document.getElementById("browseModalOverlay"),
    browseModalTitle: document.getElementById("browseModalTitle"),
    browseUpBtn: document.getElementById("browseUpBtn"),
    browseHomeBtn: document.getElementById("browseHomeBtn"),
    browsePathInput: document.getElementById("browsePathInput"),
    browseList: document.getElementById("browseList"),
    browseSelectedLabel: document.getElementById("browseSelectedLabel"),
    chooseFolderBtn: document.getElementById("chooseFolderBtn"),
    chooseItemBtn: document.getElementById("chooseItemBtn"),
    closeBrowseModal: document.getElementById("closeBrowseModal"),
  };

  let editingRouteId = null;

  // ---------------------------------------------------------------------
  // Утилиты
  // ---------------------------------------------------------------------
  async function api(path, options) {
    const resp = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, options));
    if (resp.status === 401) {
      window.location.href = "/login";
      throw new Error("Требуется авторизация");
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { /* no body */ }
    if (!resp.ok) {
      const msg = (data && data.error) ? data.error : ("Ошибка запроса: " + resp.status);
      throw new Error(msg);
    }
    return data;
  }

  function escapeHtml(str) {
    const d = document.createElement("div");
    d.textContent = str == null ? "" : String(str);
    return d.innerHTML;
  }

  function formatTime(epoch) {
    const d = new Date(epoch * 1000);
    return d.toLocaleTimeString("ru-RU", { hour12: false });
  }

  function humanBytes(n) {
    if (!n && n !== 0) return "0 Б";
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
  }

  function humanSpeed(bps) {
    return humanBytes(bps) + "/с";
  }

  function shortenPath(p, max) {
    if (!p) return "";
    if (p.length <= max) return p;
    return "…" + p.slice(p.length - max + 1);
  }

  function pluralRu(n, one, few, many) {
    const mod10 = Math.abs(n) % 10;
    const mod100 = Math.abs(n) % 100;
    if (mod100 > 10 && mod100 < 20) return many;
    if (mod10 === 1) return one;
    if (mod10 >= 2 && mod10 <= 4) return few;
    return many;
  }

  function formatRouteStats(stats) {
    const files = (stats && stats.files_copied) || 0;
    const bytes = (stats && stats.bytes_copied) || 0;
    const word = pluralRu(files, "файл", "файла", "файлов");
    return `Скопировано за всё время: ${files} ${word}, ${humanBytes(bytes)}`;
  }

  // ---------------------------------------------------------------------
  // Загрузка состояния
  // ---------------------------------------------------------------------
  async function loadState() {
    const data = await api("/api/state");
    routes = data.routes || [];
    running = !!data.running;
    el.intervalInput.value = data.interval;
    el.skipUnchangedInput.checked = !!data.skip_unchanged;
    el.threadsInput.value = data.threads || 2;
    renderRoutes();
    renderRunState();
  }

  function renderRunState() {
    el.statusPill.dataset.state = running ? "running" : "stopped";
    el.statusText.textContent = running ? "Работает" : "Остановлено";
    el.toggleRunBtn.textContent = running ? "Остановить" : "Запустить";
    el.toggleRunBtn.dataset.running = running ? "true" : "false";
  }

  // ---------------------------------------------------------------------
  // Рендер списка правил
  // ---------------------------------------------------------------------
  function renderRoutes() {
    el.routeList.innerHTML = "";
    el.emptyState.hidden = routes.length !== 0;

    routes.forEach((route) => {
      const card = document.createElement("div");
      card.className = "route-card" + (route.enabled ? "" : " disabled");
      card.dataset.routeId = route.id;
      card.dataset.status = "idle";

      const typeIcon = route.type === "folder" ? "📁" : "📄";
      const hasCustom = route.interval !== null && route.interval !== undefined
        || route.skip_unchanged !== null && route.skip_unchanged !== undefined
        || route.threads !== null && route.threads !== undefined;
      const customBadge = hasCustom ? ' <span class="badge-custom" title="У правила свои настройки">⚙</span>' : "";
      const threadsLabel = (route.threads !== null && route.threads !== undefined)
        ? `${route.threads} поток(ов)` : null;

      const threadsBadge = threadsLabel
        ? ` <span class="badge-custom" title="Потоков копирования: ${route.threads}">×${route.threads}</span>` : "";

      card.innerHTML = `
        <div class="route-name" title="${escapeHtml(route.name)}">${typeIcon} ${escapeHtml(route.name)}${customBadge}${threadsBadge}</div>
        <div class="path-chip" title="${escapeHtml(route.source)}">${escapeHtml(route.source)}</div>
        <div class="conveyor"><span class="dot"></span></div>
        <div></div>
        <div class="path-chip" title="${escapeHtml(route.destination)}">${escapeHtml(route.destination)}</div>
        <label class="switch route-toggle" title="Включить/выключить правило">
          <input type="checkbox" class="route-enabled-input" ${route.enabled ? "checked" : ""}>
          <span class="track"></span>
        </label>
        <div class="route-actions">
          <button class="icon-btn route-edit-btn" title="Редактировать">✎</button>
          <button class="icon-btn route-delete-btn" title="Удалить">🗑</button>
        </div>
        <div class="route-progress" data-route-progress hidden>
          <div class="route-progress-head">
            <span class="route-progress-phase" data-progress-phase></span>
            <span class="route-progress-speed" data-progress-speed></span>
          </div>
          <div class="route-progress-bar"><div class="route-progress-bar-fill" data-progress-bar></div></div>
          <div class="route-progress-meta" data-progress-meta></div>
          <div class="route-progress-active" data-progress-active></div>
        </div>
        <div class="route-stats" data-route-stats>${escapeHtml(formatRouteStats(route.stats))}</div>
      `;

      card.querySelector(".route-enabled-input").addEventListener("change", async (e) => {
        try {
          await api(`/api/routes/${route.id}`, {
            method: "PUT",
            body: JSON.stringify({ enabled: e.target.checked }),
          });
          route.enabled = e.target.checked;
          card.classList.toggle("disabled", !route.enabled);
        } catch (err) {
          alert(err.message);
          e.target.checked = route.enabled;
        }
      });

      card.querySelector(".route-edit-btn").addEventListener("click", () => openRouteModal(route));
      card.querySelector(".route-delete-btn").addEventListener("click", () => deleteRoute(route));

      el.routeList.appendChild(card);
    });
  }

  async function deleteRoute(route) {
    if (!confirm(`Удалить правило «${route.name}»?`)) return;
    await api(`/api/routes/${route.id}`, { method: "DELETE" });
    routes = routes.filter((r) => r.id !== route.id);
    renderRoutes();
  }

  // ---------------------------------------------------------------------
  // Модалка правила (добавление/редактирование)
  // ---------------------------------------------------------------------
  function openRouteModal(route) {
    editingRouteId = route ? route.id : null;
    el.routeModalTitle.textContent = route ? "Изменить правило" : "Новое правило";
    el.routeNameInput.value = route ? route.name : "";
    el.sourceInput.value = route ? route.source : "";
    el.destinationInput.value = route ? route.destination : "";
    el.routeHint.textContent = "Выберите отдельный файл или целую папку как источник, и папку назначения — куда копировать.";

    const hasInterval = route && route.interval !== null && route.interval !== undefined;
    const hasSkip = route && route.skip_unchanged !== null && route.skip_unchanged !== undefined;
    const hasThreads = route && route.threads !== null && route.threads !== undefined;
    const isCustom = hasInterval || hasSkip || hasThreads;

    el.routeCustomToggle.checked = isCustom;
    el.routeCustomFields.hidden = !isCustom;
    el.routeIntervalInput.value = hasInterval ? route.interval : "";
    el.routeSkipSelect.value = hasSkip ? String(route.skip_unchanged) : "default";
    el.routeThreadsInput.value = hasThreads ? route.threads : "";

    el.routeModalOverlay.hidden = false;
  }

  el.routeCustomToggle.addEventListener("change", () => {
    el.routeCustomFields.hidden = !el.routeCustomToggle.checked;
  });

  function closeRouteModal() {
    el.routeModalOverlay.hidden = true;
    editingRouteId = null;
  }

  el.addRouteBtn.addEventListener("click", () => openRouteModal(null));
  el.closeRouteModal.addEventListener("click", closeRouteModal);
  el.cancelRouteBtn.addEventListener("click", closeRouteModal);
  el.routeModalOverlay.addEventListener("click", (e) => {
    if (e.target === el.routeModalOverlay) closeRouteModal();
  });

  el.saveRouteBtn.addEventListener("click", async () => {
    const payload = {
      name: el.routeNameInput.value.trim(),
      source: el.sourceInput.value.trim(),
      destination: el.destinationInput.value.trim(),
    };
    if (!payload.source || !payload.destination) {
      el.routeHint.textContent = "Укажите и источник, и назначение.";
      el.routeHint.style.color = "var(--warn)";
      return;
    }

    if (el.routeCustomToggle.checked) {
      const iv = el.routeIntervalInput.value.trim();
      payload.interval = iv === "" ? null : parseFloat(iv);
      const sv = el.routeSkipSelect.value;
      payload.skip_unchanged = sv === "default" ? null : (sv === "true");
      const tv = el.routeThreadsInput.value.trim();
      payload.threads = tv === "" ? null : parseInt(tv, 10);
    } else {
      payload.interval = null;
      payload.skip_unchanged = null;
      payload.threads = null;
    }
    try {
      let saved;
      if (editingRouteId) {
        saved = await api(`/api/routes/${editingRouteId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        const idx = routes.findIndex((r) => r.id === editingRouteId);
        if (idx !== -1) routes[idx] = saved;
      } else {
        saved = await api("/api/routes", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        routes.push(saved);
      }
      renderRoutes();
      closeRouteModal();
    } catch (err) {
      el.routeHint.textContent = err.message;
      el.routeHint.style.color = "var(--error)";
    }
  });

  // ---------------------------------------------------------------------
  // Обзор файловой системы
  // ---------------------------------------------------------------------
  document.querySelectorAll("[data-browse-target]").forEach((btn) => {
    btn.addEventListener("click", () => {
      browseMode = btn.dataset.browseTarget; // 'source' | 'destination'
      browseSelectedEntry = null;
      el.browseModalTitle.textContent = browseMode === "source"
        ? "Выберите файл или папку-источник"
        : "Выберите папку назначения";
      el.chooseFolderBtn.hidden = false;
      const startPath = browseMode === "source" ? el.sourceInput.value : el.destinationInput.value;
      el.browseModalOverlay.hidden = false;
      navigateBrowse(startPath || null);
    });
  });

  el.closeBrowseModal.addEventListener("click", () => { el.browseModalOverlay.hidden = true; });
  el.browseModalOverlay.addEventListener("click", (e) => {
    if (e.target === el.browseModalOverlay) el.browseModalOverlay.hidden = true;
  });

  async function navigateBrowse(path) {
    el.browseList.innerHTML = `<div class="browse-empty">Загрузка…</div>`;
    browseSelectedEntry = null;
    updateBrowseSelectionUI();

    const onlyDirsParam = browseMode === "destination" ? "&dirs_only=1" : "";
    const url = "/api/browse?path=" + encodeURIComponent(path || "") + onlyDirsParam;

    let data;
    try {
      data = await api(url);
    } catch (err) {
      el.browseList.innerHTML = `<div class="browse-empty">${escapeHtml(err.message)}</div>`;
      return;
    }

    browseCurrentPath = data.current;
    el.browsePathInput.value = data.current || "";
    el.browseUpBtn.disabled = !data.parent;

    el.browseList.innerHTML = "";
    if (!data.entries.length) {
      el.browseList.innerHTML = `<div class="browse-empty">Папка пуста</div>`;
    }
    data.entries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "browse-item" + (entry.is_dir ? " is-dir" : "");
      row.innerHTML = `<span class="icon">${entry.is_dir ? "📁" : "📄"}</span><span class="name">${escapeHtml(entry.name)}</span>`;
      row.addEventListener("click", () => {
        if (entry.is_dir) {
          navigateBrowse(entry.path);
        } else {
          if (browseMode === "destination") return; // папки назначения — только каталоги
          browseSelectedEntry = entry;
          document.querySelectorAll(".browse-item.selected").forEach((n) => n.classList.remove("selected"));
          row.classList.add("selected");
          updateBrowseSelectionUI();
        }
      });
      el.browseList.appendChild(row);
    });

    // двойной клик по папке в режиме "источник" — тоже позволяет выбрать саму папку как источник
    updateBrowseSelectionUI();
  }

  function updateBrowseSelectionUI() {
    if (browseSelectedEntry) {
      el.browseSelectedLabel.textContent = browseSelectedEntry.path;
      el.chooseItemBtn.disabled = false;
    } else {
      el.browseSelectedLabel.textContent = "Ничего не выбрано";
      el.chooseItemBtn.disabled = true;
    }
  }

  el.browseUpBtn.addEventListener("click", async () => {
    try {
      const data = await api("/api/browse?path=" + encodeURIComponent(browseCurrentPath || ""));
      if (data.parent) navigateBrowse(data.parent);
      else navigateBrowse(null);
    } catch (e) { navigateBrowse(null); }
  });

  el.browseHomeBtn.addEventListener("click", async () => {
    try {
      const data = await api("/api/browse?path=" + encodeURIComponent(browseCurrentPath || ""));
      navigateBrowse(data.home);
    } catch (e) { navigateBrowse(null); }
  });

  el.browsePathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") navigateBrowse(el.browsePathInput.value.trim());
  });

  el.chooseFolderBtn.addEventListener("click", () => {
    if (!browseCurrentPath) return;
    applyBrowseSelection(browseCurrentPath);
  });

  el.chooseItemBtn.addEventListener("click", () => {
    if (!browseSelectedEntry) return;
    applyBrowseSelection(browseSelectedEntry.path);
  });

  function applyBrowseSelection(path) {
    if (browseMode === "source") el.sourceInput.value = path;
    else el.destinationInput.value = path;
    el.browseModalOverlay.hidden = true;
  }

  // ---------------------------------------------------------------------
  // Настройки
  // ---------------------------------------------------------------------
  let settingsSaveTimer = null;
  function scheduleSaveSettings() {
    clearTimeout(settingsSaveTimer);
    settingsSaveTimer = setTimeout(async () => {
      try {
        await api("/api/settings", {
          method: "POST",
          body: JSON.stringify({
            interval: parseFloat(el.intervalInput.value) || 0,
            skip_unchanged: el.skipUnchangedInput.checked,
            threads: parseInt(el.threadsInput.value, 10) || 1,
          }),
        });
      } catch (e) { /* тихо игнорируем */ }
    }, 400);
  }
  el.intervalInput.addEventListener("input", scheduleSaveSettings);
  el.skipUnchangedInput.addEventListener("change", scheduleSaveSettings);
  el.threadsInput.addEventListener("input", scheduleSaveSettings);

  // ---------------------------------------------------------------------
  // Старт / стоп
  // ---------------------------------------------------------------------
  el.toggleRunBtn.addEventListener("click", async () => {
    el.toggleRunBtn.disabled = true;
    try {
      if (running) {
        await api("/api/stop", { method: "POST" });
        running = false;
      } else {
        await api("/api/start", { method: "POST" });
        running = true;
      }
      renderRunState();
    } catch (err) {
      alert(err.message);
    } finally {
      el.toggleRunBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // Журнал (поллинг)
  // ---------------------------------------------------------------------
  function appendLogLine(entry) {
    const line = document.createElement("div");
    line.className = "log-line level-" + (entry.level || "info");
    line.innerHTML = `<span class="t">${formatTime(entry.t)}</span>${escapeHtml(entry.msg)}`;
    el.terminal.appendChild(line);

    const nearBottom = el.terminal.scrollHeight - el.terminal.scrollTop - el.terminal.clientHeight < 60;
    if (nearBottom) el.terminal.scrollTop = el.terminal.scrollHeight;

    // ограничиваем число строк в DOM
    while (el.terminal.childElementCount > 500) {
      el.terminal.removeChild(el.terminal.firstChild);
    }
  }

  function flashRouteStatus(route, status, duration) {
    const card = el.routeList.querySelector(`[data-route-id="${route.id}"]`);
    if (!card) return;
    card.dataset.status = status;
    clearTimeout(routeStatusTimers[route.id]);
    routeStatusTimers[route.id] = setTimeout(() => {
      card.dataset.status = "idle";
    }, duration);
  }

  function matchRouteByPath(path) {
    return routes.find((r) => path === r.source || path.startsWith(r.source) || path.startsWith(r.destination));
  }

  function reactToLogMessage(entry) {
    const msg = entry.msg;
    let m;
    if ((m = msg.match(/^Проверяю: (.+)$/))) {
      const route = matchRouteByPath(m[1]);
      if (route) flashRouteStatus(route, "checking", 6000);
    } else if ((m = msg.match(/^Скопировано → (.+)$/))) {
      const route = routes.find((r) => m[1].startsWith(r.destination));
      if (route) flashRouteStatus(route, "ok", 3500);
    } else if (entry.level === "error") {
      const route = routes.find((r) => msg.includes(r.source));
      if (route) flashRouteStatus(route, "error", 5000);
    }
  }

  async function pollLogs() {
    try {
      const data = await api("/api/logs?since=" + logCursor);
      data.logs.forEach((entry) => {
        appendLogLine(entry);
        reactToLogMessage(entry);
      });
      logCursor = data.last;
    } catch (e) { /* сеть могла моргнуть — не страшно */ }
  }

  function updateRouteStats(freshRoutes) {
    (freshRoutes || []).forEach((fresh) => {
      const idx = routes.findIndex((r) => r.id === fresh.id);
      if (idx !== -1) routes[idx].stats = fresh.stats;
      const statsEl = el.routeList.querySelector(`[data-route-id="${fresh.id}"] [data-route-stats]`);
      if (statsEl) statsEl.textContent = formatRouteStats(fresh.stats);
    });
  }

  async function pollStatus() {
    try {
      const data = await api("/api/state");
      if (data.running !== running) {
        running = data.running;
        renderRunState();
      }
      updateRouteStats(data.routes);
    } catch (e) { /* игнор */ }
  }

  // ---------------------------------------------------------------------
  // Прогресс копирования по каждому правилу (поллинг)
  // ---------------------------------------------------------------------
  function renderRouteProgress(routeId, p) {
    const card = el.routeList.querySelector(`[data-route-id="${routeId}"]`);
    if (!card || !p) return;

    const box = card.querySelector("[data-route-progress]");
    const activeEntries = Object.entries(p.active || {});
    const queueLen = (p.queue || []).length;
    const isBusy = p.phase === "scanning" || activeEntries.length > 0
      || (p.pass_total_files || 0) > (p.pass_done_files || 0);

    box.hidden = !isBusy;
    if (!isBusy) return;

    card.querySelector("[data-progress-phase]").textContent =
      p.phase === "scanning" ? "Сканирую…" : "Копирую…";

    const totalSpeed = activeEntries.reduce((sum, [, v]) => sum + (v.speed || 0), 0);
    card.querySelector("[data-progress-speed]").textContent = totalSpeed ? humanSpeed(totalSpeed) : "";

    const totalBytes = p.pass_total_bytes || 0;
    const doneBytes = p.pass_done_bytes || 0;
    const totalFiles = p.pass_total_files || 0;
    const doneFiles = p.pass_done_files || 0;
    const pct = totalBytes
      ? Math.min(100, (doneBytes / totalBytes) * 100)
      : (totalFiles ? (doneFiles / totalFiles) * 100 : 0);
    card.querySelector("[data-progress-bar]").style.width = pct.toFixed(1) + "%";

    const parts = [];
    if (p.phase === "scanning") {
      parts.push(`просканировано ${p.scanned_files || 0}`);
    } else {
      parts.push(`${doneFiles}/${totalFiles} файлов`);
      if (totalBytes) parts.push(`${humanBytes(doneBytes)} / ${humanBytes(totalBytes)}`);
    }
    if (p.pass_skipped_files) parts.push(`уже актуально: ${p.pass_skipped_files}`);
    if (queueLen) parts.push(`в очереди: ${queueLen}`);
    card.querySelector("[data-progress-meta]").textContent = parts.join(" · ");

    const activeBox = card.querySelector("[data-progress-active]");
    activeBox.innerHTML = "";
    activeEntries.slice(0, 3).forEach(([path, info]) => {
      const row = document.createElement("div");
      row.className = "active-file";
      const meta = info.state === "waiting"
        ? "жду стабильности…"
        : `${humanBytes(info.done)} / ${humanBytes(info.size)} — ${humanSpeed(info.speed)}`;
      row.innerHTML = `<span class="active-file-name" title="${escapeHtml(path)}">${escapeHtml(shortenPath(path, 60))}</span><span class="active-file-meta">${meta}</span>`;
      activeBox.appendChild(row);
    });
    if (activeEntries.length > 3) {
      const more = document.createElement("div");
      more.className = "active-file-meta";
      more.textContent = `…и ещё ${activeEntries.length - 3} копируется`;
      activeBox.appendChild(more);
    }
  }

  async function pollProgress() {
    try {
      const data = await api("/api/progress");
      Object.keys(data).forEach((routeId) => renderRouteProgress(routeId, data[routeId]));
    } catch (e) { /* игнор */ }
  }

  el.clearLogBtn.addEventListener("click", () => { el.terminal.innerHTML = ""; });

  // ---------------------------------------------------------------------
  // Безопасность
  // ---------------------------------------------------------------------
  let realApiKey = null;
  let keyVisible = false;

  async function loadSecurityInfo() {
    try {
      const data = await api("/api/security");
      realApiKey = data.api_key || null;
      renderKeyField();
    } catch (e) { /* игнор */ }
  }

  function renderKeyField() {
    if (!realApiKey) return;
    el.apiKeyInput.value = keyVisible ? realApiKey : "•".repeat(Math.min(realApiKey.length, 28));
  }

  el.toggleKeyVisibility.addEventListener("click", () => {
    keyVisible = !keyVisible;
    el.toggleKeyVisibility.textContent = keyVisible ? "Скрыть" : "Показать";
    renderKeyField();
  });

  el.copyKeyBtn.addEventListener("click", async () => {
    if (!realApiKey) return;
    try {
      await navigator.clipboard.writeText(realApiKey);
      el.copyKeyBtn.textContent = "Скопировано";
      setTimeout(() => { el.copyKeyBtn.textContent = "Копировать"; }, 1500);
    } catch (e) { /* буфер обмена недоступен */ }
  });

  el.regenerateKeyBtn.addEventListener("click", async () => {
    if (!confirm("Пересоздать ключ безопасности? Старый ключ перестанет работать, и все другие устройства нужно будет авторизовать заново.")) return;
    try {
      const data = await api("/api/security/regenerate", { method: "POST" });
      realApiKey = data.api_key;
      keyVisible = true;
      el.toggleKeyVisibility.textContent = "Скрыть";
      renderKeyField();
    } catch (err) {
      alert(err.message);
    }
  });

  el.logoutBtn.addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST" }); } catch (e) { /* игнор */ }
    window.location.href = "/login";
  });

  // ---------------------------------------------------------------------
  // Инициализация
  // ---------------------------------------------------------------------
  (async function init() {
    try {
      await loadState();
      await loadSecurityInfo();
    } catch (e) {
      console.error(e);
    }
    logPollTimer = setInterval(pollLogs, 1200);
    statusPollTimer = setInterval(pollStatus, 2500);
    setInterval(pollProgress, 800);
    pollLogs();
    pollProgress();
  })();
})();
