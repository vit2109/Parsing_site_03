/**
 * Константа для отображения статусов зон сближений на русском языке.
 * @typedef {Object} STATUS_LABEL
 * @property {string} alert - "красная зона" (опасное расстояние)
 * @property {string} warning - "жёлтая зона" (предупреждение)
 * @property {string} ok - "в радиусе" (безопасное расстояние)
 */
const STATUS_LABEL = { alert: "красная зона", warning: "жёлтая зона", blue: "синяя зона", ok: "в радиусе" };

/**
 * Объект DOM-элементов страницы для быстрого доступа.
 * @typedef {Object} els
 * @property {HTMLElement} form - форма загрузки истории
 * @property {HTMLSelectElement} source - выбор источника данных
 * @property {HTMLSelectElement} staticSelect - выбор стационарного объекта
 * @property {HTMLInputElement} dateFrom - дата начала периода
 * @property {HTMLInputElement} dateTo - дата конца периода
 * @property {HTMLElement} staticMeta - мета-информация о выбранном объекте
 * @property {HTMLElement} error - блок ошибок
 * @property {HTMLElement} notice - информационное сообщение
 * @property {HTMLElement} total - общее количество записей
 * @property {HTMLElement} unique - уникальные динамики
 * @property {HTMLElement} min - минимальное расстояние
 * @property {HTMLElement} legacy - записи из старой системы (px)
 * @property {HTMLElement} meta - мета-информация о результатах
 * @property {HTMLElement} body - таблица с записями сближений
 * @property {HTMLElement} summaryPanel - панель сводки объектов
 * @property {HTMLElement} summary - кнопки объектов в сводке
 * @property {HTMLElement} regenerateFake - кнопка пересоздания тестовых данных
 * @property {HTMLElement} exportHistory - кнопка экспорта в XLSX
 * @property {HTMLElement} overviewMap - обзорная карта стационарных точек
 * @property {HTMLElement} staticCount - количество стационарных объектов
 * @property {HTMLElement} trajectoryModal - модальное окно траектории динамического объекта
 * @property {HTMLElement} trajectoryTitle - заголовок траектории
 * @property {HTMLElement} trajectoryMeta - мета-информация о траектории
 * @property {HTMLElement} trajectoryPoints - список точек траектории
 * @property {HTMLElement} trajectoryMap - карта траектории динамического объекта
 * @property {HTMLElement} staticModal - модальное окно стационарного объекта
 * @property {HTMLElement} staticTitle - заголовок стационарного объекта
 * @property {HTMLElement} staticZoneMeta - мета-информация о зонах
 * @property {HTMLElement} staticTrajectories - список траекторий в зонах
 * @property {HTMLElement} staticZoneMap - карта зон и траекторий стационарного объекта
 */
/**
 * Массив объектов стационарных точек.
 * @type {Array<Object>}
 */
let staticObjects = [];

/**
 * Карта (инстанс) офлайн-карты для отображения динамической траектории.
 * @type {Object|null}
 */
let trajectoryMap;

/**
 * Карта (инстанс) офлайн-карты для отображения зон стационарного объекта.
 * @type {Object|null}
 */
let staticZoneMap;

/**
 * Кэшированный набор контуров регионов России из топологии карты.
 * @type {Array<Array<Array<[number, number]>>>|null}
 */
let russiaRegionsCache;

/** Текущее состояние слоёв подписей, общее для всех карт хронологии. */
const mapLabelVisibility = { regions: true, statics: true };


const els = {
  form: document.getElementById("history-form"),
  source: document.getElementById("history-source"),
  staticSelect: document.getElementById("history-static"),
  dateFrom: document.getElementById("history-from"),
  dateTo: document.getElementById("history-to"),
  staticMeta: document.getElementById("history-static-meta"),
  error: document.getElementById("history-error"),
  notice: document.getElementById("history-notice"),
  total: document.getElementById("history-total"),
  unique: document.getElementById("history-unique"),
  min: document.getElementById("history-min"),
  legacy: document.getElementById("history-legacy"),
  meta: document.getElementById("history-meta"),
  body: document.getElementById("history-body"),
  summaryPanel: document.getElementById("history-summary-panel"),
  summary: document.getElementById("history-summary"),
  regenerateFake: document.getElementById("btn-regenerate-fake"),
  exportHistory: document.getElementById("btn-export-history"),
  overviewMap: document.getElementById("history-overview-map"),
  staticCount: document.getElementById("history-static-count"),
  trajectoryModal: document.getElementById("trajectory-modal"),
  trajectoryTitle: document.getElementById("trajectory-title"),
  trajectoryMeta: document.getElementById("trajectory-meta"),
  trajectoryPoints: document.getElementById("trajectory-points"),
  trajectoryMap: document.getElementById("trajectory-map"),
  staticModal: document.getElementById("static-modal"),
  staticTitle: document.getElementById("static-title"),
  staticZoneMeta: document.getElementById("static-zone-meta"),
  staticTrajectories: document.getElementById("static-trajectories"),
  staticZoneMap: document.getElementById("static-zone-map"),
};





// Инициализация при загрузке страницы
setDefaultDates();
loadStaticObjects();

/**
 * Обработчик отправки формы загрузки истории.
 * @param {Event} event - событие submit
 */
els.form.addEventListener("submit", loadHistory);

/**
 * Обработчик изменения выбора стационарного объекта.
 * Показывает мета-информацию о выбранном объекте.
 * @param {Event} event - событие change
 */
els.staticSelect.addEventListener("change", showStaticMeta);

/**
 * Обработчик изменения источника данных.
 * Обновляет видимость кнопки пересоздания тестовых данных и загружает объекты.
 * @param {Event} event - событие change
 */
els.source.addEventListener("change", () => {
  els.regenerateFake.hidden = els.source.value !== "fake";
  loadStaticObjects();
});

/**
 * Обработчик клика по кнопке пересоздания тестовых данных.
 * @param {Event} event - событие click
 */
els.regenerateFake.addEventListener("click", regenerateFakeHistory);

/**
 * Обработчик клика по кнопке экспорта истории в XLSX.
 * @param {Event} event - событие click
 */
els.exportHistory.addEventListener("click", exportHistory);

/**
 * Закрытие модальных окон при клике на кнопку закрытия или фон диалога.
 */
