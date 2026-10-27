/**
 * Интерфейс мониторинга: опрос /api/state раз в секунду, таблица расстояний,
 * настройки и кнопка «Тест HTML» (снимок data_test/1.html без живого сайта).
 * Колонка «Кр. / жёл. у точки» — сколько объектов красные и жёлтые до этой статичной точки.
 */
const STATUS_LABEL = {
  alert: "близко",
  warning: "внимание",
  blue: "синяя зона",
  ok: "норма",
  running: "работает",
  starting: "запуск",
  stopping: "остановка",
  stopped: "остановлен",
  error: "ошибка",
};

let staticObjects = (window.__INITIAL_SETTINGS__?.static_objects || []).slice();
let maxGroups = (window.__INITIAL_SETTINGS__?.max_groups || []).slice();

// Отслеживание красных записей для звукового сигнала
let previousAlertKeys = new Set();
let soundEnabled = true;

const els = {
  site: document.getElementById("site-label"),
  status: document.getElementById("status-chip"),
  start: document.getElementById("btn-start"),
  stop: document.getElementById("btn-stop"),
  settingsBtn: document.getElementById("btn-settings"),
  testHtml: document.getElementById("btn-test-html"),
  pruneHistory: document.getElementById("btn-prune-history"),
  dialog: document.getElementById("settings-dialog"),
  form: document.getElementById("settings-form"),
  cancel: document.getElementById("btn-cancel"),
  formError: document.getElementById("settings-error"),
  error: document.getElementById("error-banner"),
  scaleWarning: document.getElementById("scale-warning"),
  tbody: document.getElementById("distance-body"),
  moves: document.getElementById("move-body"),
  logs: document.getElementById("log-list"),
  meta: document.getElementById("table-meta"),
  dyn: document.getElementById("stat-dyn"),
  stat: document.getElementById("stat-stat"),
  alert: document.getElementById("stat-alert"),
  warn: document.getElementById("stat-warn"),
  blue: document.getElementById("stat-blue"),
  interval: document.getElementById("stat-interval"),
  polls: document.getElementById("stat-polls"),
  historyRowsDay: document.getElementById("stat-history-rows-day"),
  historySizeDay: document.getElementById("stat-history-size-day"),
  staticList: document.getElementById("static-list"),
  addStatic: document.getElementById("btn-add-static"),
  maxList: document.getElementById("max-recipient-list"),
  addMax: document.getElementById("btn-add-max-recipient"),
  maxTokenStatus: document.getElementById("max-token-status"),
};

els.start.addEventListener("click", () => post("/api/worker/start"));
els.stop.addEventListener("click", () => post("/api/worker/stop"));
els.settingsBtn.addEventListener("click", openSettings);
if (els.testHtml) els.testHtml.addEventListener("click", loadTestHtml);
if (els.pruneHistory) els.pruneHistory.addEventListener("click", async () => {
  if (!window.confirm("Удалить историю динамических объектов старше 20 дней? Настройки стационарных объектов сохранятся.")) return;
  try {
    const result = await post("/api/history/prune");
    window.alert(`Удалено опросов: ${result.polls_deleted}; снимков таблицы: ${result.snapshots_deleted}.`);
    tick();
  } catch (error) {
    showError(error.message);
  }
});
els.cancel.addEventListener("click", () => els.dialog.close());
els.form.addEventListener("submit", saveSettings);
els.addStatic.addEventListener("click", addStaticRow);
els.addMax.addEventListener("click", () => {
  maxGroups = collectMaxGroups();
  maxGroups.push({ user_ids: "", all_objects: false, zones: [], rules: [] });
  renderMaxGroups();
});
els.maxList.addEventListener("change", event => {
  maxGroups = collectMaxGroups();
  if (event.target.matches(".max-all, .max-object-select")) renderMaxGroups();
});
els.maxList.addEventListener("click", event => {
  const button = event.target.closest("button[data-max-action]");
  if (!button) return;
  maxGroups = collectMaxGroups();
  const group = maxGroups[Number(button.dataset.groupIndex)];
  if (button.dataset.maxAction === "remove-group") {
    maxGroups.splice(Number(button.dataset.groupIndex), 1);
  } else if (button.dataset.maxAction === "add-object") {
    const used = new Set(group.rules.map(rule => rule.static_index));
    const available = collectStaticObjects().find(point => !used.has(point.id));
    if (available && !group.rules.some(rule => !rule.static_index)) {
      group.rules.push({ static_index: 0, zones: [] });
    }
  } else if (button.dataset.maxAction === "remove-object") {
    group.rules.splice(Number(button.dataset.ruleIndex), 1);
  }
  renderMaxGroups();
});

renderStaticList();

tick();
setInterval(tick, 1000);

