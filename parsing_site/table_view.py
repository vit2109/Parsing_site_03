"""Отрисовка таблицы расстояний: терминал (Rich), HTML и CSV в data/."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from parsing_site.tracking import TrackingSnapshot, row_to_dict

STATUS_STYLES = {
    "alert": "bold white on dark_red",
    "warning": "black on yellow",
    "blue": "white on blue",
    "ok": "",
}

STATUS_LABELS = {
    "alert": "близко",
    "warning": "внимание",
    "blue": "синяя зона",
    "ok": "норма",
}


def _object_line(item) -> str:
    """Форматирует маркер для диагностического терминального списка."""
    line = f"#{item.index} {item.name}: ({item.x:.1f}, {item.y:.1f})"
    if item.lat is not None and item.lng is not None:
        line += f"  {item.lat:.5f}, {item.lng:.5f}"
    return line


def build_rich_view(snapshot: TrackingSnapshot) -> Panel:
    """Строит цветную Rich-панель с текущими расстояниями и итогами."""
    table = Table(
        title="Расстояния до статичных объектов",
        expand=True,
        show_lines=False,
        header_style="bold",
    )
    table.add_column("№ стат.", justify="right", no_wrap=True)
    table.add_column("Координаты статичного", no_wrap=True)
    table.add_column("Динамический объект")
    table.add_column("№ дин.", justify="right", no_wrap=True)
    table.add_column("Координаты дин.", no_wrap=True)
    table.add_column("Расстояние", justify="right", no_wrap=True)
    table.add_column("Кр./жёл.", justify="right", no_wrap=True)
    table.add_column("Статус", no_wrap=True)

    if not snapshot.rows:
        table.add_row("-", "-", "нет объектов", "-", "-", "-", "-", "-")
    else:
        for row in snapshot.rows:
            style = STATUS_STYLES[row.status]
            table.add_row(
                str(row.static_index),
                f"{row.static_name}  {row.static_coords}",
                row.dynamic_name,
                str(row.dynamic_index) if row.dynamic_index > 0 else "номер не определён",
                row.dynamic_coords,
                f"{row.distance:.1f} {row.distance_unit}",
                row.nearby_counts_label,
                STATUS_LABELS[row.status],
                style=style or None,
            )

    alerts = sum(1 for row in snapshot.rows if row.status == "alert")
    warnings = sum(1 for row in snapshot.rows if row.status == "warning")
    header = Text.assemble(
        (datetime.now().astimezone().strftime("%H:%M:%S"), "bold"),
        f"  {snapshot.title or snapshot.url}\n",
        f"динамических: {len(snapshot.dynamics)}   статичных: {len(snapshot.statics)}   ",
        (f"красных: {alerts}  ", "bold red"),
        (f"жёлтых: {warnings}", "yellow"),
        f"\nпорог красный <= {snapshot.threshold:g} {snapshot.distance_unit}, "
        f"жёлтый <= {snapshot.warning_limit:g} {snapshot.distance_unit}",
    )
    return Panel(Group(header, table), border_style="cyan")


def print_inspect(snapshot: TrackingSnapshot, console: Console | None = None) -> None:
    """Печатает сведения о сцене и таблицу расстояний в терминал."""
    out = console or Console()
    out.print(f"[bold]URL:[/bold] {snapshot.url}")
    out.print(f"[bold]Заголовок:[/bold] {snapshot.title}")
    if snapshot.viewport[0]:
        out.print(
            f"[bold]Окно:[/bold] {snapshot.viewport[0]:.0f} x {snapshot.viewport[1]:.0f}"
        )
    out.print("\n[bold]Динамические объекты[/bold]")
    if not snapshot.dynamics:
        out.print("  не найдены")
    else:
        for item in snapshot.dynamics:
            out.print(f"  {_object_line(item)}")
    out.print("\n[bold]Статичные точки[/bold]")
    if not snapshot.statics:
        out.print("  нет. Добавьте [[tracking.static_objects]] в config.toml")
    else:
        for item in snapshot.statics:
            out.print(f"  {_object_line(item)}")
    if snapshot.geo_source and snapshot.geo_source != "none":
        out.print(f"\n[bold]Источник координат:[/bold] {snapshot.geo_source}")
    if snapshot.map_zoom is not None:
        out.print(f"[bold]Zoom:[/bold] {snapshot.map_zoom:g}")
    out.print()
    out.print(build_rich_view(snapshot))


def persist_snapshot(
    snapshot: TrackingSnapshot,
    data_dir: Path,
    refresh_seconds: float = 2.0,
) -> Path:
    """Перезаписывает актуальные HTML/CSV и дописывает JSONL-историю."""
    data_dir.mkdir(parents=True, exist_ok=True)
    html_path = data_dir / "distances.html"
    html_path.write_text(
        render_html(snapshot, refresh_seconds=refresh_seconds),
        encoding="utf-8",
    )

    csv_path = data_dir / "distances.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "static_index",
                "static_name",
                "static_x",
                "static_y",
                "dynamic_index",
                "dynamic_name",
                "dynamic_x",
                "dynamic_y",
                "distance",
                "status",
                "alert_count",
                "warning_count",
                "nearby_counts",
            ],
            # row_to_dict также содержит географические и UI-поля; этот CSV намеренно
            # сохраняет только стабильный компактный набор колонок выше.
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in snapshot.rows:
            writer.writerow(row_to_dict(row))

    history_path = data_dir / "distances_history.jsonl"
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": snapshot.url,
        "threshold": snapshot.threshold,
        "warning_limit": snapshot.warning_limit,
        "rows": [row_to_dict(row) for row in snapshot.rows],
    }
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return html_path


def render_html(snapshot: TrackingSnapshot, refresh_seconds: float = 2.0) -> str:
    """Формирует автономную HTML-таблицу с автоматическим обновлением страницы."""
    body_rows = []
    if not snapshot.rows:
        body_rows.append("<tr><td colspan='8'>Нет пар объектов для расчёта</td></tr>")
    else:
        for row in snapshot.rows:
            body_rows.append(
                f"<tr class='{row.status}'>"
                f"<td>{row.static_index}</td>"
                f"<td>{escape(row.static_name)}<br><span class='coords'>{escape(row.static_coords)}</span></td>"
                f"<td>{escape(row.dynamic_name)}</td>"
                f"<td>{row.dynamic_index if row.dynamic_index > 0 else 'номер не определён'}</td>"
                f"<td>{escape(row.dynamic_coords)}</td>"
                f"<td>{row.distance:.3f} {escape(row.distance_unit)}</td>"
                f"<td>{escape(row.nearby_counts_label)}</td>"
                f"<td>{escape(STATUS_LABELS[row.status])}</td>"
                "</tr>"
            )

    alerts = sum(1 for row in snapshot.rows if row.status == "alert")
    warnings = sum(1 for row in snapshot.rows if row.status == "warning")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    refresh = max(1, round(refresh_seconds))
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="refresh" content="{refresh}" />
  <title>Расстояния до статичных объектов</title>
  <style>
    :root {{
      --bg: #10131a;
      --ink: #e8edf4;
      --muted: #8b97a8;
      --line: #243044;
    }}
    body {{
      margin: 0;
      font-family: "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    .meta {{ color: var(--muted); margin-bottom: 16px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      overflow: hidden;
      border-radius: 12px;
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--line);
    }}
    th {{ background: #1a2230; font-weight: 650; }}
    tr.alert {{ background: #9b1c1c; color: #fff; }}
    tr.warning {{ background: #d4a017; color: #1a1400; }}
    tr.blue {{ background: #2878b8; color: #f3f9ff; }}
    tr.ok {{ background: #18202c; }}
    .coords {{ color: inherit; opacity: 0.8; font-size: 12px; }}
    .legend span {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      margin-right: 8px;
      font-size: 12px;
    }}
    .legend .alert {{ background: #9b1c1c; }}
    .legend .warning {{ background: #d4a017; color: #1a1400; }}
  </style>
</head>
<body>
  <main>
    <h1>Расстояния до статичных объектов</h1>
    <p class="meta">
      {escape(stamp)} · {escape(snapshot.title or snapshot.url)}<br />
      динамических: {len(snapshot.dynamics)} · статичных: {len(snapshot.statics)} ·
      красных: {alerts} · жёлтых: {warnings}<br />
      красный &le; {snapshot.threshold:g} {snapshot.distance_unit} · жёлтый &le; {snapshot.warning_limit:g} {snapshot.distance_unit}
    </p>
    <p class="legend">
      <span class="alert">ближе порога</span>
      <span class="warning">рядом с порогом</span>
    </p>
    <table>
      <thead>
        <tr>
          <th>№ статичного</th>
          <th>Координаты статичного</th>
          <th>Динамический объект</th>
          <th>№ динамического</th>
          <th>Координаты динамического</th>
          <th>Расстояние, {snapshot.distance_unit}</th>
          <th>Кр. / жёл. у точки</th>
          <th>Статус</th>
        </tr>
      </thead>
      <tbody>
        {"".join(body_rows)}
      </tbody>
    </table>
  </main>
</body>
</html>
"""
