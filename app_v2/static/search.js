"use strict";


const MAX_OFFSET = 10000;
const MAX_RECENT_SEARCHES = 6;
const RECENT_SEARCHES_KEY = "sochi-search:recent:v1";
const PIPELINE_REFRESH_MS = 30000;

const numberFormatter = new Intl.NumberFormat("ru-RU");

const categoryLabels = {
    news: "Новости",
    legal: "Нормативные акты",
    announcement: "Объявления",
    media: "Медиагалерея",
    press: "Пресс-служба",
    city_authority: "Городская власть",
    city_life: "Жизнь города",
    city: "О городе",
    events: "Афиша и мероприятия",
    file: "Файлы и PDF",
    page: "Страницы сайта",
    legacy: "Архивные разделы",
};

const resultTypeLabels = {
    file: "DOCX, XLSX, TXT и CSV",
    page: "Страницы сайта",
    pdf: "PDF-документы",
    attachment: "Только вложения",
};

const sortLabels = {
    relevance: "По релевантности",
    date_desc: "Сначала новые",
    date_asc: "Сначала старые",
};


const elements = {
    form: document.getElementById("search-form"),
    query: document.getElementById("query"),
    documentNumber: document.getElementById("document-number"),
    documentKind: document.getElementById("document-kind"),
    contentCategory: document.getElementById("content-category"),
    dateFrom: document.getElementById("date-from"),
    dateTo: document.getElementById("date-to"),
    resultType: document.getElementById("result-type"),
    sort: document.getElementById("sort"),
    limit: document.getElementById("limit"),

    searchButton: document.getElementById("search-button"),
    answerButton: document.getElementById("answer-button"),
    resetButton: document.getElementById("reset-button"),

    filtersPanel: document.getElementById("filters-panel"),
    filterCount: document.getElementById("filter-count"),
    activeFilters: document.getElementById("active-filters"),
    recentSearches: document.getElementById("recent-searches"),

    message: document.getElementById("message"),
    answerPanel: document.getElementById("answer-panel"),
    answerContent: document.getElementById("answer-content"),
    answerClose: document.getElementById("answer-close"),

    facetTabs: document.getElementById("facet-tabs"),
    searchMeta: document.getElementById("search-meta"),
    searchSummary: document.getElementById("search-summary"),
    exportButton: document.getElementById("export-button"),
    results: document.getElementById("results"),

    pagination: document.getElementById("pagination"),
    paginationPages: document.getElementById("pagination-pages"),
    previousPage: document.getElementById("previous-page"),
    nextPage: document.getElementById("next-page"),
    pageInfo: document.getElementById("page-info"),

    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),

    pipelinePanel: document.getElementById("pipeline-panel"),
    pipelineRefresh: document.getElementById("pipeline-refresh"),
    pipelineUpdated: document.getElementById("pipeline-updated"),
    pipelineProgressLabel: document.getElementById("pipeline-progress-label"),
    pipelineProgressDetail: document.getElementById("pipeline-progress-detail"),
    pipelineProgressbar: document.getElementById("pipeline-progressbar"),
    pipelineProgressFill: document.getElementById("pipeline-progress-fill"),
    pipelineTotal: document.getElementById("pipeline-total"),
    pipelineLoaded: document.getElementById("pipeline-loaded"),
    pipelineRemaining: document.getElementById("pipeline-remaining"),
    pipelineProcessing: document.getElementById("pipeline-processing"),
    pipelineDocuments: document.getElementById("pipeline-documents"),
    pipelineProblems: document.getElementById("pipeline-problems"),
    pipelineFormats: document.getElementById("pipeline-formats"),
    pipelinePublication: document.getElementById("pipeline-publication"),
    pipelineCleanup: document.getElementById("pipeline-cleanup"),
    pipelinePdfOcr: document.getElementById("pipeline-pdf-ocr"),
    pipelineDiscovery: document.getElementById("pipeline-discovery"),
    pipelineError: document.getElementById("pipeline-error"),
};


const state = {
    offset: 0,
    total: 0,
    limit: 10,
    loading: false,
    answerLoading: false,
    lastItemsCount: 0,
    hasExecutedSearch: false,
    currentItems: [],
    searchController: null,
    answerController: null,
    pipelineController: null,
};


function createElement(tagName, className, text) {
    const element = document.createElement(tagName);

    if (className) {
        element.className = className;
    }

    if (text !== undefined && text !== null) {
        element.textContent = String(text);
    }

    return element;
}


function setMessage(text, type = "info") {
    if (!text) {
        elements.message.hidden = true;
        elements.message.textContent = "";
        elements.message.className = "message";
        return;
    }

    elements.message.hidden = false;
    elements.message.className = `message message--${type}`;
    elements.message.textContent = text;
}


function readRecentSearches() {
    try {
        const parsed = JSON.parse(
            window.localStorage.getItem(RECENT_SEARCHES_KEY) || "[]"
        );

        if (!Array.isArray(parsed)) {
            return [];
        }

        return parsed
            .filter((value) => typeof value === "string")
            .map((value) => value.trim())
            .filter((value) => value.length >= 2 && value.length <= 300)
            .slice(0, MAX_RECENT_SEARCHES);
    } catch (error) {
        return [];
    }
}


function writeRecentSearches(values) {
    try {
        window.localStorage.setItem(
            RECENT_SEARCHES_KEY,
            JSON.stringify(values.slice(0, MAX_RECENT_SEARCHES))
        );
    } catch (error) {
        // Поиск продолжает работать, даже если браузер запретил хранилище.
    }
}