/**
 * Простой и надежный способ воспроизведения звука через Audio элемент
 */
function playBeep2() {
  if (!soundEnabled) return;
  
  try {
    // Создаем аудио контекст только при первом использовании
    if (!window._beepAudioContext) {
      window._beepAudioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    
    const ctx = window._beepAudioContext;
    
    // Если контекст приостановлен, возобновляем
    if (ctx.state === 'suspended') {
      ctx.resume();
    }
    
    // Создаем звук
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    
    osc.connect(gain);
    gain.connect(ctx.destination);
    
    // Используем синусоидальный звук - он чище и громче
    osc.type = 'sine';
    osc.frequency.value = 800;
    
    // Настройка громкости
    const now = ctx.currentTime;
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(0.3, now + 0.05);
    gain.gain.linearRampToValueAtTime(0.3, now + 0.15);
    gain.gain.linearRampToValueAtTime(0, now + 0.25);
    
    osc.start(now);
    osc.stop(now + 0.25);
    
    // Второй сигнал для заметности (двойной звук)
    setTimeout(() => {
      try {
        const osc2 = ctx.createOscillator();
        const gain2 = ctx.createGain();
        osc2.connect(gain2);
        gain2.connect(ctx.destination);
        osc2.type = 'sine';
        osc2.frequency.value = 1000;
        const now2 = ctx.currentTime;
        gain2.gain.setValueAtTime(0, now2);
        gain2.gain.linearRampToValueAtTime(0.25, now2 + 0.05);
        gain2.gain.linearRampToValueAtTime(0, now2 + 0.2);
        osc2.start(now2);
        osc2.stop(now2 + 0.2);
      } catch (e) {}
    }, 150);
    
  } catch (e) {
    console.warn('Звук не воспроизведен:', e.message);
    // Резервный вариант - использовать HTML5 Audio
    try {
      playFallbackBeep();
    } catch (e2) {}
  }
}

/** Запрашивает системный сигнал у сервера и использует Web Audio как резерв. */
async function playBeep() {
    if (!soundEnabled) return;
    
    try {
        // Пытаемся через API бэкенда (системный динамик)
        const response = await fetch('/api/beep', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            console.log('🔊 Звук через системный динамик');
            return;
        }
    } catch (e) {
        console.log('API звука не доступен, пробуем браузерный способ');
    }
    
    // Если API не сработал, используем единственный браузерный fallback.
    playBeep2();
}



/**
 * Резервный способ воспроизведения через HTML5 Audio
 */
function playFallbackBeep() {
  try {
    // Создаем звук с помощью генерации PCM данных
    const sampleRate = 8000;
    const duration = 0.2;
    const samples = Math.floor(sampleRate * duration);
    const data = new Uint8Array(samples);
    
    for (let i = 0; i < samples; i++) {
      const t = i / sampleRate;
      data[i] = 128 + 127 * Math.sin(2 * Math.PI * 800 * t);
    }
    
    const wav = createWav(data, sampleRate);
    const blob = new Blob([wav], { type: 'audio/wav' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.volume = 1.0;
    audio.play().catch(() => {});
    
    // Освобождаем URL после воспроизведения
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    console.warn('Резервный звук не сработал:', e);
  }
}

/**
 * Создает WAV файл из PCM данных
 */
function createWav(data, sampleRate) {
  const numChannels = 1;
  const bitsPerSample = 8;
  const byteRate = sampleRate * numChannels * bitsPerSample / 8;
  const blockAlign = numChannels * bitsPerSample / 8;
  const dataSize = data.length;
  const headerSize = 44;
  const totalSize = headerSize + dataSize;
  
  const buffer = new ArrayBuffer(totalSize);
  const view = new DataView(buffer);
  
  // RIFF заголовок
  writeString(view, 0, 'RIFF');
  view.setUint32(4, totalSize - 8, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  writeString(view, 36, 'data');
  view.setUint32(40, dataSize, true);
  
  // Данные
  for (let i = 0; i < data.length; i++) {
    view.setUint8(headerSize + i, data[i]);
  }
  
  return buffer;
}

/** Записывает ASCII-маркер в заголовок WAV-буфера. */
function writeString(view, offset, string) {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

/**
 * Проверяет, появились ли новые красные объекты
 * Возвращает true, если есть новые красные записи
 */
function checkNewAlerts(rows) {
  if (!rows || !rows.length) {
    previousAlertKeys = new Set();
    return false;
  }
  
  // Собираем ключи всех красных строк
  const currentAlertKeys = new Set();
  
  rows.forEach((row) => {
    if (row.status === "alert") {
      // Используем комбинацию static_index + dynamic_index + dynamic_name как уникальный ключ
      const key = `${row.static_index}|${row.dynamic_index}|${row.dynamic_name}`;
      currentAlertKeys.add(key);
    }
  });
  
  // Находим новые ключи (которых не было в предыдущем наборе)
  let hasNewAlerts = false;
  currentAlertKeys.forEach(key => {
    if (!previousAlertKeys.has(key)) {
      hasNewAlerts = true;
    }
  });
  
  // Обновляем предыдущий набор для следующей проверки
  previousAlertKeys = currentAlertKeys;
  
  // Если есть новые красные записи → звуковой сигнал
  if (hasNewAlerts) {
    console.log('🔴 Обнаружены новые красные объекты');
    return true;
  }
  
  return false;
}

// ===== Группировка строк по статичным объектам =====
// Состояние раскрытых групп живёт между опросами, чтобы не схлопывалось
// при каждом обновлении (иначе раскрытая точка закрывается каждую секунду).
const expandedStatics = new Set();

/**
 * Группирует строки снимка по static_index, сохраняя порядок первого появления.
 * Внутри каждой группы сортирует по возрастанию дистанции — ближайший первым.
 */
function groupRowsByStatic(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = row.static_index;
    if (!groups.has(key)) {
      groups.set(key, {
        static_index: row.static_index,
        static_name: row.static_name,
        static_coords: row.static_coords,
        alert_count: row.alert_count ?? 0,
        warning_count: row.warning_count ?? 0,
        phone: row.phone || "",
        rows: [],
      });
    }
    groups.get(key).rows.push(row);
  }
  for (const group of groups.values()) {
    group.rows.sort((a, b) => a.distance - b.distance);
  }
  return [...groups.values()];
}

/**
 * Рендерит таблицу расстояний с группировкой по статичным объектам.
 * В свёрнутом виде — только ближайший объект, в раскрытом — все.
 */
function renderDistanceTable(rows, unit, settings) {
  if (!rows.length) {
    els.tbody.innerHTML = `<tr class="empty"><td colspan="10">Объекты не найдены (${escapeHtml(settings.dynamic_selector)})</td></tr>`;
    return;
  }

  const decimals = unit === "km" ? 2 : 1;
  const groups = groupRowsByStatic(rows);
  const html = [];

  for (const group of groups) {
    const nearest = group.rows[0];
    const expanded = expandedStatics.has(group.static_index);
    const extraCount = group.rows.length - 1;

    // --- Строка-заголовок группы (всегда видна) ---
    // Показывает ближайший объект, стрелку и счётчик остальных.
    html.push(`
      <tr class="static-group ${nearest.status}" data-static-index="${group.static_index}">
        <td class="cell-toggle">
          ${extraCount > 0
            ? `<button type="button" class="group-toggle" data-toggle="${group.static_index}"
                 aria-expanded="${expanded}" title="${expanded ? "Свернуть" : "Показать все объекты"}">
                 <span class="chevron">${expanded ? "▾" : "▸"}</span>
               </button>`
            : `<span class="group-toggle-placeholder"></span>`}
        </td>
        <td>${group.static_index}</td>
        <td>
          ${escapeHtml(group.static_name)}
          <span class="coords">${escapeHtml(group.static_coords)}</span>
        </td>
        <td>${escapeHtml(nearest.dynamic_name)}</td>
        <td>${dynamicNumberLabel(nearest.dynamic_index)}</td>
        <td>${escapeHtml(nearest.dynamic_coords)}</td>
        <td>${Number(nearest.distance).toFixed(decimals)} ${nearest.distance_unit || unit}</td>
        <td title="красных / жёлтых до этой статичной точки">
          ${escapeHtml(nearest.nearby_counts || `${group.alert_count} / ${group.warning_count}`)}
        </td>
        <td>
          ${STATUS_LABEL[nearest.status] || nearest.status}
          ${extraCount > 0 ? `<span class="group-badge" title="Всего объектов у точки">+${extraCount}</span>` : ""}
        </td>
        <td>${escapeHtml(group.phone || "—")}</td>
      </tr>
    `);

    // --- Вложенные строки (видны только при expanded) ---
    // Первый (ближайший) уже показан в заголовке — начинаем со второго.
    if (expanded && extraCount > 0) {
      for (let i = 1; i < group.rows.length; i++) {
        const row = group.rows[i];
        html.push(`
          <tr class="static-child ${row.status}" data-parent="${group.static_index}">
            <td class="cell-toggle"></td>
            <td>${group.static_index}</td>
            <td class="muted-cell">${escapeHtml(group.static_name)}</td>
            <td>${escapeHtml(row.dynamic_name)}</td>
            <td>${dynamicNumberLabel(row.dynamic_index)}</td>
            <td>${escapeHtml(row.dynamic_coords)}</td>
            <td>${Number(row.distance).toFixed(decimals)} ${row.distance_unit || unit}</td>
            <td>${escapeHtml(row.nearby_counts || `${row.alert_count ?? 0} / ${row.warning_count ?? 0}`)}</td>
            <td>${STATUS_LABEL[row.status] || row.status}</td>
            <td>${escapeHtml(row.phone || group.phone || "—")}</td>
          </tr>
        `);
      }
    }
  }

  els.tbody.innerHTML = html.join("");
  bindGroupToggles();
}

/**
 * Навешивает обработчики на кнопки-раскрывашки.
 * Перерисовывает таблицу через сохранённый последний снимок.
 */
function bindGroupToggles() {
  els.tbody.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = Number(btn.dataset.toggle);
      if (expandedStatics.has(key)) expandedStatics.delete(key);
      else expandedStatics.add(key);
      rerenderLastSnapshot();
    });
  });
}

