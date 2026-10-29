"""
Съём сцены с карты и таблица расстояний.

Живая страница: JS в Playwright читает маркеры Leaflet и переводит их в lat/lng.
Снимок HTML: те же поля собирает html_probe без браузера.

Каждая строка таблицы — пара «статичная точка ↔ динамический объект».
После расчёта дистанций для каждой точки заполняются alert_count / warning_count:
сколько объектов красные и жёлтые относительно этой точки.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

from parsing_site.config import StaticObjectConfig, TrackingConfig
from parsing_site.coordinates import (
    CoordinateMode,
    MapView,
    Point2D,
    container_to_latlng,
    distance_between,
    distance_unit,
    haversine_km,
    latlng_to_container,
    map_view_from_raw,
    normalize_mode,
    project_3857,
    static_to_point,
)
from parsing_site.phone_loader import get_phone_for_static

BLUE_ZONE_KM = 350.0

Status = Literal["ok", "blue", "warning", "alert"]

_EXTRACT_SCRIPT = """
/* Скрипт выполняется в контексте вкладки Chrome (Playwright page.evaluate).
   Ищет экземпляр L.Map; если его нет — lat/lng из transform .leaflet-proxy (EPSG:3857).
   Пиксели x/y зависят от текущего зума; lat/lng при пересчёте вида карты сохраняются. */