document.querySelectorAll(".modal-close").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

/**
 * Загружает точки выбранного источника и обновляет обзорную карту.
 * @async
 * @function loadStaticObjects
 * @returns {Promise<void>}
 */
async function loadStaticObjects() {
  try {
    const source = encodeURIComponent(els.source.value);
    const [objects, settings] = await Promise.all([
      getJson(`/api/history/static-objects?source=${source}`),
      getJson("/api/settings"),
    ]);
    staticObjects = objects;
    document.documentElement.style.setProperty("--blue", settings.blue_color || "#2878b8");
    if (!staticObjects.length) {
      els.staticSelect.innerHTML = '<option value="">Нет стационарных объектов</option>';
      showError("Сначала добавьте стационарный объект в настройках мониторинга.");
      return;
    }
    els.staticSelect.innerHTML = staticObjects.map((item) => {
      const samples = Number(item.samples || 0);
      const kmSamples = Number(item.km_samples || 0);
      const suffix = samples
        ? ` · всего ${samples}, гео ${kmSamples}`
        : " · истории пока нет";
      return `<option value="${item.static_index}" data-name="${escapeAttr(item.static_name)}">${escapeHtml(item.static_name)} (#${item.static_index})${suffix}</option>`;
    }).join("");
    showStaticMeta();
    renderOverviewMap();
  } catch (error) {
    showError(error.message);
  }
}

/** Пересоздаёт демонстрационную историю и повторяет текущий запрос. */
async function regenerateFakeHistory() {
  hideError();
  els.regenerateFake.disabled = true;
  els.regenerateFake.textContent = "Создание…";
  try {
    await postJson("/api/history/fake/generate");
    await loadStaticObjects();
    els.form.requestSubmit();
  } catch (error) {
    showError(error.message);
  } finally {
    els.regenerateFake.disabled = false;
    els.regenerateFake.textContent = "Пересоздать тестовые данные";
  }
}

/** Загружает хронологию по фильтрам формы и передаёт её в отрисовку. */
async function loadHistory(event) {
  event.preventDefault();
  hideError();
  const button = els.form.querySelector("button[type='submit']");
  button.disabled = true;
  button.textContent = "Загрузка…";
  try {
    const params = new URLSearchParams(new FormData(els.form));
    const data = await getJson(`/api/history?${params}`);
    renderHistory(data);
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Показать";
  }
}

/** Скачивает XLSX для текущего набора фильтров. */
async function exportHistory() {
  hideError();
  if (!els.form.reportValidity()) return;
  const originalText = els.exportHistory.textContent;
  els.exportHistory.disabled = true;
  els.exportHistory.textContent = "Подготовка…";
  try {
    const params = new URLSearchParams(new FormData(els.form));
    const response = await fetch(`/api/history/export.xlsx?${params}`);
    if (!response.ok) {
      let message = response.statusText;
      try {
        const data = await response.json();
        message = data.detail || message;
      } catch (_) {}
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/i);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = match?.[1] || "chronology.xlsx";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  } catch (error) {
    showError(error.message);
  } finally {
    els.exportHistory.disabled = false;
    els.exportHistory.textContent = originalText;
  }
}

/** Отрисовывает итоги, карточки объектов и строки сближений. */
function renderHistory(data) {
  const rows = data.rows || [];
  const summary = data.summary || [];
  els.total.textContent = String(data.total || 0);
  els.unique.textContent = String(data.unique_dynamics || 0);
  els.min.textContent = rows.length
    ? `${Math.min(...rows.map((row) => Number(row.distance))).toFixed(3)} км`
    : "—";
  els.legacy.textContent = String(data.legacy_records || 0);
  if (data.notice) {
    els.notice.hidden = false;
    els.notice.textContent = `${data.notice} Используемая база: ${data.database}.`;
  } else {
    els.notice.hidden = true;
    els.notice.textContent = "";
  }
  const radius = Number(data.max_distance_km || 0).toFixed(3);
  els.meta.textContent = data.truncated
    ? `Показаны первые ${rows.length} записей в радиусе ${radius} км — уточните фильтры`
    : `Найдено записей: ${rows.length} · все расстояния ≤ ${radius} км`;

  els.summaryPanel.hidden = !summary.length;
  els.summary.innerHTML = summary.map((item) => `
    <button type="button" class="history-object-card history-status-${escapeAttr(item.status || "ok")}" data-dynamic-index="${item.dynamic_index}" data-object-date="${escapeAttr(item.object_date)}">
      <strong>${dynamicNumberLabel(item.dynamic_index)} ${escapeHtml(item.dynamic_name)}</strong>
      <span>${Number(item.min_distance_km).toFixed(3)} км</span>
      <small>${escapeHtml(item.object_date)} · ${item.events} ${pluralRecords(item.events)}</small>
    </button>
  `).join("");
  els.summary.querySelectorAll("[data-dynamic-index]").forEach((button) => {
    button.addEventListener("click", () => openDynamicTrajectory(Number(button.dataset.dynamicIndex), button.dataset.objectDate));
  });

  if (!rows.length) {
    els.body.innerHTML = '<tr class="empty"><td colspan="7">За выбранный период сближений не найдено</td></tr>';
    return;
  }
  els.body.innerHTML = rows.map((row) => `
    <tr class="${escapeAttr(row.status)}">
      <td>${escapeHtml(formatDateTime(row.ts))}</td>
      <td><button type="button" class="history-link" data-static-index="${row.static_index}">#${row.static_index} ${escapeHtml(row.static_name)}</button></td>
      <td>${formatCoords(row.static_lat, row.static_lng)}</td>
      <td><button type="button" class="history-link" data-dynamic-index="${row.dynamic_index}" data-object-date="${escapeAttr(row.object_date)}">${dynamicNumberLabel(row.dynamic_index)} ${escapeHtml(row.dynamic_name)}</button></td>
      <td>${formatCoords(row.dynamic_lat, row.dynamic_lng)}</td>
      <td><strong>${Number(row.distance).toFixed(3)} км</strong></td>
      <td>${STATUS_LABEL[row.status] || escapeHtml(row.status)}</td>
    </tr>
  `).join("");
  els.body.querySelectorAll("[data-dynamic-index]").forEach((button) => {
    button.addEventListener("click", () => openDynamicTrajectory(Number(button.dataset.dynamicIndex), button.dataset.objectDate));
  });
  els.body.querySelectorAll("[data-static-index]").forEach((button) => {
    button.addEventListener("click", () => openStaticZones(Number(button.dataset.staticIndex)));
  });
}

