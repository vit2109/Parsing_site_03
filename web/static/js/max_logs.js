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
        cell(tr, item.screenshot ? "Да" : "Нет");
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
    } catch (exc) {
      result.textContent = `Отправка не начата: ${exc.message}`;
    } finally {
      button.disabled = false;
    }
  });

  load();
  setInterval(load, 5000);
})();