async (opts) => {
  const mode = opts.mode || "screen";
  const mapSelector = opts.mapContainer || ".leaflet-container";
  const round2 = (v) => Math.round(v * 100) / 100;
  const round6 = (v) => Math.round(v * 1e6) / 1e6;

  const mapEl = mapSelector ? document.querySelector(mapSelector) : null;
  const mapRect = mapEl ? mapEl.getBoundingClientRect() : null;

  const isMap = (v) => {
    try {
      return v && typeof v === "object" &&
        typeof v.getZoom === "function" &&
        (typeof v.containerPointToLatLng === "function" ||
         typeof v.latLngToContainerPoint === "function");
    } catch (e) { return false; }
  };

  const searchMap = (root) => {
    const seen = new WeakSet();
    const queue = [[root, 0]];
    let visited = 0;
    while (queue.length && visited < 4000) {
      const [obj, depth] = queue.shift();
      if (!obj || typeof obj !== "object" || seen.has(obj)) continue;
      if (isMap(obj)) return obj;
      seen.add(obj);
      visited++;
      if (depth >= 6 || obj.nodeType || obj === window || obj === document) continue;
      const keys = new Set([
        "value", "map", "leafletMap", "leafletObject", "mapObject", "_map",
        "setupState", "ctx", "exposed", "data", "proxy", "provides", "refs",
      ]);
      try { Object.keys(obj).forEach((key) => keys.add(key)); } catch (e) {}
      for (const key of keys) {
        if (["parent", "appContext", "vnode", "subTree", "$el", "_container"].includes(key)) continue;
        try {
          const value = obj[key];
          if (isMap(value)) return value;
          if (value && typeof value === "object" && !seen.has(value)) queue.push([value, depth + 1]);
        } catch (e) {}
      }
    }
    return null;
  };

  const findLeafletMap = (el) => {
    if (!el) return null;
    for (let node = el, depth = 0; node && depth < 25; node = node.parentElement, depth++) {
      for (const key of ["_leaflet_map", "__leaflet_map", "_leaflet", "leafletMap"]) {
        try { if (isMap(node[key])) return node[key]; } catch (e) {}
      }
      try {
        for (const key of Object.getOwnPropertyNames(node)) {
          if (isMap(node[key])) return node[key];
        }
      } catch (e) {}
      let vue = node.__vueParentComponent || node.__vue__ || node.__vue_app__?._instance;
      for (let i = 0; i < 30 && vue; i++) {
        const found = searchMap(vue);
        if (found) return found;
        vue = vue.parent || vue.$?.parent;
      }
    }
    return null;
  };

  const parseTransform = (style) => {
    const text = style || "";
    const m = text.match(/translate3d\\(([-\\d.]+)px,\\s*([-\\d.]+)px/);
    if (!m) return null;
    const s = text.match(/scale\\(([-\\d.]+)\\)/);
    return {
      x: parseFloat(m[1]),
      y: parseFloat(m[2]),
      scale: s ? parseFloat(s[1]) : 1,
    };
  };

  const unproject3857 = (x, y, zoom) => {
    const scale = 256 * Math.pow(2, zoom);
    const lng = x / scale * 360 - 180;
    const yNorm = y / scale;
    const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * yNorm))) * 180 / Math.PI;
    return { lat, lng };
  };

  const project3857 = (lat, lng, zoom) => {
    const scale = 256 * Math.pow(2, zoom);
    const x = (lng + 180) / 360 * scale;
    const clamped = Math.max(Math.min(lat, 85.0511287798), -85.0511287798);
    const siny = Math.sin(clamped * Math.PI / 180);
    const y = (0.5 - Math.log((1 + siny) / (1 - siny)) / (4 * Math.PI)) * scale;
    return { x, y };
  };

  const inferZoom = (proxy) => {
    if (!proxy || !(proxy.scale > 0)) return null;
    const logS = Math.log2(proxy.scale);
    const cands = [
      { zoom: logS, world: 256 * proxy.scale },
      { zoom: logS + 1, world: 256 * proxy.scale * 2 },
    ];
    const ok = (c) => {
      const ll = unproject3857(proxy.x, proxy.y, c.zoom);
      return Math.abs(ll.lat) <= 85.0511287798 && Math.abs(ll.lng) <= 180;
    };
    const inside = cands.find((c) =>
      proxy.x >= 0 && proxy.x <= c.world && proxy.y >= 0 && proxy.y <= c.world && ok(c)
    );
    if (inside) return inside.zoom;
    const valid = cands.find(ok);
    return valid ? valid.zoom : null;
  };

  const map = findLeafletMap(mapEl);
  window.__parsingSiteMap = map || null;
  const proxyEl = mapEl && mapEl.querySelector(".leaflet-proxy");
  const proxy = proxyEl ? parseTransform(proxyEl.getAttribute("style") || "") : null;
  const inferredZoom = inferZoom(proxy);
  const zoom = map && typeof map.getZoom === "function"
    ? map.getZoom()
    : inferredZoom;
  let geoSource = "none";
  if (map && map.containerPointToLatLng) geoSource = "leaflet";
  else if (proxy && inferredZoom != null && mapRect) geoSource = "proxy3857";

  const containerToLatLng = (cx, cy) => {
    if (map && map.containerPointToLatLng) {
      const ll = map.containerPointToLatLng([cx, cy]);
      if (ll && Number.isFinite(ll.lat) && Number.isFinite(ll.lng)) {
        return { lat: ll.lat, lng: ll.lng };
      }
    }
    if (geoSource === "proxy3857" && proxy && inferredZoom != null && mapRect) {
      return unproject3857(
        cx + proxy.x - mapRect.width / 2,
        cy + proxy.y - mapRect.height / 2,
        inferredZoom
      );
    }
    return null;
  };

  const latLngToContainer = (lat, lng) => {
    if (map && map.latLngToContainerPoint) {
      const p = map.latLngToContainerPoint([lat, lng]);
      if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) {
        return { x: p.x, y: p.y };
      }
    }
    if (proxy && inferredZoom != null && mapRect) {
      const p = project3857(lat, lng, inferredZoom);
      return {
        x: p.x - proxy.x + mapRect.width / 2,
        y: p.y - proxy.y + mapRect.height / 2,
      };
    }
    return null;
  };

  let center = null;
  if (map && typeof map.getCenter === "function") {
    const c = map.getCenter();
    if (c) center = { lat: c.lat, lng: c.lng };
  } else if (proxy && inferredZoom != null) {
    center = unproject3857(proxy.x, proxy.y, inferredZoom);
  }

  const xyForMode = (cx, cy) => {
    let x = cx;
    let y = cy;
    if ((mode === "map_relative" || mode === "latlng") && mapRect) {
      x = cx - mapRect.left;
      y = cy - mapRect.top;
    } else if (mode === "percent" && mapRect && mapRect.width && mapRect.height) {
      x = ((cx - mapRect.left) / mapRect.width) * 100;
      y = ((cy - mapRect.top) / mapRect.height) * 100;
    }
    return { x, y };
  };

  const centerOf = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const xy = xyForMode(cx, cy);
    let lat = null;
    let lng = null;
    const relX = mapRect ? cx - mapRect.left : xy.x;
    const relY = mapRect ? cy - mapRect.top : xy.y;
    const ll = containerToLatLng(relX, relY);
    if (ll) {
      lat = round6(ll.lat);
      lng = round6(ll.lng);
    }
    const style = el.getAttribute("style") || "";
    const translate = parseTransform(style);
    return {
      x: round2(xy.x),
      y: round2(xy.y),
      lat,
      lng,
      width: Math.round(r.width * 10) / 10,
      height: Math.round(r.height * 10) / 10,
      translate3d: translate ? { x: translate.x, y: translate.y } : null,
    };
  };

  const pickName = (el, index, prefix) => {
    const alt = el.querySelector("img")?.getAttribute("alt");
    const text = (alt || el.dataset.label || el.dataset.name || el.id || "").trim();
    if (text) return text;
    const cls = [...el.classList].find((c) => c.startsWith("map-incident_"));
    if (cls) return cls.replace("map-incident_", "");
    return `${prefix}${index + 1}`;
  };

  // На целевой странице внутренний id инцидента меняется, а видимый номер
  // сопровождаемого объекта (например 5, 4, 3) является его суточным номером.
  // Именно он используется как стабильная идентичность в таблице и истории.
  const objectTypeFrom = (value) => {
    const text = String(value || "");
    if (/БПЛА|aircraft-drone/i.test(text)) return "БПЛА";
    if (/МВШ|mvsh|balloon|aerostat/i.test(text)) return "МВШ";
    if (/ракет|missile|rocket/i.test(text)) return "Ракета";
    const markerClass = text.match(/map-incident_([a-z][\\w-]*)/i);
    if (markerClass && markerClass[1] !== "new") return markerClass[1].replace(/-/g, " ");
    return "Объект";
  };

  // В новых версиях страницы суточный номер рисуется отдельным Leaflet-маркером.
  // Подписей может быть две на объект, поэтому связываем номер с инцидентом по
  // одинаковому translate3d, а не по порядку элементов в DOM.
  const operatorNumbers = () => {
    const labels = [...document.querySelectorAll(".leaflet-marker-icon.map-incident-operator-number")]
    .map((el) => {
      const match = String(el.textContent || "").trim().match(/^№?\\s*(\\d+)$/);
      const transform = parseTransform(el.getAttribute("style") || "");
      // Начальная подпись центрирована (-9px), текущая поднята над значком (-19px).
      const marginTop = Number.parseFloat(el.style?.marginTop ?? "");
      return match && transform
        ? { number: Number(match[1]), x: transform.x, y: transform.y,
            current: Number.isFinite(marginTop) && marginTop <= -15,
            origin: Number.isFinite(marginTop) && marginTop > -15 }
        : null;
    })
    .filter((item) => item && Number.isInteger(item.number) && item.number > 0 &&
      !item.origin);
    // Подпись начальной точки траектории не является номером текущего маркера.
    // Подписи без margin-top тоже допустимы (например, в сохранённом HTML).
    const byPosition = new Map();
    labels.forEach((item) => {
      const key = `${Math.floor(item.x / 2)}:${Math.floor(item.y / 2)}`;
      if (!byPosition.has(key)) byPosition.set(key, []);
      byPosition.get(key).push(item);
    });
    return byPosition;
  };

  const numberAtMarker = (box, numbers) => {
    if (!box.translate3d || !numbers.size) return null;
    let nearest = null;
    const cellX = Math.floor(box.translate3d.x / 2);
    const cellY = Math.floor(box.translate3d.y / 2);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (const item of numbers.get(`${cellX + dx}:${cellY + dy}`) || []) {
          const distance = (item.x - box.translate3d.x) ** 2 + (item.y - box.translate3d.y) ** 2;
          if (distance <= 4 && (!nearest || distance < nearest.distance ||
              (distance === nearest.distance && item.current && !nearest.current))) {
            nearest = { ...item, distance };
          }
        }
      }
    }
    return nearest?.number ?? null;
  };

  // На малом масштабе сайт скрывает подписи, но сами маркеры остаются.
  // Отрицательная область ID не пересекается с положительными суточными номерами.
  const hashIdentity = (value) => {
    let hash = 5381;
    for (const char of String(value)) hash = ((hash * 33) ^ char.charCodeAt(0)) >>> 0;
    return -(hash % 1000000000 + 1);
  };
  const fallbackIndex = (el, box, objectType, occurrences) => {
    const layer = map?._layers && Object.values(map._layers).find((item) => item?._icon === el);
    const sourceId = el.dataset.incidentId || el.dataset.objectId || el.dataset.id ||
      layer?.options?.incidentId || layer?.options?.objectId || layer?.options?.id ||
      layer?.incidentId || layer?.id || layer?._leaflet_id || el._leaflet_id;
    if (sourceId != null && String(sourceId).trim()) return hashIdentity(`id:${sourceId}`);
    const point = Number.isFinite(box.lat) && Number.isFinite(box.lng)
      ? `${box.lat.toFixed(4)}:${box.lng.toFixed(4)}`
      : `${box.translate3d?.x ?? box.x}:${box.translate3d?.y ?? box.y}`;
    const identity = `geo:${objectType}:${point}`;
    const occurrence = occurrences.get(identity) || 0;
    occurrences.set(identity, occurrence + 1);
    return hashIdentity(occurrence ? `${identity}:copy:${occurrence + 1}` : identity);
  };

  const collect = (selector, prefix, requireNumber = false) => {
    if (!selector) return [];
    const occurrences = new Map();
    const seenNumbers = new Set();
    const numbers = operatorNumbers();
    return [...document.querySelectorAll(selector)].map((el, index) => {
          const box = centerOf(el);
          const positionedNumber = numberAtMarker(box, numbers);
          const markerType = objectTypeFrom([...el.classList].join(" "));
          const displayedObject = Number.isInteger(positionedNumber)
            ? { number: positionedNumber, type: markerType }
            : null;
          const objectType = markerType !== "Объект"
            ? markerType
            : displayedObject?.type || "Объект";
          const fallback = requireNumber ? fallbackIndex(el, box, objectType, occurrences) : null;
          const displayedNumber = displayedObject?.number;
          if (requireNumber && Number.isInteger(displayedNumber) && seenNumbers.has(displayedNumber)) {
            return null;
          }
          const objectIndex = Number.isInteger(displayedNumber)
            ? displayedNumber : requireNumber ? fallback : index + 1;
          if (objectIndex > 0) seenNumbers.add(objectIndex);
          return {
            index: objectIndex,
            fallback_id: fallback,
            name: requireNumber ? objectType : String(pickName(el, index, prefix)),
            object_type: requireNumber ? objectType : objectTypeFrom(pickName(el, index, prefix)),
            color: getComputedStyle(el).backgroundColor,
            x: box.x,
            y: box.y,
            lat: box.lat,
            lng: box.lng,
            width: box.width,
            height: box.height,
            translate3d: box.translate3d,
          };
        }).filter((item, index, all) => item && (!requireNumber || all.findIndex((other) => other?.index === item.index) === index));
  };

  const configStatics = (opts.staticObjects || []).map((item, index) => {
    let lat = item.lat == null || item.lat === "" ? null : Number(item.lat);
    let lng = item.lng == null || item.lng === "" ? null : Number(item.lng);
    let x = Number(item.x || 0);
    let y = Number(item.y || 0);
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      const p = latLngToContainer(lat, lng);
      if (p) {
        x = round2(p.x);
        y = round2(p.y);
      }
      lat = round6(lat);
      lng = round6(lng);
    } else {
      const ll = containerToLatLng(x, y);
      if (ll) {
        lat = round6(ll.lat);
        lng = round6(ll.lng);
      }
    }
    return {
      index: item.id || index + 1,
      name: String(item.name || ("S" + (index + 1))),
      x,
      y,
      lat,
      lng,
      color: "",
      width: 0,
      height: 0,
      translate3d: null,
    };
  });

  const dynamics = collect(opts.dynamic, "D", true);
  const statics = collect(opts.static, "S");
  const usedNumbers = new Set(dynamics.filter((item) => item.index > 0).map((item) => item.index));
  const applyKnownNumber = (item) => {
    if (item.index > 0 || !Number.isFinite(item.lat) || !Number.isFinite(item.lng)) return;
    const known = (opts.knownNumbers || {})[item.fallback_id];
    if (!known || usedNumbers.has(known.number)) return;
    const dLat = (item.lat - known.lat) * Math.PI / 180;
    const dLng = (item.lng - known.lng) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(item.lat * Math.PI / 180) *
      Math.cos(known.lat * Math.PI / 180) * Math.sin(dLng / 2) ** 2;
    if (6371 * 2 * Math.asin(Math.min(1, Math.sqrt(a))) <= 20) {
      item.index = known.number;
      usedNumbers.add(item.index);
    }
  };
  dynamics.forEach(applyKnownNumber);
  const scan = { status: map ? "ready" : "no_leaflet_instance", views: 0, overview_count: dynamics.length };

  // Сначала собираем маркеры при отдалении, затем читаем подписи в увеличенном виде.
  // Позиции всех объектов в результате пересчитываются для исходного вида карты.
  if (opts.scanNumbers && map && typeof map.setView === "function" &&
      typeof map.getCenter === "function" && typeof map.latLngToContainerPoint === "function" &&
      Number.isFinite(zoom) && mapRect?.width && mapRect?.height) {
    scan.status = "running";
    const originalCenter = map.getCenter();
    const scanWaitMs = Math.max(200, Math.min(10000, Number(opts.scanWaitMs) || 1500));
    const scanStarted = Date.now();
    const scanMoveLimit = 32;
    const scanTimeLimitMs = 75000;
    const canExplore = () => scan.views < scanMoveLimit &&
      Date.now() - scanStarted < scanTimeLimitMs;
    const staticPoints = opts.mode === "latlng" ? (opts.staticObjects || [])
      .filter((item) => Number.isFinite(item.lat) && Number.isFinite(item.lng) &&
        Math.abs(item.lat) <= 85.0511287798 && Math.abs(item.lng) <= 180) : [];
    const scanRadiusKm = Math.max(1, Number(opts.scanRadiusKm) || 350);
    const inStaticArea = (lat, lng) => !staticPoints.length || staticPoints.some((point) => {
      const dLat = (lat - point.lat) * Math.PI / 180;
      const dLng = (lng - point.lng) * Math.PI / 180;
      const a = Math.sin(dLat / 2) ** 2 +
        Math.cos(lat * Math.PI / 180) * Math.cos(point.lat * Math.PI / 180) *
        Math.sin(dLng / 2) ** 2;
      return 12742 * Math.asin(Math.min(1, Math.sqrt(a))) <= scanRadiusKm;
    });
    if (staticPoints.length) {
      const nearby = dynamics.filter((item) => !Number.isFinite(item.lat) ||
        !Number.isFinite(item.lng) || inStaticArea(item.lat, item.lng));
      dynamics.length = 0;
      dynamics.push(...nearby);
      usedNumbers.clear();
      dynamics.filter((item) => item.index > 0).forEach((item) => usedNumbers.add(item.index));
    }
    const reportedMaxZoom = typeof map.getMaxZoom === "function" ? map.getMaxZoom() : Infinity;
    const configuredMaxZoom = Math.max(4, Math.min(18, Number(opts.scanMaxZoom) || 10));
    const maxZoom = Math.min(
      Number.isFinite(reportedMaxZoom) ? reportedMaxZoom : Infinity,
      Math.max(zoom, configuredMaxZoom),
    );
    const reportedMinZoom = typeof map.getMinZoom === "function" ? map.getMinZoom() : -Infinity;
    const minZoom = Number.isFinite(reportedMinZoom) ? reportedMinZoom : zoom - 4;
    const targetZoom = Math.min(
      dynamics.some((item) => item.index > 0) ? zoom : zoom + 2, maxZoom
    );
    const recordNumberedView = (captureView = false) => {
      if (!opts.captureNumbered) return 0;
      const numbered = collect(opts.dynamic, "D", true).filter((item) => {
        if (item.index <= 0 || !Number.isFinite(item.lat) || !Number.isFinite(item.lng)) return false;
        if (!inStaticArea(item.lat, item.lng)) return false;
        const point = map.latLngToContainerPoint([item.lat, item.lng]);
        return point && point.x >= 0 && point.x <= mapRect.width &&
          point.y >= 0 && point.y <= mapRect.height;
      });
      if (numbered.length) {
        const current = map.getCenter();
        const view = {
          lat: current.lat, lng: current.lng, zoom: map.getZoom(), count: numbered.length,
        };
        if (numbered.length > (scan.best_numbered_view?.count || 0)) {
          scan.best_numbered_view = view;
        }
        if (captureView) {
          (scan.numbered_views ||= []).push(view);
        }
      }
      return numbered.length;
    };
    recordNumberedView(true);
    const moveTo = async (lat, lng, z, captureView = false) => {
      scan.views++;
      const zoomingIn = z > map.getZoom();
      await new Promise((resolve) => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          if (typeof map.off === "function") map.off("moveend", finish);
          clearTimeout(timer);
          resolve();
        };
        const timer = setTimeout(finish, 1500);
        if (typeof map.on === "function") map.on("moveend", finish);
        try { map.setView([lat, lng], z, { animate: false }); }
        catch (error) { finish(); }
        if (typeof map.on !== "function") finish();
      });
      // Vue может перерисовать маркеры после moveend и ответа API.
      await new Promise((resolve) => setTimeout(resolve, scanWaitMs));
      return recordNumberedView(captureView);
    };
    const matchVisible = () => {
      const visible = collect(opts.dynamic, "D", true).filter((item) => item.index > 0);
      const unresolvedById = new Map(dynamics.filter((item) => item.index < 0)
        .map((item) => [item.fallback_id, item]));
      let matched = 0;
      for (const item of visible) {
        if (usedNumbers.has(item.index) || !Number.isFinite(item.lat) ||
            !Number.isFinite(item.lng) || !inStaticArea(item.lat, item.lng)) continue;
        let best = null;
        const identical = unresolvedById.get(item.fallback_id);
        if (identical && identical.object_type === item.object_type) {
          best = {candidate: identical, distance: 0};
        }
        for (const candidate of best ? [] : dynamics) {
          if (candidate.index >= 0 || !Number.isFinite(candidate.lat) || !Number.isFinite(candidate.lng)) continue;
          if (candidate.object_type !== item.object_type) continue;
          let distance = Infinity;
          if (candidate.fallback_id === item.fallback_id) distance = 0;
          else {
            const p = map.latLngToContainerPoint([candidate.lat, candidate.lng]);
            const q = map.latLngToContainerPoint([item.lat, item.lng]);
            if (p && q) distance = Math.hypot(p.x - q.x, p.y - q.y);
          }
          if (distance <= 8 && (!best || distance < best.distance)) best = { candidate, distance };
        }
        if (best) {
          best.candidate.index = item.index;
          usedNumbers.add(item.index);
          matched++;
        }
      }
      return matched;
    };
    try {
      const byIdentity = new Map(dynamics.map((item) => [item.fallback_id, item]));
      const rememberVisible = (visible = collect(opts.dynamic, "D", true)) => {
        let added = 0;
        for (const item of visible) {
          if (!Number.isFinite(item.lat) || !Number.isFinite(item.lng) ||
              !inStaticArea(item.lat, item.lng)) continue;
          const existing = byIdentity.get(item.fallback_id);
          if (!existing) {
            applyKnownNumber(item);
            dynamics.push(item);
            byIdentity.set(item.fallback_id, item);
            added++;
          }
          else if (existing.index < 0 && item.index > 0 && !usedNumbers.has(item.index)) {
            existing.index = item.index;
            usedNumbers.add(item.index);
          }
        }
        return added;
      };
      // В html_test/44.html правая вкладка показывает 9 нужных маркеров при zoom 4.
      const incidentMap = String(opts.dynamic || "").includes("map-incident");
      const overviewZoom = Math.min(maxZoom, Math.max(minZoom, zoom - 4, incidentMap ? 4 : -Infinity));
      for (let level = zoom - 1; level >= overviewZoom && canExplore(); level--) {
        await moveTo(originalCenter.lat, originalCenter.lng, level);
        rememberVisible();
      }
      if (map.getZoom() !== overviewZoom && canExplore()) {
        await moveTo(originalCenter.lat, originalCenter.lng, overviewZoom);
        rememberVisible();
      }
      if (staticPoints.length) {
        // Обзор строится вокруг стационарных точек; далёкие маркеры не ведут
        // карту за пределы контролируемых зон.
        const survey = [];
        for (const point of staticPoints) {
          const projected = project3857(point.lat, point.lng, overviewZoom);
          if (!survey.some((entry) =>
            Math.abs(entry.x - projected.x) < mapRect.width * 0.4 &&
            Math.abs(entry.y - projected.y) < mapRect.height * 0.4)) {
            survey.push({lat: point.lat, lng: point.lng, ...projected});
          }
        }
        scan.static_areas = survey.length;
        for (const center of survey) {
          if (!canExplore()) break;
          await moveTo(center.lat, center.lng, overviewZoom);
          rememberVisible();
        }
      } else {
        // Без географических координат статичных точек сохраняем локальный
        // обзор, но ограничиваем число перемещений и время обхода.
        const centers = new Map();
        for (let row = -4; row <= 4; row++) {
          for (let col = -4; col <= 4; col++) {
            const center = map.containerPointToLatLng([
              mapRect.width * (0.5 + col * 0.75),
              mapRect.height * (0.5 + row * 0.75),
            ]);
            centers.set(`${col},${row}`, center);
          }
        }
        const queue = [[0, 0]];
        const seen = new Set();
        while (queue.length && seen.size < 16 && canExplore()) {
          const [col, row] = queue.shift();
          const key = `${col},${row}`;
          if (seen.has(key) || !centers.has(key)) continue;
          const center = centers.get(key);
          if (!center || !Number.isFinite(center.lat) || !Number.isFinite(center.lng) ||
              Math.abs(center.lat) > 85.0511287798 || Math.abs(center.lng) > 180) continue;
          seen.add(key);
          if (col || row) await moveTo(center.lat, center.lng, overviewZoom);
          const visible = collect(opts.dynamic, "D", true);
          const added = rememberVisible(visible);
          if ((col || row) && !added) continue;
          const edges = new Set();
          for (const item of visible) {
            if (!Number.isFinite(item.lat) || !Number.isFinite(item.lng)) continue;
            const p = map.latLngToContainerPoint([item.lat, item.lng]);
            if (!p || p.x < 0 || p.x > mapRect.width || p.y < 0 || p.y > mapRect.height) continue;
            if (p.x < mapRect.width * 0.2) edges.add(`${col - 1},${row}`);
            if (p.x > mapRect.width * 0.8) edges.add(`${col + 1},${row}`);
            if (p.y < mapRect.height * 0.2) edges.add(`${col},${row - 1}`);
            if (p.y > mapRect.height * 0.8) edges.add(`${col},${row + 1}`);
          }
          edges.forEach((next) => {
            if (!seen.has(next) && centers.has(next)) queue.push(next.split(",").map(Number));
          });
        }
      }
      dynamics.filter((item) => item.index > 0).forEach((item) => usedNumbers.add(item.index));
      scan.overview_count = dynamics.length;
      // Объединяем маркеры, попадающие в один безопасный прямоугольник вида.
      // Если вся группа поместится на максимальном масштабе, сразу идём туда
      // один раз: повторные заходы к копиям с тем же номером ничего не дают.
      const finalZoom = Math.min(zoom + 6, maxZoom);
      const groupAt = (items, level) => {
        const positioned = items.map((item) => ({
          item, point: project3857(item.lat, item.lng, level),
        }));
        const minX = Math.min(...positioned.map((entry) => entry.point.x));
        const minY = Math.min(...positioned.map((entry) => entry.point.y));
        const cellW = Math.max(1, mapRect.width * 0.6);
        const cellH = Math.max(1, mapRect.height * 0.6);
        const cells = new Map();
        for (const entry of positioned) {
          const key = `${Math.floor((entry.point.x - minX) / cellW)},` +
            `${Math.floor((entry.point.y - minY) / cellH)}`;
          if (!cells.has(key)) cells.set(key, []);
          cells.get(key).push(entry.item);
        }
        return [...cells.values()].sort((a, b) => b.length - a.length);
      };
      const attemptedAtFinal = new Set();
      for (const level of [...new Set([targetZoom, finalZoom])]) {
        if (!canExplore()) break;
        const pending = dynamics.filter((item) => item.index < 0 &&
          Number.isFinite(item.lat) && Number.isFinite(item.lng) &&
          inStaticArea(item.lat, item.lng) &&
          !attemptedAtFinal.has(item.fallback_id));
        if (!pending.length) break;
        for (const group of groupAt(pending, level)) {
          if (!canExplore()) break;
          const remaining = group.filter((item) => item.index < 0 &&
            !attemptedAtFinal.has(item.fallback_id));
          if (!remaining.length) continue;
          const highPoints = remaining.map((item) => project3857(item.lat, item.lng, finalZoom));
          const highX = highPoints.map((point) => point.x);
          const highY = highPoints.map((point) => point.y);
          const fitsHigh = Math.max(...highX) - Math.min(...highX) <= mapRect.width * 0.6 &&
            Math.max(...highY) - Math.min(...highY) <= mapRect.height * 0.6;
          const visitZoom = level === finalZoom || fitsHigh ? finalZoom : level;
          const points = remaining.map((item) => project3857(item.lat, item.lng, visitZoom));
          const xs = points.map((point) => point.x);
          const ys = points.map((point) => point.y);
          const center = unproject3857(
            (Math.min(...xs) + Math.max(...xs)) / 2,
            (Math.min(...ys) + Math.max(...ys)) / 2, visitZoom,
          );
          const numberedAtMove = await moveTo(center.lat, center.lng, visitZoom, true);
          const visibleIncidents = collect(opts.dynamic, "D", true).filter((item) =>
            Number.isFinite(item.lat) && Number.isFinite(item.lng) &&
            inStaticArea(item.lat, item.lng));
          if (!visibleIncidents.length) {
            remaining.forEach((item) => attemptedAtFinal.add(item.fallback_id));
            continue;
          }
          const matched = matchVisible();
          if (!matched && canExplore()) {
            await new Promise((resolve) => setTimeout(resolve, scanWaitMs));
            if (!numberedAtMove) recordNumberedView(true);
            matchVisible();
          }
          if (visitZoom === finalZoom) {
            remaining.forEach((item) => attemptedAtFinal.add(item.fallback_id));
          }
        }
      }
      scan.budget_exhausted = !canExplore();
    } finally {
      await moveTo(originalCenter.lat, originalCenter.lng, zoom);
      const restored = () => {
        const actual = map.getCenter();
        const point = map.latLngToContainerPoint([originalCenter.lat, originalCenter.lng]);
        return map.getZoom() === zoom && actual && point &&
          Math.hypot(point.x - mapRect.width / 2, point.y - mapRect.height / 2) < 2;
      };
      if (!restored()) await moveTo(originalCenter.lat, originalCenter.lng, zoom);
      for (const item of dynamics) {
        if (!Number.isFinite(item.lat) || !Number.isFinite(item.lng)) continue;
        const p = map.latLngToContainerPoint([item.lat, item.lng]);
        if (!p) continue;
        const xy = xyForMode(mapRect.left + p.x, mapRect.top + p.y);
        item.x = round2(xy.x);
        item.y = round2(xy.y);
      }
      scan.status = !restored() ? "restore_failed" :
        !scan.budget_exhausted && dynamics.every((item) => item.index > 0)
          ? "complete" : "partial";
    }
  } else if (map && opts.scanNumbers) {
    scan.status = "map_controls_unavailable";
  }

  return {
    dynamics,
    scan,
    unnumbered_dynamics: dynamics.filter((item) => item.index < 0).length,
    statics,
    config_statics: configStatics,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    map: mapRect
      ? {
          width: Math.round(mapRect.width),
          height: Math.round(mapRect.height),
          left: Math.round(mapRect.left),
          top: Math.round(mapRect.top),
          zoom: zoom,
          has_leaflet: Boolean(map),
          geo_source: geoSource,
          proxy: proxy,
          center: center,
        }
      : null,
    map_zoom: zoom,
    geo_source: geoSource,
    coordinate_mode: mode,
  };
}
"""


@dataclass(slots=True)
class SceneObject:
    """Маркер на карте: экранные x/y и при наличии географические lat/lng."""

    index: int
    name: str
    x: float
    y: float
    color: str = ""
    width: float = 0.0
    height: float = 0.0
    lat: float | None = None
    lng: float | None = None
    translate3d: tuple[float, float] | None = None
    object_type: str = ""
    fallback_id: int | None = None


@dataclass(slots=True)
class DistanceRow:
    """Одна строка таблицы: статичная точка, динамический объект, дистанция, цвет."""

    static_index: int
    static_name: str
    static_x: float
    static_y: float
    dynamic_index: int
    dynamic_name: str
    dynamic_x: float
    dynamic_y: float
    dynamic_color: str
    distance: float
    status: Status
    distance_unit: str = "px"
    static_lat: float | None = None
    static_lng: float | None = None
    dynamic_lat: float | None = None
    dynamic_lng: float | None = None
    # Сколько динамических объектов красные / жёлтые относительно этой статичной точки.
    alert_count: int = 0
    warning_count: int = 0
    phone: str = ""

    @property
    def static_coords(self) -> str:
        """Форматирует географические координаты или резервные X/Y статичной точки."""
        if self.static_lat is not None and self.static_lng is not None:
            return f"({self.static_lat:.6f}, {self.static_lng:.6f})"
        return _fmt_point(self.static_x, self.static_y)

    @property
    def dynamic_coords(self) -> str:
        """Форматирует географические координаты или резервные X/Y маркера."""
        if self.dynamic_lat is not None and self.dynamic_lng is not None:
            return f"({self.dynamic_lat:.6f}, {self.dynamic_lng:.6f})"
        return _fmt_point(self.dynamic_x, self.dynamic_y)

    @property
    def nearby_counts_label(self) -> str:
        """Подпись колонки: сколько красных и жёлтых объектов до этой точки."""
        return f"кр. {self.alert_count} · жёл. {self.warning_count}"

    @property
    def status_group_count(self) -> int:
        """Число объектов того же цвета, что и строка (красный или жёлтый)."""
        if self.status == "alert":
            return self.alert_count
        if self.status == "warning":
            return self.warning_count
        return 0


@dataclass(slots=True)
class TrackingSnapshot:
    """Полный снимок опроса: объекты, строки таблицы, зум карты, предупреждения."""

    url: str
    title: str
    viewport: tuple[float, float]
    dynamics: list[SceneObject]
    statics: list[SceneObject]
    rows: list[DistanceRow]
    threshold: float
    warning_limit: float
    coordinate_mode: CoordinateMode = "screen"
    distance_unit: str = "px"
    map_size: tuple[float, float] | None = None
    map_zoom: float | None = None
    reference_zoom: float | None = None
    scale_warning: str = ""
    geo_source: str = "none"
    map_center: tuple[float, float] | None = None
    blue_limit: float = BLUE_ZONE_KM
    unnumbered_dynamics: int = 0
    scan_status: str = ""


def extract_scene_from_page(
    page: Any, tracking: TrackingConfig, *, scan_numbers: bool = True,
    known_numbers: dict[int, tuple[int, float, float]] | None = None,
    capture_numbered: bool = False,
) -> dict[str, Any]:
    """Снимает маркеры и вид карты из живой вкладки (JS _EXTRACT_SCRIPT)."""
    return page.evaluate(
        _EXTRACT_SCRIPT,
        {
            "dynamic": tracking.dynamic_selector,
            "static": tracking.static_selector,
            "mode": tracking.coordinate_mode,
            "mapContainer": tracking.map_container_selector,
            "scanNumbers": scan_numbers,
            "captureNumbered": capture_numbered,
            "scanWaitMs": round(tracking.scan_wait_seconds * 1000),
            "scanMaxZoom": tracking.scan_max_zoom,
            "scanRadiusKm": max(tracking.threshold, tracking.warning_limit,
                                tracking.blue_limit),
            "knownNumbers": {
                str(identity): {"number": number, "lat": lat, "lng": lng}
                for identity, (number, lat, lng) in (known_numbers or {}).items()
            },
            "staticObjects": [
                {
                    "id": item.id,
                    "name": item.name,
                    "x": item.x,
                    "y": item.y,
                    "lat": item.lat,
                    "lng": item.lng,
                }
                for item in tracking.static_objects
            ],
        },
    )


def _scan_target_groups(
    targets: list[dict[str, Any]], width: float, height: float, zoom: float,
) -> list[list[dict[str, Any]]]:
    """Собирает близкие маркеры в один вид на масштабе проверки номеров."""
    positioned = []
    for item in targets:
        lat, lng = _maybe_float(item.get("lat")), _maybe_float(item.get("lng"))
        if lat is not None and lng is not None:
            positioned.append((item, project_3857(lat, lng, zoom)))
    if not positioned:
        return []
    origin_x = min(point[0] for _, point in positioned)
    origin_y = min(point[1] for _, point in positioned)
    cell_width = max(1.0, width * 0.4)
    cell_height = max(1.0, height * 0.4)
    cells: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item, (x, y) in positioned:
        key = (int((x - origin_x) // cell_width), int((y - origin_y) // cell_height))
        cells.setdefault(key, []).append(item)
    return sorted(cells.values(), key=len, reverse=True)


def extract_scene_with_mouse_scan(
    page: Any,
    tracking: TrackingConfig,
    number_cache: dict[int, tuple[int, float, float, float]] | None = None,
    on_numbered_view: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Обходит карту колесом браузера, если Vue не отдаёт экземпляр L.Map."""
    raw = extract_scene_from_page(page, tracking, scan_numbers=False)
    initial_labels_visible = any(item.get("index", 0) > 0 for item in raw.get("dynamics") or [])
    if on_numbered_view is None:
        _apply_number_cache(raw, number_cache)
    scan = raw.get("scan") or {}
    if scan.get("status") == "ready":
        now = monotonic()
        recent_numbers = {
            identity: (number, lat, lng)
            for identity, (number, lat, lng, captured_at) in (number_cache or {}).items()
            if now - captured_at <= 60
        }
        raw = extract_scene_from_page(
            page, tracking, scan_numbers=True, known_numbers=recent_numbers,
            capture_numbered=on_numbered_view is not None,
        )
        _apply_number_cache(raw, number_cache)
        _remember_numbers(raw, number_cache)
        return raw
    if scan.get("status") != "no_leaflet_instance":
        _apply_number_cache(raw, number_cache)
        _remember_numbers(raw, number_cache)
        return raw
    static_points = [
        (item.lat, item.lng) for item in tracking.static_objects
        if tracking.coordinate_mode == "latlng"
        and item.lat is not None and item.lng is not None
    ]
    scan_radius = max(tracking.threshold, tracking.warning_limit, tracking.blue_limit)

    def in_static_area(lat: float, lng: float) -> bool:
        return not static_points or any(
            haversine_km(lat, lng, static_lat, static_lng) <= scan_radius
            for static_lat, static_lng in static_points
        )

    if static_points:
        raw["dynamics"] = [
            item for item in raw.get("dynamics") or []
            if (lat := _maybe_float(item.get("lat"))) is None
            or (lng := _maybe_float(item.get("lng"))) is None
            or in_static_area(lat, lng)
        ]
        raw["unnumbered_dynamics"] = sum(
            item["index"] < 0 for item in raw["dynamics"]
        )
    scan_deadline = monotonic() + 75
    base_map = raw.get("map") or {}
    base_view = map_view_from_raw(base_map)
    base_zoom = _maybe_float(raw.get("map_zoom"))
    if base_view is None or not base_view.can_unproject or base_zoom is None:
        scan["status"] = "mouse_scan_unavailable"
        raw["scan"] = scan
        _apply_number_cache(raw, number_cache)
        return raw
    scan_zoom_ceiling = max(base_zoom, tracking.scan_max_zoom)
    original_lat = base_view.center_lat
    original_lng = base_view.center_lng
    scan_wait_ms = round(tracking.scan_wait_seconds * 1000)
    def record_numbered(scene: dict[str, Any]) -> None:
        if on_numbered_view is None:
            return
        view = map_view_from_raw(scene.get("map"))
        if view is None:
            return
        for item in scene.get("dynamics") or []:
            if item.get("index", 0) <= 0:
                continue
            lat, lng = _maybe_float(item.get("lat")), _maybe_float(item.get("lng"))
            point = latlng_to_container(lat, lng, view) \
                if lat is not None and lng is not None else None
            if point is not None and 0 <= point[0] <= view.width \
                    and 0 <= point[1] <= view.height:
                on_numbered_view(scene)
                break

    if initial_labels_visible:
        record_numbered(raw)

    # Даже если все видимые объекты уже подписаны, за границей исходного окна
    # могут быть ещё не созданные сайтом маркеры. Сначала отдаляем карту.
    map_left = float(base_map.get("left") or 0)
    map_top = float(base_map.get("top") or 0)
    center_x = map_left + base_view.width / 2
    center_y = map_top + base_view.height / 2
    overview = raw
    overview_moves: list[int] = []
    overview_views = 0
    target_overview = 4.0 if "map-incident" in tracking.dynamic_selector else base_zoom - 4
    for _ in range(4):
        if monotonic() >= scan_deadline:
            break
        previous_zoom = _maybe_float(overview.get("map_zoom"))
        if previous_zoom is None or abs(previous_zoom - target_overview) < 0.01:
            break
        delta = 120 if previous_zoom > target_overview else -120
        page.mouse.move(center_x, center_y)
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(scan_wait_ms)
        candidate = extract_scene_from_page(page, tracking, scan_numbers=False)
        current_zoom = _maybe_float(candidate.get("map_zoom"))
        if current_zoom is None or (current_zoom - previous_zoom) * delta >= 0:
            break
        overview_moves.append(delta)
        overview_views += 1
        overview = candidate
        _merge_overview_scene(raw, candidate, base_view, in_static_area)
    overview_view = map_view_from_raw(overview.get("map"))
    if overview_view is not None and overview_view.can_unproject:
        if callable(getattr(page.mouse, "down", None)) and callable(getattr(page.mouse, "up", None)):
            queue = [(0, 0)]
            seen: set[tuple[int, int]] = set()
            while queue and len(seen) < 16 and overview_views < 32 \
                    and monotonic() < scan_deadline:
                column, row = queue.pop(0)
                if (column, row) in seen or abs(column) > 4 or abs(row) > 4:
                    continue
                position = container_to_latlng(
                    overview_view.width * (0.5 + column * 0.75),
                    overview_view.height * (0.5 + row * 0.75),
                    overview_view,
                )
                if position is None or abs(position[0]) > 85 or abs(position[1]) > 180:
                    continue
                if not in_static_area(*position):
                    continue
                seen.add((column, row))
                if column or row:
                    overview = _pan_mouse_to(page, tracking, overview, *position)
                    overview_views += 1
                added = _merge_overview_scene(raw, overview, base_view, in_static_area)
                if (column or row) and not added:
                    continue
                current_view = map_view_from_raw(overview.get("map"))
                if current_view is None:
                    continue
                for item in overview.get("dynamics") or []:
                    lat = _maybe_float(item.get("lat"))
                    lng = _maybe_float(item.get("lng"))
                    point = latlng_to_container(lat, lng, current_view) \
                        if lat is not None and lng is not None else None
                    if point is None or not in_static_area(lat, lng) or not (0 <= point[0] <= current_view.width \
                            and 0 <= point[1] <= current_view.height):
                        continue
                    if point[0] < current_view.width * 0.3:
                        queue.append((column - 1, row))
                    if point[0] > current_view.width * 0.7:
                        queue.append((column + 1, row))
                    if point[1] < current_view.height * 0.3:
                        queue.append((column, row - 1))
                    if point[1] > current_view.height * 0.7:
                        queue.append((column, row + 1))
            overview = _pan_mouse_to(
                page, tracking, overview, overview_view.center_lat, overview_view.center_lng,
                max_gestures=24,
            )
        used = {item["index"] for item in raw["dynamics"] if item["index"] > 0}
        offscreen = []
        for item in raw["dynamics"]:
            if item["index"] > 0:
                continue
            if not in_static_area(item["lat"], item["lng"]):
                continue
            point = latlng_to_container(item["lat"], item["lng"], base_view)
            if point is not None and not (0 <= point[0] <= base_view.width
                                          and 0 <= point[1] <= base_view.height):
                offscreen.append(item)
        for group in _scan_target_groups(
            offscreen, base_view.width, base_view.height, scan_zoom_ceiling,
        ):
            if overview_views >= 32 or monotonic() >= scan_deadline:
                break
            target = group[0]
            current_view = map_view_from_raw(overview.get("map"))
            point = latlng_to_container(target["lat"], target["lng"], current_view) \
                if current_view is not None else None
            if point is None:
                continue
            if not (0 <= point[0] <= current_view.width and 0 <= point[1] <= current_view.height):
                if not callable(getattr(page.mouse, "down", None)):
                    continue
                overview = _pan_mouse_to(page, tracking, overview, target["lat"], target["lng"])
                current_view = map_view_from_raw(overview.get("map"))
                if current_view is None:
                    continue
                point = latlng_to_container(target["lat"], target["lng"], current_view)
                if point is None or not (0 <= point[0] <= current_view.width \
                        and 0 <= point[1] <= current_view.height):
                    continue
            anchor_x, anchor_y = map_left + point[0], map_top + point[1]
            ticks = 0
            current = overview
            while any(item["index"] < 0 for item in group) and ticks < 6 \
                    and monotonic() < scan_deadline:
                previous_zoom = _maybe_float(current.get("map_zoom"))
                if previous_zoom is None or previous_zoom >= scan_zoom_ceiling:
                    break
                page.mouse.move(anchor_x, anchor_y)
                page.mouse.wheel(0, -120)
                page.wait_for_timeout(scan_wait_ms)
                current = extract_scene_from_page(page, tracking, scan_numbers=False)
                record_numbered(current)
                current_zoom = _maybe_float(current.get("map_zoom"))
                if current_zoom is None or previous_zoom is None or current_zoom <= previous_zoom:
                    break
                if not current.get("dynamics"):
                    break
                ticks += 1
                _merge_numbered_scene(raw, current, used)
            overview_views += 1
            for _ in range(ticks):
                page.mouse.move(anchor_x, anchor_y)
                page.mouse.wheel(0, 120)
                page.wait_for_timeout(scan_wait_ms)
                current = extract_scene_from_page(page, tracking, scan_numbers=False)
            overview = current
            if callable(getattr(page.mouse, "down", None)):
                overview = _pan_mouse_to(
                    page, tracking, overview, overview_view.center_lat, overview_view.center_lng
                )
    for delta in reversed(overview_moves):
        page.mouse.move(center_x, center_y)
        page.mouse.wheel(0, -delta)
        page.wait_for_timeout(scan_wait_ms)
        overview = extract_scene_from_page(page, tracking, scan_numbers=False)
    restored_zoom = _maybe_float(overview.get("map_zoom"))
    if restored_zoom is None or abs(restored_zoom - base_zoom) > 0.01:
        scan["status"] = "mouse_restore_failed"
        raw["scan"] = scan
        return raw
    if on_numbered_view is None:
        _apply_number_cache(raw, number_cache)
    if not raw.get("unnumbered_dynamics"):
        if callable(getattr(page.mouse, "down", None)) and original_lat is not None \
                and original_lng is not None:
            overview = _pan_mouse_to(page, tracking, overview, original_lat, original_lng,
                                     max_gestures=24)
        scan["status"] = "complete" if _scene_at_origin(overview, base_zoom, original_lat,
                                                          original_lng) else "mouse_restore_failed"
        scan["views"] = overview_views
        scan["overview_count"] = len(raw.get("dynamics") or [])
        _remember_numbers(raw, number_cache)
        return raw

    targets = [
        item for item in raw.get("dynamics") or []
        if item.get("index", 0) < 0 and _maybe_float(item.get("lat")) is not None
        and _maybe_float(item.get("lng")) is not None
        and in_static_area(item["lat"], item["lng"])
        and (point := latlng_to_container(item["lat"], item["lng"], base_view)) is not None
        and 0 <= point[0] <= base_view.width and 0 <= point[1] <= base_view.height
    ]
    used_numbers = {item["index"] for item in raw["dynamics"] if item["index"] > 0}
    wheel_ticks: int | None = None
    views = 0
    wheel_worked = False

    def scene_after_wheel(x: float, y: float, delta: int) -> dict[str, Any]:
        page.mouse.move(x, y)
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(scan_wait_ms)
        scene = extract_scene_from_page(page, tracking, scan_numbers=False)
        if delta < 0:
            record_numbered(scene)
        zoom_after = _maybe_float(scene.get("map_zoom"))
        if delta < 0 and zoom_after is not None and zoom_after >= base_zoom + 2:
            for _ in range(3):
                if any(item["index"] > 0 for item in scene.get("dynamics") or []):
                    break
                page.wait_for_timeout(scan_wait_ms)
                scene = extract_scene_from_page(page, tracking, scan_numbers=False)
                record_numbered(scene)
        return scene

    for group in _scan_target_groups(
        targets, base_view.width, base_view.height, scan_zoom_ceiling,
    ):
        if views + overview_views >= 32 or monotonic() >= scan_deadline:
            break
        if all(item["index"] > 0 for item in group):
            continue
        target = next(item for item in group if item["index"] < 0)
        point = latlng_to_container(target["lat"], target["lng"], base_view)
        if point is None:
            continue
        x, y = point
        if not (0 <= x <= base_view.width and 0 <= y <= base_view.height):
            continue
        screen_x, screen_y = map_left + x, map_top + y
        current = raw
        ticks = 0
        restore_failed = False
        try:
            limit = wheel_ticks or 6
            while ticks < limit:
                previous_zoom = _maybe_float(current.get("map_zoom"))
                if previous_zoom is None or previous_zoom >= scan_zoom_ceiling \
                        or monotonic() >= scan_deadline:
                    break
                current = scene_after_wheel(screen_x, screen_y, -120)
                ticks += 1
                now_zoom = _maybe_float(current.get("map_zoom"))
                if now_zoom is not None and previous_zoom is not None and now_zoom > previous_zoom:
                    wheel_worked = True
                if ticks == 1 and not wheel_worked:
                    break
                if not current.get("dynamics"):
                    break
                if wheel_ticks is None and any(item["index"] > 0 for item in current.get("dynamics") or []):
                    wheel_ticks = ticks
                    break
            if not wheel_worked:
                scan["status"] = "mouse_wheel_unavailable"
                break
            views += 1
            _merge_numbered_scene(raw, current, used_numbers)
            # Проверяем всю группу: номера её соседей могут появиться позже.
            max_ticks = min(10, (wheel_ticks or 6) + 4)
            while any(item["index"] < 0 for item in group) and ticks < max_ticks:
                previous_zoom = _maybe_float(current.get("map_zoom"))
                if previous_zoom is None or previous_zoom >= scan_zoom_ceiling \
                        or monotonic() >= scan_deadline:
                    break
                current = scene_after_wheel(screen_x, screen_y, -120)
                ticks += 1
                _merge_numbered_scene(raw, current, used_numbers)
                if not current.get("dynamics"):
                    break
                current_zoom = _maybe_float(current.get("map_zoom"))
                if current_zoom is not None and previous_zoom is not None \
                        and current_zoom <= previous_zoom:
                    break
        finally:
            for _ in range(ticks + 4):
                current_zoom = _maybe_float(current.get("map_zoom"))
                if current_zoom is not None and abs(current_zoom - base_zoom) <= 0.01:
                    break
                current = scene_after_wheel(
                    screen_x, screen_y, 120 if current_zoom is None or current_zoom > base_zoom else -120
                )
            restored_zoom = _maybe_float(current.get("map_zoom"))
            if restored_zoom is None or abs(restored_zoom - base_zoom) > 0.01:
                scan["status"] = "mouse_restore_failed"
                restore_failed = True
            restored_view = map_view_from_raw(current.get("map"))
            if restored_view is not None:
                base_view = restored_view
        if restore_failed:
            break
    else:
        scan["status"] = "complete" if all(item["index"] > 0 for item in raw["dynamics"]) else "partial"

    if callable(getattr(page.mouse, "down", None)) and original_lat is not None \
            and original_lng is not None:
        current = extract_scene_from_page(page, tracking, scan_numbers=False)
        current = _pan_mouse_to(page, tracking, current, original_lat, original_lng,
                                max_gestures=24)
        if not _scene_at_origin(current, base_zoom, original_lat, original_lng):
            scan["status"] = "mouse_restore_failed"

    scan["views"] = overview_views + views
    scan["overview_count"] = len(raw.get("dynamics") or [])
    if scan.get("status") == "no_leaflet_instance":
        scan["status"] = "partial"
    scan["budget_exhausted"] = monotonic() >= scan_deadline or scan["views"] >= 32
    raw["scan"] = scan
    _apply_number_cache(raw, number_cache)
    raw["unnumbered_dynamics"] = sum(item["index"] < 0 for item in raw["dynamics"])
    _remember_numbers(raw, number_cache)
    return raw