/** Показывает обзорное положение доступных стационарных точек. */
function renderOverviewMap() {
  els.staticCount.textContent = `${staticObjects.length} ${pluralObjects(staticObjects.length)}`;
  const valid = staticObjects.filter((item) => item.static_lat != null && item.static_lng != null);
  renderOfflineMap(els.overviewMap, {
    points: valid.map((item) => ({
      ...item,
      lat: item.static_lat,
      lng: item.static_lng,
      label: `${item.static_name} (#${item.static_index})`,
      markerClass: "offline-marker--static",
      staticIndex: item.static_index,
    })),
    emptyText: "Нет стационарных объектов с географическими координатами.",
  });
  els.overviewMap.querySelectorAll("[data-static-index]").forEach((marker) => {
    marker.addEventListener("click", () => openStaticZones(Number(marker.dataset.staticIndex)));
  });
}

/** Отличает временный ID маркера от суточного номера сайта. */
function dynamicNumberLabel(index) {
  const value = Number(index);
  return value < 0 ? `ID ${-value}` : `№${value}`;
}

/** Загружает и открывает диалог траектории динамического объекта. */
async function openDynamicTrajectory(dynamicIndex, objectDate) {
  hideError();
  els.trajectoryTitle.textContent = `Объект ${dynamicNumberLabel(dynamicIndex)} · ${objectDate}`;
  els.trajectoryMeta.textContent = "Загрузка точек…";
  els.trajectoryPoints.innerHTML = '<p class="muted">Загрузка…</p>';
  els.trajectoryModal.showModal();
  try {
    const params = periodParams();
    params.set("object_date", objectDate);
    const data = await getJson(`/api/history/trajectory/${dynamicIndex}?${params}`);
    els.trajectoryTitle.textContent = `${dynamicNumberLabel(data.dynamic_index)} ${data.dynamic_name} · ${data.object_date}`;
    els.trajectoryMeta.textContent = `${data.total} ${pluralPoints(data.total)} за сутки${data.truncated ? ` · показаны первые ${data.total} точек` : ""}`;
    if (!els.trajectoryModal.open) return;
    renderDynamicMap(data);
  } catch (error) {
    els.trajectoryModal.close();
    showError(error.message);
  }
}

/** Готовит слои офлайн-карты для одной динамической траектории. */
function renderDynamicMap(data) {
  const color = safeColor(data.color);
  trajectoryMap = renderOfflineMap(els.trajectoryMap, {
    points: data.points.map((point, index) => ({
      ...point,
      key: String(index),
      label: `${index + 1}. ${formatDateTime(point.ts)}`,
      color,
    })),
    polylines: [{ id: `${data.object_date}:${data.dynamic_index}`, label: `${dynamicNumberLabel(data.dynamic_index)} · ${data.object_date}`, color, points: data.points }],
  });
  els.trajectoryPoints.innerHTML = data.points.map((point, index) => `
    <button type="button" class="history-point" data-point-index="${index}">
      <strong>${index + 1}. ${escapeHtml(formatDateTime(point.ts))}</strong>
      <span>${escapeHtml(coordsText(point.lat, point.lng))}</span>
    </button>
  `).join("");
  els.trajectoryPoints.querySelectorAll("[data-point-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.pointIndex);
      els.trajectoryPoints.querySelectorAll(".selected").forEach((item) => item.classList.remove("selected"));
      button.classList.add("selected");
      const point = data.points[index];
      trajectoryMap.selectPoint(point, `${formatDateTime(point.ts)} · ${coordsText(point.lat, point.lng)}`, color);
    });
  });
}

/** Загружает зоны точки и траектории входивших в них объектов. */
async function openStaticZones(staticIndex) {
  hideError();
  els.staticTitle.textContent = `Объект #${staticIndex}`;
  els.staticZoneMeta.textContent = "Загрузка зон и траекторий…";
  els.staticTrajectories.innerHTML = '<p class="muted">Загрузка…</p>';
  els.staticModal.showModal();
  try {
    const data = await getJson(`/api/history/static-zones/${staticIndex}?${periodParams()}`);
    els.staticTitle.textContent = `#${data.static_index} ${data.static_name}`;
    els.staticZoneMeta.textContent = `Красная зона: ${formatKm(data.alert_radius_km)} · жёлтая зона: ${formatKm(data.warning_radius_km)} · синяя зона: ${formatKm(data.blue_radius_km)} · объектов: ${data.objects_total}${data.truncated ? ` · показаны первые ${data.points_total} точек; уточните период для остальных` : ""}`;
    if (!els.staticModal.open) return;
    renderStaticZoneMap(data);
  } catch (error) {
    els.staticModal.close();
    showError(error.message);
  }
}

