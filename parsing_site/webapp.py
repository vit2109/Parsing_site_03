"""HTTP API и страница мониторинга (FastAPI + шаблоны web/)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from parsing_site.config import ROOT, WEB_DIR, load_config
from parsing_site.db import Database
from parsing_site.history_capture import capture_history_maps
from parsing_site.history_export import build_history_workbook
from parsing_site.history_report import build_history_report
from parsing_site.html_probe import analyze_html_file
from parsing_site.settings_store import SettingsStore, from_dict, resolve_max_token
from parsing_site.table_history_db import TableHistoryDatabase
from parsing_site.worker import ParseWorker

db = Database()
table_history_db = TableHistoryDatabase()
store = SettingsStore(db)
worker = ParseWorker(db, store, table_history_db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Инициализирует данные при старте и освобождает ресурсы при остановке ASGI."""
    db.mark_interrupted_max_deliveries()
    store.ensure_defaults(load_config())
    db.prune_dynamic_history()
    table_history_db.prune_dynamic_history()
    db.ensure_fake_history()
    db.add_log("INFO", "web", "Интерфейс запущен")
    try:
        yield
    finally:
        # Освобождение выполняется и при исключении внутри жизненного цикла ASGI.
        if worker.status["running"]:
            worker.stop()
        table_history_db.close()
        db.close()


app = FastAPI(title="Парсинг объектов", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class ScanZoneIn(BaseModel):
    name: str = Field(min_length=1)
    lat: float = Field(ge=-85, le=85)
    lng: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=350, ge=1, le=2000)