def _apply_number_cache(
    raw: dict[str, Any], cache: dict[int, tuple[int, float, float, float]] | None
) -> None:
    if not cache:
        return
    now = monotonic()
    used = {item["index"] for item in raw.get("dynamics") or [] if item["index"] > 0}
    for item in raw.get("dynamics") or []:
        identity = item.get("fallback_id")
        known = cache.get(identity) if isinstance(identity, int) else None
        lat = _maybe_float(item.get("lat"))
        lng = _maybe_float(item.get("lng"))
        if not known or item["index"] > 0 or lat is None or lng is None:
            continue
        number, old_lat, old_lng, captured_at = known
        if now - captured_at <= 60 and haversine_km(lat, lng, old_lat, old_lng) <= 20 and number not in used:
            item["index"] = number
            used.add(number)
    raw["unnumbered_dynamics"] = sum(item["index"] < 0 for item in raw.get("dynamics") or [])


def _remember_numbers(
    raw: dict[str, Any], cache: dict[int, tuple[int, float, float, float]] | None
) -> None:
    if cache is None:
        return
    now = monotonic()
    for item in raw.get("dynamics") or []:
        identity = item.get("fallback_id")
        lat = _maybe_float(item.get("lat"))
        lng = _maybe_float(item.get("lng"))
        if isinstance(identity, int) and item["index"] > 0 and lat is not None and lng is not None:
            cache[identity] = (item["index"], lat, lng, now)