function renderRecentSearches() {
    elements.recentSearches.replaceChildren();
    const values = readRecentSearches();

    if (values.length === 0) {
        elements.recentSearches.hidden = true;
        return;
    }

    elements.recentSearches.append(
        createElement("span", "recent-searches__label", "Недавние:")
    );

    for (const value of values) {
        const button = createElement(
            "button",
            "recent-search",
            value
        );
        button.type = "button";
        button.title = `Повторить поиск: ${value}`;
        button.addEventListener("click", () => {
            elements.query.value = value;
            state.offset = 0;
            renderActiveFilters();
            updateAnswerButtonState();
            runSearch(true);
        });
        elements.recentSearches.append(button);
    }

    const clearButton = createElement(
        "button",
        "recent-searches__clear",
        "Очистить"
    );
    clearButton.type = "button";
    clearButton.addEventListener("click", () => {
        writeRecentSearches([]);
        renderRecentSearches();
    });
    elements.recentSearches.append(clearButton);
    elements.recentSearches.hidden = false;
}


function rememberSearch(value) {
    const query = String(value || "").trim();

    if (query.length < 2 || query.length > 300) {
        return;
    }

    const comparison = query.toLocaleLowerCase("ru-RU");
    const values = readRecentSearches().filter(
        (item) => item.toLocaleLowerCase("ru-RU") !== comparison
    );
    values.unshift(query);
    writeRecentSearches(values);
    renderRecentSearches();
}


function spreadsheetSafe(value) {
    const normalized = String(value ?? "")
        .replace(/\r?\n/g, " ")
        .trim();

    if (/^[=+\-@]/.test(normalized)) {
        return `'${normalized}`;
    }

    return normalized;
}


function csvCell(value) {
    return `"${spreadsheetSafe(value).replace(/"/g, '""')}"`;
}