/** Готовит карту радиусов и связанных траекторий для статичной точки. */
function renderStaticZoneMap(data) {
  const allPoints = data.trajectories.flatMap((trajectory) => trajectory.points);
  const blueColor = safeColor(data.blue_color || "#2878b8");
  staticZoneMap = renderOfflineMap(els.staticZoneMap, {
    points: allPoints.map((point) => ({
      ...point,
      color: point.status === "alert" ? "#ff6b6b" : point.status === "warning" ? "#f1c232" : point.status === "blue" ? blueColor : "#8794a8",
      markerClass: point.status === "outside" ? "offline-marker--dim" : "",
    })),
    polylines: data.trajectories.map((trajectory) => ({
      id: trajectory.trajectory_key,
      label: `${dynamicNumberLabel(trajectory.dynamic_index)} · ${trajectory.object_date}`,
      color: safeColor(trajectory.color),
      points: trajectory.points,
    })),
    zones: [
      { center: { lat: data.lat, lng: data.lng }, radiusKm: Number(data.blue_radius_km), status: "blue" },
      { center: { lat: data.lat, lng: data.lng }, radiusKm: Number(data.warning_radius_km), status: "warning" },
      { center: { lat: data.lat, lng: data.lng }, radiusKm: Number(data.alert_radius_km), status: "alert" },
    ],
    centerPoint: { lat: data.lat, lng: data.lng, label: `#${data.static_index} ${data.static_name}` },
  });
  els.staticTrajectories.innerHTML = data.trajectories.length ? data.trajectories.map((trajectory, trajectoryIndex) => `
    <section class="history-trajectory-group" data-trajectory-group="${escapeAttr(trajectory.trajectory_key)}">
      <button type="button" class="history-point history-trajectory-toggle" data-zone-trajectory="${trajectoryIndex}" aria-expanded="false">
        <strong><i class="trajectory-swatch" style="background:${safeColor(trajectory.color)}"></i>${dynamicNumberLabel(trajectory.dynamic_index)} ${escapeHtml(trajectory.dynamic_name)}</strong>
        <span>${escapeHtml(trajectory.object_date)} · ${trajectory.points.length} ${pluralPoints(trajectory.points.length)} · ${trajectory.entered_alert ? "входил в красную зону" : trajectory.entered_warning ? "входил в жёлтую зону" : "входил в синюю зону"}</span>
        <i class="trajectory-chevron" aria-hidden="true">⌄</i>
      </button>
      <div class="history-trajectory-points" hidden>
        ${trajectory.points.map((point, index) => `
          <button type="button" class="history-point history-point--nested" data-zone-point="${trajectoryIndex}:${index}">
            <strong><i class="zone-status-dot zone-status-${escapeAttr(point.status)}"></i>${index + 1}. ${escapeHtml(formatDateTime(point.ts))}</strong>
            <span>${escapeHtml(coordsText(point.lat, point.lng))} · ${Number(point.distance).toFixed(3)} км · ${zoneStatusLabel(point.status)}</span>
          </button>
        `).join("")}
      </div>
    </section>
  `).join("") : '<p class="muted history-empty-detail">За выбранный период никто не входил в зоны.</p>';
  els.staticTrajectories.querySelectorAll("[data-zone-trajectory]").forEach((button) => {
    button.addEventListener("click", () => {
      const group = button.closest(".history-trajectory-group");
      const points = group.querySelector(".history-trajectory-points");
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!expanded));
      points.hidden = expanded;
      const trajectory = data.trajectories[Number(button.dataset.zoneTrajectory)];
      staticZoneMap.highlightTrajectory(trajectory.trajectory_key);
    });
  });
  els.staticTrajectories.querySelectorAll("[data-zone-point]").forEach((button) => {
    button.addEventListener("click", () => {
      const [trajectoryIndex, pointIndex] = button.dataset.zonePoint.split(":").map(Number);
      const trajectory = data.trajectories[trajectoryIndex];
      const point = trajectory?.points[pointIndex];
      if (!point) return;
      els.staticTrajectories.querySelectorAll("[data-zone-point].selected").forEach((item) => item.classList.remove("selected"));
      button.classList.add("selected");
      staticZoneMap.highlightTrajectory(trajectory.trajectory_key);
      staticZoneMap.selectPoint(
        point,
        `${trajectory.dynamic_name} · ${formatDateTime(point.ts)} · ${Number(point.distance).toFixed(3)} км`,
        safeColor(trajectory.color),
      );
    });
  });
}