def _pan_mouse_to(
    page: Any, tracking: TrackingConfig, scene: dict[str, Any], lat: float, lng: float,
    max_gestures: int = 8,
) -> dict[str, Any]:
    """Перетаскивает карту к центру, ограничивая каждый жест размером окна."""
    current = scene
    for _ in range(max_gestures):
        map_raw = current.get("map") or {}
        view = map_view_from_raw(map_raw)
        if view is None or not view.can_unproject:
            break
        point = latlng_to_container(lat, lng, view)
        if point is None:
            break
        dx = max(-view.width * 0.4, min(view.width * 0.4, view.width / 2 - point[0]))
        dy = max(-view.height * 0.4, min(view.height * 0.4, view.height / 2 - point[1]))
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            break
        start_x = float(map_raw.get("left") or 0) + view.width / 2
        start_y = float(map_raw.get("top") or 0) + view.height / 2
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        try:
            page.mouse.move(start_x + dx, start_y + dy, steps=5)
        finally:
            page.mouse.up()
        page.wait_for_timeout(round(tracking.scan_wait_seconds * 1000))
        candidate = extract_scene_from_page(page, tracking, scan_numbers=False)
        next_view = map_view_from_raw(candidate.get("map"))
        current = candidate
        if next_view is None or next_view.center_lat is None or next_view.center_lng is None:
            break
        if view.center_lat is not None and view.center_lng is not None \
                and haversine_km(view.center_lat, view.center_lng,
                                 next_view.center_lat, next_view.center_lng) < 0.01:
            break
    return current


