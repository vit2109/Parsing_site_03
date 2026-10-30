"""Автономные сценарии карты и предпросмотр MAX без живого браузера и отправки."""

from __future__ import annotations

from dataclasses import replace
from math import asin, atan2, cos, degrees, radians, sin
from random import Random
from typing import Any

from parsing_site.config import StaticObjectConfig
from parsing_site.coordinates import haversine_km
from parsing_site.max_bot import MaxAlertEngine, SCREENSHOTS_DISABLED_NOTE
from parsing_site.scan_routes import group_static_points
from parsing_site.settings_store import MaxObjectRule, MaxRecipient, RuntimeSettings
from parsing_site.tracking import (
    DistanceRow, SceneObject, TrackingSnapshot, classify_distance,
)

PRESETS = {
    "northwest": [("Санкт-Петербург", 59.9386, 30.3141),
                  ("Псков", 57.8194, 28.3318), ("Великий Новгород", 58.5215, 31.2755)],
    "center": [("Москва", 55.7558, 37.6173), ("Тверь", 56.8587, 35.9176),
               ("Рязань", 54.6292, 39.7364)],
    "south": [("Белгород", 50.5954, 36.5873), ("Курск", 51.7373, 36.1874),
              ("Воронеж", 51.6608, 39.2003)],
}
DEMO_STATICS = [StaticObjectConfig(1, "Белгород", 0, 0, 50.5954, 36.5873),
                StaticObjectConfig(2, "Курск", 0, 0, 51.7373, 36.1874)]
ZONE_LABEL = {"alert": "Красная", "warning": "Жёлтая", "blue": "Синяя", "ok": "Вне зон"}


def _shift(lat: float, lng: float, distance_km: float, bearing: float) -> tuple[float, float]:
    angular = distance_km / 6371.0088
    la, lo, az = radians(lat), radians(lng), radians(bearing)
    new_lat = asin(sin(la) * cos(angular) + cos(la) * sin(angular) * cos(az))
    new_lng = lo + atan2(sin(az) * sin(angular) * cos(la),
                         cos(angular) - sin(la) * sin(new_lat))
    return max(-85, min(85, degrees(new_lat))), (degrees(new_lng) + 180) % 360 - 180


def _valid_statics(settings: RuntimeSettings, recent: dict[str, Any] | None) -> tuple[list[StaticObjectConfig], str]:
    points = [point for point in settings.static_objects if point.lat is not None
              and point.lng is not None and -85 <= point.lat <= 85 and -180 <= point.lng <= 180]
    if points:
        return points, "Сохранённые стационарные объекты"
    historical = (recent or {}).get("statics") or []
    points = [StaticObjectConfig(int(row["static_index"]), str(row["static_name"]), 0, 0,
                                 float(row["lat"]), float(row["lng"])) for row in historical
              if row.get("lat") is not None and row.get("lng") is not None
              and -85 <= float(row["lat"]) <= 85 and -180 <= float(row["lng"]) <= 180]
    return (points, "Стационарные объекты последнего опроса") if points else (
        DEMO_STATICS, "Демонстрационные точки из тестовой карты")


def _anchors(area: str, statics: list[StaticObjectConfig],
             recent: dict[str, Any] | None) -> tuple[list[tuple[str, float, float]], str]:
    static_anchors = [(point.name, float(point.lat), float(point.lng)) for point in statics]
    recent_anchors = [(str(row.get("dynamic_name") or "Последний опрос"),
                       float(row["lat"]), float(row["lng"]))
                      for row in ((recent or {}).get("dynamics") or [])[:30]
                      if row.get("lat") is not None and row.get("lng") is not None
                      and -85 <= float(row["lat"]) <= 85 and -180 <= float(row["lng"]) <= 180]
    if area == "recent" and recent_anchors:
        return recent_anchors, "Положения последнего живого опроса"
    if area in PRESETS:
        return PRESETS[area], "Демонстрационные географические районы"
    if area == "mixed":
        return static_anchors + recent_anchors + PRESETS["northwest"] + PRESETS["center"] + PRESETS["south"], (
            "Стационарные точки, последний опрос и демонстрационные районы")
    return static_anchors, "Районы сохранённых стационарных объектов"


def _band_distance(band: int, settings: RuntimeSettings, rng: Random) -> float:
    red = max(0.1, settings.threshold)
    yellow = max(red, settings.warning_distance)
    blue = max(yellow, settings.blue_distance)
    if band == 0:
        return red * rng.uniform(0.12, 0.88)
    if band == 1 and yellow > red:
        return red + (yellow - red) * rng.uniform(0.15, 0.85)
    if band == 2 and blue > yellow:
        return yellow + (blue - yellow) * rng.uniform(0.15, 0.85)
    return blue + rng.uniform(25, 180)