// Последний применённый снимок нужен, чтобы перерисовывать таблицу
// при клике по раскрывашке без нового запроса к серверу.
let lastSnapshot = null;
let lastSettings = null;

/** Перерисовывает таблицу из последнего снимка (после toggle). */
function rerenderLastSnapshot() {
  if (!lastSnapshot || !lastSettings) return;
  const unit = lastSnapshot.distance_unit || "px";
  renderDistanceTable(lastSnapshot.rows || [], unit, lastSettings);
}

/**
 * Применяет данные снимка к интерфейсу
 */
// function applySnapshot(snapshot, settings) {
//   if (!snapshot) {
//     els.dyn.textContent = "0";
//     els.stat.textContent = "0";
//     els.alert.textContent = "0";
//     els.warn.textContent = "0";
//     els.scaleWarning.hidden = true;
//     return;
//   }
  
//   const rows = snapshot.rows || [];
//   const alerts = snapshot.alerts || 0;
  
//   // Проверяем наличие новых красных объектов
//   const hasNewAlerts = checkNewAlerts(rows);
  
//   // Воспроизводим звук при появлении новых красных объектов
//   if (hasNewAlerts) {
//     playBeep();
    
//     // Показываем визуальное предупреждение
//     showAlertBanner(alerts);
//   }
  