def _scene_at_origin(scene: dict[str, Any], zoom: float, lat: float, lng: float) -> bool:
    """Проверяет масштаб и центр карты с допуском два экранных пикселя."""
    current_zoom = _maybe_float(scene.get("map_zoom"))
    view = map_view_from_raw(scene.get("map"))
    if current_zoom is None or abs(current_zoom - zoom) > 0.01 or view is None:
        return False
    point = latlng_to_container(lat, lng, view)
    return point is not None and abs(point[0] - view.width / 2) < 2 \
        and abs(point[1] - view.height / 2) < 2


def _merge_overview_scene(
    overview: dict[str, Any], detailed: dict[str, Any], base_view: MapView,
    in_area: Callable[[float, float], bool] | None = None,
) -> int:
    """Добавляет найденные при отдалении маркеры в координатах исходного вида."""
    items = overview.setdefault("dynamics", [])
    known = {item.get("fallback_id"): item for item in items}
    used_numbers = {item["index"] for item in items if item.get("index", 0) > 0}
    map_raw = overview.get("map") or {}
    mode = overview.get("coordinate_mode") or "latlng"
    added = 0
    for item in detailed.get("dynamics") or []:
        identity = item.get("fallback_id")
        lat = _maybe_float(item.get("lat"))
        lng = _maybe_float(item.get("lng"))
        if identity is None or lat is None or lng is None \
                or (in_area is not None and not in_area(lat, lng)):
            continue
        existing = known.get(identity)
        if existing is not None:
            if existing.get("index", 0) < 0 and item.get("index", 0) > 0 \
                    and item["index"] not in used_numbers:
                existing["index"] = item["index"]
                used_numbers.add(item["index"])
            continue
        point = latlng_to_container(lat, lng, base_view)
        if point is None:
            continue
        x, y = point
        if mode == "screen":
            x += float(map_raw.get("left") or 0)
            y += float(map_raw.get("top") or 0)
        elif mode == "percent":
            x = x / base_view.width * 100
            y = y / base_view.height * 100
        found = dict(item, x=round(x, 2), y=round(y, 2))
        if found.get("index", 0) > 0 and found["index"] in used_numbers:
            continue
        items.append(found)
        added += 1
        known[identity] = found
        if found.get("index", 0) > 0:
            used_numbers.add(found["index"])
    overview["unnumbered_dynamics"] = sum(item["index"] < 0 for item in items)
    return added