class SettingsIn(BaseModel):
    """Тело запроса сохранения настроек с базовыми ограничениями значений."""

    site: str = Field(min_length=1)
    cdp_url: str = "http://127.0.0.1:9222"
    interval_seconds: float = Field(ge=0.4, le=600)
    scan_wait_seconds: float = Field(default=1.5, ge=0.2, le=10)
    scan_max_zoom: int = Field(default=10, ge=4, le=18)
    scan_mode: str = "adaptive"
    scan_group_radius_km: float = Field(default=50, ge=1, le=500)
    scan_zones: list[ScanZoneIn] = Field(default_factory=list)
    screenshot_enabled: bool = False
    screenshot_dir: str = "data/screenshots"
    screenshot_period_hours: int = 1
    screenshot_width: int = Field(default=1280, ge=320, le=3840)
    screenshot_height: int = Field(default=720, ge=240, le=2160)
    archive_html_enabled: bool = False
    archive_html_dir: str = "data/html"
    archive_html_period_hours: int = 1
    max_enabled: bool = False
    max_token: str = ""
    max_display_distance_km: float = Field(default=150.0, gt=0, le=20_000)
    max_token_clear: bool = False
    max_recipients: list[dict[str, Any]] = Field(default_factory=list)
    max_groups: list[dict[str, Any]] | None = None
    threshold: float = Field(gt=0)
    warning_distance: float = Field(gt=0)
    blue_distance: float = Field(default=350.0, gt=0)
    alert_color: str
    warning_color: str
    blue_color: str = "#2878b8"
    sound_enabled: bool = True
    wait_for_selector: str = ".leaflet-marker-icon.map-incident"
    dynamic_selector: str = ".leaflet-marker-icon.map-incident"
    coordinate_mode: str = "latlng"
    map_coordinate_precision: int = Field(default=4, ge=3, le=6)
    map_container_selector: str = ".leaflet-container"
    reference_zoom: float = 0.0
    lock_scale: bool = False
    static_source: str = "config"
    static_objects: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Отдаёт основную страницу мониторинга с текущими настройками."""
    settings = store.current()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"settings": settings.public_dict()},
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    """Отдаёт страницу фильтрации и визуализации хронологии."""
    return templates.TemplateResponse(request, "history.html", {})


@app.get("/max-logs", response_class=HTMLResponse)
def max_logs_page(request: Request) -> HTMLResponse:
    """Отдаёт журнал доставки уведомлений MAX."""
    return templates.TemplateResponse(request, "max_logs.html", {})


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    """Возвращает единый снимок настроек, worker, таблицы, хранилища и логов."""
    settings = store.current()
    status = worker.status
    snapshot = status["snapshot"]
    current_rows = len(snapshot.get("rows", [])) if snapshot else None
    return {
        "settings": settings.public_dict(),
        "worker": {
            "running": status["running"],
            "status": status["status"],
            "error": status["error"],
            "polls": status["polls"],
        },
        "snapshot": snapshot,
        "table_history": table_history_db.storage_stats(
            interval_seconds=settings.interval_seconds,
            current_rows=current_rows,
        ),
        "logs": db.list_logs(40),
    }


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    """Возвращает сохранённые runtime-настройки."""
    return store.current().public_dict()


@app.put("/api/settings")
def api_put_settings(payload: SettingsIn) -> dict[str, Any]:
    """Проверяет и полностью заменяет runtime-настройки переданного PUT-запроса."""
    data = payload.model_dump()
    if data.pop("max_token_clear"):
        data["max_token"] = ""
    elif not data["max_token"].strip():
        data["max_token"] = store.current().max_token
    try:
        settings = store.save(from_dict(data))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add_log("INFO", "settings", f"Настройки сохранены: {settings.site}")
    return settings.public_dict()


@app.post("/api/worker/start")
def api_start() -> dict[str, Any]:
    """Запускает фоновый опрос и возвращает его состояние."""
    worker.start()
    return worker.status


@app.post("/api/worker/stop")
def api_stop() -> dict[str, Any]:
    """Останавливает фоновый опрос и возвращает его состояние."""
    worker.stop()
    return worker.status


@app.get("/api/logs")
def api_logs(limit: int = 80) -> list[dict[str, Any]]:
    """Возвращает ограниченный список последних событий приложения."""
    return db.list_logs(limit)


class MaxTestIn(BaseModel):
    user_id: int = Field(gt=0, le=9223372036854775807)


@app.get("/api/max/logs")
def api_max_logs(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, pattern="^(queued|sent|error|skipped)$"),
    user_id: int | None = Query(default=None, gt=0),
) -> dict[str, Any]:
    """Возвращает записи отправки MAX с фильтрами и постраничной выдачей."""
    return db.list_max_deliveries(limit, offset, status, user_id)


@app.get("/api/max/status")
def api_max_status() -> dict[str, Any]:
    """Показывает условия автоматической рассылки без раскрытия токена."""
    settings = store.current()
    state = worker.status
    return {
        "enabled": settings.max_enabled,
        "token_set": bool(resolve_max_token(settings)),
        "recipients": len(settings.max_recipients),
        "rules": sum(len(item.rules) for item in settings.max_recipients),
        "worker_running": state["running"],
        "worker_status": state["status"],
        "worker_error": state["error"],
        "last_poll": state["max_poll"],
    }


@app.post("/api/max/test")
def api_max_test(payload: MaxTestIn) -> dict[str, Any]:
    """Отправляет тестовое сообщение через общую очередь MAX."""
    try:
        delivery_id = worker.send_max_test(payload.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": delivery_id, "status": "queued", "user_id": payload.user_id}


@app.get("/api/movements")
def api_movements(limit: int = 200) -> list[dict[str, Any]]:
    """Возвращает последние перемещения динамических объектов."""
    return db.list_movements(limit)


@app.get("/api/history/static-objects")
def api_history_statics(
    source: str = Query(default="real", pattern="^(real|fake)$"),
) -> list[dict[str, Any]]:
    """Возвращает доступные статичные точки и объём их истории."""
    historical = {
        (int(item["static_index"]), str(item["static_name"])): item
        for item in db.list_history_statics(source)
    }
    if source == "real":
        settings = store.current()
        for item in settings.static_objects:
            key = (item.id, item.name)
            historical.setdefault(
                key,
                {
                    "static_index": item.id,
                    "static_name": item.name,
                    "static_lat": item.lat,
                    "static_lng": item.lng,
                    "first_seen": None,
                    "last_seen": None,
                    "samples": 0,
                    "km_samples": 0,
                    "legacy_samples": 0,
                    "alert_radius_km": (
                        settings.threshold
                        if settings.coordinate_mode == "latlng"
                        else None
                    ),
                    "warning_radius_km": (
                        settings.warning_distance
                        if settings.coordinate_mode == "latlng"
                        else None
                    ),
                    "blue_radius_km": (
                        settings.blue_distance
                        if settings.coordinate_mode == "latlng"
                        else None
                    ),
                },
            )
    return sorted(
        historical.values(),
        key=lambda item: (str(item["static_name"]).lower(), int(item["static_index"])),
    )


@app.get("/api/history")
def api_history(
    static_index: int | None = Query(default=None, gt=0),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    time_from: str = Query(default="00:00", pattern=r"^\d{2}:\d{2}$"),
    time_to: str = Query(default="23:59", pattern=r"^\d{2}:\d{2}$"),
    max_distance_km: float = Query(gt=0, le=20_000),
    dynamic: str = Query(default="", max_length=200),
    limit: int = Query(default=5000, ge=1, le=20_000),
    source: str = Query(default="real", pattern="^(real|fake)$"),
) -> dict[str, Any]:
    """Фильтрует историю сближений и формирует сводку по объектам."""
    start, end = _history_bounds(date_from, date_to, time_from, time_to)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    static_index, dynamic = _history_search_scope(static_index, dynamic)
    rows = db.list_distance_history(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(),
        max_distance_km=max_distance_km,
        dynamic_query=dynamic,
        limit=limit,
        data_source=source,
    )
    legacy_records = db.count_legacy_history(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(),
        dynamic_query=dynamic,
        data_source=source,
    )
    dynamics: dict[tuple[str, int, str], dict[str, Any]] = {}
    status_rank = {"ok": 0, "blue": 1, "warning": 2, "alert": 3}
    for row in rows:
        key = (
            str(row["object_date"]),
            int(row["dynamic_index"]),
            str(row["dynamic_name"]),
        )
        current = dynamics.setdefault(
            key,
            {
                "dynamic_index": row["dynamic_index"],
                "dynamic_name": row["dynamic_name"],
                "object_date": row["object_date"],
                "events": 0,
                "min_distance_km": float(row["distance"]),
                "last_seen": row["ts"],
                "status": row["status"],
            },
        )
        current["events"] += 1
        current["min_distance_km"] = min(
            float(current["min_distance_km"]), float(row["distance"])
        )
        if status_rank.get(str(row["status"]), 0) > status_rank.get(
            str(current["status"]), 0
        ):
            current["status"] = row["status"]
    summary = sorted(
        dynamics.values(),
        key=lambda item: (item["min_distance_km"], item["dynamic_name"]),
    )
    for item in summary:
        item["min_distance_km"] = round(float(item["min_distance_km"]), 3)
    return {
        "rows": rows,
        "summary": summary,
        "total": len(rows),
        "unique_dynamics": len(summary),
        "truncated": len(rows) >= limit,
        "unit": "km",
        "max_distance_km": max_distance_km,
        "legacy_records": legacy_records,
        "database": db.path.name,
        "source": source,
        "notice": (
            f"В выбранном периоде найдено {legacy_records} старых записей в пикселях. "
            "Они не показаны как километры, потому что географические координаты "
            "в них не сохранялись. Для новой истории включите режим lat/lng."
            if legacy_records
            else ""
        ),
    }


@app.get("/api/history/export.xlsx")
def api_history_export(
    static_index: int | None = Query(default=None, gt=0),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    time_from: str = Query(default="00:00", pattern=r"^\d{2}:\d{2}$"),
    time_to: str = Query(default="23:59", pattern=r"^\d{2}:\d{2}$"),
    max_distance_km: float = Query(gt=0, le=20_000),
    dynamic: str = Query(default="", max_length=200),
    source: str = Query(default="real", pattern="^(real|fake)$"),
) -> Response:
    """Выгружает выбранную историю сближений в XLSX."""
    start, end = _history_bounds(date_from, date_to, time_from, time_to)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    static_index, dynamic = _history_search_scope(static_index, dynamic)
    rows = db.list_distance_history(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(),
        max_distance_km=max_distance_km,
        dynamic_query=dynamic,
        limit=20_000,
        data_source=source,
    )
    content = build_history_workbook(
        rows,
        date_from=date_from,
        date_to=date_to,
        static_index=static_index,
        max_distance_km=max_distance_km,
        dynamic_query=dynamic,
        source=source,
        time_from=time_from,
        time_to=time_to,
    )
    filename = f"chronology_{date_from}_{date_to}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/history/report.docx")
def api_history_report(
    static_index: int | None = Query(default=None, gt=0),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    time_from: str = Query(default="00:00", pattern=r"^\d{2}:\d{2}$"),
    time_to: str = Query(default="23:59", pattern=r"^\d{2}:\d{2}$"),
    max_distance_km: float = Query(gt=0, le=20_000),
    dynamic: str = Query(default="", max_length=200),
    source: str = Query(default="real", pattern="^(real|fake)$"),
    map_mode: str = Query(default="geographic", pattern="^(geographic|offline)$"),
) -> Response:
    """Формирует справку Word по фильтрам хронологии с картами зон и маршрута."""
    start, end = _history_bounds(date_from, date_to, time_from, time_to)
    if start > end:
        raise HTTPException(status_code=400, detail="Начало периода позже его окончания.")
    _, dynamic = _history_search_scope(None, dynamic)
    start_iso, end_iso = start.isoformat(timespec="seconds"), end.isoformat()
    rows = db.list_distance_history(
        static_index=static_index,
        date_from=start_iso,
        date_to=end_iso,
        max_distance_km=max_distance_km,
        dynamic_query=dynamic,
        limit=20_000,
        data_source=source,
    )
    settings = store.current()
    static_scene = None
    if static_index is not None:
        static_scene = db.get_static_zone_trajectories(
            static_index=static_index,
            date_from=start_iso,
            date_to=end_iso,
            data_source=source,
            limit=5_000,
            coordinate_precision=settings.map_coordinate_precision,
        )
    dynamic_points = None
    if dynamic.lstrip("-").isdigit():
        dynamic_points = db.list_dynamic_trajectory(
            dynamic_index=int(dynamic),
            date_from=start_iso,
            date_to=end_iso,
            data_source=source,
            limit=5_000,
            coordinate_precision=settings.map_coordinate_precision,
        )
    statics = db.list_history_statics(source) if dynamic_points is not None else None
    try:
        map_images = capture_history_maps(
            static_scene=static_scene, dynamic_points=dynamic_points,
            statics=statics, rows=rows, map_mode=map_mode,
        )
    except (PlaywrightError, OSError, RuntimeError, ValueError) as exc:
        db.add_log("WARNING", "history", f"Снимок карты для справки недоступен: {exc}")
        map_images = {}
    content = build_history_report(
        rows,
        date_from=date_from, date_to=date_to,
        time_from=time_from, time_to=time_to,
        source=source, max_distance_km=max_distance_km,
        static_index=static_index, dynamic_query=dynamic,
        static_scene=static_scene, dynamic_points=dynamic_points,
        statics=statics, map_images=map_images,
    )
    filename = f"history_report_{date_from}_{date_to}.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/history/trajectory/{dynamic_index}")
def api_history_trajectory(
    dynamic_index: int,
    object_date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    time_from: str = Query(default="00:00", pattern=r"^\d{2}:\d{2}$"),
    time_to: str = Query(default="23:59", pattern=r"^\d{2}:\d{2}$"),
    source: str = Query(default="real", pattern="^(real|fake)$"),
    limit: int = Query(default=1_500, ge=1, le=20_000),
) -> dict[str, Any]:
    """Возвращает географическую траекторию одного динамического объекта."""
    start, end = _history_bounds(date_from, date_to, time_from, time_to)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    coordinate_precision = store.current().map_coordinate_precision
    points = db.list_dynamic_trajectory(
        dynamic_index=dynamic_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(),
        object_date=object_date,
        data_source=source,
        limit=limit + 1,
        coordinate_precision=coordinate_precision,
    )
    if not points:
        raise HTTPException(
            status_code=404,
            detail="Для объекта нет точек с географическими координатами за этот период.",
        )
    truncated = len(points) > limit
    points = points[:limit]
    return {
        "dynamic_index": dynamic_index,
        "dynamic_name": points[-1]["dynamic_name"],
        "object_date": object_date,
        "color": points[-1].get("color") or "#3d8bfd",
        "points": points,
        "total": len(points),
        "coordinate_precision": coordinate_precision,
        "truncated": truncated,
    }


@app.get("/api/history/context/{poll_id}")
def api_history_context(
    poll_id: int, source: str = Query(default="real", pattern="^(real|fake)$")
) -> dict[str, Any]:
    """Возвращает соседние объекты из того же опроса."""
    result = db.get_poll_map_context(poll_id, source)
    if result is None:
        raise HTTPException(status_code=404, detail="Опрос не найден.")
    return result


@app.get("/api/history/static-zones/{static_index}")
def api_history_static_zones(
    static_index: int,
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    time_from: str = Query(default="00:00", pattern=r"^\d{2}:\d{2}$"),
    time_to: str = Query(default="23:59", pattern=r"^\d{2}:\d{2}$"),
    source: str = Query(default="real", pattern="^(real|fake)$"),
    limit: int = Query(default=1_500, ge=1, le=20_000),
    include_outside: bool = False,
    dynamic_query: str = Query(default="", max_length=200),
) -> dict[str, Any]:
    """Возвращает траектории объектов, входивших в зоны выбранной точки."""
    start, end = _history_bounds(date_from, date_to, time_from, time_to)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    settings = store.current()
    coordinate_precision = settings.map_coordinate_precision
    result = db.get_static_zone_trajectories(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(),
        data_source=source,
        limit=limit + 1,
        coordinate_precision=coordinate_precision,
        include_outside=include_outside,
        dynamic_query=dynamic_query,
    )
    if result is None:
        configured = next(
            (item for item in settings.static_objects if item.id == static_index), None
        ) if source == "real" else None
        if configured is None or configured.lat is None or configured.lng is None:
            raise HTTPException(
                status_code=404,
                detail="Для стационарного объекта нет географических координат.",
            )
        result = {
            "static_index": static_index,
            "static_name": configured.name,
            "lat": configured.lat,
            "lng": configured.lng,
            "alert_radius_km": settings.threshold if settings.coordinate_mode == "latlng" else None,
            "warning_radius_km": settings.warning_distance if settings.coordinate_mode == "latlng" else None,
            "blue_radius_km": settings.blue_distance if settings.coordinate_mode == "latlng" else None,
            "points": [],
        }
    points = result.pop("points")
    truncated = len(points) > limit
    result["blue_color"] = settings.blue_color
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for point in points[:limit]:
        object_date = str(point["object_date"])
        dynamic_index = int(point["dynamic_index"])
        key = (object_date, dynamic_index)
        item = grouped.setdefault(
            key,
            {
                "dynamic_index": point["dynamic_index"],
                "dynamic_name": point["dynamic_name"],
                "object_date": object_date,
                "trajectory_key": f"{object_date}:{dynamic_index}",
                "color": point.get("color") or "#3d8bfd",
                "points": [],
                "entered_alert": False,
                "entered_warning": False,
                "entered_blue": False,
                "entered_zone": False,
            },
        )
        item["points"].append(point)
        item["entered_alert"] = item["entered_alert"] or point["status"] == "alert"
        item["entered_warning"] = (
            item["entered_warning"] or point["status"] == "warning"
        )
        item["entered_blue"] = item["entered_blue"] or point["status"] == "blue"
        item["entered_zone"] = item["entered_zone"] or point["status"] != "outside"
    return {
        **result,
        "trajectories": list(grouped.values()),
        "objects_total": len(grouped),
        "points_total": sum(len(item["points"]) for item in grouped.values()),
        "coordinate_precision": coordinate_precision,
        "truncated": truncated,
    }


@app.post("/api/history/fake/generate")
def api_generate_fake_history() -> dict[str, Any]:
    """Пересоздаёт изолированный демонстрационный набор хронологии."""
    counts = db.ensure_fake_history(force=True)
    db.add_log(
        "INFO",
        "history",
        f"Тестовая хронология пересоздана: {counts['polls']} опросов",
    )
    return {"source": "fake", **counts}


@app.post("/api/history/prune")
def api_prune_history() -> dict[str, Any]:
    """Вручную удаляет реальные динамические наблюдения старше 20 дней."""
    tracking_result = db.prune_dynamic_history()
    table_result = table_history_db.prune_dynamic_history()
    if tracking_result["polls_deleted"]:
        db.compact()
    if table_result["snapshots_deleted"]:
        table_history_db.compact()
    db.add_log(
        "INFO", "history",
        f"Очистка истории до {tracking_result['cutoff']}: "
        f"{tracking_result['polls_deleted']} опросов, "
        f"{table_result['snapshots_deleted']} снимков таблицы",
    )
    return {**tracking_result, **table_result}


@app.post("/api/test-file")
def api_test_file(path: str = "data_test/1.html") -> dict[str, Any]:
    """Разбирает HTML из data_test и сохраняет снимок в обе базы без Chrome."""
    target = (ROOT / path).resolve()
    allowed = (ROOT / "data_test").resolve()
    try:
        target.relative_to(allowed)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Можно открывать только файлы из data_test."
        ) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"Файл не найден: {path}")
    try:
        snapshot = worker.load_html_file(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot


@app.get("/api/probe")
def api_probe(path: str = "data_test/1.html") -> dict[str, Any]:
    """Диагностирует HTML-файл только из разрешённого каталога ``data_test``."""
    target = (ROOT / path).resolve()
    allowed = (ROOT / "data_test").resolve()
    try:
        target.relative_to(allowed)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Можно открывать только файлы из data_test."
        ) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"Файл не найден: {path}")
    return analyze_html_file(target)


@app.post("/api/beep")
def api_beep() -> dict[str, Any]:
    """Воспроизводит тестовый сигнал через механизм текущей ОС."""
    from parsing_site.sound import play_system_beep

    success = play_system_beep()
    return {"success": success}


def _parse_history_date(value: str, *, end_of_day: bool) -> datetime:
    """Разбирает ISO-дату и при необходимости расширяет её до конца суток."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Некорректная дата. Используйте формат ГГГГ-ММ-ДД."
        ) from exc
    if "T" not in value and end_of_day:
        return parsed.replace(hour=23, minute=59, second=59)
    return parsed


def _history_bounds(
    date_from: str, date_to: str, time_from: str, time_to: str
) -> tuple[datetime, datetime]:
    """Собирает включительный интервал из даты и времени с точностью до минуты."""
    start_value = date_from if "T" in date_from else f"{date_from}T{time_from}:00"
    end_value = date_to if "T" in date_to else f"{date_to}T{time_to}:59"
    start = _parse_history_date(start_value, end_of_day=False)
    end = _parse_history_date(end_value, end_of_day=False)
    if "T" not in date_to:
        end = end.replace(microsecond=999999)
    return start, end


def _history_search_scope(
    static_index: int | None, dynamic_query: str
) -> tuple[int | None, str]:
    """Ищет введённый номер объекта по всем стационарным точкам."""
    query = dynamic_query.strip()
    number = query.removeprefix("№").strip()
    if number.lower().startswith("id"):
        identity = number[2:].lstrip(" :#-№")
        if identity.isdecimal():
            return None, f"-{identity}"
    if number.isdecimal():
        return None, number
    return static_index, query
