(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const fields = {
    count: $("sim-count"), area: $("sim-area"), distribution: $("sim-distribution"),
    mode: $("sim-mode"), radius: $("sim-radius"), reveal: $("sim-reveal"),
    delay: $("sim-delay"), seed: $("sim-seed"),
    redKm: $("sim-red-km"), yellowKm: $("sim-yellow-km"), blueKm: $("sim-blue-km"),
  };
  const colors = { alert: "#ef5a5a", warning: "#e2b535", blue: "#3c9be0", ok: "#a1a9b4" };
  const labels = { alert: "красная", warning: "жёлтая", blue: "синяя", ok: "вне зон" };
  fields.mode.value = window.__SIM_DEFAULT_MODE__ || "adaptive";

  const map = L.map("sim-map", { preferCanvas: true, minZoom: 3, maxZoom: 18 })
    .setView([55.75, 37.62], 5);
  const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  tiles.on("tileerror", () => { $("sim-tile-note").hidden = false; });
  const zoneLayer = L.layerGroup().addTo(map);
  const scanZoneLayer = L.layerGroup().addTo(map);
  const staticLayer = L.layerGroup().addTo(map);
  const objectLayer = L.layerGroup().addTo(map);
  const labelLayer = L.layerGroup().addTo(map);
  const routeLayer = L.layerGroup().addTo(map);
  let scenario = null;
  let home = null;
  let stepIndex = 0;
  let transitions = 0;
  let known = new Set();
  let timer = null;
  let running = false;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function stopAuto() {
    if (timer) clearTimeout(timer);
    timer = null;
    running = false;
    $("sim-auto").textContent = "Автопроход";
  }

  function updateLabels() {
    labelLayer.clearLayers();
    if (!scenario) return;
    const zoom = map.getZoom();
    const bounds = map.getBounds();
    let visible = 0;
    for (const item of scenario.objects) {
      if (zoom < item.reveal_zoom || !bounds.contains([item.lat, item.lng])) continue;
      visible += 1;
      known.add(item.id);
      L.marker([item.lat, item.lng], {
        interactive: false, keyboard: false,
        icon: L.divIcon({ className: "", html: `<span class="sim-label">№${item.id}</span>`,
                          iconSize: [55, 18], iconAnchor: [-7, 25] }),
      }).addTo(labelLayer);
    }
    $("sim-progress").dataset.visible = String(visible);
    $("sim-found").textContent = String(known.size);
    $("sim-missing").textContent = String(scenario.objects.length - known.size);
  }
  map.on("zoomend moveend", updateLabels);

  function renderMap() {
    zoneLayer.clearLayers(); scanZoneLayer.clearLayers(); staticLayer.clearLayers();
    objectLayer.clearLayers(); labelLayer.clearLayers();
    if (!scenario) return;
    const t = scenario.thresholds;
    const all = [];
    for (const point of scenario.statics) {
      const center = [point.lat, point.lng];
      if (t.blue > t.warning) L.circle(center, { radius: t.blue * 1000,
        color: colors.blue, fillColor: colors.blue, fillOpacity: .035, weight: 1 }).addTo(zoneLayer);
      if (t.warning > t.alert) L.circle(center, { radius: t.warning * 1000,
        color: colors.warning, fillColor: colors.warning, fillOpacity: .045, weight: 1 }).addTo(zoneLayer);
      L.circle(center, { radius: t.alert * 1000,
        color: colors.alert, fillColor: colors.alert, fillOpacity: .065, weight: 1 }).addTo(zoneLayer);
      L.circleMarker(center, { radius: 7, weight: 2, color: "#145842", fillColor: "#e5fff4",
        fillOpacity: 1 }).bindTooltip(escapeHtml(point.name)).addTo(staticLayer);
      all.push(center);
    }
    for (const item of scenario.objects) {
      const center = [item.lat, item.lng];
      const marker = L.circleMarker(center, { radius: 5, weight: 1.5, color: "#f4f7fc",
        fillColor: colors[item.zone], fillOpacity: .85 }).addTo(objectLayer);
      marker.on("click", () => {
        const number = map.getZoom() >= item.reveal_zoom ? `№${item.id}` : "номер пока скрыт";
        marker.bindPopup(`<strong>${number}</strong><br>${escapeHtml(item.region)}<br>`
          + `Ближайшая точка: ${item.nearest_km} км · ${labels[item.zone]} зона<br>`
          + `Подпись появляется с zoom ${item.reveal_zoom}`).openPopup();
      });
      all.push(center);
    }
    map.fitBounds(L.latLngBounds(all), { padding: [30, 30], maxZoom: 7, animate: false });
    home = { center: map.getCenter(), zoom: map.getZoom() };
    updateLabels();
  }

  function route() { return scenario?.routes[fields.mode.value] || []; }

  function renderRoute() {
    routeLayer.clearLayers();
    scanZoneLayer.clearLayers();
    const list = $("sim-route");
    list.replaceChildren();
    if (!scenario) return;
    const steps = route();
    if (fields.mode.value === "zones") {
      for (const zone of scenario.zones) {
        L.circle([zone.lat, zone.lng], { radius: zone.radius_km * 1000,
          color: "#a881ff", weight: 2, dashArray: "6,6", fillOpacity: 0 })
          .bindTooltip(escapeHtml(zone.name)).addTo(scanZoneLayer);
      }
    }
    $("sim-route-count").textContent = fields.mode.value === "full_coverage"
      ? `${steps.length} целей · без лимита переходов`
      : `${steps.length} переходов · предел ${scenario.route_limit}`;
    if (steps.length > 1) L.polyline(steps.map(step => [step.lat, step.lng]), {
      color: "#8cb6f4", weight: 2, dashArray: "5,6", opacity: .85,
    }).addTo(routeLayer);
    steps.forEach((step, index) => {
      if (fields.mode.value !== "full_coverage" || index < 100) {
        L.circleMarker([step.lat, step.lng], { radius: 7, weight: 2,
          color: "#ffffff", fillColor: "#3d84ed", fillOpacity: 1 })
          .bindTooltip(`${index + 1}. ${escapeHtml(step.title)}`)
          .on("click", () => goTo(index)).addTo(routeLayer);
      }
      const li = document.createElement("li");
      li.dataset.step = String(index);
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${step.title} · ${step.object_ids.length} рядом`;
      button.addEventListener("click", () => goTo(index));
      li.append(button); list.append(li);
    });
    $("sim-progress").textContent = steps.length
      ? `Обзор · шаг 0 из ${steps.length} · карта вернётся к исходному виду`
      : "Для этого режима нет опорных точек";
  }

  function markRoute() {
    const steps = route();
    $("sim-route").querySelectorAll("li").forEach((li, index) => {
      li.classList.toggle("is-current", index === stepIndex - 1);
      li.classList.toggle("is-done", fields.mode.value === "full_coverage"
        ? known.has(steps[index].target_id) : index < stepIndex - 1);
    });
  }

  function goTo(index) {
    if (!scenario) return;
    const step = route()[index];
    if (!step) return;
    stepIndex = index + 1;
    transitions += 1;
    map.setView([step.lat, step.lng], step.zoom, { animate: false });
    updateLabels(); markRoute();
    $("sim-progress").textContent = fields.mode.value === "full_coverage"
      ? `Переход ${transitions} · найдено ${known.size} из ${scenario.objects.length} · ${step.title} · zoom ${step.zoom}`
      : `Шаг ${stepIndex} из ${route().length}: ${step.title} · zoom ${step.zoom}`;
  }

  function nextStep() {
    if (!scenario) return;
    const steps = route();
    const next = fields.mode.value === "full_coverage"
      ? steps.findIndex(step => !known.has(step.target_id)) : stepIndex;
    if (next < 0 || next >= steps.length) {
      stopAuto();
      if (home) map.setView(home.center, home.zoom, { animate: false });
      $("sim-progress").textContent = `Обход завершён · переходов ${transitions} · найдено ${known.size} из ${scenario.objects.length} номеров · без номера ${scenario.objects.length - known.size} · исходный вид восстановлен`;
      return;
    }
    goTo(next);
    if (fields.mode.value === "full_coverage" && known.size === scenario.objects.length) nextStep();
  }

  function scheduleStep() {
    if (!running) return;
    nextStep();
    if (!running) return;
    timer = setTimeout(scheduleStep, Math.max(.2, Number(fields.delay.value) || 1.5) * 1000);
  }

  function resetView() {
    stopAuto(); stepIndex = 0; transitions = 0; known = new Set();
    $("sim-found").textContent = "0";
    if (scenario) $("sim-missing").textContent = String(scenario.objects.length);
    if (home) map.setView(home.center, home.zoom, { animate: false });
    renderRoute();
    updateLabels();
    markRoute();
  }

  function renderMessages() {
    const container = $("sim-messages");
    container.replaceChildren();
    $("sim-preview-source").textContent = scenario.preview_source
      + ". Снимок в тренажёре не создаётся.";
    if (!scenario.preview.length) {
      const p = document.createElement("p");
      p.className = "sim-note";
      p.textContent = scenario.preview_message;
      container.append(p);
      return;
    }
    for (const item of scenario.preview) {
      const card = document.createElement("article");
      card.className = "sim-message";
      const heading = document.createElement("h3");
      heading.textContent = item.user_id ? `Получатель ${item.user_id} · пример` : "Демонстрационное правило · пример";
      const content = document.createElement("pre");
      content.textContent = item.text;
      card.append(heading, content);
      container.append(card);
    }
  }

  async function generate() {
    stopAuto();
    for (const field of [fields.count, fields.radius, fields.reveal, fields.seed,
                         fields.redKm, fields.yellowKm, fields.blueKm]) {
      if (!field.checkValidity()) { field.reportValidity(); return; }
    }
    if (!(Number(fields.redKm.value) <= Number(fields.yellowKm.value)
          && Number(fields.yellowKm.value) <= Number(fields.blueKm.value))) {
      $("sim-error").textContent = "Пороги должны возрастать: красная ≤ жёлтая ≤ синяя.";
      $("sim-error").hidden = false;
      return;
    }
    const button = $("sim-generate");
    button.disabled = true;
    $("sim-error").hidden = true;
    try {
      const response = await fetch("/api/simulator/generate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: Number(fields.count.value), area: fields.area.value,
          distribution: fields.distribution.value, seed: Number(fields.seed.value),
          reveal_zoom: Number(fields.reveal.value), group_radius: Number(fields.radius.value),
          red_km: Number(fields.redKm.value), yellow_km: Number(fields.yellowKm.value),
          blue_km: Number(fields.blueKm.value) }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
      scenario = await response.json();
      $("sim-total").textContent = String(scenario.objects.length);
      $("sim-red").textContent = String(scenario.zone_counts.alert);
      $("sim-yellow").textContent = String(scenario.zone_counts.warning);
      $("sim-blue").textContent = String(scenario.zone_counts.blue);
      $("sim-outside").textContent = String(scenario.zone_counts.ok);
      $("sim-source").textContent = `Основа данных: ${scenario.source}. Пороги: красная ${scenario.thresholds.alert} км, `
        + `жёлтая ${scenario.thresholds.warning} км, синяя ${scenario.thresholds.blue} км. `
        + `Максимальный zoom обычных режимов: ${scenario.scan_max_zoom}.`;
      renderMap(); renderMessages(); resetView();
      $("sim-step").disabled = false;
      $("sim-auto").disabled = false;
      $("sim-reset").disabled = false;
    } catch (error) {
      $("sim-error").textContent = `Не удалось создать сценарий: ${error.message}`;
      $("sim-error").hidden = false;
    } finally { button.disabled = false; }
  }

  $("sim-generate").addEventListener("click", generate);
  $("sim-step").addEventListener("click", nextStep);
  $("sim-auto").addEventListener("click", () => {
    if (running) { stopAuto(); return; }
    running = true;
    $("sim-auto").textContent = "Пауза";
    scheduleStep();
  });
  $("sim-reset").addEventListener("click", resetView);
  fields.mode.addEventListener("change", resetView);
  generate();
})();