def _merge_numbered_scene(
    overview: dict[str, Any], detailed: dict[str, Any], used_numbers: set[int]
) -> None:
    """Привязывает номера на увеличенной карте к обзорным маркерам."""
    zoom = _maybe_float(detailed.get("map_zoom"))
    if zoom is None:
        return
    for visible in detailed.get("dynamics") or []:
        number = visible.get("index")
        if not isinstance(number, int) or number <= 0 or number in used_numbers:
            continue
        lat = _maybe_float(visible.get("lat"))
        lng = _maybe_float(visible.get("lng"))
        if lat is None or lng is None:
            continue
        visible_x, visible_y = project_3857(lat, lng, zoom)
        best: tuple[float, dict[str, Any]] | None = None
        for candidate in overview.get("dynamics") or []:
            if candidate.get("index", 0) >= 0:
                continue
            if visible.get("object_type") and candidate.get("object_type") \
                    and visible["object_type"] != candidate["object_type"]:
                continue
            candidate_lat = _maybe_float(candidate.get("lat"))
            candidate_lng = _maybe_float(candidate.get("lng"))
            if candidate_lat is None or candidate_lng is None:
                continue
            if visible.get("fallback_id") is not None and visible.get("fallback_id") == candidate.get("fallback_id"):
                distance = 0.0
            else:
                candidate_x, candidate_y = project_3857(candidate_lat, candidate_lng, zoom)
                distance = ((candidate_x - visible_x) ** 2 + (candidate_y - visible_y) ** 2) ** 0.5
            if distance <= 8 and (best is None or distance < best[0]):
                best = (distance, candidate)
        if best:
            best[1]["index"] = number
            used_numbers.add(number)


