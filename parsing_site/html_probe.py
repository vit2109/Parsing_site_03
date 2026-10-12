"""
Разбор сохранённого HTML (data_test/*.html) без живого сайта.

Из DOM читаются маркеры map-incident и leaflet-proxy; по EPSG:3857
восстанавливаются lat/lng и строится тот же TrackingSnapshot, что и с CDP.
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from parsing_site.config import TrackingConfig
from parsing_site.coordinates import (
    MapView,
    container_to_latlng,
    haversine_km,
    infer_webmercator_zoom,
    parse_css_transform,
    parse_translate3d,
    unproject_3857,
)
from parsing_site.tracking import TrackingSnapshot, build_tracking_snapshot

ROOT = Path(__file__).resolve().parent.parent


def analyze_html_file(path: Path) -> dict[str, object]:
    """Читает HTML-файл и возвращает диагностический отчёт о карте и маркерах."""
    html = path.read_text(encoding="utf-8", errors="replace")
    return analyze_html(html, source=str(path))


def analyze_html(html: str, *, source: str = "") -> dict[str, object]:
    """Анализирует DOM, Leaflet-маркеры и возможность восстановить географию."""
    soup = BeautifulSoup(html, "lxml")
    map_view = _map_view_from_soup(soup)
    pane_x, pane_y = _pane_offset(soup)
    markers = []
    for el in soup.find_all(True):
        style = el.get("style") or ""
        classes = " ".join(el.get("class") or [])
        translate = parse_translate3d(style)
        is_leaflet = "leaflet-marker-icon" in classes
        is_incident = "map-incident" in classes and is_leaflet
        if not translate and not is_leaflet:
            continue
        lat = lng = None
        if map_view and translate:
            geo = container_to_latlng(
                translate[0] + pane_x, translate[1] + pane_y, map_view
            )
            if geo:
                lat, lng = geo
        markers.append(
            {
                "tag": el.name,
                "classes": classes,
                "translate3d": translate,
                "width": _style_px(style, "width"),
                "height": _style_px(style, "height"),
                "margin_left": _style_px(style, "margin-left"),
                "margin_top": _style_px(style, "margin-top"),
                "lat": round(lat, 6) if lat is not None else None,
                "lng": round(lng, 6) if lng is not None else None,
                "is_leaflet_marker": is_leaflet,
                "is_map_incident": is_incident,
                "icon": el.find("img").get("src", "") if el.find("img") else "",
            }
        )

    leaflet = any(item["is_leaflet_marker"] for item in markers)
    incidents = [item for item in markers if item["is_map_incident"]]
    recommendation = _recommend(leaflet, incidents, map_view)

    sample_km: list[dict[str, object]] = []
    geo_incidents = [
        item
        for item in incidents
        if item["lat"] is not None and item["lng"] is not None
    ]
    if len(geo_incidents) >= 2:
        a, b = geo_incidents[0], geo_incidents[1]
        sample_km.append(
            {
                "from": a["translate3d"],
                "to": b["translate3d"],
                "km": round(haversine_km(a["lat"], a["lng"], b["lat"], b["lng"]), 3),
            }
        )

    return {
        "source": source,
        "markers_found": len(markers),
        "leaflet_detected": leaflet,
        "incidents_found": len(incidents),
        "map": _map_view_dict(map_view),
        "sample_distance_km": sample_km,
        "markers": markers,
        "recommendation": recommendation,
    }


def extract_scene_from_html(html: str, tracking: TrackingConfig) -> dict[str, object]:
    """Сцена в том же формате, что и живой extract Leaflet — без браузера."""
    soup = BeautifulSoup(html, "lxml")
    map_view = _map_view_from_soup(soup)
    pane_x, pane_y = _pane_offset(soup)
    dynamics = _collect_markers(
        soup,
        tracking.dynamic_selector or ".leaflet-marker-icon.map-incident",
        "D",
        map_view,
        pane_x,
        pane_y,
        displayed_objects=_daily_objects(soup),
    )
    page_statics = _collect_markers(
        soup,
        tracking.static_selector or "",
        "S",
        map_view,
        pane_x,
        pane_y,
    )
    map_raw = _map_view_dict(map_view)
    if map_raw is not None and map_view is not None:
        map_raw["has_leaflet"] = False
        map_raw["left"] = 0
        map_raw["top"] = 0
        map_raw["zoom"] = map_view.zoom
    return {
        "dynamics": dynamics,
        "statics": page_statics,
        "config_statics": [],
        "viewport": {
            "width": map_view.width if map_view else 0,
            "height": map_view.height if map_view else 0,
        },
        "map": map_raw,
        "map_zoom": map_view.zoom if map_view else None,
        "geo_source": map_view.geo_source if map_view else "none",
        "coordinate_mode": tracking.coordinate_mode,
    }


def snapshot_from_html_file(path: Path, tracking: TrackingConfig) -> TrackingSnapshot:
    """Строит таблицу расстояний по файлу HTML (без Chrome)."""
    html = path.read_text(encoding="utf-8", errors="replace")
    raw = extract_scene_from_html(html, tracking)
    return build_tracking_snapshot(
        url=str(path),
        title=path.name,
        raw=raw,
        tracking=tracking,
    )


def _collect_markers(
    soup: BeautifulSoup,
    selector: str,
    prefix: str,
    map_view: MapView | None,
    pane_x: float,
    pane_y: float,
    displayed_objects: list[tuple[int, str]] | None = None,
) -> list[dict[str, object]]:
    """Собирает подходящие маркеры и переводит их координаты в формат сцены."""
    if not selector.strip():
        return []
    items: list[dict[str, object]] = []
    for index, el in enumerate(soup.select(selector), start=1):
        style = el.get("style") or ""
        translate = parse_translate3d(style)
        if not translate:
            continue
        x = translate[0] + pane_x
        y = translate[1] + pane_y
        lat = lng = None
        if map_view:
            geo = container_to_latlng(x, y, map_view)
            if geo:
                lat, lng = round(geo[0], 6), round(geo[1], 6)
        displayed_object = (
            displayed_objects[index - 1]
            if displayed_objects and index <= len(displayed_objects)
            else None
        )
        displayed_number = displayed_object[0] if displayed_object else None
        object_type = displayed_object[1] if displayed_object else ""
        items.append(
            {
                "index": displayed_number or index,
                "name": (
                    object_type
                    if displayed_number is not None
                    else _marker_name(el, index, prefix)
                ),
                "object_type": object_type,
                "color": "",
                "x": round(x, 2),
                "y": round(y, 2),
                "lat": lat,
                "lng": lng,
                "width": _style_px(style, "width") or 0.0,
                "height": _style_px(style, "height") or 0.0,
                "translate3d": {"x": translate[0], "y": translate[1]},
            }
        )
    return items


def _daily_objects(soup: BeautifulSoup) -> list[tuple[int, str]]:
    """Читает суточные номера и типы сопровождаемых объектов из карточек."""
    objects: list[tuple[int, str]] = []
    for element in soup.select('[class*="_operatorNumber_"]'):
        match = re.fullmatch(r"№?\s*(\d+)", element.get_text(" ", strip=True))
        if match:
            number = int(match.group(1))
            card = _incident_card(element)
            object_type = _object_type(card.get_text(" ", strip=True) if card else "")
            if number > 0 and all(item[0] != number for item in objects):
                objects.append((number, object_type))
    if objects:
        return objects
    for card in soup.select('[class*="_card_"][class*="_isActive_"]'):
        match = re.search(
            r"(?:БПЛА|МВШ|ракет\w*|объект\w*)\s*№\s*(\d+)",
            card.get_text(" ", strip=True),
            re.IGNORECASE,
        )
        if match:
            number = int(match.group(1))
            if number > 0 and all(item[0] != number for item in objects):
                objects.append((number, _object_type(card.get_text(" ", strip=True))))
    return objects


def _incident_card(element: object) -> object | None:
    """Находит ближайшую карточку инцидента для номера оператора."""
    parent = getattr(element, "parent", None)
    while parent is not None:
        classes = [str(value) for value in (parent.get("class") or [])]
        if any("_card_" in value for value in classes):
            return parent
        parent = getattr(parent, "parent", None)
    return None


def _object_type(text: str) -> str:
    """Нормализует тип объекта из заголовка карточки сайта."""
    if re.search(r"БПЛА|aircraft-drone", text, re.IGNORECASE):
        return "БПЛА"
    if re.search(r"МВШ", text, re.IGNORECASE):
        return "МВШ"
    if re.search(r"ракет|missile|rocket", text, re.IGNORECASE):
        return "Ракета"
    return "Объект"


def _marker_name(el: object, index: int, prefix: str) -> str:
    """Определяет имя маркера по alt, CSS-классу, тексту или индексу."""
    img = el.find("img") if hasattr(el, "find") else None
    alt = (img.get("alt") or "").strip() if img else ""
    if alt:
        return alt
    classes = el.get("class") or []
    for cls in classes:
        text = str(cls)
        if text.startswith("map-incident_") and text != "map-incident_":
            return text.replace("map-incident_", "")
    label = " ".join(str(el.get_text(" ", strip=True)).split())
    if label:
        return label[:80]
    return f"{prefix}{index}"


def _map_view_from_soup(soup: BeautifulSoup) -> MapView | None:
    """Восстанавливает вид Leaflet из контейнера и элемента ``leaflet-proxy``."""
    container = soup.select_one(".leaflet-container")
    proxy_el = soup.select_one(".leaflet-proxy")
    if container is None or proxy_el is None:
        return None
    proxy = parse_css_transform(proxy_el.get("style") or "")
    if proxy is None:
        return None
    zoom = infer_webmercator_zoom(proxy)
    width, height = _container_size(soup)
    if zoom is None or width <= 0 or height <= 0:
        return None
    center_lat, center_lng = unproject_3857(proxy.x, proxy.y, zoom)
    return MapView(
        width=width,
        height=height,
        zoom=zoom,
        proxy_x=proxy.x,
        proxy_y=proxy.y,
        proxy_scale=proxy.scale,
        has_leaflet=False,
        geo_source="proxy3857",
        center_lat=center_lat,
        center_lng=center_lng,
    )


def _pane_offset(soup: BeautifulSoup) -> tuple[float, float]:
    """Возвращает смещение слоя маркеров относительно контейнера карты."""
    pane = soup.select_one(".leaflet-map-pane")
    if pane is None:
        return 0.0, 0.0
    transform = parse_css_transform(pane.get("style") or "")
    if transform is None:
        return 0.0, 0.0
    return transform.x, transform.y


def _container_size(soup: BeautifulSoup) -> tuple[float, float]:
    """Определяет размеры карты по SVG-слою сохранённой страницы."""
    for svg in soup.select(".leaflet-container svg.leaflet-zoom-animated"):
        view = svg.get("viewBox") or svg.get("viewbox") or ""
        parts = view.split()
        if len(parts) == 4:
            try:
                return float(parts[2]), float(parts[3])
            except ValueError:
                continue
        try:
            w = float(svg.get("width") or 0)
            h = float(svg.get("height") or 0)
            if w and h:
                return w, h
        except ValueError:
            continue
    return 0.0, 0.0


def _map_view_dict(view: MapView | None) -> dict[str, object] | None:
    """Сериализует вид карты в формат, общий с браузерным JavaScript-снимком."""
    if view is None:
        return None
    return {
        "width": view.width,
        "height": view.height,
        "zoom": view.zoom,
        "geo_source": view.geo_source,
        "center": {"lat": view.center_lat, "lng": view.center_lng},
        "proxy": {
            "x": view.proxy_x,
            "y": view.proxy_y,
            "scale": view.proxy_scale,
        },
    }


def _recommend(
    leaflet: bool,
    incidents: list[dict[str, object]],
    map_view: MapView | None,
) -> dict[str, str]:
    """Предлагает селекторы и режим координат по найденным элементам страницы."""
    if leaflet and incidents:
        extra = ""
        if map_view:
            extra = (
                f" Из снимка HTML восстановлен zoom≈{map_view.zoom:g} и центр "
                f"({map_view.center_lat:.2f}, {map_view.center_lng:.2f}). "
            )
        return {
            "dynamic_selector": ".leaflet-marker-icon.map-incident",
            "coordinate_mode": "latlng",
            "map_container_selector": ".leaflet-container",
            "note": (
                "Маркер Leaflet (map-incident): translate3d — экранные пиксели, "
                "они плывут при зуме и сдвиге. Для километров нужны lat/lng: "
                "живой map.containerPointToLatLng или восстановление EPSG:3857 "
                "из leaflet-proxy. Статичную точку задавайте lat/lng — при "
                "изменении масштаба пиксели пересчитаются, километры нет." + extra
            ),
        }
    if leaflet:
        return {
            "dynamic_selector": ".leaflet-marker-icon",
            "coordinate_mode": "latlng",
            "map_container_selector": ".leaflet-container",
            "note": "Leaflet-маркер. Режим latlng даёт километры и устойчив к зуму.",
        }
    return {
        "dynamic_selector": ".dynamic",
        "coordinate_mode": "screen",
        "map_container_selector": "",
        "note": "Обычный DOM-объект. Подойдут screen или map_relative.",
    }


def _style_px(style: str, prop: str) -> float | None:
    """Извлекает пиксельное значение указанного свойства из inline CSS."""
    match = re.search(rf"{re.escape(prop)}\s*:\s*(-?\d+(?:\.\d+)?)px", style)
    if not match:
        return None
    return float(match.group(1))