/** Рисует автономную SVG-карту России, зоны, линии и интерактивную подсказку. */
function renderOfflineMap(container, options = {}) {
  const width = 1000;
  const height = 620;
  const padding = 52;
  const minZoom = 0.35;
  const points = options.points || [];
  const polylines = options.polylines || [];
  const zones = (options.zones || []).filter((zone) => Number.isFinite(zone.radiusKm));
  const coordinates = [
    ...points,
    ...polylines.flatMap((line) => line.points || []),
    ...(options.centerPoint ? [options.centerPoint] : []),
    ...zones.map((zone) => zone.center),
  ].filter((point) => Number.isFinite(Number(point.lat)) && Number.isFinite(Number(point.lng)));
  if (!coordinates.length) {
    container.innerHTML = `<p class="map-unavailable">${escapeHtml(options.emptyText || "Нет географических данных для отображения.")}</p>`;
    return { selectPoint() {}, highlightTrajectory() {} };
  }

  const referenceLat = coordinates.reduce((sum, point) => sum + Number(point.lat), 0) / coordinates.length;
  const longitudeScale = 111.32 * Math.max(0.15, Math.cos(referenceLat * Math.PI / 180));
  const toWorld = (point) => ({ x: Number(point.lng) * longitudeScale, y: Number(point.lat) * 111.32 });
  const worldPoints = coordinates.map(toWorld);
  zones.forEach((zone) => {
    const center = toWorld(zone.center);
    worldPoints.push(
      { x: center.x - zone.radiusKm, y: center.y - zone.radiusKm },
      { x: center.x + zone.radiusKm, y: center.y + zone.radiusKm },
    );
  });
  let minX = Math.min(...worldPoints.map((point) => point.x));
  let maxX = Math.max(...worldPoints.map((point) => point.x));
  let minY = Math.min(...worldPoints.map((point) => point.y));
  let maxY = Math.max(...worldPoints.map((point) => point.y));
  const minimumSpan = Math.max(2, ...zones.map((zone) => zone.radiusKm * 2.3));
  if (maxX - minX < minimumSpan) {
    const extra = (minimumSpan - (maxX - minX)) / 2;
    minX -= extra;
    maxX += extra;
  }
  if (maxY - minY < minimumSpan) {
    const extra = (minimumSpan - (maxY - minY)) / 2;
    minY -= extra;
    maxY += extra;
  }
  const innerWidth = width - padding * 2;
  const innerHeight = height - padding * 2;
  const targetRatio = innerWidth / innerHeight;
  const currentRatio = (maxX - minX) / (maxY - minY);
  if (currentRatio > targetRatio) {
    const desiredHeight = (maxX - minX) / targetRatio;
    const extra = (desiredHeight - (maxY - minY)) / 2;
    minY -= extra;
    maxY += extra;
  } else {
    const desiredWidth = (maxY - minY) * targetRatio;
    const extra = (desiredWidth - (maxX - minX)) / 2;
    minX -= extra;
    maxX += extra;
  }
  const scale = innerWidth / (maxX - minX);
  const project = (point) => {
    const world = toWorld(point);
    return {
      x: padding + (world.x - minX) * scale,
      y: height - padding - (world.y - minY) * scale,
    };
  };

  const grid = Array.from({ length: 6 }, (_, index) => {
    const fraction = index / 5;
    const x = padding + innerWidth * fraction;
    const y = padding + innerHeight * fraction;
    const lng = (minX + (maxX - minX) * fraction) / longitudeScale;
    const lat = (maxY - (maxY - minY) * fraction) / 111.32;
    return `<path class="offline-grid-line" d="M ${x} ${padding} V ${height - padding} M ${padding} ${y} H ${width - padding}" />
      <text class="offline-grid-label" x="${x}" y="${height - 18}" text-anchor="middle">${lng.toFixed(2)}°</text>
      <text class="offline-grid-label" x="12" y="${y + 4}">${lat.toFixed(2)}°</text>`;
  }).join("");
  const mapExpansion = (1 / minZoom - 1) / 2;
  const russiaMap = buildRussiaMapMarkup(project, toWorld, {
    minX: minX - (maxX - minX) * mapExpansion,
    maxX: maxX + (maxX - minX) * mapExpansion,
    minY: minY - (maxY - minY) * mapExpansion,
    maxY: maxY + (maxY - minY) * mapExpansion,
  });
  const zoneMarkup = zones.map((zone) => {
    const center = project(zone.center);
    const radius = Math.max(1, zone.radiusKm * scale);
    return `<circle class="offline-zone offline-zone--${escapeAttr(zone.status)}" cx="${center.x}" cy="${center.y}" r="${radius}" stroke-width="${radius*0}"/>`;
  }).join("");
  const lineMarkup = polylines.map((line) => {
    const path = (line.points || []).map((point) => {
      const projected = project(point);
      return `${projected.x},${projected.y}`;
    }).join(" ");
    const routeId = String(line.id);
    return `<polyline class="offline-route" data-route-id="${escapeAttr(routeId)}" points="${path}" style="--route-color:${safeColor(line.color)}"><title>Траектория ${escapeHtml(line.label || routeId)}</title></polyline>`;
  }).join("");
  const pointMarkup = points.map((point, index) => {
    const projected = project(point);
    const staticIndex = point.staticIndex == null ? "" : ` data-static-index="${Number(point.staticIndex)}"`;
    const markerClass = escapeAttr(point.markerClass || "");
    const color = safeColor(point.color || "#3d8bfd");
    const markerRadius = point.staticIndex != null
      ? 7
      : (point.markerClass || "").includes("offline-marker--dim") ? 2.2 : 3.2;
    return `<circle class="offline-marker ${markerClass}" data-map-point="${index}" data-marker-radius="${markerRadius}"${staticIndex} cx="${projected.x}" cy="${projected.y}" r="${markerRadius}" style="--marker-color:${color}"><title>${escapeHtml(point.label || coordsText(point.lat, point.lng))}</title></circle>`;
  }).join("");
  const staticLabelMarkup = points.map((point) => {
    if (point.staticIndex == null) return "";
    const projected = project(point);
    return mapLabelMarkup("offline-static-label", projected, point.label || `Объект #${point.staticIndex}`, "start");
  }).join("");
  let centerMarkup = "";
  let centerLabelMarkup = "";
  if (options.centerPoint) {
    const center = project(options.centerPoint);
    centerMarkup = `<g class="offline-center-marker" data-map-x="${center.x}" data-map-y="${center.y}" transform="translate(${center.x} ${center.y})"><circle r="11"></circle><path d="M -4 -1 H 4 M 0 -5 V 3"></path><title>${escapeHtml(options.centerPoint.label || "Стационарный объект")}</title></g>`;
    centerLabelMarkup = mapLabelMarkup("offline-static-label offline-static-label--center", center, options.centerPoint.label || "Стационарный объект", "start");
  }
  // <div class="offline-map-badge">Офлайн-карта России · регионы</div>
  // <div class="offline-map-context">ЛНР, ДНР, Херсонская и Запорожская области показаны по административной модели РФ</div>
  container.innerHTML = `
    <svg class="offline-map-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Офлайн-карта России с объектами">
      <rect class="offline-map-background" x="${-width * mapExpansion}" y="${-height * mapExpansion}" width="${width / minZoom}" height="${height / minZoom}" rx="16"></rect>
      <g class="offline-russia-map">${russiaMap.regions}</g>
      <g>${grid}</g>
      <g>${zoneMarkup}</g>
      <g class="offline-map-label-layer offline-map-label-layer--regions${mapLabelVisibility.regions ? "" : " is-hidden"}">${russiaMap.labels}</g>
      <g>${lineMarkup}</g>
      <g>${pointMarkup}</g>
      ${centerMarkup}
      <g class="offline-map-label-layer offline-map-label-layer--statics${mapLabelVisibility.statics ? "" : " is-hidden"}">${staticLabelMarkup}${centerLabelMarkup}</g>
      <g class="offline-selected-point" hidden><circle class="offline-selected-halo" r="15"></circle><circle class="offline-selected-core" r="6"></circle></g>
      <g class="offline-north" transform="translate(${width - 34} 42)"><text text-anchor="middle">С</text><path d="M 0 8 L -7 23 L 0 19 L 7 23 Z"></path></g>
    </svg>
    <div class="offline-map-zoom" role="group" aria-label="Масштаб карты">
      <button type="button" data-map-zoom="in" title="Приблизить" aria-label="Приблизить карту">+</button>
      <button type="button" data-map-zoom="out" title="Отдалить" aria-label="Отдалить карту">−</button>
      <button class="offline-map-zoom__reset" type="button" data-map-zoom="reset" title="Сбросить масштаб">100%</button>
    </div>
    <div class="offline-map-layers" role="group" aria-label="Подписи на карте">
      <label><input type="checkbox" data-map-label-toggle="regions"${mapLabelVisibility.regions ? " checked" : ""}${russiaMap.labels ? "" : " disabled"}> Субъекты</label>
      <label><input type="checkbox" data-map-label-toggle="statics"${mapLabelVisibility.statics ? " checked" : ""}${staticLabelMarkup || centerLabelMarkup ? "" : " disabled"}> Стационарные объекты</label>
    </div>
    <div class="offline-map-selection" hidden></div>
  `;
  const svg = container.querySelector(".offline-map-svg");
  const selected = container.querySelector(".offline-selected-point");
  const centerMarker = container.querySelector(".offline-center-marker");
  const markers = [...container.querySelectorAll(".offline-marker")];
  const mapLabels = [...container.querySelectorAll(".offline-map-label")];
  const selection = container.querySelector(".offline-map-selection");
  const zoomIn = container.querySelector('[data-map-zoom="in"]');
  const zoomOut = container.querySelector('[data-map-zoom="out"]');
  const zoomReset = container.querySelector('[data-map-zoom="reset"]');
  const maxZoom = 8;
  const zoomStep = 1.45;
  const view = { x: 0, y: 0, width, height, zoom: 1 };
  let selectedPosition = null;
  let drag = null;
  let suppressNextClick = false;

  const applyView = () => {
    const inverseZoom = 1 / view.zoom;
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.width} ${view.height}`);
    markers.forEach((marker) => {
      const radius = marker.dataset.markerHover === "true"
        ? 10
        : Number(marker.dataset.markerRadius);
      marker.setAttribute("r", String(radius * inverseZoom));
      const strokeWidth = marker.classList.contains("offline-marker--static") ? 2.5 : 0.2;
      marker.style.strokeWidth = `${strokeWidth * inverseZoom}px`;
    });
    mapLabels.forEach((label) => {
      label.setAttribute(
        "transform",
        `translate(${label.dataset.mapX} ${label.dataset.mapY}) scale(${inverseZoom})`,
      );
    });
    if (centerMarker) {
      centerMarker.setAttribute(
        "transform",
        `translate(${centerMarker.dataset.mapX} ${centerMarker.dataset.mapY}) scale(${inverseZoom})`,
      );
      centerMarker.querySelector("circle").style.strokeWidth = `${3 * inverseZoom}px`;
      centerMarker.querySelector("path").style.strokeWidth = `${2.5 * inverseZoom}px`;
    }
    if (selectedPosition) {
      selected.setAttribute(
        "transform",
        `translate(${selectedPosition.x} ${selectedPosition.y}) scale(${inverseZoom})`,
      );
      selected.querySelector(".offline-selected-halo").style.strokeWidth = `${5 * inverseZoom}px`;
      selected.querySelector(".offline-selected-core").style.strokeWidth = `${2 * inverseZoom}px`;
    }
    svg.classList.toggle("is-zoomed", Math.abs(view.zoom - 1) > 0.001);
    zoomReset.textContent = `${Math.round(view.zoom * 100)}%`;
    zoomIn.disabled = view.zoom >= maxZoom - 0.001;
    zoomOut.disabled = view.zoom <= minZoom + 0.001;
  };
  const changeZoom = (requestedZoom, anchorX = 0.5, anchorY = 0.5) => {
    const nextZoom = Math.max(minZoom, Math.min(maxZoom, requestedZoom));
    if (Math.abs(nextZoom - view.zoom) < 0.001) return;
    const worldAnchorX = view.x + view.width * anchorX;
    const worldAnchorY = view.y + view.height * anchorY;
    const nextWidth = width / nextZoom;
    const nextHeight = height / nextZoom;
    view.x = clampMapOrigin(worldAnchorX - nextWidth * anchorX, width, nextWidth);
    view.y = clampMapOrigin(worldAnchorY - nextHeight * anchorY, height, nextHeight);
    view.width = nextWidth;
    view.height = nextHeight;
    view.zoom = nextZoom;
    applyView();
  };
  const resetZoom = () => {
    Object.assign(view, { x: 0, y: 0, width, height, zoom: 1 });
    applyView();
  };

  zoomIn.addEventListener("click", () => changeZoom(view.zoom * zoomStep));
  zoomOut.addEventListener("click", () => changeZoom(view.zoom / zoomStep));
  zoomReset.addEventListener("click", resetZoom);
  container.querySelectorAll("[data-map-label-toggle]").forEach((toggle) => {
    toggle.addEventListener("change", () => setMapLabelVisibility(toggle.dataset.mapLabelToggle, toggle.checked));
  });
  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = svg.getBoundingClientRect();
    const anchorX = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const anchorY = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    changeZoom(view.zoom * (event.deltaY < 0 ? zoomStep : 1 / zoomStep), anchorX, anchorY);
  }, { passive: false });
  svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || Math.abs(view.zoom - 1) < 0.001) return;
    // Маркер остаётся обычной кликабельной целью при любом масштабе.
    // Захват указателя на SVG раньше подменял цель click после zoom > 100%.
    if (event.target.closest("[data-static-index]")) return;
    drag = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      viewX: view.x,
      viewY: view.y,
      moved: false,
    };
    svg.setPointerCapture(event.pointerId);
    svg.classList.add("is-panning");
  });
  svg.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const rect = svg.getBoundingClientRect();
    const deltaX = event.clientX - drag.clientX;
    const deltaY = event.clientY - drag.clientY;
    if (Math.hypot(deltaX, deltaY) >= 4) drag.moved = true;
    view.x = clampMapOrigin(drag.viewX - deltaX * view.width / rect.width, width, view.width);
    view.y = clampMapOrigin(drag.viewY - deltaY * view.height / rect.height, height, view.height);
    applyView();
  });
  const finishDrag = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    suppressNextClick = drag.moved;
    drag = null;
    svg.classList.remove("is-panning");
    if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    if (suppressNextClick) setTimeout(() => { suppressNextClick = false; }, 0);
  };
  svg.addEventListener("pointerup", finishDrag);
  svg.addEventListener("pointercancel", finishDrag);
  svg.addEventListener("click", (event) => {
    if (!suppressNextClick) return;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  markers.filter((marker) => marker.classList.contains("offline-marker--static")).forEach((marker) => {
    marker.addEventListener("pointerenter", () => {
      marker.dataset.markerHover = "true";
      marker.setAttribute("r", String(8 / view.zoom));
      marker.setAttribute("--marker-color", "#ff0000");
    });
    marker.addEventListener("pointerleave", () => {
      marker.dataset.markerHover = "false";
      marker.setAttribute("r", String(Number(marker.dataset.markerRadius) / view.zoom));
    });
  });
  applyView();
  return {
    selectPoint(point, label, color = "#3d8bfd") {
      const projected = project(point);
      selectedPosition = projected;
      selected.hidden = false;
      selected.setAttribute(
        "transform",
        `translate(${projected.x} ${projected.y}) scale(${1 / view.zoom})`,
      );
      selected.querySelector(".offline-selected-halo").style.strokeWidth = `${5 / view.zoom}px`;
      selected.querySelector(".offline-selected-core").style.strokeWidth = `${2 / view.zoom}px`;
      selected.style.setProperty("--selected-color", safeColor(color));
      selection.hidden = false;
      selection.textContent = label;
    },
    highlightTrajectory(id) {
      container.querySelectorAll("[data-route-id]").forEach((route) => {
        route.classList.toggle("selected", route.dataset.routeId === String(id));
      });
    },
  };
}

/** Ограничивает панорамирование как при приближении, так и при масштабе ниже 100%. */
function clampMapOrigin(value, baseSize, viewSize) {
  const minimum = Math.min(0, baseSize - viewSize);
  const maximum = Math.max(0, baseSize - viewSize);
  return Math.max(minimum, Math.min(maximum, value));
}

/** Формирует SVG-подпись, сохраняющую размер при масштабировании карты. */
function mapLabelMarkup(className, point, label, anchor = "middle") {
  const offset = anchor === "start" ? 11 : 0;
  return `<g class="offline-map-label ${className}" data-map-x="${point.x}" data-map-y="${point.y}" transform="translate(${point.x} ${point.y})"><text x="${offset}" text-anchor="${anchor}">${escapeHtml(label)}</text><title>${escapeHtml(label)}</title></g>`;
}

/** Переключает выбранный слой подписей сразу на всех открытых картах. */
function setMapLabelVisibility(layer, visible) {
  if (!(layer in mapLabelVisibility)) return;
  mapLabelVisibility[layer] = visible;
  document.querySelectorAll(`.offline-map-label-layer--${layer}`).forEach((group) => {
    group.classList.toggle("is-hidden", !visible);
  });
  document.querySelectorAll(`[data-map-label-toggle="${layer}"]`).forEach((toggle) => {
    toggle.checked = visible;
  });
}

/** Проецирует контуры и подписи регионов для SVG-карты. */
function buildRussiaMapMarkup(project, toWorld, bounds) {
  const regions = getRussiaRegions();
  if (!regions.length) return { regions: "", labels: "" };
  const marginX = (bounds.maxX - bounds.minX) * 0.08;
  const marginY = (bounds.maxY - bounds.minY) * 0.08;
  const polygonPath = (polygon) => {
    const world = polygon.flat().map(([lng, lat]) => toWorld({ lat, lng }));
    if (!world.length) return "";
    const polygonBounds = {
      minX: Math.min(...world.map((point) => point.x)),
      maxX: Math.max(...world.map((point) => point.x)),
      minY: Math.min(...world.map((point) => point.y)),
      maxY: Math.max(...world.map((point) => point.y)),
    };
    if (
      polygonBounds.maxX < bounds.minX - marginX
      || polygonBounds.minX > bounds.maxX + marginX
      || polygonBounds.maxY < bounds.minY - marginY
      || polygonBounds.minY > bounds.maxY + marginY
    ) return "";
    return polygon.map((ring) => ring.map(([lng, lat], index) => {
      const point = project({ lat, lng });
      return `${index ? "L" : "M"} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
    }).join(" ") + " Z").join(" ");
  };
  const visibleRegions = [];
  const baseMarkup = regions.map((region, regionIndex) => {
    const paths = region.polygons.map((polygon) => {
      return polygonPath(polygon);
    }).filter(Boolean).join(" ");
    if (paths) visibleRegions.push(region);
    return paths
      ? `<path class="offline-russia-region region-tone-${regionIndex % 4}" d="${paths}" fill-rule="evenodd"><title>${escapeHtml(region.name)}</title></path>`
      : "";
  }).join("");
  const additionalRegions = [];
  const additionalMarkup = getAdditionalRussiaRegions().map((region) => {
    const paths = region.polygons.map(polygonPath).filter(Boolean).join(" ");
    if (!paths) return "";
    additionalRegions.push(region);
    return `<path class="offline-russia-region offline-russia-region--added" d="${paths}" fill-rule="evenodd"><title>${escapeHtml(region.name)}</title></path>`;
  }).join("");
  const labels = [...visibleRegions, ...additionalRegions].map((region) => {
    const labelPoint = regionLabelPoint(region.polygons);
    if (!labelPoint) return "";
    const world = toWorld(labelPoint);
    if (world.x < bounds.minX - marginX || world.x > bounds.maxX + marginX || world.y < bounds.minY - marginY || world.y > bounds.maxY + marginY) return "";
    return mapLabelMarkup("offline-region-label", project(labelPoint), region.name);
  }).join("");
  return { regions: baseMarkup + additionalMarkup, labels };
}