def objects_from_raw(items: list[dict[str, Any]] | None) -> list[SceneObject]:
    """Преобразует результат JavaScript/HTML-разбора в типизированные маркеры."""
    result: list[SceneObject] = []
    for index, item in enumerate(items or [], start=1):
        translate = item.get("translate3d")
        t3d = None
        if isinstance(translate, dict):
            t3d = (float(translate["x"]), float(translate["y"]))
        result.append(
            SceneObject(
                index=int(item.get("index", index)),
                name=str(item.get("name") or f"D{index}"),
                x=float(item["x"]),
                y=float(item["y"]),
                color=str(item.get("color") or ""),
                width=float(item.get("width") or 0.0),
                height=float(item.get("height") or 0.0),
                lat=_maybe_float(item.get("lat")),
                lng=_maybe_float(item.get("lng")),
                translate3d=t3d,
                object_type=str(item.get("object_type") or ""),
                fallback_id=(
                    int(item["fallback_id"])
                    if item.get("fallback_id") is not None else None
                ),
            )
        )
    return result


def statics_from_config(
    items: list[StaticObjectConfig],
    *,
    mode: CoordinateMode,
    map_size: tuple[float, float] | None,
    map_view: MapView | None = None,
) -> list[SceneObject]:
    """Строит маркеры статичных точек из конфигурации пользователя."""
    statics: list[SceneObject] = []
    for item in items:
        point = static_to_point(item, mode=mode, map_size=map_size, map_view=map_view)
        statics.append(
            SceneObject(
                index=item.id,
                name=item.name,
                x=point.x,
                y=point.y,
                lat=point.lat,
                lng=point.lng,
            )
        )
    return statics


def resolve_statics(
    tracking: TrackingConfig,
    page_statics: list[SceneObject],
    *,
    map_size: tuple[float, float] | None,
    map_view: MapView | None = None,
    projected_config: list[SceneObject] | None = None,
) -> list[SceneObject]:
    """Выбирает или объединяет статичные точки страницы и конфигурации."""
    mode = normalize_mode(tracking.coordinate_mode)
    configured = projected_config or statics_from_config(
        tracking.static_objects, mode=mode, map_size=map_size, map_view=map_view
    )
    source = tracking.static_source
    if source == "page":
        return page_statics
    if source == "both":
        merged = list(configured)
        offset = max((item.index for item in merged), default=0)
        for item in page_statics:
            merged.append(
                SceneObject(
                    index=offset + item.index,
                    name=f"{item.name} (страница)",
                    x=item.x,
                    y=item.y,
                    color=item.color,
                    width=item.width,
                    height=item.height,
                    lat=item.lat,
                    lng=item.lng,
                )
            )
        return merged
    return configured


def classify_distance(
    distance: float, tracking: TrackingConfig, *, unit: str = "px"
) -> Status:
    """Красный/жёлтый по настройкам, затем синяя зона до заданного порога."""
    if distance <= tracking.threshold:
        return "alert"
    if distance <= tracking.warning_limit:
        return "warning"
    if unit == "km" and distance <= tracking.blue_limit:
        return "blue"
    return "ok"


def build_distance_rows(
    statics: list[SceneObject],
    dynamics: list[SceneObject],
    tracking: TrackingConfig,
    *,
    mode: CoordinateMode,
) -> list[DistanceRow]:
    """Все пары точка–объект, сортировка по дистанции, счётчики кр./жёл. у точки."""
    unit = distance_unit(mode)
    rows: list[DistanceRow] = []

    # Кэш для телефонов, чтобы не искать каждый раз
    phone_cache: dict[str, str] = {}

    for static in statics:
        sp = Point2D(static.x, static.y, static.lat, static.lng)

        # Получаем телефон для статичной точки (с кэшированием)
        if static.name in phone_cache:
            phone = phone_cache[static.name]
        else:
            phone = get_phone_for_static(static.name) or ""
            phone_cache[static.name] = phone

        for dynamic in dynamics:
            dp = Point2D(dynamic.x, dynamic.y, dynamic.lat, dynamic.lng)
            try:
                distance = distance_between(sp, dp, mode)
            except ValueError:
                continue
            rows.append(
                DistanceRow(
                    static_index=static.index,
                    static_name=static.name,
                    static_x=static.x,
                    static_y=static.y,
                    static_lat=static.lat,
                    static_lng=static.lng,
                    dynamic_index=dynamic.index,
                    dynamic_name=_dynamic_label(dynamic),
                    dynamic_x=dynamic.x,
                    dynamic_y=dynamic.y,
                    dynamic_lat=dynamic.lat,
                    dynamic_lng=dynamic.lng,
                    dynamic_color=dynamic.color,
                    distance=distance,
                    status=classify_distance(distance, tracking, unit=unit),
                    distance_unit=unit,
                    phone=phone,
                )
            )
    rows.sort(key=lambda row: (row.distance, row.static_index, row.dynamic_index))
    _fill_nearby_counts(rows)
    return rows


def _fill_nearby_counts(rows: list[DistanceRow]) -> None:
    """Для каждой статичной точки считает, сколько до неё красных и жёлтых объектов."""
    alerts: dict[int, int] = {}
    warnings: dict[int, int] = {}
    for row in rows:
        if row.status == "alert":
            alerts[row.static_index] = alerts.get(row.static_index, 0) + 1
        elif row.status == "warning":
            warnings[row.static_index] = warnings.get(row.static_index, 0) + 1
    for row in rows:
        row.alert_count = alerts.get(row.static_index, 0)
        row.warning_count = warnings.get(row.static_index, 0)