//   const unit = snapshot.distance_unit || "px";
//   const decimals = unit === "km" ? 2 : 1;
//   els.dyn.textContent = String(snapshot.dynamics_count);
//   els.stat.textContent = String(snapshot.statics_count);
//   els.alert.textContent = String(alerts);
//   els.warn.textContent = String(snapshot.warnings || 0);
  
//   const mode = snapshot.coordinate_mode || settings.coordinate_mode;
//   const zoom = snapshot.map_zoom != null ? ` · zoom=${snapshot.map_zoom}` : "";
//   const geo = snapshot.geo_source && snapshot.geo_source !== "none"
//     ? ` · geo=${snapshot.geo_source}`
//     : "";
//   els.meta.textContent =
//     `${snapshot.title || snapshot.url} · режим ${mode}${zoom}${geo} · красный ≤ ${snapshot.threshold} ${unit} · жёлтый ≤ ${snapshot.warning_limit} ${unit}`;
  
//   if (snapshot.scale_warning) {
//     els.scaleWarning.hidden = false;
//     els.scaleWarning.textContent = snapshot.scale_warning;
//   } else {
//     els.scaleWarning.hidden = true;
//   }
  
//   if (!rows.length) {
//     els.tbody.innerHTML = `<tr class="empty"><td colspan="9">Объекты не найдены (${escapeHtml(settings.dynamic_selector)})</td></tr>`;
//     return;
//   }
  
//   els.tbody.innerHTML = rows.map((row) => `
//     <tr class="${row.status}">
//       <td>${row.static_index}</td>
//       <td>${escapeHtml(row.static_name)}</td>
//       <td>${escapeHtml(row.dynamic_name)}</td>
//       <td>${row.dynamic_index}</td>
//       <td>${escapeHtml(row.dynamic_coords)}</td>
//       <td>${Number(row.distance).toFixed(decimals)} ${row.distance_unit || unit}</td>
//       <td title="красных / жёлтых до этой статичной точки">${escapeHtml(row.nearby_counts || `${row.alert_count ?? 0} / ${row.warning_count ?? 0}`)}</td>
//       <td>${STATUS_LABEL[row.status] || row.status}</td>
//       <td>${escapeHtml(row.phone || "—")}</td> <!-- Добавлено поле phone -->
//     </tr>
//   `).join("");
// }


