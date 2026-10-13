"""
Съём сцены с карты и таблица расстояний.

Живая страница: JS в Playwright читает маркеры Leaflet и переводит их в lat/lng.
Снимок HTML: те же поля собирает html_probe без браузера.

Каждая строка таблицы — пара «статичная точка ↔ динамический объект».
После расчёта дистанций для каждой точки заполняются alert_count / warning_count:
сколько объектов красные и жёлтые относительно этой точки.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from parsing_site.config import StaticObjectConfig, TrackingConfig
from parsing_site.coordinates import (
    CoordinateMode,
    MapView,
    Point2D,
    container_to_latlng,
    distance_between,
    distance_unit,
    map_view_from_raw,
    normalize_mode,
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

  const isMap = (v) =>
    v && typeof v === "object" &&
    typeof v.getZoom === "function" &&
    (typeof v.containerPointToLatLng === "function" ||
     typeof v.latLngToContainerPoint === "function");

  const searchMap = (obj, depth) => {
    if (!obj || typeof obj !== "object" || depth > 5) return null;
    if (isMap(obj)) return obj;
    const keys = ["map", "leafletMap", "leafletObject", "mapObject", "_map"];
    for (const key of keys) {
      try { if (isMap(obj[key])) return obj[key]; } catch (e) {}
    }
    return null;
  };

  const findLeafletMap = (el) => {
    if (!el) return null;
    const direct = el._leaflet_map || el.__leaflet_map || el._leaflet || el.leafletMap;
    if (isMap(direct)) return direct;
    try {
      for (const key of Object.getOwnPropertyNames(el)) {
        if (isMap(el[key])) return el[key];
      }
    } catch (e) {}
    let vue = el.__vueParentComponent || el.__vue__;
    for (let i = 0; i < 30 && vue; i++) {
      const bags = [
        vue, vue.setupState, vue.ctx, vue.exposed, vue.data,
        vue.ctx && vue.ctx.map, vue.proxy,
      ];
      for (const bag of bags) {
        const found = searchMap(bag, 0);
        if (found) return found;
      }
      vue = vue.parent || (vue.$ && vue.$.parent);
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
      const ll = map.containerPointToLatLng({ x: cx, y: cy });
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
  const operatorNumbers = () => [...document.querySelectorAll(".leaflet-marker-icon.map-incident-operator-number")]
    .map((el) => {
      const match = String(el.textContent || "").trim().match(/^№?\\s*(\\d+)$/);
      const transform = parseTransform(el.getAttribute("style") || "");
      return match && transform
        ? { number: Number(match[1]), x: transform.x, y: transform.y }
        : null;
    })
    .filter((item) => item && Number.isInteger(item.number) && item.number > 0);

  const numberAtMarker = (box) => {
    const numbers = operatorNumbers();
    if (!box.translate3d || !numbers.length) return null;
    let nearest = null;
    numbers.forEach((item) => {
      const distance = (item.x - box.translate3d.x) ** 2 + (item.y - box.translate3d.y) ** 2;
      if (!nearest || distance < nearest.distance) nearest = { ...item, distance };
    });
    return nearest && nearest.distance <= 4 ? nearest.number : null;
  };

  // На малом масштабе сайт скрывает подписи, но сами маркеры остаются.
  // Отрицательная область ID не пересекается с положительными суточными номерами.
  const hashIdentity = (value) => {
    let hash = 5381;
    for (const char of String(value)) hash = ((hash * 33) ^ char.charCodeAt(0)) >>> 0;
    return -(hash % 1000000000 + 1);
  };
  const fallbackIndex = (el, box, objectType) => {
    const layer = map?._layers && Object.values(map._layers).find((item) => item?._icon === el);
    const sourceId = el.dataset.incidentId || el.dataset.objectId || el.dataset.id ||
      layer?.options?.incidentId || layer?.options?.objectId || layer?.options?.id ||
      layer?.incidentId || layer?.id || layer?._leaflet_id || el._leaflet_id;
    if (sourceId != null && String(sourceId).trim()) return hashIdentity(`id:${sourceId}`);
    const point = Number.isFinite(box.lat) && Number.isFinite(box.lng)
      ? `${box.lat.toFixed(4)}:${box.lng.toFixed(4)}`
      : `${box.translate3d?.x ?? box.x}:${box.translate3d?.y ?? box.y}`;
    return hashIdentity(`geo:${objectType}:${point}`);
  };

  const collect = (selector, prefix, requireNumber = false) =>
    selector
      ? [...document.querySelectorAll(selector)].map((el, index) => {
          const box = centerOf(el);
          const positionedNumber = numberAtMarker(box);
          const markerType = objectTypeFrom([...el.classList].join(" "));
          const displayedObject = Number.isInteger(positionedNumber)
            ? { number: positionedNumber, type: markerType }
            : null;
          const objectType = markerType !== "Объект"
            ? markerType
            : displayedObject?.type || "Объект";
          const objectIndex = Number.isInteger(displayedObject?.number)
            ? displayedObject.number
            : requireNumber ? fallbackIndex(el, box, objectType) : index + 1;
          return {
            index: objectIndex,
            fallback_id: requireNumber ? fallbackIndex(el, box, objectType) : null,
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
        }).filter((item, index, all) => item && (!requireNumber || all.findIndex((other) => other?.index === item.index) === index))
      : [];

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

  // Сначала собираем маркеры при отдалении, затем читаем подписи в увеличенном виде.
  // Позиции всех объектов в результате пересчитываются для исходного вида карты.
  if (opts.scanNumbers && map && typeof map.setView === "function" &&
      typeof map.getCenter === "function" && typeof map.latLngToContainerPoint === "function" &&
      Number.isFinite(zoom) && mapRect?.width && mapRect?.height) {
    const originalCenter = map.getCenter();
    const reportedMaxZoom = typeof map.getMaxZoom === "function" ? map.getMaxZoom() : Infinity;
    const maxZoom = Number.isFinite(reportedMaxZoom) ? reportedMaxZoom : zoom + 6;
    const reportedMinZoom = typeof map.getMinZoom === "function" ? map.getMinZoom() : -Infinity;
    const minZoom = Number.isFinite(reportedMinZoom) ? reportedMinZoom : zoom - 4;
    const targetZoom = Math.min(
      dynamics.some((item) => item.index > 0) ? zoom : zoom + 4, maxZoom
    );
    const attempted = new Set();
    const usedNumbers = new Set(dynamics.filter((item) => item.index > 0).map((item) => item.index));
    const moveTo = async (lat, lng, z) => {
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
      await new Promise((resolve) => setTimeout(resolve, 200));
    };
    const matchVisible = () => {
      const visible = collect(opts.dynamic, "D", true).filter((item) => item.index > 0);
      for (const item of visible) {
        if (usedNumbers.has(item.index) || !Number.isFinite(item.lat) || !Number.isFinite(item.lng)) continue;
        let best = null;
        for (const candidate of dynamics) {
          if (candidate.index >= 0 || !Number.isFinite(candidate.lat) || !Number.isFinite(candidate.lng)) continue;
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
        }
      }
      return visible.length;
    };
    try {
      for (let step = 1; step <= 4 && zoom - step >= minZoom; step++) {
        await moveTo(originalCenter.lat, originalCenter.lng, zoom - step);
        for (const item of collect(opts.dynamic, "D", true)) {
          if (!Number.isFinite(item.lat) || !Number.isFinite(item.lng)) continue;
          const duplicate = dynamics.some((existing) => existing.fallback_id === item.fallback_id);
          if (!duplicate) dynamics.push(item);
        }
      }
      dynamics.filter((item) => item.index > 0).forEach((item) => usedNumbers.add(item.index));
      if (targetZoom > zoom) {
        let scans = 0;
        for (const target of dynamics) {
          if (target.index >= 0 || !Number.isFinite(target.lat) || !Number.isFinite(target.lng) ||
              attempted.has(target.fallback_id) || scans >= 200) continue;
          await moveTo(target.lat, target.lng, targetZoom);
          scans++;
          let found = matchVisible();
          // На некоторых картах номера появляются только ещё на один-два уровня выше.
          if (!found && targetZoom < Math.min(zoom + 6, maxZoom)) {
            await moveTo(target.lat, target.lng, Math.min(zoom + 6, maxZoom));
            scans++;
            found = matchVisible();
          }
          for (const candidate of dynamics) {
            if (candidate.index >= 0 || !Number.isFinite(candidate.lat) || !Number.isFinite(candidate.lng)) continue;
            const p = map.latLngToContainerPoint([candidate.lat, candidate.lng]);
            if (p && p.x >= mapRect.width * 0.1 && p.x <= mapRect.width * 0.9 &&
                p.y >= mapRect.height * 0.1 && p.y <= mapRect.height * 0.9) {
              attempted.add(candidate.fallback_id);
            }
          }
        }
      } else if (dynamics.some((item) => item.index < 0)) {
        // Текущий масштаб уже показывает номера; обходим остальные области на нём.
        let scans = 0;
        for (const target of dynamics) {
          if (target.index >= 0 || !Number.isFinite(target.lat) || !Number.isFinite(target.lng) ||
              attempted.has(target.fallback_id) || scans >= 200) continue;
          await moveTo(target.lat, target.lng, targetZoom);
          scans++;
          matchVisible();
          for (const candidate of dynamics) {
            if (candidate.index >= 0 || !Number.isFinite(candidate.lat) || !Number.isFinite(candidate.lng)) continue;
            const p = map.latLngToContainerPoint([candidate.lat, candidate.lng]);
            if (p && p.x >= mapRect.width * 0.1 && p.x <= mapRect.width * 0.9 &&
                p.y >= mapRect.height * 0.1 && p.y <= mapRect.height * 0.9) {
              attempted.add(candidate.fallback_id);
            }
          }
        }
      }
    } finally {
      await moveTo(originalCenter.lat, originalCenter.lng, zoom);
      for (const item of dynamics) {
        if (!Number.isFinite(item.lat) || !Number.isFinite(item.lng)) continue;
        const p = map.latLngToContainerPoint([item.lat, item.lng]);
        if (!p) continue;
        const xy = xyForMode(mapRect.left + p.x, mapRect.top + p.y);
        item.x = round2(xy.x);
        item.y = round2(xy.y);
      }
    }
  }

  return {
    dynamics,
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


def extract_scene_from_page(page: Any, tracking: TrackingConfig) -> dict[str, Any]:
    """Снимает маркеры и вид карты из живой вкладки (JS _EXTRACT_SCRIPT)."""
    return page.evaluate(
        _EXTRACT_SCRIPT,
        {
            "dynamic": tracking.dynamic_selector,
            "static": tracking.static_selector,
            "mode": tracking.coordinate_mode,
            "mapContainer": tracking.map_container_selector,
            "scanNumbers": True,
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
    if unnumbered_dynamics:
        number_warning = (
            f"Для {unnumbered_dynamics} объектов подписи суточных номеров скрыты: "
            "в таблице и истории показаны временные ID. "
            "При появлении подписей снова используются номера сайта."
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
