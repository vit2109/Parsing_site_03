(() => {
  const size = 100;
  let offset = 0;
  let filters = { status: "", user_id: "" };
  const rows = document.querySelector("#max-log-rows");
  const count = document.querySelector("#max-log-count");
  const error = document.querySelector("#max-log-error");
  const prev = document.querySelector("#max-log-prev");
  const next = document.querySelector("#max-log-next");
  const page = document.querySelector("#max-log-page");

  function validUser(value) {
    return /^[1-9]\d*$/.test(value) && BigInt(value) <= 9223372036854775807n;
  }

  function cell(row, value, className = "") {
    const td = document.createElement("td");
    td.textContent = value;
    if (className) td.className = className;
    row.append(td);
    return td;
  }

  async function loadStatus() {
    const config = document.querySelector("#max-config-state");
    const poll = document.querySelector("#max-poll-state");
    try {
      const response = await fetch("/api/max/status");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      const problems = [];
      if (!state.enabled) problems.push("рассылка по зонам выключена");
      if (!state.token_set) problems.push("токен не найден");
      if (!state.recipients || !state.rules) problems.push("получатели и зоны не настроены");
      config.textContent = problems.length
        ? `Автоматические сообщения не отправляются: ${problems.join("; ")}. Измените настройки на странице мониторинга.`
        : `Рассылка включена · получателей: ${state.recipients} · правил по объектам: ${state.rules}`;
      config.classList.toggle("max-status-problem", problems.length > 0);
      if (state.last_poll) {
        const last = state.last_poll;
        const zones = last.zone_pairs || {};
        poll.textContent = `${state.worker_running ? "" : "Обход остановлен. "}Опрос #${last.poll_id}: синих пар ${zones.blue || 0}, жёлтых ${zones.warning || 0}, красных ${zones.alert || 0}; подходящих выбранным зонам ${last.eligible_pairs}; новых уведомлений ${last.prepared}, из них со скриншотом ${last.prepared_with_screenshot}; снимков с номерами ${last.screenshots}. Обход: ${last.scan_status || "—"}.`
          + (last.archive_errors?.length ? ` Ошибки снимков: ${last.archive_errors.join("; ")}` : "")
          + (state.worker_error ? ` Ошибка опроса: ${state.worker_error}` : "");
      } else {
        poll.textContent = state.worker_running
          ? "Ожидание первого завершённого опроса карты."
          : `Опрос карты не запущен${state.worker_error ? `: ${state.worker_error}` : "."}`;
      }
    } catch (exc) {
      config.textContent = `Не удалось получить состояние рассылки: ${exc.message}`;
      config.classList.add("max-status-problem");
    }
  }

  async function load() {
    const params = new URLSearchParams({ limit: String(size), offset: String(offset) });
    if (filters.status) params.set("status", filters.status);
    if (filters.user_id) params.set("user_id", filters.user_id);
    try {
      const response = await fetch(`/api/max/logs?${params}`);
      if (!response.ok) throw new Error(`Ошибка загрузки: HTTP ${response.status}`);
      const data = await response.json();
      error.hidden = true;
      rows.replaceChildren();
      for (const item of data.items) {
        const tr = document.createElement("tr");
        const time = cell(tr, item.queued_at, "max-log-time");
        if (item.finished_at) time.title = `Завершено: ${item.finished_at}`;
        cell(tr, String(item.user_id));
        cell(tr, item.kind === "test" ? "Тест" : "Оповещение");
        const status = { queued: "В очереди", sent: "Отправлено", error: "Ошибка", skipped: "Пропущено" }[item.status] || item.status;
        cell(tr, status, `max-log-status max-log-status-${item.status}`);
        const message = cell(tr, item.message, "max-log-message");
        if (item.error) {
          const detail = document.createElement("div");
          detail.className = "max-log-detail";
          detail.textContent = item.error;
          message.append(detail);
        }
        const photo = !item.screenshot ? "Нет" : item.status === "sent"
          ? item.error?.startsWith("Скриншот не отправлен:") ? "Ошибка" : "Отправлен"
          : "Выбран";
        cell(tr, photo);
        rows.append(tr);
      }
      if (!data.items.length) {
        const tr = document.createElement("tr");
        tr.className = "empty";
        const td = cell(tr, "Записей пока нет");
        td.colSpan = 6;
        rows.append(tr);
      }
      count.textContent = `Найдено: ${data.total}`;
      page.textContent = data.total ? `${offset + 1}–${offset + data.items.length} из ${data.total}` : "";
      prev.disabled = offset === 0;
      next.disabled = offset + data.items.length >= data.total;
    } catch (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    }
  }

  document.querySelector("#max-log-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    const userId = document.querySelector("#max-log-user").value.trim();
    if (userId && !validUser(userId)) {
      error.textContent = "Укажите положительный числовой MAX user ID.";
      error.hidden = false;
      return;
    }
    filters = { status: document.querySelector("#max-log-status").value, user_id: userId };
    offset = 0;
    load();
  });
  document.querySelector("#max-log-refresh").addEventListener("click", load);
  prev.addEventListener("click", () => { offset = Math.max(0, offset - size); load(); });
  next.addEventListener("click", () => { offset += size; load(); });

  document.querySelector("#max-test-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const userId = document.querySelector("#max-test-user").value.trim();
    const result = document.querySelector("#max-test-result");
    if (!validUser(userId)) {
      result.textContent = "Укажите положительный числовой MAX user ID.";
      return;
    }
    const button = document.querySelector("#max-test-button");
    button.disabled = true;
    try {
      const response = await fetch("/api/max/test", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      result.textContent = `Тестовое сообщение пользователю ${userId} добавлено в очередь (запись #${data.id}).`;
      offset = 0;
      await load();
      await loadStatus();
    } catch (exc) {
      result.textContent = `Отправка не начата: ${exc.message}`;
    } finally {
      button.disabled = false;
    }
  });

  load();
  loadStatus();
  setInterval(() => { load(); loadStatus(); }, 5000);
})();