def build_tracking_snapshot(
    *,
    url: str,
    title: str,
    raw: dict[str, Any],
    tracking: TrackingConfig,
) -> TrackingSnapshot:
    """Собирает снимок: объекты, пересчёт lat/lng, таблица расстояний."""
    mode = normalize_mode(raw.get("coordinate_mode") or tracking.coordinate_mode)
    dynamics = objects_from_raw(raw.get("dynamics"))
    page_statics = objects_from_raw(raw.get("statics"))
    projected_config = objects_from_raw(raw.get("config_statics"))
    viewport_raw = raw.get("viewport") or {}
    map_raw = raw.get("map") or {}
    map_view = map_view_from_raw(map_raw)
    map_size = None
    if map_raw.get("width") and map_raw.get("height"):
        map_size = (float(map_raw["width"]), float(map_raw["height"]))
    # Географию извлекаем всегда, даже если основная таблица настроена в px.
    # Это позволяет сохранять независимую историю расстояний в километрах.
    dynamics = [_ensure_geo(item, map_view) for item in dynamics]
    page_statics = [_ensure_geo(item, map_view) for item in page_statics]
    statics = resolve_statics(
        tracking,
        page_statics,
        map_size=map_size,
        map_view=map_view,
        projected_config=projected_config or None,
    )
    statics = [_ensure_geo(item, map_view) for item in statics]
    # Нулевой zoom корректен для Leaflet, поэтому нельзя выбирать значение через ``or``.
    raw_zoom = _maybe_float(raw.get("map_zoom"))
    map_zoom = (
        raw_zoom if raw_zoom is not None else (map_view.zoom if map_view else None)
    )
    geo_source = str(
        raw.get("geo_source") or (map_view.geo_source if map_view else "none")
    )
    scale_warning = _scale_warning(
        mode=mode,
        map_zoom=map_zoom,
        reference_zoom=tracking.reference_zoom,
        lock_scale=tracking.lock_scale,
        geo_source=geo_source,
        dynamics=dynamics,
        statics=statics,
    )
    unnumbered_dynamics = int(raw.get("unnumbered_dynamics") or 0)
    scan_status = str((raw.get("scan") or {}).get("status") or "")
    if scan_status in {"restore_failed", "mouse_restore_failed"}:
        scale_warning = (
            f"{scale_warning} После обхода не удалось подтвердить возврат карты "
            "к исходному центру и масштабу."
        ).strip()
    if unnumbered_dynamics:
        detail = {
            "no_leaflet_instance": "Экземпляр Leaflet не найден; обход карты не выполнен.",
            "map_controls_unavailable": "У карты недоступны команды смены масштаба и центра.",
            "partial": "Обход выполнен, но сайт не показал номера всех объектов.",
            "mouse_scan_unavailable": "У карты нет данных для обхода колесом браузера.",
            "mouse_wheel_unavailable": "Карта не меняет масштаб от колеса браузера.",
            "mouse_restore_failed": "После обхода не удалось восстановить исходный вид карты.",
            "restore_failed": "После обхода не удалось восстановить исходный вид карты.",
        }.get(scan_status, "")
        if (raw.get("scan") or {}).get("budget_exhausted"):
            detail = "Достигнут лимит времени или перемещений; показан частичный результат."
        number_warning = (
            f"Номера {unnumbered_dynamics} объектов не определены. {detail} "
            "Временные ID не являются номерами сайта."
        )
        scale_warning = f"{scale_warning} {number_warning}".strip()
    center = None
    if map_view and map_view.center_lat is not None and map_view.center_lng is not None:
        center = (map_view.center_lat, map_view.center_lng)
    return TrackingSnapshot(
        url=url,
        title=title,
        viewport=(
            float(viewport_raw.get("width") or 0.0),
            float(viewport_raw.get("height") or 0.0),
        ),
        dynamics=dynamics,
        statics=statics,
        rows=build_distance_rows(statics, dynamics, tracking, mode=mode),
        threshold=tracking.threshold,
        warning_limit=tracking.warning_limit,
        blue_limit=tracking.blue_limit,
        coordinate_mode=mode,
        distance_unit=distance_unit(mode),
        map_size=map_size,
        map_zoom=map_zoom,
        reference_zoom=tracking.reference_zoom or None,
        scale_warning=scale_warning,
        geo_source=geo_source,
        map_center=center,
        unnumbered_dynamics=unnumbered_dynamics,
        scan_status=scan_status,
    )


def _ensure_geo(item: SceneObject, map_view: MapView | None) -> SceneObject:
    """Дополняет маркер lat/lng, если вид карты допускает обратную проекцию."""
    if item.lat is not None and item.lng is not None:
        return item
    if not map_view:
        return item
    geo = container_to_latlng(item.x, item.y, map_view)
    if geo is None:
        return item
    return SceneObject(
        index=item.index,
        name=item.name,
        x=item.x,
        y=item.y,
        color=item.color,
        width=item.width,
        height=item.height,
        lat=geo[0],
        lng=geo[1],
        translate3d=item.translate3d,
        object_type=item.object_type,
    )


def _scale_warning(
    *,
    mode: CoordinateMode,
    map_zoom: float | None,
    reference_zoom: float | None,
    lock_scale: bool,
    geo_source: str = "none",
    dynamics: list[SceneObject] | None = None,
    statics: list[SceneObject] | None = None,
) -> str:
    """Формирует предупреждение о недоступной географии или несовпадении zoom."""
    if mode == "latlng":
        missing_dyn = sum(
            1 for item in dynamics or [] if item.lat is None or item.lng is None
        )
        missing_stat = sum(
            1 for item in statics or [] if item.lat is None or item.lng is None
        )
        if geo_source == "none" or missing_dyn or missing_stat:
            return (
                "Географические координаты не получены. Откройте живую Leaflet-карту "
                "или задайте lat/lng статичным точкам — иначе километры посчитать нельзя."
            )
        if geo_source == "proxy3857":
            return (
                "lat/lng восстановлены из leaflet-proxy (EPSG:3857). "
                "При зуме и сдвиге карты координаты пересчитываются; "
                "точнее будет живой экземпляр Leaflet."
            )
        return ""
    if not lock_scale or reference_zoom is None or map_zoom is None:
        if mode in {"map_relative", "percent"}:
            return (
                "Расстояния в px относительно карты. При изменении зума включите "
                "режим latlng (км) — тогда координаты пересчитаются сами."
            )
        return ""
    if abs(map_zoom - reference_zoom) > 0.01:
        return (
            f"Текущий zoom={map_zoom:g}, эталон={reference_zoom:g}. "
            "Расстояния в px зависят от масштаба — используйте latlng (км)."
        )
    return ""


def _dynamic_label(item: SceneObject) -> str:
    """В колонке объекта показывает тип, если он определён по маркеру."""
    return item.object_type if item.object_type and item.object_type != "Объект" else item.name


def css_color_to_hex(value: str) -> str:
    """Нормализует CSS-цвет ``rgb(...)`` или ``#RGB`` в шестнадцатеричную форму."""
    text = (value or "").strip().lower()
    if text.startswith("#") and len(text) in {4, 7}:
        return text.upper()
    if text.startswith("rgb"):
        inner = text[text.find("(") + 1 : text.rfind(")")]
        parts = [part.strip() for part in inner.split(",")][:3]
        try:
            channels = [max(0, min(255, int(float(part)))) for part in parts]
        except ValueError:
            return ""
        return f"#{channels[0]:02X}{channels[1]:02X}{channels[2]:02X}"
    return ""


def _fmt_point(x: float, y: float) -> str:
    """Форматирует пару плоских координат с одним знаком после запятой."""
    return f"({x:.1f}, {y:.1f})"


def _maybe_float(value: Any) -> float | None:
    """Преобразует заполненное значение в число, сохраняя ``None``."""
    if value is None or value == "":
        return None
    return float(value)


def row_to_dict(row: DistanceRow) -> dict[str, object]:
    """Сериализует строку расстояния для API, CSV и баз данных."""
    return {
        "static_index": row.static_index,
        "static_name": row.static_name,
        "static_x": round(row.static_x, 2),
        "static_y": round(row.static_y, 2),
        "static_lat": row.static_lat,
        "static_lng": row.static_lng,
        "static_coords": row.static_coords,
        "dynamic_index": row.dynamic_index,
        "dynamic_name": row.dynamic_name,
        "dynamic_x": round(row.dynamic_x, 2),
        "dynamic_y": round(row.dynamic_y, 2),
        "dynamic_lat": row.dynamic_lat,
        "dynamic_lng": row.dynamic_lng,
        "dynamic_coords": row.dynamic_coords,
        "dynamic_color": row.dynamic_color,
        "distance": round(row.distance, 3 if row.distance_unit == "km" else 2),
        "distance_unit": row.distance_unit,
        "status": row.status,
        "alert_count": row.alert_count,
        "warning_count": row.warning_count,
        "status_group_count": row.status_group_count,
        "nearby_counts": row.nearby_counts_label,
        "phone": row.phone,
    }


def snapshot_to_dict(snapshot: TrackingSnapshot) -> dict[str, object]:
    """Сериализует снимок мониторинга в JSON-совместимый словарь."""
    return {
        "url": snapshot.url,
        "title": snapshot.title,
        "viewport": {"width": snapshot.viewport[0], "height": snapshot.viewport[1]},
        "threshold": snapshot.threshold,
        "warning_limit": snapshot.warning_limit,
        "blue_limit": snapshot.blue_limit,
        "coordinate_mode": snapshot.coordinate_mode,
        "distance_unit": snapshot.distance_unit,
        "map_size": (
            {"width": snapshot.map_size[0], "height": snapshot.map_size[1]}
            if snapshot.map_size
            else None
        ),
        "map_zoom": snapshot.map_zoom,
        "reference_zoom": snapshot.reference_zoom,
        "scale_warning": snapshot.scale_warning,
        "unnumbered_dynamics": snapshot.unnumbered_dynamics,
        "scan_status": snapshot.scan_status,
        "geo_source": snapshot.geo_source,
        "map_center": (
            {"lat": snapshot.map_center[0], "lng": snapshot.map_center[1]}
            if snapshot.map_center
            else None
        ),
        "dynamics_count": len(snapshot.dynamics),
        "statics_count": len(snapshot.statics),
        "alerts": sum(1 for row in snapshot.rows if row.status == "alert"),
        "warnings": sum(1 for row in snapshot.rows if row.status == "warning"),
        "blues": sum(1 for row in snapshot.rows if row.status == "blue"),
        "dynamics": [
            {
                "index": item.index,
                "name": item.name,
                "x": round(item.x, 2),
                "y": round(item.y, 2),
                "lat": item.lat,
                "lng": item.lng,
                "translate3d": item.translate3d,
                "color": item.color,
                "object_type": item.object_type,
            }
            for item in snapshot.dynamics
        ],
        "rows": [row_to_dict(row) for row in snapshot.rows],
    }


def capture_snapshot(session: Any, tracking: TrackingConfig) -> TrackingSnapshot:
    """Снимает сырую сцену через браузерную сессию и строит итоговую таблицу."""
    page_snapshot, raw = session.capture_scene(tracking)
    return build_tracking_snapshot(
        url=page_snapshot.url,
        title=page_snapshot.title,
        raw=raw,
        tracking=tracking,
    )