function applySnapshot(snapshot, settings) {
  if (!snapshot) {
    els.dyn.textContent = "0";
    els.stat.textContent = "0";
    els.alert.textContent = "0";
    els.warn.textContent = "0";
    els.blue.textContent = "0";
    els.scaleWarning.hidden = true;
    return;
  }

  lastSnapshot = snapshot;
  lastSettings = settings;

  const rows = snapshot.rows || [];
  const alerts = snapshot.alerts || 0;

  const hasNewAlerts = checkNewAlerts(rows);
  if (hasNewAlerts) {
    playBeep();
    showAlertBanner(alerts);
  }

  const unit = snapshot.distance_unit || "px";
  els.dyn.textContent = String(snapshot.dynamics_count);
  els.stat.textContent = String(snapshot.statics_count);
  els.alert.textContent = String(alerts);
  els.warn.textContent = String(snapshot.warnings || 0);
  els.blue.textContent = String(snapshot.blues || 0);

  const mode = snapshot.coordinate_mode || settings.coordinate_mode;
  const zoom = snapshot.map_zoom != null ? ` · zoom=${snapshot.map_zoom}` : "";
  const geo = snapshot.geo_source && snapshot.geo_source !== "none"
    ? ` · geo=${snapshot.geo_source}`
    : "";
  els.meta.textContent =
    `${snapshot.title || snapshot.url} · режим ${mode}${zoom}${geo} · красный ≤ ${snapshot.threshold} ${unit} · жёлтый ≤ ${snapshot.warning_limit} ${unit}${unit === "km" ? ` · синий ≤ ${snapshot.blue_limit || 350} км` : ""}`;

  if (snapshot.scale_warning) {
    els.scaleWarning.hidden = false;
    els.scaleWarning.textContent = snapshot.scale_warning;
  } else {
    els.scaleWarning.hidden = true;
  }

  renderDistanceTable(rows, unit, settings);
}

/**
 * Показывает визуальное предупреждение о красных объектах
 */
function showAlertBanner(count) {
  // Удаляем существующий баннер
  const existingAlert = document.getElementById("alert-banner");
  if (existingAlert) existingAlert.remove();
  
  const alertBanner = document.createElement("div");
  alertBanner.id = "alert-banner";
  alertBanner.className = "alert-banner";
  alertBanner.innerHTML = `
    <span class="alert-icon">⚠️</span>
    <strong>${count}</strong> красных записей обнаружено!
    <button type="button" onclick="this.parentElement.remove()" class="btn btn-sm">Закрыть</button>
  `;
  
  const panel = document.querySelector(".panel");
  if (panel) {
    panel.insertBefore(alertBanner, panel.firstChild);
  }
  
  // Автоматическое скрытие через 8 секунд
  setTimeout(() => {
    const banner = document.getElementById("alert-banner");
    if (banner && !document.hidden) {
      banner.remove();
    }
  }, 8000);
}

/** Загружает стандартный тестовый HTML и обновляет экран. */
async function loadTestHtml() {
  try {
    hideError();
    await post("/api/test-file");
    await tick();
  } catch (err) {
    showError(err.message);
  }
}

/** Синхронизирует список точек и открывает диалог настроек. */
function openSettings() {
  renderStaticList();
  els.dialog.showModal();
}

/** Получает очередное состояние сервера, историю движений и обновляет UI. */
async function tick() {
  try {
    const [state, movements] = await Promise.all([
      get("/api/state"),
      get("/api/movements?limit=40"),
    ]);
    applySettings(state.settings);
    applyWorker(state.worker);
    applySnapshot(state.snapshot, state.settings);
    applyTableHistoryStats(state.table_history);
    applyLogs(state.logs || []);
    const visibleNumbers = new Set((state.snapshot?.dynamics || []).map((item) => Number(item.index)));
    applyMoves((movements || []).filter((item) => visibleNumbers.has(Number(item.object_index))));
  } catch (err) {
    showError("Нет связи с сервером: " + err.message);
  }
}

/** Применяет серверные настройки к карточкам и форме. */
function applySettings(settings) {
  if (!settings) return;
  soundEnabled = settings.sound_enabled !== false;
  document.documentElement.style.setProperty("--alert", settings.alert_color);
  document.documentElement.style.setProperty("--warning", settings.warning_color);
  document.documentElement.style.setProperty("--blue", settings.blue_color);
  els.site.textContent = settings.site;
  els.interval.textContent = `${settings.interval_seconds} с`;
  if (!els.dialog.open) {
    staticObjects = (settings.static_objects || []).slice();
    fillForm(settings);
  }
}

