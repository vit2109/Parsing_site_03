/** Рисует карту справки тем же компонентом, что использует хронология. */
window.renderReportMap = function renderReportMap(kind, payload) {
  let container = document.getElementById("report-map-capture");
  if (!container) {
    container = document.createElement("div");
    container.id = "report-map-capture";
    container.className = "history-map history-detail-map";
    container.style.width = "1200px";
    container.style.minHeight = "700px";
    container.style.margin = "0";
    document.body.appendChild(container);
  }
  const palette = ["#155e9a", "#8b3c88", "#17816c", "#aa5b21", "#5860b0", "#b04a63"];
  const zoneColor = { alert: "#ff6b6b", warning: "#f1c232", blue: "#2878b8" };
  const number = (index) => Number(index) < 0 ? `ID ${-Number(index)}` : `№${index}`;
  let options = {};

  if (kind === "static") {
    const scene = payload.scene;
    const points = scene.points || [];
    const groups = new Map();
    for (const point of points) {
      const key = `${point.object_date}:${point.dynamic_index}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(point);
    }
    options = {
      points: points.map((point) => ({
        ...point, label: `${number(point.dynamic_index)} ${point.dynamic_name} · ${point.ts}`,
        color: zoneColor[point.status] || "#8794a8",
      })),
      polylines: [...groups.entries()].map(([key, route], index) => ({
        id: key, label: number(route[0].dynamic_index),
        color: palette[index % palette.length], points: route,
      })),
      zones: [
        { center: scene, radiusKm: Number(scene.blue_radius_km), status: "blue" },
        { center: scene, radiusKm: Number(scene.warning_radius_km), status: "warning" },
        { center: scene, radiusKm: Number(scene.alert_radius_km), status: "alert" },
      ],
      centerPoint: { lat: scene.lat, lng: scene.lng,
        label: `#${scene.static_index} ${scene.static_name}` },
    };
  } else if (kind === "dynamic") {
    const points = payload.points || [];
    const groups = new Map();
    for (const point of points) {
      const key = String(point.object_date);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(point);
    }
    options = {
      points: [
        ...points.map((point) => ({ ...point,
          label: `${number(point.dynamic_index)} ${point.dynamic_name} · ${point.ts}`,
          color: "#236aab",
        })),
        ...(payload.statics || []).map((item) => ({
          lat: item.static_lat, lng: item.static_lng,
          staticIndex: item.static_index,
          label: `#${item.static_index} ${item.static_name}`,
          color: "#172c42",
        })),
      ],
      polylines: [...groups.entries()].map(([day, route]) => ({
        id: day, label: `${number(route[0].dynamic_index)} · ${day}`,
        color: "#12629c", points: route,
      })),
    };
  } else {
    const rows = payload.rows || [];
    const statics = new Map();
    for (const row of rows) {
      if (row.static_lat != null && row.static_lng != null) {
        statics.set(row.static_index, {
          lat: row.static_lat, lng: row.static_lng,
          staticIndex: row.static_index,
          label: `#${row.static_index} ${row.static_name}`,
          color: "#172c42",
        });
      }
    }
    options = {
      points: [
        ...rows.filter((row) => row.dynamic_lat != null && row.dynamic_lng != null)
          .map((row) => ({
            lat: row.dynamic_lat, lng: row.dynamic_lng,
            label: `${number(row.dynamic_index)} ${row.dynamic_name} · ${row.ts}`,
            color: zoneColor[row.status] || "#8794a8",
          })),
        ...statics.values(),
      ],
    };
  }
  renderHistoryMap(container, options);
  const credit = container.querySelector(".history-map-credit");
  if (credit) credit.textContent = "© OpenStreetMap contributors";
  return Boolean(container.querySelector(".history-map-surface"));
};
