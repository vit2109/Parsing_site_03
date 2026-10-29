"""Подготовка географических центров для обхода стационарных точек."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import atan2, cos, degrees, radians, sin, sqrt
from typing import Iterable

from parsing_site.config import StaticObjectConfig
from parsing_site.coordinates import haversine_km


@dataclass(frozen=True, slots=True)
class StaticScanGroup:
    lat: float
    lng: float
    static_ids: tuple[int, ...]


def _center(points: Iterable[StaticObjectConfig]) -> tuple[float, float]:
    """Находит географический центр, включая точки по обе стороны меридиана 180°."""
    x = y = z = 0.0
    count = 0
    for point in points:
        lat, lng = radians(point.lat), radians(point.lng)
        x += cos(lat) * cos(lng)
        y += cos(lat) * sin(lng)
        z += sin(lat)
        count += 1
    x /= count
    y /= count
    z /= count
    return degrees(atan2(z, sqrt(x * x + y * y))), degrees(atan2(y, x))


def group_static_points(
    points: Iterable[StaticObjectConfig], radius_km: float,
) -> list[StaticScanGroup]:
    """Объединяет точки, если каждая не дальше радиуса от нового центра группы.

    Проверка нового центра не позволяет цепочке близких соседей растянуть одну
    группу на сотни километров за пределы выбранного радиуса.
    """
    valid = tuple(sorted(
        ((point.id, point.lat, point.lng) for point in points
         if point.lat is not None and point.lng is not None
         and -85 <= point.lat <= 85 and -180 <= point.lng <= 180),
        key=lambda point: (point[1], point[2], point[0]),
    ))
    return list(_group_static_points_cached(valid, radius_km))


@lru_cache(maxsize=64)
def _group_static_points_cached(
    coordinates: tuple[tuple[int, float, float], ...], radius_km: float,
) -> tuple[StaticScanGroup, ...]:
    valid = [StaticObjectConfig(index, "", 0, 0, lat, lng)
             for index, lat, lng in coordinates]
    groups: list[list[StaticObjectConfig]] = []
    for point in valid:
        best_index = None
        best_distance = float("inf")
        for index, members in enumerate(groups):
            center_lat, center_lng = _center((*members, point))
            if any(haversine_km(center_lat, center_lng, member.lat, member.lng) > radius_km
                   for member in (*members, point)):
                continue
            old_lat, old_lng = _center(members)
            distance = haversine_km(point.lat, point.lng, old_lat, old_lng)
            if distance < best_distance:
                best_index, best_distance = index, distance
        if best_index is None:
            groups.append([point])
        else:
            groups[best_index].append(point)
    return tuple(
        StaticScanGroup(*_center(members), tuple(point.id for point in members))
        for members in groups
    )