/** Заполняет поля диалога значениями runtime-настроек. */
function fillForm(settings) {
  setFormValue("site", settings.site);
  setFormValue("dynamic_selector", settings.dynamic_selector);
  setFormValue("coordinate_mode", settings.coordinate_mode);
  setFormValue("map_coordinate_precision", settings.map_coordinate_precision ?? 4);
  setFormValue("map_container_selector", settings.map_container_selector);
  setFormValue("interval_seconds", settings.interval_seconds);
  setFormValue("scan_wait_seconds", settings.scan_wait_seconds ?? 1.5);
  setFormValue("screenshot_dir", settings.screenshot_dir ?? "data/screenshots");
  setFormValue("screenshot_period_hours", settings.screenshot_period_hours ?? 1);
  setFormValue("screenshot_width", settings.screenshot_width ?? 1280);
  setFormValue("screenshot_height", settings.screenshot_height ?? 720);
  setFormValue("archive_html_dir", settings.archive_html_dir ?? "data/html");
  setFormValue("archive_html_period_hours", settings.archive_html_period_hours ?? 1);
  els.form.elements.namedItem("screenshot_enabled").checked = Boolean(settings.screenshot_enabled);
  els.form.elements.namedItem("archive_html_enabled").checked = Boolean(settings.archive_html_enabled);
  els.form.elements.namedItem("max_enabled").checked = Boolean(settings.max_enabled);
  els.form.elements.namedItem("max_token").value = "";
  els.form.elements.namedItem("max_token_clear").checked = false;
  els.maxTokenStatus.textContent = settings.max_token_set ? "Токен доступен" : "Токен не задан";
  maxGroups = (settings.max_groups || []).slice();
  renderMaxGroups();
  setFormValue("threshold", settings.threshold);
  setFormValue("warning_distance", settings.warning_distance);
  setFormValue("blue_distance", settings.blue_distance);
  setFormValue("reference_zoom", settings.reference_zoom);
  setFormValue("alert_color", settings.alert_color);
  setFormValue("warning_color", settings.warning_color);
  setFormValue("blue_color", settings.blue_color);
  
  const soundField = els.form.elements.namedItem("sound_enabled");
  if (soundField) {
    soundField.checked = soundEnabled;
  }
  
  const lock = els.form.elements.namedItem("lock_scale");
  if (lock) lock.checked = Boolean(settings.lock_scale);
}

/** Отображает состояние, счётчик и последнюю ошибку фонового потока. */
function applyWorker(worker) {
  const status = worker.status || "stopped";
  els.status.className = `status ${status}`;
  els.status.textContent = STATUS_LABEL[status] || status;
  els.start.disabled = Boolean(worker.running);
  els.stop.disabled = !worker.running;
  els.polls.textContent = String(worker.polls || 0);
  if (worker.error) showError(worker.error);
  else hideError();
}

/** Безопасно отрисовывает последние сообщения внутреннего журнала. */
function applyLogs(logs) {
  if (!logs.length) {
    els.logs.innerHTML = "<li class='muted'>Логов пока нет</li>";
    return;
  }
  els.logs.innerHTML = logs.map((item) => `
    <li>
      <span>${escapeHtml(item.ts)}</span>
      <span class="${escapeHtml(item.level)}">${escapeHtml(item.level)}</span>
      <span>${escapeHtml(item.message)}</span>
    </li>
  `).join("");
}

/** Не выдаёт временный ID за суточный номер сайта. */
function dynamicNumberLabel(index) {
  const value = Number(index);
  return value < 0 ? "номер не определён" : `№${value}`;
}

