"use strict";

/*
 * Служебный раздел.
 *
 * Разметка собирается через createElement: innerHTML здесь не
 * используется нигде, чтобы содержимое журналов и сообщений об ошибках
 * не могло превратиться в разметку.
 */

const STATE_URL = "/admin/api/state";
const REFRESH_MS = 15000;

const elements = {
    refreshed: document.getElementById("refreshed"),
    stateCards: document.getElementById("state-cards"),
    stateError: document.getElementById("state-error"),
    progressSummary: document.getElementById("progress-summary"),
    queueTable: document.getElementById("queue-table"),
    formatsTable: document.getElementById("formats-table"),
    stagesTable: document.getElementById("stages-table"),
    servicesTable: document.getElementById("services-table"),
    tasks: document.getElementById("tasks"),
    logSource: document.getElementById("log-source"),
    logRefresh: document.getElementById("log-refresh"),
    logFollow: document.getElementById("log-follow"),
    logOutput: document.getElementById("log-output"),
};

let logTimer = null;
let knownTasks = [];

function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function number(value) {
    if (value === null || value === undefined || value === "") return "—";
    const parsed = Number(value);
    if (Number.isNaN(parsed)) return String(value);
    return parsed.toLocaleString("ru-RU");
}

function duration(seconds) {
    if (!seconds && seconds !== 0) return "";
    const total = Math.max(0, Math.round(Number(seconds)));
    const minutes = Math.floor(total / 60);
    if (minutes < 1) return `${total} с`;
    if (minutes < 60) return `${minutes} мин`;
    return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}

function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
}

function card(label, value, note, tone) {
    const box = make("div", "admin-card");
    box.appendChild(make("span", "admin-card__label", label));
    const valueNode = make("span", "admin-card__value", value);
    if (tone) valueNode.classList.add(`admin-card__value--${tone}`);
    box.appendChild(valueNode);
    if (note) box.appendChild(make("span", "admin-card__note", note));
    return box;
}

function table(headers, rows) {
    const element = make("table", "admin-table");
    const head = make("thead");
    const headRow = make("tr");

    headers.forEach((header) => {
        const cell = make("th", header.numeric ? "numeric" : "", header.label);
        headRow.appendChild(cell);
    });

    head.appendChild(headRow);
    element.appendChild(head);

    const body = make("tbody");

    rows.forEach((row) => {
        const bodyRow = make("tr");

        row.forEach((cellValue, index) => {
            const numeric = headers[index] && headers[index].numeric;
            const cell = make("td", numeric ? "numeric" : "");

            if (cellValue && cellValue.nodeType === 1) {
                cell.appendChild(cellValue);
            } else {
                cell.textContent = cellValue === undefined || cellValue === null
                    ? "—"
                    : String(cellValue);
            }

            bodyRow.appendChild(cell);
        });

        body.appendChild(bodyRow);
    });

    element.appendChild(body);
    return element;
}

function pill(text, tone) {
    return make("span", `pill pill--${tone}`, text);
}

// ------------------------------------------------------------ состояние

function renderState(data) {
    clear(elements.stateCards);

    const search = data.elasticsearch || {};

    if (search.available) {
        const statusTone = search.cluster_status === "green"
            ? "ok"
            : search.cluster_status === "yellow" ? "warn" : "bad";

        elements.stateCards.appendChild(
            card("Elasticsearch", search.cluster_status || "?",
                 `версия ${search.version || "?"}`, statusTone));
        elements.stateCards.appendChild(
            card("Фрагментов в индексе", number(search.documents),
                 search.index || ""));
        elements.stateError.hidden = true;
    } else {
        elements.stateCards.appendChild(
            card("Elasticsearch", "недоступен", "", "bad"));
        elements.stateError.hidden = false;
        elements.stateError.textContent =
            search.error || "Нет связи с Elasticsearch";
    }

    const pipeline = data.pipeline || {};
    const crawler = pipeline.crawler || {};

    elements.stateCards.appendChild(
        card("Всего адресов", number(crawler.total_urls)));
    elements.stateCards.appendChild(
        card("Загружено", number(crawler.loaded_urls), "", "ok"));
    elements.stateCards.appendChild(
        card("Осталось", number(crawler.remaining_urls), "", "warn"));
    elements.stateCards.appendChild(
        card("Требуют внимания", number(crawler.problem_urls),
             "", crawler.problem_urls ? "bad" : "ok"));

    const ocr = pipeline.pdf_ocr || {};
    elements.stateCards.appendChild(
        card("Распознано PDF",
             `${number(ocr.completed)} / ${number(ocr.total)}`,
             `версия ${ocr.version || "?"}`));
}

