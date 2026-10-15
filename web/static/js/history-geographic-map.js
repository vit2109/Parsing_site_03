/** Географическая подложка хронологии с теми же точками, зонами и маршрутами. */
function renderGeographicMap(container, options = {}) {
  const valid = (point) => point && point.lat != null && point.lng != null
    && Number.isFinite(Number(point.lat)) && Math.abs(Number(point.lat)) <= 90
    && Number.isFinite(Number(point.lng)) && Math.abs(Number(point.lng)) <= 180;
  const latlng = (point) => [Number(point.lat), Number(point.lng)];
  const text = (value) => {
    const element = document.createElement("span");
    element.textContent = String(value || "");
    return element;
  };
  const map = L.map(container, { preferCanvas: true, minZoom: 2, maxZoom: 19 })
    .setView([61, 95], 3);
  const bounds = L.latLngBounds([]);
  const routes = new Map();
  const tileLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  });
  const message = document.createElement("p");
  message.className = "geographic-map-message";
  message.hidden = true;
  message.textContent = "Подложка не загрузилась. Проверьте интернет или выберите «Схема».";
  container.appendChild(message);
  tileLayer.on("tileerror", () => { message.hidden = false; });
  tileLayer.on("tileload", (event) => {
    if (event.tile.naturalWidth > 0) message.hidden = true;
  });

  for (const zone of options.zones || []) {
    if (!valid(zone.center) || !Number.isFinite(zone.radiusKm) || zone.radiusKm <= 0) continue;
    const color = { alert: "#ff6b6b", warning: "#f1c232", blue: "#2878b8" }[zone.status] || "#2878b8";
    const circle = L.circle(latlng(zone.center), {
      radius: zone.radiusKm * 1000, color, weight: 1.5, fillColor: color,
      fillOpacity: 0.12, interactive: false,
    }).addTo(map);
    bounds.extend(circle.getBounds());
  }
  for (const line of options.polylines || []) {
    const coordinates = (line.points || []).filter(valid).map(latlng);
    if (!coordinates.length) continue;
    const route = L.polyline(coordinates, {
      color: safeColor(line.color), weight: 3, opacity: 0.75,
    }).addTo(map);
    route.bindTooltip(text(line.label || line.id));
    routes.set(String(line.id), route);
    coordinates.forEach((point) => bounds.extend(point));
  }
  for (const point of options.points || []) {
    if (!valid(point)) continue;
    const isStatic = point.staticIndex != null;
    const marker = L.circleMarker(latlng(point), {
      radius: isStatic ? 7 : 3.2, weight: isStatic ? 2 : 1,
      color: isStatic ? "#fff" : safeColor(point.color),
      fillColor: safeColor(point.color || "#3d8bfd"), fillOpacity: 0.9,
    }).addTo(map);
    marker.bindTooltip(text(point.label || coordsText(point.lat, point.lng)));
    if (isStatic && options.onStaticSelect) {
      marker.on("click", () => options.onStaticSelect(Number(point.staticIndex)));
    }
    bounds.extend(latlng(point));
  }
  if (valid(options.centerPoint)) {
    L.circleMarker(latlng(options.centerPoint), {
      radius: 10, color: "#fff", weight: 3, fillColor: "#172c42", fillOpacity: 1,
    }).bindTooltip(text(options.centerPoint.label)).addTo(map);
    bounds.extend(latlng(options.centerPoint));
  }
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [35, 35], maxZoom: 13 });
  else map.setView([61, 95], 3);
  tileLayer.addTo(map);
  const resize = new ResizeObserver(() => map.invalidateSize({ pan: false }));
  resize.observe(container);
  let selected;
  return {
    selectPoint(point, label, color = "#3d8bfd") {
      if (!valid(point)) return;
      if (selected) selected.remove();
      selected = L.circleMarker(latlng(point), {
        radius: 9, color: "#fff", weight: 3, fillColor: safeColor(color), fillOpacity: 1,
      }).addTo(map).bindPopup(text(label)).openPopup();
      map.panTo(latlng(point));
    },
    highlightTrajectory(id) {
      routes.forEach((route, routeId) => {
        const active = routeId === String(id);
        route.setStyle({ weight: active ? 6 : 3, opacity: active ? 1 : 0.4 });
        if (active) route.bringToFront();
      });
    },
    destroy() {
      resize.disconnect();
      map.remove();
    },
  };
}