def _make_routes(objects: list[dict[str, Any]], statics: list[StaticObjectConfig],
                 settings: RuntimeSettings, group_radius: float) -> dict[str, list[dict[str, Any]]]:
    zoom = settings.scan_max_zoom
    radius = max(20.0, 70.0 * 2 ** (10 - zoom))
    entries: dict[str, list[tuple[str, float, float]]] = {
        "static_points": [(point.name, float(point.lat), float(point.lng)) for point in statics],
        "grouped_static": [(f"Группа {i + 1} · {len(group.static_ids)} точек", group.lat, group.lng)
                           for i, group in enumerate(group_static_points(statics, group_radius))],
        "zones": [(zone.name, zone.lat, zone.lng) for zone in settings.scan_zones] or [
            (f"Демо сектор · {point.name}", float(point.lat), float(point.lng)) for point in statics[:8]],
    }
    clusters: list[list[dict[str, Any]]] = []
    for item in objects:
        match = next((cluster for cluster in clusters if haversine_km(
            item["lat"], item["lng"], cluster[0]["lat"], cluster[0]["lng"]) <= radius), None)
        if match is None:
            clusters.append([item])
        else:
            match.append(item)
    entries["adaptive"] = [(f"Группа маркеров {i + 1} · {len(cluster)} объектов",
                            sum(item["lat"] for item in cluster) / len(cluster),
                            sum(item["lng"] for item in cluster) / len(cluster))
                           for i, cluster in enumerate(clusters)]
    routes: dict[str, list[dict[str, Any]]] = {}
    for mode, centers in entries.items():
        if centers:
            ordered = [centers[0]]
            remaining = centers[1:]
            while remaining:
                previous = ordered[-1]
                nearest = min(range(len(remaining)), key=lambda i: haversine_km(
                    previous[1], previous[2], remaining[i][1], remaining[i][2]))
                ordered.append(remaining.pop(nearest))
            centers = ordered
        steps = []
        for title, lat, lng in centers[:32]:
            covered = [item["id"] for item in objects
                       if haversine_km(lat, lng, item["lat"], item["lng"]) <= radius]
            steps.append({"title": title, "lat": lat, "lng": lng, "zoom": zoom,
                          "radius_km": round(radius, 1), "object_ids": covered})
        routes[mode] = steps

    # Each generated object remains a target. The UI skips a target when an
    # earlier viewport has already revealed its number, but never drops an
    # uncovered target because of the normal 32-move scan budget.
    remaining = objects[:]
    ordered_objects: list[dict[str, Any]] = []
    while remaining:
        if not ordered_objects:
            nearest = 0
        else:
            previous = ordered_objects[-1]
            nearest = min(range(len(remaining)), key=lambda i: haversine_km(
                previous["lat"], previous["lng"], remaining[i]["lat"], remaining[i]["lng"]))
        ordered_objects.append(remaining.pop(nearest))
    routes["full_coverage"] = []
    for item in ordered_objects:
        reveal_zoom = item["reveal_zoom"]
        routes["full_coverage"].append({
            "title": f"Объект №{item['id']}",
            "lat": item["lat"], "lng": item["lng"],
            "zoom": reveal_zoom,
            "radius_km": 0,
            "target_id": item["id"],
            "object_ids": [item["id"]],
        })
    return routes