/** Отрисовывает последние смещения динамических объектов. */
function applyMoves(rows) {
  if (!rows.length) {
    els.moves.innerHTML = `<tr class="empty"><td colspan="5">Пока пусто</td></tr>`;
    return;
  }
  els.moves.innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.ts)}</td>
      <td>${dynamicNumberLabel(row.object_index)} ${escapeHtml(row.name)}</td>
      <td>${row.x}</td>
      <td>${row.y}</td>
      <td>${row.distance_moved == null ? "—" : row.distance_moved}</td>
    </tr>
  `).join("");
}

/** Перестраивает редактируемый список стационарных точек. */
function renderStaticList() {
  if (!staticObjects.length) {
    els.staticList.innerHTML = `<p class="muted">Нет статичных точек. Нажмите «+ Точка».</p>`;
    renderMaxGroups();
    return;
  }
  els.staticList.innerHTML = staticObjects.map((item, index) => `
    <div class="static-row" data-index="${index}">
      <input type="text" data-field="name" value="${escapeAttr(item.name || "")}" placeholder="Название" />
      <input type="number" step="any" data-field="x" value="${num(item.x)}" placeholder="x / % " />
      <input type="number" step="any" data-field="y" value="${num(item.y)}" placeholder="y / % " />
      <input type="number" step="any" data-field="lat" value="${num(item.lat)}" placeholder="lat" />
      <input type="number" step="any" data-field="lng" value="${num(item.lng)}" placeholder="lng" />
      <button type="button" class="btn btn-remove" data-remove="${index}">×</button>
    </div>
  `).join("");
  els.staticList.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const removed = Number(btn.dataset.remove) + 1;
      maxGroups = collectMaxGroups().map(group => ({
        ...group,
        rules: group.rules.filter(rule => rule.static_index !== removed).map(rule => ({
          ...rule, static_index: rule.static_index > removed ? rule.static_index - 1 : rule.static_index,
        })),
      }));
      staticObjects = collectStaticObjects();
      staticObjects.splice(removed - 1, 1);
      renumberStatics();
      renderStaticList();
    });
  });
  renderMaxGroups();
}

/** Добавляет новую пустую стационарную точку и перерисовывает список. */
function addStaticRow() {
  maxGroups = collectMaxGroups();
  staticObjects = collectStaticObjects();
  staticObjects.push({
    id: staticObjects.length + 1,
    name: `Точка ${staticObjects.length + 1}`,
    x: 0,
    y: 0,
    lat: null,
    lng: null,
  });
  renderStaticList();
}

/** Группы получателей MAX и выбранные зоны точек. */
function renderMaxGroups() {
  if (!els.maxList) return;
  const points = collectStaticObjects();
  const zoneChoices = (zones, attribute) => [["blue", "Синяя"], ["warning", "Жёлтая"], ["alert", "Красная"]]
    .map(([zone, label]) => `<label class="checkbox"><input type="checkbox" ${attribute}="${zone}" ${zones.includes(zone) ? "checked" : ""} />${label}</label>`).join("");
  els.maxList.innerHTML = maxGroups.map((group, index) => {
    const used = new Set((group.rules || []).map(rule => rule.static_index));
    return `
    <div class="max-recipient" data-group="${index}">
      <div class="static-block__head">
        <label>MAX user ID (через пробел или запятую)
          <input class="max-user-ids" type="text" value="${escapeAttr(Array.isArray(group.user_ids) ? group.user_ids.join(", ") : String(group.user_ids || ""))}" placeholder="123456, 789012" />
        </label>
        <button type="button" class="btn btn-remove" data-max-action="remove-group" data-group-index="${index}">×</button>
      </div>
      <label class="checkbox"><input class="max-all" type="checkbox" ${group.all_objects ? "checked" : ""} />Все объекты</label>
      <div class="max-all-zones" ${group.all_objects ? "" : "hidden"}>Зоны для всех объектов: ${zoneChoices(group.zones || [], "data-all-zone")}</div>
      <div class="max-rules" ${group.all_objects ? "hidden" : ""}>
        ${(group.rules || []).map((rule, ruleIndex) => `
          <div class="max-rule" data-rule="${ruleIndex}">
            <select class="max-object-select" aria-label="Стационарный объект">
              ${rule.static_index ? "" : '<option value="" selected disabled>Выберите объект</option>'}
              ${points.filter(point => point.id === rule.static_index || !used.has(point.id))
                .map(point => `<option value="${point.id}" ${point.id === rule.static_index ? "selected" : ""}>${escapeHtml(point.name)}</option>`).join("")}
            </select>
            ${zoneChoices(rule.zones || [], "data-zone")}
            <button type="button" class="btn btn-remove" data-max-action="remove-object" data-group-index="${index}" data-rule-index="${ruleIndex}" aria-label="Удалить объект">×</button>
          </div>`).join("")}
        <button type="button" class="btn" data-max-action="add-object" data-group-index="${index}" ${used.size >= points.length || used.has(0) ? "disabled" : ""}>+ Объект</button>
      </div>
    </div>
  `;
  }).join("") || `<p class="muted">Группы получателей не добавлены.</p>`;
}

function collectMaxGroups() {
  if (!els.maxList) return [];
  return [...els.maxList.querySelectorAll(".max-recipient")].map(card => ({
    user_ids: card.querySelector(".max-user-ids").value.trim(),
    all_objects: card.querySelector(".max-all").checked,
    zones: [...card.querySelectorAll("[data-all-zone]:checked")].map(input => input.dataset.allZone),
    rules: [...card.querySelectorAll(".max-rule")].map(row => ({
      static_index: Number(row.querySelector(".max-object-select").value),
      zones: [...row.querySelectorAll("[data-zone]:checked")].map(input => input.dataset.zone),
    })),
  }));
}

/** Читает и нормализует стационарные точки из полей формы. */
function collectStaticObjects() {
  const rows = els.staticList.querySelectorAll(".static-row");
  return [...rows].map((row, index) => ({
    id: index + 1,
    name: row.querySelector('[data-field="name"]').value.trim() || `Точка ${index + 1}`,
    x: parseFloat(row.querySelector('[data-field="x"]').value) || 0,
    y: parseFloat(row.querySelector('[data-field="y"]').value) || 0,
    lat: parseOptional(row.querySelector('[data-field="lat"]').value),
    lng: parseOptional(row.querySelector('[data-field="lng"]').value),
  }));
}

/** Назначает точкам последовательные идентификаторы после удаления. */
function renumberStatics() {
  staticObjects.forEach((item, index) => {
    item.id = index + 1;
  });
}

/** Валидирует форму, сохраняет настройки и закрывает диалог. */
async function saveSettings(event) {
  event.preventDefault();
  els.formError.hidden = true;
  const data = Object.fromEntries(new FormData(els.form).entries());
  data.interval_seconds = Number(data.interval_seconds);
  data.scan_wait_seconds = Number(data.scan_wait_seconds);
  data.screenshot_enabled = Boolean(els.form.elements.namedItem("screenshot_enabled")?.checked);
  data.screenshot_period_hours = Number(data.screenshot_period_hours);
  data.screenshot_width = Number(data.screenshot_width);
  data.screenshot_height = Number(data.screenshot_height);
  data.archive_html_enabled = Boolean(els.form.elements.namedItem("archive_html_enabled")?.checked);
  data.max_enabled = Boolean(els.form.elements.namedItem("max_enabled")?.checked);
  data.max_token_clear = Boolean(els.form.elements.namedItem("max_token_clear")?.checked);
  data.max_groups = collectMaxGroups();
  data.archive_html_period_hours = Number(data.archive_html_period_hours);
  data.threshold = Number(data.threshold);
  data.warning_distance = Number(data.warning_distance);
  data.blue_distance = Number(data.blue_distance);
  data.reference_zoom = Number(data.reference_zoom || 0);
  data.map_coordinate_precision = Number(data.map_coordinate_precision);
  data.lock_scale = Boolean(els.form.elements.namedItem("lock_scale")?.checked);
  data.wait_for_selector = data.dynamic_selector;
  
  const soundField = els.form.elements.namedItem("sound_enabled");
  if (soundField) {
    data.sound_enabled = soundField.checked;
    soundEnabled = data.sound_enabled;
  }
  
  data.static_objects = collectStaticObjects();
  try {
    const current = await get("/api/settings");
    const saved = await put("/api/settings", { ...current, ...data });
    staticObjects = saved.static_objects || [];
    applySettings(saved);
    els.dialog.close();
  } catch (err) {
    els.formError.hidden = false;
    els.formError.textContent = err.message;
  }
}

/** Устанавливает значение именованного элемента формы, если он существует. */
function setFormValue(name, value) {
  const field = els.form.elements.namedItem(name);
  if (field) field.value = value ?? "";
}

/** Показывает пользователю текст ошибки. */
function showError(message) {
  els.error.hidden = false;
  els.error.textContent = message;
}

/** Скрывает предыдущую ошибку. */
function hideError() {
  els.error.hidden = true;
  els.error.textContent = "";
}

/** Выполняет GET и разбирает успешный JSON-ответ. */
async function get(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** Отправляет JSON методом PUT и разбирает ответ. */
async function put(url, body) {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** Выполняет POST без тела и разбирает JSON-ответ. */
async function post(url) {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

/** Извлекает понятное сообщение из ошибки FastAPI. */
async function errorText(response) {
  try {
    const data = await response.json();
    if (Array.isArray(data.detail)) return data.detail.map((x) => x.msg || x).join("; ");
    return data.detail || JSON.stringify(data);
  } catch {
    return response.statusText;
  }
}

/** Экранирует значение перед вставкой в HTML-разметку. */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** Дополнительно экранирует одинарную кавычку для HTML-атрибутов. */
function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

/** Преобразует отсутствующее число в пустое значение поля формы. */
function num(value) {
  return value == null || value === "" ? "" : value;
}

/** Разбирает необязательное числовое поле или возвращает null. */
function parseOptional(value) {
  if (value === "" || value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Обновляет оценку суточного роста отдельной истории таблицы. */
function applyTableHistoryStats(stats) {
  if (!stats) return;
  els.historyRowsDay.textContent = formatCount(stats.estimated_rows_per_day || 0);
  els.historySizeDay.textContent = formatBytes(stats.estimated_bytes_per_day || 0);
  const details = `${stats.database} · ${formatCount(stats.snapshots_per_day || 0)} обновлений/сутки · сейчас ${formatBytes(stats.current_size_bytes || 0)}`;
  els.historyRowsDay.closest("article").title = details;
  els.historySizeDay.closest("article").title = details;
}

/** Форматирует целое количество по русской локали. */
function formatCount(value) {
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value) || 0);
}

/** Представляет число байтов в компактных двоичных единицах. */
function formatBytes(value) {
  let size = Number(value) || 0;
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits).replace(".", ",")} ${units[unit]}`;
}