function renderProgress(pipeline) {
    const crawler = (pipeline && pipeline.crawler) || {};
    const percent = Number(crawler.completion_percent) || 0;

    clear(elements.progressSummary);

    const label = make("div", "admin-progress__label");
    label.appendChild(make("span", "", `Обработано: ${percent.toFixed(1)}%`));
    label.appendChild(make("span", "",
        `${number(crawler.handled_urls)} из ${number(crawler.total_urls)}`));

    const bar = make("div", "admin-progress__bar");
    const fill = make("div", "admin-progress__fill");
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    bar.appendChild(fill);

    elements.progressSummary.appendChild(label);
    elements.progressSummary.appendChild(bar);

    const statusLabels = {
        indexed: "проиндексировано",
        indexed_existing: "уже было в индексе",
        unchanged: "без изменений",
        discovered: "найдено, в очереди",
        retry: "будет повторено",
        missing: "не отвечает",
        gone: "удалено с сайта",
        skipped_empty: "без пригодного текста",
        skipped_unsupported: "формат не поддержан",
        skipped_too_large: "слишком крупные",
        skipped_corrupt: "повреждены",
        quarantined: "в карантине",
        processing: "обрабатывается",
    };

    const statuses = crawler.statuses || {};
    const rows = Object.keys(statuses)
        .sort((a, b) => statuses[b] - statuses[a])
        .map((key) => [statusLabels[key] || key, number(statuses[key])]);

    clear(elements.queueTable);
    elements.queueTable.appendChild(table(
        [{ label: "Состояние" }, { label: "Адресов", numeric: true }],
        rows,
    ));

    const formats = crawler.formats || [];
    clear(elements.formatsTable);
    elements.formatsTable.appendChild(table(
        [
            { label: "Формат" },
            { label: "Всего", numeric: true },
            { label: "Загружено", numeric: true },
            { label: "Осталось", numeric: true },
            { label: "Завершено окончательно", numeric: true },
        ],
        formats.map((item) => [
            item.label || item.key,
            number(item.total),
            number(item.loaded),
            number(item.remaining),
            number(item.terminal),
        ]),
    ));

    const stageLabels = {
        publication_date: "Даты публикации",
        content_cleanup: "Очистка текстов",
        pdf_ocr: "Распознавание PDF",
    };

    const stageRows = Object.keys(stageLabels)
        .filter((key) => pipeline && pipeline[key])
        .map((key) => {
            const stage = pipeline[key];
            return [
                stageLabels[key],
                number(stage.total),
                number(stage.completed),
                number(stage.remaining !== undefined
                    ? stage.remaining : stage.pending),
                number(stage.problems || 0),
            ];
        });

    clear(elements.stagesTable);
    elements.stagesTable.appendChild(table(
        [
            { label: "Проход" },
            { label: "Всего", numeric: true },
            { label: "Готово", numeric: true },
            { label: "Осталось", numeric: true },
            { label: "Проблем", numeric: true },
        ],
        stageRows,
    ));
}

function renderServices(services) {
    clear(elements.servicesTable);

    elements.servicesTable.appendChild(table(
        [{ label: "Служба" }, { label: "Работает" }, { label: "В автозапуске" }],
        (services || []).map((item) => {
            const activeTone = item.active === "active" ? "ok"
                : item.active === "failed" ? "bad" : "idle";
            const enabledTone = item.enabled === "enabled" ? "ok" : "idle";

            return [
                item.label,
                pill(item.active, activeTone),
                pill(item.enabled, enabledTone),
            ];
        }),
    ));
}

// -------------------------------------------------------------- задачи

function taskStateLabel(state) {
    switch (state) {
        case "running": return ["выполняется", "warn"];
        case "finished": return ["завершена", "ok"];
        case "stopped": return ["остановлена", "idle"];
        case "never": return ["не запускалась", "idle"];
        default: return [state || "?", "idle"];
    }
}