/** Находит устойчивую точку подписи по центроиду крупнейшего внешнего контура. */
function regionLabelPoint(polygons) {
  const rings = polygons.map((polygon) => polygon[0]).filter((ring) => Array.isArray(ring) && ring.length > 2);
  if (!rings.length) return null;
  const area = (ring) => Math.abs(ring.reduce((sum, point, index) => {
    const next = ring[(index + 1) % ring.length];
    return sum + point[0] * next[1] - next[0] * point[1];
  }, 0) / 2);
  const ring = rings.reduce((largest, candidate) => area(candidate) > area(largest) ? candidate : largest);
  let twiceArea = 0;
  let lng = 0;
  let lat = 0;
  ring.forEach((point, index) => {
    const next = ring[(index + 1) % ring.length];
    const cross = point[0] * next[1] - next[0] * point[1];
    twiceArea += cross;
    lng += (point[0] + next[0]) * cross;
    lat += (point[1] + next[1]) * cross;
  });
  if (Math.abs(twiceArea) < 1e-8) {
    return { lng: ring.reduce((sum, point) => sum + point[0], 0) / ring.length, lat: ring.reduce((sum, point) => sum + point[1], 0) / ring.length };
  }
  return { lng: lng / (3 * twiceArea), lat: lat / (3 * twiceArea) };
}