def build_scenario(settings: RuntimeSettings, recent: dict[str, Any] | None, *,
                   count: int, area: str, distribution: str, seed: int,
                   reveal_zoom: int, group_radius: float,
                   red_km: float | None = None, yellow_km: float | None = None,
                   blue_km: float | None = None) -> dict[str, Any]:
    """Создаёт воспроизводимый сценарий; не пишет данные и не отправляет MAX."""
    red = settings.threshold if red_km is None else red_km
    yellow = settings.warning_distance if yellow_km is None else yellow_km
    blue = settings.blue_distance if blue_km is None else blue_km
    if not 0 < red <= yellow <= blue <= 20_000:
        raise ValueError("Укажите пороги зон по возрастанию: красная ≤ жёлтая ≤ синяя.")
    settings = replace(settings, threshold=red, warning_distance=yellow, blue_distance=blue)
    rng = Random(seed)
    statics, static_source = _valid_statics(settings, recent)
    anchors, anchor_source = _anchors(area, statics, recent)
    tracking = settings.to_app_config().tracking
    bands = {"balanced": [0, 1, 2, 3], "close": [0, 0, 1, 2],
             "wide": [1, 2, 3, 3]}[distribution]
    objects: list[dict[str, Any]] = []
    for i in range(count):
        name, lat, lng = anchors[i % len(anchors)]
        # Возле стационарной точки расстояние выбирается из заданных зон.
        distance = (_band_distance(bands[i % 4], settings, rng) if area == "static" else
                    rng.uniform(*{"balanced": (10, 90), "close": (2, 35),
                                  "wide": (80, 200)}[distribution]))
        point_lat, point_lng = _shift(lat, lng, distance, rng.uniform(0, 360))
        objects.append({"id": 1001 + i, "name": "БПЛА", "lat": round(point_lat, 6),
                        "lng": round(point_lng, 6), "region": name,
                        "reveal_zoom": max(4, min(18, reveal_zoom + rng.choice((-1, 0, 0, 1))))})

    zone_counts = {"alert": 0, "warning": 0, "blue": 0, "ok": 0}
    static_counts = {point.id: {"alert": 0, "warning": 0, "blue": 0} for point in statics}
    sample_ids: list[int] = []
    preview_rules = settings.max_recipients or [MaxRecipient(0, [
        MaxObjectRule(point.id, ["alert", "warning", "blue"]) for point in statics])]
    allowed: dict[int, set[str]] = {}
    for recipient in preview_rules:
        for rule in recipient.rules:
            allowed.setdefault(rule.static_index, set()).update(rule.zones)
    sample_rows: list[DistanceRow] = []
    for item in objects:
        nearest = (float("inf"), "ok")
        distances = []
        eligible = False
        for point in statics:
            distance = haversine_km(float(point.lat), float(point.lng), item["lat"], item["lng"])
            status = classify_distance(distance, tracking, unit="km")
            if status in static_counts[point.id]:
                static_counts[point.id][status] += 1
            if distance < nearest[0]:
                nearest = distance, status
            if status in allowed.get(point.id, set()):
                eligible = True
            distances.append((point, distance, status))
        zone_counts[nearest[1]] += 1
        item["zone"] = nearest[1]
        item["nearest_km"] = round(nearest[0], 1)
        if eligible and len(sample_ids) < 3:
            sample_ids.append(item["id"])
            for point, distance, status in distances:
                sample_rows.append(DistanceRow(
                    static_index=point.id, static_name=point.name, static_x=0, static_y=0,
                    static_lat=point.lat, static_lng=point.lng,
                    dynamic_index=item["id"], dynamic_name=item["name"],
                    dynamic_x=0, dynamic_y=0, dynamic_lat=item["lat"], dynamic_lng=item["lng"],
                    dynamic_color="", distance=distance, status=status, distance_unit="km"))

    sample = [SceneObject(item["id"], item["name"], 0, 0,
                          lat=item["lat"], lng=item["lng"], object_type=item["name"])
              for item in objects if item["id"] in sample_ids]
    snapshot = TrackingSnapshot("simulator", "Тренажёр", (0, 0), sample, [], sample_rows,
                                tracking.threshold, tracking.warning_limit,
                                coordinate_mode="latlng", distance_unit="km",
                                blue_limit=tracking.blue_limit)
    simulated = replace(settings, max_enabled=True, max_token="SIMULATOR",
                        screenshot_enabled=False, max_recipients=preview_rules)
    previews = MaxAlertEngine().collect(
        snapshot, simulated, [], zone_counts_override=static_counts,
        total_dynamics_override=len(objects))
    preview = [{"user_id": item.user_id if item.user_id else None,
                "text": item.text.removesuffix("\n" + SCREENSHOTS_DISABLED_NOTE),
                "dynamic_id": item.dynamic_index} for item in previews[:3]]
    routes = _make_routes(objects, statics, settings, group_radius)
    display_zones = ([{"name": zone.name, "lat": zone.lat, "lng": zone.lng,
                       "radius_km": zone.radius_km} for zone in settings.scan_zones]
                     if settings.scan_zones else [
                         {"name": f"Демо сектор · {point.name}", "lat": point.lat,
                          "lng": point.lng, "radius_km": max(red, yellow, blue)}
                         for point in statics[:8]])
    return {
        "objects": objects,
        "statics": [{"id": point.id, "name": point.name, "lat": point.lat, "lng": point.lng,
                     "counts": static_counts[point.id]} for point in statics],
        "zones": display_zones,
        "routes": routes, "zone_counts": zone_counts, "preview": preview,
        "thresholds": {"alert": tracking.threshold, "warning": tracking.warning_limit,
                       "blue": tracking.blue_limit},
        "scan_max_zoom": settings.scan_max_zoom,
        "source": f"{static_source}; {anchor_source}",
        "preview_source": "Текущие правила MAX" if settings.max_recipients else "Демонстрационное правило для всех зон",
        "preview_message": "Событий по выбранным зонам нет" if not preview else "",
        "route_limit": 32,
    }