function renderTasks(tasks) {
    knownTasks = tasks || [];
    clear(elements.tasks);

    knownTasks.forEach((task) => {
        const box = make("div", "admin-task");

        const head = make("div", "admin-task__head");
        head.appendChild(make("span", "admin-task__title", task.title));

        const [label, tone] = taskStateLabel(task.state);
        head.appendChild(pill(label, tone));
        box.appendChild(head);

        box.appendChild(make("p", "admin-task__description", task.description));

        if (task.started_at) {
            const note = task.state === "running" && task.elapsed_seconds
                ? `запущена ${task.started_at}, идёт ${duration(task.elapsed_seconds)}`
                : `последний запуск ${task.started_at}`;
            box.appendChild(make("p", "admin-task__description", note));
        }

        if (task.note) {
            box.appendChild(make("p", "admin-task__description", task.note));
        }

        const actions = make("div", "admin-task__actions");
        let urlInput = null;

        if (task.accepts_url) {
            urlInput = document.createElement("input");
            urlInput.type = "url";
            urlInput.placeholder = "https://sochi.ru/...";
            actions.appendChild(urlInput);
        }

        const startButton = make("button", "btn-primary", "Запустить");
        startButton.type = "button";
        startButton.disabled = task.state === "running";
        startButton.addEventListener("click", () => {
            startTask(task.key, urlInput ? urlInput.value : null, startButton);
        });
        actions.appendChild(startButton);

        if (task.state === "running") {
            const stopButton = make("button", "btn-danger", "Остановить");
            stopButton.type = "button";
            stopButton.addEventListener("click", () => stopTask(task.key));
            actions.appendChild(stopButton);
        }

        const logButton = make("button", "btn-secondary", "Журнал");
        logButton.type = "button";
        logButton.addEventListener("click", () => {
            elements.logSource.value = `task:${task.key}`;
            loadLog();
        });
        actions.appendChild(logButton);

        box.appendChild(actions);
        elements.tasks.appendChild(box);
    });

    fillLogSources();
}

async function startTask(key, url, button) {
    if (button) button.disabled = true;

    try {
        const response = await fetch(`/admin/api/tasks/${key}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: url || null }),
        });

        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            window.alert(payload.detail || `Ошибка ${response.status}`);
        } else {
            elements.logSource.value = `task:${key}`;
            loadLog();
        }
    } catch (error) {
        window.alert(`Не удалось запустить: ${error}`);
    }

    refresh();
}

async function stopTask(key) {
    if (!window.confirm("Остановить задачу?")) return;

    try {
        await fetch(`/admin/api/tasks/${key}/stop`, { method: "POST" });
    } catch (error) {
        window.alert(`Не удалось остановить: ${error}`);
    }

    refresh();
}

// -------------------------------------------------------------- журнал

function fillLogSources() {
    const current = elements.logSource.value;
    clear(elements.logSource);

    knownTasks.forEach((task) => {
        const option = document.createElement("option");
        option.value = `task:${task.key}`;
        option.textContent = `Задача: ${task.title}`;
        elements.logSource.appendChild(option);
    });

    (window.__services || []).forEach((service) => {
        const option = document.createElement("option");
        option.value = `unit:${service.unit}`;
        option.textContent = `Служба: ${service.label}`;
        elements.logSource.appendChild(option);
    });

    if (current) elements.logSource.value = current;
}

async function loadLog() {
    const value = elements.logSource.value || "";
    if (!value) return;

    const [kind, name] = value.split(":");
    const url = kind === "task"
        ? `/admin/api/tasks/${name}/log?lines=400`
        : `/admin/api/journal/${encodeURIComponent(name)}?lines=400`;

    try {
        const response = await fetch(url);
        const payload = await response.json();
        elements.logOutput.textContent = payload.log || "(пусто)";
        elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
    } catch (error) {
        elements.logOutput.textContent = `Не удалось получить журнал: ${error}`;
    }
}

// --------------------------------------------------------------- общее

async function refresh() {
    try {
        const response = await fetch(STATE_URL, {
            headers: { Accept: "application/json" },
        });

        if (!response.ok) {
            elements.stateError.hidden = false;
            elements.stateError.textContent = `Ошибка ${response.status}`;
            return;
        }

        const data = await response.json();
        window.__services = data.services || [];

        renderState(data);
        renderProgress(data.pipeline);
        renderServices(data.services);
        renderTasks(data.tasks);

        elements.refreshed.textContent =
            `обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
    } catch (error) {
        elements.stateError.hidden = false;
        elements.stateError.textContent = `Не удалось получить состояние: ${error}`;
    }
}

elements.logRefresh.addEventListener("click", loadLog);
elements.logSource.addEventListener("change", loadLog);

elements.logFollow.addEventListener("change", () => {
    if (logTimer) {
        window.clearInterval(logTimer);
        logTimer = null;
    }

    if (elements.logFollow.checked) {
        logTimer = window.setInterval(loadLog, 5000);
        loadLog();
    }
});

refresh();
window.setInterval(refresh, REFRESH_MS);
