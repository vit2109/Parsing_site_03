"""HTTP API и страница мониторинга (FastAPI + шаблоны web/)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from parsing_site.config import ROOT, WEB_DIR, load_config
from parsing_site.db import Database
from parsing_site.history_export import build_history_workbook
from parsing_site.html_probe import analyze_html_file
from parsing_site.settings_store import SettingsStore, from_dict
from parsing_site.table_history_db import TableHistoryDatabase
from parsing_site.worker import ParseWorker

db = Database()
table_history_db = TableHistoryDatabase()
store = SettingsStore(db)
worker = ParseWorker(db, store, table_history_db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Инициализирует данные при старте и освобождает ресурсы при остановке ASGI."""
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


class SettingsIn(BaseModel):
    """Тело запроса сохранения настроек с базовыми ограничениями значений."""

    site: str = Field(min_length=1)
    cdp_url: str = "http://127.0.0.1:9222"
    interval_seconds: float = Field(ge=0.4, le=600)
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
        {"settings": settings.to_dict()},
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    """Отдаёт страницу фильтрации и визуализации хронологии."""
    return templates.TemplateResponse(request, "history.html", {})


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    """Возвращает единый снимок настроек, worker, таблицы, хранилища и логов."""
    settings = store.current()
    status = worker.status
    snapshot = status["snapshot"]
    current_rows = len(snapshot.get("rows", [])) if snapshot else None
    return {
        "settings": settings.to_dict(),
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
    return store.current().to_dict()


@app.put("/api/settings")
def api_put_settings(payload: SettingsIn) -> dict[str, Any]:
    """Проверяет и полностью заменяет runtime-настройки переданного PUT-запроса."""
    data = payload.model_dump()
    try:
        settings = store.save(from_dict(data))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add_log("INFO", "settings", f"Настройки сохранены: {settings.site}")
    return settings.to_dict()


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
    static_index: int = Query(gt=0),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    max_distance_km: float = Query(gt=0, le=20_000),
    dynamic: str = Query(default="", max_length=200),
    limit: int = Query(default=5000, ge=1, le=20_000),
    source: str = Query(default="real", pattern="^(real|fake)$"),
) -> dict[str, Any]:
    """Фильтрует историю сближений и формирует сводку по объектам."""
    start = _parse_history_date(date_from, end_of_day=False)
    end = _parse_history_date(date_to, end_of_day=True)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    rows = db.list_distance_history(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(timespec="seconds"),
        max_distance_km=max_distance_km,
        dynamic_query=dynamic,
        limit=limit,
        data_source=source,
    )
    legacy_records = db.count_legacy_history(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(timespec="seconds"),
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
    static_index: int = Query(gt=0),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    max_distance_km: float = Query(gt=0, le=20_000),
    dynamic: str = Query(default="", max_length=200),
    source: str = Query(default="real", pattern="^(real|fake)$"),
) -> Response:
    """Выгружает выбранную историю сближений в XLSX."""
    start = _parse_history_date(date_from, end_of_day=False)
    end = _parse_history_date(date_to, end_of_day=True)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    rows = db.list_distance_history(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(timespec="seconds"),
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
    )
    filename = f"chronology_{date_from}_{date_to}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/history/trajectory/{dynamic_index}")
def api_history_trajectory(
    dynamic_index: int,
    object_date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    source: str = Query(default="real", pattern="^(real|fake)$"),
    limit: int = Query(default=1_500, ge=1, le=20_000),
) -> dict[str, Any]:
    """Возвращает географическую траекторию одного динамического объекта."""
    start = _parse_history_date(date_from, end_of_day=False)
    end = _parse_history_date(date_to, end_of_day=True)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    points = db.list_dynamic_trajectory(
        dynamic_index=dynamic_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(timespec="seconds"),
        object_date=object_date,
        data_source=source,
        limit=limit + 1,
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
        "truncated": truncated,
    }


@app.get("/api/history/static-zones/{static_index}")
def api_history_static_zones(
    static_index: int,
    date_from: str = Query(min_length=10),
    date_to: str = Query(min_length=10),
    source: str = Query(default="real", pattern="^(real|fake)$"),
    limit: int = Query(default=1_500, ge=1, le=20_000),
) -> dict[str, Any]:
    """Возвращает траектории объектов, входивших в зоны выбранной точки."""
    start = _parse_history_date(date_from, end_of_day=False)
    end = _parse_history_date(date_to, end_of_day=True)
    if start > end:
        raise HTTPException(
            status_code=400, detail="Начало периода позже его окончания."
        )
    result = db.get_static_zone_trajectories(
        static_index=static_index,
        date_from=start.isoformat(timespec="seconds"),
        date_to=end.isoformat(timespec="seconds"),
        data_source=source,
        limit=limit + 1,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Для стационарного объекта нет географических данных и зон за этот период.",
        )
    points = result.pop("points")
    truncated = len(points) > limit
    result["blue_color"] = store.current().blue_color
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
            },
        )
        item["points"].append(point)
        item["entered_alert"] = item["entered_alert"] or point["status"] == "alert"
        item["entered_warning"] = (
            item["entered_warning"] or point["status"] == "warning"
        )
        item["entered_blue"] = item["entered_blue"] or point["status"] == "blue"
    return {
        **result,
        "trajectories": list(grouped.values()),
        "objects_total": len(grouped),
        "points_total": sum(len(item["points"]) for item in grouped.values()),
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