/** Возвращает основной встроенный набор контуров регионов России. */
function getRussiaRegions() {
  if (russiaRegionsCache) return russiaRegionsCache;
  const topology = window.RUSSIA_MAP_TOPOLOGY;
  const geometries = topology?.objects?.collection?.geometries;
  if (!topology || !Array.isArray(geometries)) return [];
  const scale = topology.transform?.scale || [1, 1];
  const translate = topology.transform?.translate || [0, 0];
  const decodedArcs = topology.arcs.map((arc) => {
    let x = 0;
    let y = 0;
    return arc.map(([dx, dy]) => {
      x += dx;
      y += dy;
      return [x * scale[0] + translate[0], y * scale[1] + translate[1]];
    });
  });
  const joinArcs = (arcIndexes) => {
    const ring = [];
    arcIndexes.forEach((arcIndex) => {
      const source = decodedArcs[arcIndex < 0 ? ~arcIndex : arcIndex] || [];
      const arc = arcIndex < 0 ? [...source].reverse() : source;
      ring.push(...(ring.length ? arc.slice(1) : arc));
    });
    return ring;
  };
  const decodePolygon = (rings) => rings.map(joinArcs).filter((ring) => ring.length > 2);
  const names = window.RUSSIA_REGION_NAMES || [];
  russiaRegionsCache = geometries.map((geometry, index) => {
    let polygons = [];
    if (geometry.type === "Polygon") polygons = [decodePolygon(geometry.arcs)];
    if (geometry.type === "MultiPolygon") polygons = geometry.arcs.map(decodePolygon);
    return { name: names[index] || `Субъект ${index + 1}`, polygons };
  }).filter((region) => region.polygons.length);
  return russiaRegionsCache;
}