function exportCurrentPage() {
    if (!Array.isArray(state.currentItems) || state.currentItems.length === 0) {
        setMessage("На текущей странице нет данных для экспорта.", "info");
        return;
    }

    const headings = [
        "Заголовок",
        "Раздел",
        "Вид документа",
        "Номер документа",
        "Дата",
        "Тип",
        "URL",
        "Фрагмент",
    ];

    const rows = state.currentItems.map((item) => {
        const category = (
            item.content_category_label
            || categoryLabels[item.content_category]
            || item.section
            || ""
        );
        const date = (
            item.document_date
            || item.published_at
            || item.sort_date
            || ""
        );
        const type = item.doc_type === "pdf"
            ? "PDF"
            : (item.doc_type === "file" ? "Файл" : "Страница");

        return [
            item.title,
            category,
            item.document_kind,
            item.document_number,
            formatDate(date),
            type,
            item.url || item.file_url || item.page_url,
            item.snippet,
        ].map(csvCell).join(";");
    });

    const csv = `\uFEFF${headings.map(csvCell).join(";")}\r\n${rows.join("\r\n")}\r\n`;
    const blob = new Blob([csv], {type: "text/csv;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const date = new Date().toISOString().slice(0, 10);
    link.href = url;
    link.download = `sochi-search-${date}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
}


function safeExternalUrl(value) {
    if (!value) {
        return "";
    }

    try {
        const parsed = new URL(String(value), window.location.origin);

        if (!["http:", "https:"].includes(parsed.protocol)) {
            return "";
        }

        return parsed.href;
    } catch (error) {
        return "";
    }
}


function formatDate(value) {
    const source = String(value || "").trim();

    if (!source) {
        return "";
    }

    let parsed;

    const isoDate = source.match(
        /^(\d{4})-(\d{2})-(\d{2})/
    );

    if (isoDate) {
        const [, year, month, day] = isoDate;
        parsed = new Date(
            Number(year),
            Number(month) - 1,
            Number(day)
        );
    } else {
        parsed = new Date(source);
    }

    if (Number.isNaN(parsed.getTime())) {
        return source;
    }

    return parsed.toLocaleDateString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
    });
}


function documentWord(value) {
    const absolute = Math.abs(Number(value || 0));
    const lastTwo = absolute % 100;
    const last = absolute % 10;

    if (lastTwo >= 11 && lastTwo <= 14) {
        return "документов";
    }

    if (last === 1) {
        return "документ";
    }

    if (last >= 2 && last <= 4) {
        return "документа";
    }

    return "документов";
}


function normalizedHighlightTerms(query) {
    const normalized = String(query || "").trim();

    if (!normalized) {
        return [];
    }

    const terms = [
        normalized,
        ...normalized.split(/\s+/),
    ]
        .map((value) => value.trim())
        .filter((value) => value.length >= 2);

    return Array.from(new Set(terms)).sort(
        (left, right) => right.length - left.length
    );
}


function escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}


function appendHighlightedText(container, text, query) {
    const source = String(text || "");
    const terms = normalizedHighlightTerms(query);

    if (!source || terms.length === 0) {
        container.textContent = source;
        return;
    }

    const expression = new RegExp(
        `(${terms.map(escapeRegExp).join("|")})`,
        "giu"
    );

    const normalizedTerms = new Set(
        terms.map((term) => term.toLocaleLowerCase("ru-RU"))
    );

    for (const part of source.split(expression)) {
        if (!part) {
            continue;
        }

        if (normalizedTerms.has(part.toLocaleLowerCase("ru-RU"))) {
            container.append(createElement("mark", "", part));
        } else {
            container.append(document.createTextNode(part));
        }
    }
}


function addChip(container, text, neutral = false) {
    if (!text) {
        return;
    }

    container.append(
        createElement(
            "span",
            neutral ? "chip chip--neutral" : "chip",
            text
        )
    );
}


async function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        return;
    }

    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();

    const copied = document.execCommand("copy");
    textarea.remove();

    if (!copied) {
        throw new Error("Копирование недоступно");
    }
}


function flashButtonText(button, text) {
    const original = button.textContent;
    button.textContent = text;
    button.disabled = true;

    window.setTimeout(() => {
        button.textContent = original;
        button.disabled = false;
    }, 1400);
}


function buildResultCard(item, query) {
    const card = createElement("article", "result-card");

    const category = String(
        item.content_category_label
        || categoryLabels[item.content_category]
        || ""
    );

    if (category) {
        card.append(
            createElement("p", "result-card__category", category)
        );
    }

    const primaryUrl = safeExternalUrl(
        item.url || item.file_url || item.page_url
    );
    const title = createElement("h2", "result-card__title");
    const titleText = item.title || "Документ без названия";

    if (primaryUrl) {
        const link = createElement("a");
        link.href = primaryUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        appendHighlightedText(link, titleText, query);
        title.append(link);
    } else {
        appendHighlightedText(title, titleText, query);
    }

    card.append(title);

    if (item.section && item.section !== category) {
        card.append(
            createElement("p", "result-card__section", item.section)
        );
    }

    const metadata = createElement("div", "result-card__meta");
    addChip(metadata, item.document_kind);

    if (item.document_number) {
        addChip(metadata, `№ ${item.document_number}`);
    }

    const resultDate = (
        item.document_date
        || item.published_at
        || item.sort_date
    );
    addChip(metadata, formatDate(resultDate));

    if (item.doc_type === "pdf") {
        addChip(metadata, "PDF", true);
    } else if (item.doc_type === "file") {
        addChip(metadata, "Файл", true);
    } else if (item.doc_type === "page") {
        addChip(metadata, "Страница", true);
    }

    if (item.is_attachment === true) {
        addChip(metadata, "Вложение", true);
    }

    if (item.ocr_used === true) {
        addChip(metadata, "Распознано OCR", true);
    } else if (item.text_extraction === "metadata_only") {
        addChip(metadata, "Только метаданные", true);
    }

    if (Number.isInteger(item.page_number)) {
        addChip(metadata, `стр. ${item.page_number}`, true);
    }

    if (metadata.children.length > 0) {
        card.append(metadata);
    }

    const snippet = createElement("p", "result-card__snippet");
    appendHighlightedText(
        snippet,
        item.snippet || "Текстовый фрагмент отсутствует.",
        query
    );
    card.append(snippet);

    const links = createElement("div", "result-card__links");

    if (primaryUrl) {
        const sourceLink = createElement(
            "a",
            "",
            item.doc_type === "pdf"
                ? "Открыть PDF"
                : (item.doc_type === "file" ? "Открыть файл" : "Открыть источник")
        );
        sourceLink.href = primaryUrl;
        sourceLink.target = "_blank";
        sourceLink.rel = "noopener noreferrer";
        links.append(sourceLink);
    }

    const parentUrl = safeExternalUrl(item.parent_url);

    if (parentUrl && parentUrl !== primaryUrl) {
        const parentLink = createElement("a", "", "Страница документа");
        parentLink.href = parentUrl;
        parentLink.target = "_blank";
        parentLink.rel = "noopener noreferrer";
        links.append(parentLink);
    }

    if (primaryUrl) {
        const copyButton = createElement(
            "button",
            "link-button",
            "Скопировать ссылку"
        );
        copyButton.type = "button";
        copyButton.addEventListener("click", async () => {
            try {
                await copyText(primaryUrl);
                flashButtonText(copyButton, "Скопировано");
            } catch (error) {
                setMessage("Не удалось скопировать ссылку.", "error");
            }
        });
        links.append(copyButton);
    }

    if (links.children.length > 0) {
        card.append(links);
    }

    return card;
}


function setOptionalParameter(parameters, name, value) {
    if (value !== "" && value !== null && value !== undefined) {
        parameters.set(name, String(value));
    }
}


function buildParameters({includePagination = true} = {}) {
    const parameters = new URLSearchParams();

    setOptionalParameter(parameters, "q", elements.query.value.trim());
    setOptionalParameter(
        parameters,
        "document_number",
        elements.documentNumber.value.trim()
    );
    setOptionalParameter(
        parameters,
        "document_kind",
        elements.documentKind.value.trim()
    );
    setOptionalParameter(
        parameters,
        "content_category",
        elements.contentCategory.value
    );
    setOptionalParameter(parameters, "date_from", elements.dateFrom.value);
    setOptionalParameter(parameters, "date_to", elements.dateTo.value);

    const resultType = elements.resultType.value;

    if (resultType === "page") {
        parameters.set("doc_type", "page");
        parameters.set("is_attachment", "false");
    } else if (resultType === "pdf") {
        parameters.set("doc_type", "pdf");
    } else if (resultType === "file") {
        parameters.set("doc_type", "file");
    } else if (resultType === "attachment") {
        parameters.set("is_attachment", "true");
    }

    if (includePagination) {
        parameters.set("sort", elements.sort.value);
        parameters.set("limit", elements.limit.value);
        parameters.set("offset", String(state.offset));
    } else {
        parameters.set("limit", "5");
    }

    return parameters;
}


function hasSearchCondition() {
    return Boolean(
        elements.query.value.trim()
        || elements.documentNumber.value.trim()
        || elements.documentKind.value.trim()
        || elements.contentCategory.value
        || elements.dateFrom.value
        || elements.dateTo.value
        || elements.resultType.value
    );
}


function validateForm({requireQuery = false} = {}) {
    const query = elements.query.value.trim();

    if (query.length > 0 && query.length < 2) {
        setMessage(
            "Текстовый запрос должен содержать минимум два символа.",
            "error"
        );
        elements.query.focus();
        return false;
    }

    if (requireQuery && query.length < 2) {
        setMessage(
            "Для краткого ответа введите минимум два символа.",
            "error"
        );
        elements.query.focus();
        return false;
    }

    if (!hasSearchCondition()) {
        setMessage(
            "Введите текст запроса или задайте хотя бы один фильтр.",
            "error"
        );
        elements.query.focus();
        return false;
    }

    if (
        elements.dateFrom.value
        && elements.dateTo.value
        && elements.dateFrom.value > elements.dateTo.value
    ) {
        setMessage(
            "Начальная дата не может быть позже конечной.",
            "error"
        );
        elements.dateFrom.focus();
        return false;
    }

    return true;
}


function filterDefinitions() {
    return [
        {
            active: Boolean(elements.documentNumber.value.trim()),
            label: `Номер: ${elements.documentNumber.value.trim()}`,
            clear: () => { elements.documentNumber.value = ""; },
        },
        {
            active: Boolean(elements.contentCategory.value),
            label: categoryLabels[elements.contentCategory.value] || "Раздел",
            clear: () => { elements.contentCategory.value = ""; },
        },
        {
            active: Boolean(elements.documentKind.value),
            label: elements.documentKind.value,
            clear: () => { elements.documentKind.value = ""; },
        },
        {
            active: Boolean(elements.dateFrom.value),
            label: `С ${formatDate(elements.dateFrom.value)}`,
            clear: () => { elements.dateFrom.value = ""; },
        },
        {
            active: Boolean(elements.dateTo.value),
            label: `По ${formatDate(elements.dateTo.value)}`,
            clear: () => { elements.dateTo.value = ""; },
        },
        {
            active: Boolean(elements.resultType.value),
            label: resultTypeLabels[elements.resultType.value] || "Тип результата",
            clear: () => { elements.resultType.value = ""; },
        },
        {
            active: elements.sort.value !== "relevance",
            label: sortLabels[elements.sort.value] || "Сортировка",
            clear: () => { elements.sort.value = "relevance"; },
        },
    ];
}


function renderActiveFilters() {
    elements.activeFilters.replaceChildren();

    const active = filterDefinitions().filter((item) => item.active);

    elements.filterCount.hidden = active.length === 0;
    elements.filterCount.textContent = String(active.length);

    if (active.length === 0) {
        elements.activeFilters.hidden = true;
        return;
    }

    elements.activeFilters.append(
        createElement("span", "active-filters__label", "Выбрано:")
    );

    for (const definition of active) {
        const button = createElement("button", "filter-pill");
        button.type = "button";
        button.setAttribute("aria-label", `Убрать фильтр: ${definition.label}`);
        button.append(
            createElement("span", "", definition.label),
            createElement("span", "filter-pill__close", "×")
        );
        button.addEventListener("click", () => {
            definition.clear();
            state.offset = 0;
            clearAnswer();
            renderActiveFilters();

            if (hasSearchCondition()) {
                runSearch(true);
            } else {
                clearSearchUi();
                updateBrowserUrl(new URLSearchParams());
            }
        });
        elements.activeFilters.append(button);
    }

    elements.activeFilters.hidden = false;
}


function updateAnswerButtonState() {
    const queryReady = elements.query.value.trim().length >= 2;
    elements.answerButton.disabled = (
        !queryReady || state.loading || state.answerLoading
    );
}


function setLoading(loading) {
    state.loading = loading;
    elements.searchButton.disabled = loading;
    elements.exportButton.disabled = loading;
    elements.previousPage.disabled = loading;
    elements.nextPage.disabled = loading;
    elements.results.setAttribute("aria-busy", loading ? "true" : "false");
    elements.searchButton.textContent = loading ? "Поиск…" : "Найти";
    updateAnswerButtonState();
}


function setAnswerLoading(loading) {
    state.answerLoading = loading;
    elements.answerButton.textContent = loading ? "Подготовка…" : "Краткий ответ";
    updateAnswerButtonState();
}


function renderLoading() {
    elements.results.replaceChildren();
    elements.searchMeta.hidden = true;
    elements.pagination.hidden = true;

    for (let index = 0; index < 3; index += 1) {
        elements.results.append(createElement("div", "loading-card"));
    }
}


function updateBrowserUrl(parameters, replace = false) {
    const queryString = parameters.toString();
    const nextUrl = queryString ? `/ui?${queryString}` : "/ui";
    const currentUrl = `${window.location.pathname}${window.location.search}`;

    if (currentUrl === nextUrl || currentUrl === `${nextUrl}/`) {
        return;
    }

    const method = replace ? "replaceState" : "pushState";
    window.history[method](null, "", nextUrl);
}


function renderFacets(facets) {
    elements.facetTabs.replaceChildren();

    if (!Array.isArray(facets) || facets.length === 0) {
        elements.facetTabs.hidden = true;
        return;
    }

    const selectedCategory = elements.contentCategory.value || "all";

    for (const facet of facets) {
        const code = String(facet.code || "");
        const count = Number(facet.count || 0);

        if (!code || (count === 0 && code !== selectedCategory)) {
            continue;
        }

        const label = String(facet.label || categoryLabels[code] || code);
        const button = createElement("button", "facet-tab");
        button.type = "button";

        if (code === selectedCategory) {
            button.classList.add("facet-tab--active");
        }

        button.setAttribute(
            "aria-pressed",
            code === selectedCategory ? "true" : "false"
        );
        button.append(
            createElement("span", "facet-tab__label", label),
            createElement(
                "span",
                "facet-tab__count",
                numberFormatter.format(count)
            )
        );
        button.addEventListener("click", () => {
            const nextValue = code === "all" ? "" : code;

            if (elements.contentCategory.value === nextValue) {
                return;
            }

            elements.contentCategory.value = nextValue;
            state.offset = 0;
            renderActiveFilters();
            clearAnswer();
            runSearch(true);
        });
        elements.facetTabs.append(button);
    }

    elements.facetTabs.hidden = elements.facetTabs.children.length === 0;
}


function resetFilters({keepQuery = true, run = true} = {}) {
    const query = keepQuery ? elements.query.value : "";
    elements.form.reset();
    elements.query.value = query;
    state.offset = 0;
    state.limit = 10;
    clearAnswer();
    renderActiveFilters();

    if (run && hasSearchCondition()) {
        runSearch(true);
    } else if (!hasSearchCondition()) {
        clearSearchUi();
        updateBrowserUrl(new URLSearchParams());
    }
}


function renderEmptyState() {
    const empty = createElement("div", "empty-state");
    empty.append(
        createElement("h2", "", "Ничего не найдено"),
        createElement(
            "p",
            "",
            "Попробуйте сократить запрос, изменить даты или убрать часть фильтров."
        )
    );

    const actions = createElement("div", "state-actions");

    if (filterDefinitions().some((item) => item.active)) {
        const reset = createElement(
            "button",
            "button button--secondary",
            "Убрать фильтры"
        );
        reset.type = "button";
        reset.addEventListener("click", () => resetFilters({keepQuery: true}));
        actions.append(reset);
    }

    const edit = createElement(
        "button",
        "button button--primary",
        "Изменить запрос"
    );
    edit.type = "button";
    edit.addEventListener("click", () => {
        elements.query.focus();
        window.scrollTo({top: 0, behavior: "smooth"});
    });
    actions.append(edit);
    empty.append(actions);
    elements.results.append(empty);
}


function renderSearchError(message) {
    elements.results.replaceChildren();
    elements.searchMeta.hidden = true;
    elements.facetTabs.hidden = true;
    elements.pagination.hidden = true;

    const errorState = createElement("div", "error-state");
    errorState.append(
        createElement("h2", "", "Поиск временно недоступен"),
        createElement("p", "", message)
    );

    const actions = createElement("div", "state-actions");
    const retry = createElement(
        "button",
        "button button--primary",
        "Повторить"
    );
    retry.type = "button";
    retry.addEventListener("click", () => runSearch(false));
    actions.append(retry);
    errorState.append(actions);
    elements.results.append(errorState);
}


function setPipelineCollapsed(collapsed) {
    const panel = elements.pipelinePanel;

    if (!panel) {
        return;
    }

    panel.classList.toggle("pipeline-panel--collapsed", collapsed);

    const toggle = document.getElementById("pipeline-panel-toggle");

    if (toggle) {
        toggle.textContent = collapsed
            ? "Показать состояние индекса"
            : "Свернуть состояние индекса";
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
}

function ensurePipelineToggle() {
    const panel = elements.pipelinePanel;

    if (!panel || document.getElementById("pipeline-panel-toggle")) {
        return;
    }

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.id = "pipeline-panel-toggle";
    toggle.className = "pipeline-panel__toggle";
    toggle.addEventListener("click", () => {
        setPipelineCollapsed(
            !panel.classList.contains("pipeline-panel--collapsed")
        );
    });

    panel.appendChild(toggle);
}

function renderResults(data) {
    // Есть запрос — статистика уходит на второй план.
    ensurePipelineToggle();
    setPipelineCollapsed(
        Boolean(data && (data.query || "").trim())
    );


    elements.results.replaceChildren();
    renderFacets(data.facets);

    const items = Array.isArray(data.items) ? data.items : [];
    state.total = Number(data.total || 0);
    state.limit = Number(data.limit || 10);
    state.offset = Number(data.offset || 0);
    state.lastItemsCount = items.length;
    state.hasExecutedSearch = true;
    state.currentItems = items;
    elements.exportButton.hidden = items.length === 0;

    const firstResult = items.length > 0 ? state.offset + 1 : 0;
    const lastResult = state.offset + items.length;
    const timing = data.took_ms !== undefined
        ? ` · ${numberFormatter.format(data.took_ms)} мс`
        : "";

    elements.searchSummary.textContent = (
        `Найдено ${numberFormatter.format(state.total)} `
        + `${documentWord(state.total)}. `
        + `Показано ${numberFormatter.format(firstResult)}–`
        + `${numberFormatter.format(lastResult)}${timing}`
    );
    elements.searchMeta.hidden = false;

    const warnings = [];

    if (data.fallback_applied === true) {
        warnings.push(
            "Точных совпадений не найдено — показаны близкие варианты с учётом возможной опечатки."
        );
    }

    if (data.timed_out === true) {
        warnings.push(
            "Elasticsearch завершил запрос по таймауту; выдача может быть неполной."
        );
    }

    if (warnings.length > 0) {
        setMessage(
            warnings.join(" "),
            "warning"
        );
    }

    if (items.length === 0) {
        renderEmptyState();
        renderPagination();
        return;
    }

    const query = String(data.query || "");

    for (const item of items) {
        elements.results.append(buildResultCard(item, query));
    }

    renderPagination();
}


function paginationSequence(currentPage, totalPages) {
    if (totalPages <= 7) {
        return Array.from({length: totalPages}, (_, index) => index + 1);
    }

    const pages = new Set([1, totalPages, currentPage]);

    for (let offset = -1; offset <= 1; offset += 1) {
        const page = currentPage + offset;

        if (page >= 1 && page <= totalPages) {
            pages.add(page);
        }
    }

    if (currentPage <= 4) {
        [2, 3, 4, 5].forEach((page) => pages.add(page));
    }

    if (currentPage >= totalPages - 3) {
        [
            totalPages - 4,
            totalPages - 3,
            totalPages - 2,
            totalPages - 1,
        ].forEach((page) => pages.add(page));
    }

    const sorted = Array.from(pages)
        .filter((page) => page >= 1 && page <= totalPages)
        .sort((left, right) => left - right);
    const result = [];

    for (const page of sorted) {
        const previous = result[result.length - 1];

        if (typeof previous === "number" && page - previous > 1) {
            result.push("ellipsis");
        }

        result.push(page);
    }

    return result;
}


function goToPage(page) {
    const nextOffset = Math.min((page - 1) * state.limit, MAX_OFFSET);

    if (nextOffset === state.offset) {
        return;
    }

    state.offset = nextOffset;
    clearAnswer();
    runSearch(true);
    elements.searchMeta.scrollIntoView({behavior: "smooth", block: "start"});
}


function renderPagination() {
    const naturalPages = Math.max(1, Math.ceil(state.total / state.limit));
    const maximumAccessiblePages = Math.floor(MAX_OFFSET / state.limit) + 1;
    const totalPages = Math.min(naturalPages, maximumAccessiblePages);
    const currentPage = Math.min(
        totalPages,
        Math.floor(state.offset / state.limit) + 1
    );
    const hasPrevious = state.offset > 0;
    const hasNext = (
        state.offset + state.lastItemsCount < state.total
        && state.offset + state.limit <= MAX_OFFSET
    );

    elements.pagination.hidden = totalPages <= 1;
    elements.previousPage.disabled = state.loading || !hasPrevious;
    elements.nextPage.disabled = state.loading || !hasNext;
    elements.paginationPages.replaceChildren();

    if (totalPages <= 1) {
        return;
    }

    for (const entry of paginationSequence(currentPage, totalPages)) {
        if (entry === "ellipsis") {
            elements.paginationPages.append(
                createElement("span", "pagination__ellipsis", "…")
            );
            continue;
        }

        const button = createElement("button", "page-button", entry);
        button.type = "button";
        button.setAttribute("aria-label", `Страница ${entry}`);

        if (entry === currentPage) {
            button.classList.add("page-button--current");
            button.setAttribute("aria-current", "page");
        } else {
            button.addEventListener("click", () => goToPage(entry));
        }

        elements.paginationPages.append(button);
    }

    elements.pageInfo.textContent = naturalPages > totalPages
        ? `Страница ${currentPage} из ${totalPages} доступных — уточните запрос для остальных результатов`
        : `Страница ${currentPage} из ${totalPages}`;
}


function extractErrorMessage(payload, status) {
    if (payload && typeof payload.detail === "string") {
        return payload.detail;
    }

    if (
        payload
        && payload.detail
        && typeof payload.detail.message === "string"
    ) {
        return payload.detail.message;
    }

    if (payload && Array.isArray(payload.detail)) {
        const messages = payload.detail
            .map((item) => item && item.msg)
            .filter(Boolean);

        if (messages.length > 0) {
            return messages.join("; ");
        }
    }

    return `Ошибка поискового запроса: HTTP ${status}`;
}


async function runSearch(updateUrl = true) {
    if (!validateForm()) {
        return;
    }

    if (state.searchController) {
        state.searchController.abort();
    }

    const controller = new AbortController();
    state.searchController = controller;
    const parameters = buildParameters();

    setMessage("");
    setLoading(true);
    renderActiveFilters();
    clearAnswer();
    renderLoading();

    if (updateUrl) {
        updateBrowserUrl(parameters);
    }

    try {
        const response = await fetch(`/search?${parameters.toString()}`, {
            headers: {"Accept": "application/json"},
            signal: controller.signal,
        });

        let payload = null;

        try {
            payload = await response.json();
        } catch (error) {
            payload = null;
        }

        if (!response.ok) {
            throw new Error(extractErrorMessage(payload, response.status));
        }

        if (state.searchController !== controller) {
            return;
        }

        renderResults(payload);

        if (state.offset === 0 && state.currentItems.length > 0) {
            rememberSearch(payload.query);
        }
    } catch (error) {
        if (error && error.name === "AbortError") {
            return;
        }

        const message = error instanceof Error
            ? error.message
            : "Не удалось выполнить поиск.";
        setMessage(message, "error");
        renderSearchError(message);
    } finally {
        if (state.searchController === controller) {
            state.searchController = null;
            setLoading(false);
            renderPagination();
        }
    }
}


function answerSourceMetadata(source) {
    const values = [];

    if (source.document_kind) {
        values.push(source.document_kind);
    }

    if (source.document_number) {
        values.push(`№ ${source.document_number}`);
    }

    const date = formatDate(source.document_date || source.published_at);

    if (date) {
        values.push(date);
    }

    if (source.section) {
        values.push(source.section);
    }

    if (Number.isInteger(source.page_number)) {
        values.push(`стр. ${source.page_number}`);
    }

    return values.join(" · ");
}


function renderAnswer(payload) {
    elements.answerContent.replaceChildren();

    elements.answerContent.append(
        createElement(
            "p",
            "answer-panel__text",
            payload.answer || "Краткий ответ не сформирован."
        )
    );

    const sources = Array.isArray(payload.sources) ? payload.sources : [];

    if (sources.length > 0) {
        const section = createElement("section", "answer-sources");
        section.append(
            createElement(
                "h3",
                "",
                `Источники (${numberFormatter.format(sources.length)})`
            )
        );
        const list = createElement("ol", "answer-sources__list");

        for (const source of sources) {
            const item = createElement("li", "answer-source");
            const sourceUrl = safeExternalUrl(source.url);

            if (sourceUrl) {
                const link = createElement(
                    "a",
                    "answer-source__link",
                    source.title || "Источник"
                );
                link.href = sourceUrl;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                item.append(link);
            } else {
                item.append(
                    createElement(
                        "span",
                        "answer-source__link",
                        source.title || "Источник"
                    )
                );
            }

            const metadata = answerSourceMetadata(source);

            if (metadata) {
                item.append(
                    createElement("div", "answer-source__meta", metadata)
                );
            }

            list.append(item);
        }

        section.append(list);
        elements.answerContent.append(section);
    }

    if (payload.notice) {
        elements.answerContent.append(
            createElement("p", "answer-panel__notice", payload.notice)
        );
    }
}


function clearAnswer() {
    if (state.answerController) {
        state.answerController.abort();
        state.answerController = null;
    }

    elements.answerPanel.hidden = true;
    elements.answerContent.replaceChildren();
    setAnswerLoading(false);
}


async function runAnswer() {
    if (!validateForm({requireQuery: true})) {
        return;
    }

    if (state.answerController) {
        state.answerController.abort();
    }

    const controller = new AbortController();
    state.answerController = controller;
    const parameters = buildParameters({includePagination: false});

    setMessage("");
    setAnswerLoading(true);
    elements.answerPanel.hidden = false;
    elements.answerContent.replaceChildren(
        createElement(
            "p",
            "answer-panel__loading",
            "Подбираем наиболее релевантные фрагменты…"
        )
    );
    elements.answerPanel.scrollIntoView({behavior: "smooth", block: "nearest"});

    try {
        const response = await fetch(`/answer?${parameters.toString()}`, {
            headers: {"Accept": "application/json"},
            signal: controller.signal,
        });
        let payload = null;

        try {
            payload = await response.json();
        } catch (error) {
            payload = null;
        }

        if (!response.ok) {
            throw new Error(extractErrorMessage(payload, response.status));
        }

        if (state.answerController === controller) {
            renderAnswer(payload);
        }
    } catch (error) {
        if (error && error.name === "AbortError") {
            return;
        }

        const message = error instanceof Error
            ? error.message
            : "Не удалось подготовить краткий ответ.";
        elements.answerContent.replaceChildren(
            createElement("p", "answer-panel__text", message)
        );
    } finally {
        if (state.answerController === controller) {
            state.answerController = null;
            setAnswerLoading(false);
        }
    }
}


function clearSearchUi() {
    if (state.searchController) {
        state.searchController.abort();
        state.searchController = null;
    }

    state.offset = 0;
    state.total = 0;
    state.limit = Number(elements.limit.value || 10);
    state.lastItemsCount = 0;
    state.hasExecutedSearch = false;
    state.currentItems = [];

    elements.results.replaceChildren();
    elements.searchMeta.hidden = true;
    elements.exportButton.hidden = true;
    elements.pagination.hidden = true;
    elements.paginationPages.replaceChildren();
    elements.facetTabs.replaceChildren();
    elements.facetTabs.hidden = true;
    setMessage("");
    setLoading(false);
    clearAnswer();
}


function resetForm() {
    elements.form.reset();
    renderActiveFilters();
    clearSearchUi();
    updateBrowserUrl(new URLSearchParams());
    elements.query.focus();
}


function applyStateFromUrl() {
    const parameters = new URLSearchParams(window.location.search);
    elements.form.reset();

    const assignments = [
        ["q", elements.query],
        ["document_number", elements.documentNumber],
        ["document_kind", elements.documentKind],
        ["content_category", elements.contentCategory],
        ["date_from", elements.dateFrom],
        ["date_to", elements.dateTo],
        ["sort", elements.sort],
        ["limit", elements.limit],
    ];

    for (const [name, element] of assignments) {
        const value = parameters.get(name);

        if (value !== null) {
            element.value = value;
        }
    }

    const docType = parameters.get("doc_type");
    const isAttachment = parameters.get("is_attachment");

    if (isAttachment === "true") {
        elements.resultType.value = "attachment";
    } else if (docType === "pdf") {
        elements.resultType.value = "pdf";
    } else if (docType === "file") {
        elements.resultType.value = "file";
    } else if (docType === "page") {
        elements.resultType.value = "page";
    }

    const parsedOffset = Number(parameters.get("offset") || "0");
    state.offset = Number.isFinite(parsedOffset)
        ? Math.min(Math.max(0, parsedOffset), MAX_OFFSET)
        : 0;

    const advancedUsed = filterDefinitions().some((item) => item.active);
    elements.filtersPanel.open = advancedUsed;
    renderActiveFilters();
    updateAnswerButtonState();

    return hasSearchCondition();
}


async function loadSystemStatus() {
    elements.statusDot.className = "status-dot status-dot--loading";

    try {
        const [healthResponse, statsResponse] = await Promise.all([
            fetch("/health/ready", {headers: {"Accept": "application/json"}}),
            fetch("/stats", {headers: {"Accept": "application/json"}}),
        ]);

        if (!healthResponse.ok) {
            throw new Error(`HTTP ${healthResponse.status}`);
        }

        const health = await healthResponse.json();
        let documentCount = null;

        if (statsResponse.ok) {
            const stats = await statsResponse.json();
            documentCount = Number(stats.documents);
        }

        elements.statusDot.className = "status-dot status-dot--ready";
        elements.statusText.textContent = Number.isFinite(documentCount)
            ? `Индекс готов · ${numberFormatter.format(documentCount)} фрагментов`
            : "Индекс готов";

        if (health.cluster_status === "yellow") {
            elements.statusText.textContent += " · кластер yellow";
        }
    } catch (error) {
        elements.statusDot.className = "status-dot status-dot--error";
        elements.statusText.textContent = "Система поиска недоступна";
    }
}


function pipelineCount(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? numberFormatter.format(parsed) : "—";
}


function pipelineDateTime(value) {
    const parsed = new Date(String(value || ""));

    if (Number.isNaN(parsed.getTime())) {
        return "время неизвестно";
    }

    return parsed.toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
}


function pipelineQueueText(summary) {
    if (!summary || summary.available !== true) {
        return "Очередь отсутствует";
    }

    const parts = [
        `${pipelineCount(summary.completed)} завершено`,
        `${pipelineCount(summary.pending)} ожидает`,
        `${pipelineCount(summary.processing)} в работе`,
    ];

    if (Number(summary.problems) > 0) {
        parts.push(`${pipelineCount(summary.problems)} требуют внимания`);
    }

    return parts.join(" · ");
}


function pipelinePdfOcrText(summary) {
    if (!summary || summary.available !== true) {
        return "Учёт OCR ещё не подготовлен";
    }

    const percent = Number(summary.completion_percent) || 0;
    const parts = [
        `${pipelineCount(summary.completed)} из ${pipelineCount(summary.total)} PDF проверено`,
        `${percent.toLocaleString("ru-RU")}%`,
    ];

    if (Number(summary.remaining) > 0) {
        parts.push(`${pipelineCount(summary.remaining)} ожидает`);
    }

    if (Number(summary.fast_indexed) > 0) {
        parts.push(
            `${pipelineCount(summary.fast_indexed)} уже доступны в быстром режиме`
        );
    }

    if (Number(summary.processing) > 0) {
        parts.push(`${pipelineCount(summary.processing)} в работе`);
    }

    return parts.join(" · ");
}


function renderPipelineFormats(formats) {
    elements.pipelineFormats.replaceChildren();

    for (const item of Array.isArray(formats) ? formats : []) {
        const row = createElement("tr");
        const label = createElement("th", "", item.label || item.key || "Прочее");
        label.scope = "row";
        row.append(label);

        for (const field of [
            "total",
            "loaded",
            "remaining",
            "processing",
            "terminal",
        ]) {
            row.append(createElement("td", "", pipelineCount(item[field])));
        }

        elements.pipelineFormats.append(row);
    }
}


function renderPipelineStatus(payload) {
    const crawler = payload.crawler || {};
    const elasticsearch = payload.elasticsearch || {};
    const percent = Math.min(
        100,
        Math.max(0, Number(crawler.completion_percent) || 0)
    );

    elements.pipelinePanel.setAttribute("aria-busy", "false");
    elements.pipelineProgressLabel.textContent = `Первичная обработка: ${percent.toLocaleString("ru-RU")}%`;
    elements.pipelineProgressDetail.textContent = (
        `${pipelineCount(crawler.handled_urls)} из `
        + `${pipelineCount(crawler.total_urls)} URL обработано`
    );
    elements.pipelineProgressbar.setAttribute("aria-valuenow", String(percent));
    elements.pipelineProgressFill.style.width = `${percent}%`;

    elements.pipelineTotal.textContent = pipelineCount(crawler.total_urls);
    elements.pipelineLoaded.textContent = pipelineCount(crawler.loaded_urls);
    elements.pipelineRemaining.textContent = pipelineCount(crawler.remaining_urls);
    elements.pipelineProcessing.textContent = pipelineCount(crawler.processing_urls);
    elements.pipelineProblems.textContent = pipelineCount(crawler.problem_urls);
    elements.pipelineDocuments.textContent = elasticsearch.available === false
        ? "Недоступно"
        : pipelineCount(elasticsearch.documents);
    elements.pipelineUpdated.textContent = (
        `Обновлено ${pipelineDateTime(payload.generated_at)} · `
        + "автоматически каждые 30 секунд"
    );

    renderPipelineFormats(crawler.formats);
    elements.pipelinePublication.textContent = pipelineQueueText(payload.publication_date);
    elements.pipelineCleanup.textContent = pipelineQueueText(payload.content_cleanup);
    elements.pipelinePdfOcr.textContent = pipelinePdfOcrText(payload.pdf_ocr);

    const discovery = payload.latest_discovery;
    elements.pipelineDiscovery.textContent = discovery
        ? (
            `${pipelineDateTime(discovery.finished_at || discovery.started_at)} · `
            + `${pipelineCount(discovery.inserted_urls)} новых URL · `
            + `статус ${discovery.status || "неизвестен"}`
        )
        : "Запуски discovery пока не зафиксированы";

    elements.pipelineError.hidden = true;
    elements.pipelineError.textContent = "";
}


async function loadPipelineStatus() {
    if (state.pipelineController) {
        state.pipelineController.abort();
    }

    const controller = new AbortController();
    state.pipelineController = controller;
    elements.pipelineRefresh.disabled = true;
    elements.pipelinePanel.setAttribute("aria-busy", "true");

    try {
        const response = await fetch("/pipeline/status", {
            headers: {"Accept": "application/json"},
            signal: controller.signal,
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        renderPipelineStatus(await response.json());
    } catch (error) {
        if (error.name === "AbortError") {
            return;
        }

        elements.pipelinePanel.setAttribute("aria-busy", "false");
        elements.pipelineError.hidden = false;
        elements.pipelineError.textContent = (
            "Статистика загрузки временно недоступна. "
            + "Поиск продолжает работать независимо."
        );
        elements.pipelineUpdated.textContent = "Не удалось обновить данные";
    } finally {
        if (state.pipelineController === controller) {
            state.pipelineController = null;
            elements.pipelineRefresh.disabled = false;
        }
    }
}


elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    state.offset = 0;
    runSearch(true);
});

elements.form.addEventListener("input", () => {
    renderActiveFilters();
    clearAnswer();
    updateAnswerButtonState();
});

elements.answerButton.addEventListener("click", runAnswer);
elements.answerClose.addEventListener("click", clearAnswer);
elements.resetButton.addEventListener("click", resetForm);
elements.exportButton.addEventListener("click", exportCurrentPage);
elements.pipelineRefresh.addEventListener("click", loadPipelineStatus);

elements.sort.addEventListener("change", () => {
    if (state.hasExecutedSearch && hasSearchCondition()) {
        state.offset = 0;
        runSearch(true);
    }
});

elements.limit.addEventListener("change", () => {
    if (state.hasExecutedSearch && hasSearchCondition()) {
        state.offset = 0;
        runSearch(true);
    }
});

elements.previousPage.addEventListener("click", () => {
    if (state.offset <= 0) {
        return;
    }

    state.offset = Math.max(0, state.offset - state.limit);
    clearAnswer();
    runSearch(true);
    elements.searchMeta.scrollIntoView({behavior: "smooth", block: "start"});
});

elements.nextPage.addEventListener("click", () => {
    if (
        state.offset + state.lastItemsCount >= state.total
        || state.offset + state.limit > MAX_OFFSET
    ) {
        return;
    }

    state.offset += state.limit;
    clearAnswer();
    runSearch(true);
    elements.searchMeta.scrollIntoView({behavior: "smooth", block: "start"});
});

window.addEventListener("popstate", () => {
    clearSearchUi();

    if (applyStateFromUrl()) {
        runSearch(false);
    }
});

document.addEventListener("keydown", (event) => {
    const target = event.target;
    const editing = target && ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName);

    if (event.key === "/" && !editing) {
        event.preventDefault();
        elements.query.focus();
    }
});


loadSystemStatus();
loadPipelineStatus();
renderRecentSearches();

window.setInterval(loadPipelineStatus, PIPELINE_REFRESH_MS);

if (applyStateFromUrl()) {
    runSearch(false);
} else {
    updateAnswerButtonState();
}