/** Возвращает дополнительный встроенный набор региональных контуров. */
function getAdditionalRussiaRegions() {
  const features = window.RUSSIA_ADDITIONAL_REGIONS?.features || [];
  return features.map((feature) => {
    const geometry = feature.geometry || {};
    let polygons = [];
    if (geometry.type === "Polygon") polygons = [geometry.coordinates];
    if (geometry.type === "MultiPolygon") polygons = geometry.coordinates;
    return { name: feature.properties?.name || "Регион", polygons };
  }).filter((region) => region.polygons.length);
}

/** Преобразует внутренний статус зоны в русскую подпись. */
function zoneStatusLabel(status) {
  if (status === "alert") return "красная зона";
  if (status === "warning") return "жёлтая зона";
  if (status === "blue") return "синяя зона";
  return "вне зон";
}

/** Собирает общие параметры периода и источника для запросов траекторий. */
function periodParams() {
  return new URLSearchParams({
    date_from: els.dateFrom.value,
    date_to: els.dateTo.value,
    source: els.source.value,
  });
}

/** Показывает координаты и доступность географической истории выбранной точки. */
function showStaticMeta() {
  const selected = staticObjects.find(
    (item) => String(item.static_index) === els.staticSelect.value
  );
  if (!selected) {
    els.staticMeta.textContent = "";
    return;
  }
  const coords = coordsText(selected.static_lat, selected.static_lng);
  const kmSamples = Number(selected.km_samples || 0);
  const legacySamples = Number(selected.legacy_samples || 0);
  const range = selected.first_seen && selected.last_seen
    ? ` · история: ${formatDateTime(selected.first_seen)} — ${formatDateTime(selected.last_seen)}`
    : " · накопленных данных пока нет";
  const compatibility = ` · географических записей: ${kmSamples}, старых в px: ${legacySamples}`;
  els.staticMeta.textContent = `Географические координаты: ${coords}${range}${compatibility}`;
  if (selected.first_seen && selected.last_seen) {
    els.dateFrom.value = selected.first_seen.slice(0, 10);
    els.dateTo.value = selected.last_seen.slice(0, 10);
  }
}

/** Устанавливает период по умолчанию: последние семь суток. */
function setDefaultDates() {
  const now = new Date();
  const weekAgo = new Date(now);
  weekAgo.setDate(now.getDate() - 7);
  els.dateTo.value = localDate(now);
  els.dateFrom.value = localDate(weekAgo);
}

/** Форматирует локальную календарную дату как YYYY-MM-DD. */
function localDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Форматирует ISO-время для русской локали. */
function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ru-RU");
}

/** Возвращает HTML-представление пары географических координат. */
function formatCoords(lat, lng) {
  return `<span class="geo-coords">${escapeHtml(coordsText(lat, lng))}</span>`;
}

/** Возвращает текстовое представление пары координат или тире. */
function coordsText(lat, lng) {
  if (lat == null || lng == null) return "нет данных";
  return `${Number(lat).toFixed(6)}°, ${Number(lng).toFixed(6)}°`;
}

/** Выбирает русскую форму слова «запись». */
function pluralRecords(count) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "запись";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "записи";
  return "записей";
}

/** Выбирает русскую форму слова «точка». */
function pluralPoints(count) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "точка";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "точки";
  return "точек";
}

/** Выбирает русскую форму слова «объект». */
function pluralObjects(count) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "объект";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "объекта";
  return "объектов";
}

/** Форматирует радиус в километрах или сообщает об отсутствии значения. */
function formatKm(value) {
  return value == null ? "не задана" : `${Number(value).toFixed(3)} км`;
}

/** Пропускает безопасный цвет #RRGGBB или возвращает стандартный синий. */
function safeColor(value) {
  const color = String(value || "");
  return /^#[0-9a-f]{6}$/i.test(color) ? color : "#3d8bfd";
}

/** Выполняет GET и превращает ошибку FastAPI в исключение JavaScript. */
async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = Array.isArray(data.detail)
        ? data.detail.map((item) => item.msg || item).join("; ")
        : data.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

/** Выполняет POST без тела и разбирает JSON-ответ. */
async function postJson(url) {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

/** Показывает сообщение об ошибке страницы истории. */
function showError(message) {
  els.error.hidden = false;
  els.error.textContent = message;
}

/** Скрывает сообщение об ошибке страницы истории. */
function hideError() {
  els.error.hidden = true;
  els.error.textContent = "";
}

/** Экранирует данные перед вставкой в HTML-разметку. */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** Экранирует значение для безопасной вставки в HTML-атрибут. */
function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}
