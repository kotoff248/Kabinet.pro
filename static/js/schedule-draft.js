(function () {
    "use strict";

    let previewController = null;
    let previewRequestId = 0;
    let urgentPreviewController = null;
    let urgentPreviewRequestId = 0;
    let autoPlacePollTimer = null;
    const SEARCH_DEBOUNCE_MS = 320;
    const MAX_MANUAL_PERIODS = 3;

    function getNavigation() {
        return window.KabinetNavigation || {};
    }

    function normalizeSearch(value) {
        return (value || "").trim().replace(/\s+/g, " ");
    }

    function navigateTo(url, options) {
        const nextOptions = options || {};
        if (nextOptions.focusSearch) {
            window.__scheduleDraftFocusSearch = true;
        }
        if (url === window.location.href) {
            return;
        }
        const navigation = getNavigation();
        if (navigation && typeof navigation.navigate === "function" && navigation.navigate(url, true)) {
            return;
        }
        window.location.href = url;
    }

    function buildSearchUrl(form, query) {
        const url = new URL(form.action || window.location.href, window.location.href);
        const sourceInput = form.querySelector('input[type="hidden"][name="from"]');
        if (sourceInput && sourceInput.value) {
            url.searchParams.set("from", sourceInput.value);
        }
        if (query) {
            url.searchParams.set("q", query);
        } else {
            url.searchParams.delete("q");
        }
        return url.href;
    }

    function setText(node, value) {
        if (node) {
            node.textContent = value || "—";
        }
    }

    function getForm() {
        return document.getElementById("schedule-draft-placement-form");
    }

    function getSubmitButton() {
        return document.getElementById("submit-draft-placement-btn");
    }

    function formatNumber(value) {
        if (value === null || value === undefined || value === "") {
            return "—";
        }
        const numericValue = Number(value);
        if (!Number.isFinite(numericValue)) {
            return "—";
        }
        return numericValue.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
    }

    function setPreviewValue(id, value) {
        setText(document.getElementById(id), formatNumber(value));
    }

    function setSubmitEnabled(isEnabled) {
        const button = getSubmitButton();
        if (button) {
            button.disabled = !isEnabled;
            button.classList.toggle("is-disabled", !isEnabled);
        }
    }

    function setPreviewState(state) {
        const panel = document.getElementById("draft-placement-preview-panel");
        if (!panel) {
            return;
        }
        panel.classList.remove("is-idle", "is-loading", "is-ready", "is-warning", "is-error");
        panel.classList.add("is-" + state);
    }

    function setHint(message, state) {
        const hint = document.getElementById("draft-placement-form-hint");
        if (!hint) {
            return;
        }
        hint.textContent = message || "Выберите даты, чтобы проверить списываемые дни, остаток и риск состава.";
        hint.classList.remove("is-success", "is-warning", "is-error");
        if (state) {
            hint.classList.add("is-" + state);
        }
    }

    function abortPreviewRequest() {
        previewRequestId += 1;
        if (previewController) {
            previewController.abort();
            previewController = null;
        }
    }

    function abortUrgentPreviewRequest() {
        urgentPreviewRequestId += 1;
        if (urgentPreviewController) {
            urgentPreviewController.abort();
            urgentPreviewController = null;
        }
    }

    function openNativeDatePicker(input) {
        if (!input || input.disabled || input.readOnly || typeof input.showPicker !== "function") {
            return;
        }
        try {
            input.showPicker();
        } catch (error) {
            // Browsers can require showPicker to run directly from a user gesture.
        }
    }

    function syncDateInputVisualState(input) {
        if (!input || input.type !== "date") {
            return;
        }
        input.classList.toggle("is-empty", !input.value);
    }

    function initScheduleDraftSearch() {
        const previousController = window.__scheduleDraftSearchController;
        if (previousController) {
            previousController.abort();
            window.__scheduleDraftSearchController = null;
        }
        const root = document.querySelector("[data-page='schedule-draft']");
        if (!root) {
            return;
        }

        const controller = new AbortController();
        const signal = controller.signal;
        window.__scheduleDraftSearchController = controller;

        const searchForm = root.querySelector("[data-schedule-draft-search]");
        const searchInput = searchForm ? searchForm.querySelector("[data-schedule-draft-search-input]") : null;
        const searchToggle = searchForm ? searchForm.querySelector("[data-schedule-draft-search-toggle]") : null;
        const searchClear = searchForm ? searchForm.querySelector("[data-schedule-draft-search-clear]") : null;
        if (!searchForm || !searchInput) {
            return;
        }

        let currentSearch = normalizeSearch(searchInput.value);
        let searchTimer = null;

        function setSearchOpen(isOpen) {
            const shouldOpen = Boolean(isOpen || currentSearch);
            searchForm.classList.toggle("is-open", shouldOpen);
            if (searchToggle) {
                searchToggle.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
            }
        }

        function syncSearchControls() {
            const hasFocus = searchForm.contains(document.activeElement);
            setSearchOpen(hasFocus || Boolean(currentSearch));
            if (searchClear) {
                searchClear.hidden = !currentSearch;
            }
        }

        function focusSearchInput() {
            searchInput.focus({ preventScroll: true });
            window.requestAnimationFrame(function () {
                searchInput.focus({ preventScroll: true });
                window.requestAnimationFrame(function () {
                    searchInput.focus({ preventScroll: true });
                });
            });
        }

        function submitSearch() {
            window.clearTimeout(searchTimer);
            navigateTo(buildSearchUrl(searchForm, currentSearch), { focusSearch: true });
        }

        function scheduleSearch() {
            window.clearTimeout(searchTimer);
            searchTimer = window.setTimeout(submitSearch, SEARCH_DEBOUNCE_MS);
        }

        searchForm.addEventListener("submit", function (event) {
            event.preventDefault();
            currentSearch = normalizeSearch(searchInput.value);
            searchInput.value = currentSearch;
            syncSearchControls();
            submitSearch();
        }, { signal: signal });

        searchInput.addEventListener("input", function () {
            currentSearch = normalizeSearch(searchInput.value);
            syncSearchControls();
            scheduleSearch();
        }, { signal: signal });

        searchForm.addEventListener("focusout", function () {
            window.setTimeout(syncSearchControls, 0);
        }, { signal: signal });

        if (searchToggle) {
            searchToggle.addEventListener("pointerdown", function (event) {
                event.preventDefault();
                setSearchOpen(true);
                focusSearchInput();
            }, { signal: signal });

            searchToggle.addEventListener("click", function () {
                setSearchOpen(true);
                focusSearchInput();
            }, { signal: signal });
        }

        if (searchClear) {
            searchClear.addEventListener("click", function () {
                searchInput.value = "";
                currentSearch = "";
                syncSearchControls();
                submitSearch();
                focusSearchInput();
            }, { signal: signal });
        }

        signal.addEventListener("abort", function () {
            window.clearTimeout(searchTimer);
        }, { once: true });

        syncSearchControls();
        if (window.__scheduleDraftFocusSearch) {
            window.__scheduleDraftFocusSearch = false;
            setSearchOpen(true);
            focusSearchInput();
        }
    }

    function resetPreview() {
        const form = getForm();
        const risk = document.getElementById("draft-placement-risk");
        const periodsList = document.getElementById("draft-placement-preview-periods");
        abortPreviewRequest();
        setPreviewState("idle");
        setPreviewValue("draft-placement-calendar-days", null);
        setPreviewValue("draft-placement-chargeable-days", null);
        setPreviewValue("draft-placement-remaining-days", null);
        setPreviewValue("draft-placement-merged-days", null);
        setText(document.getElementById("draft-placement-merged-period"), "Выберите даты");
        if (periodsList) {
            periodsList.hidden = true;
            periodsList.replaceChildren();
        }
        if (risk) {
            risk.hidden = true;
        }
        if (form) {
            form.dataset.previewCanSubmit = "false";
        }
        setSubmitEnabled(false);
        setHint("", "");
    }

    function getPeriodRows() {
        const list = document.getElementById("draft-placement-periods-list");
        return list ? Array.from(list.querySelectorAll("[data-draft-period-row]")) : [];
    }

    function getDateBounds(form) {
        return {
            min: form ? form.dataset.dateMin || "" : "",
            max: form ? form.dataset.dateMax || "" : "",
            year: form ? form.dataset.planningYear || "" : "",
        };
    }

    function syncPeriodRowDateBounds(row) {
        const form = getForm();
        const bounds = getDateBounds(form);
        row.querySelectorAll('input[type="date"]').forEach(function (input) {
            if (bounds.min) {
                input.min = bounds.min;
            }
            if (bounds.max) {
                input.max = bounds.max;
            }
        });
    }

    function updatePeriodRemoveButtons() {
        const rows = getPeriodRows();
        rows.forEach(function (row) {
            const button = row.querySelector("[data-draft-period-remove]");
            if (button) {
                button.disabled = rows.length <= 1;
                button.classList.toggle("is-disabled", rows.length <= 1);
            }
        });
        const addButton = document.querySelector("[data-draft-period-add]");
        if (addButton) {
            addButton.disabled = rows.length >= MAX_MANUAL_PERIODS;
            addButton.classList.toggle("is-disabled", rows.length >= MAX_MANUAL_PERIODS);
        }
    }

    function collectManualPeriods() {
        return getPeriodRows().map(function (row) {
            const start = row.querySelector("[data-period-start]");
            const end = row.querySelector("[data-period-end]");
            return {
                start_date: start ? start.value : "",
                end_date: end ? end.value : "",
            };
        });
    }

    function syncPeriodsJson() {
        const field = document.getElementById("draft-placement-periods-json");
        const periods = collectManualPeriods().filter(function (period) {
            return period.start_date || period.end_date;
        });
        if (field) {
            field.value = JSON.stringify(periods);
        }
        return periods;
    }

    function createPeriodRow(period, options) {
        const list = document.getElementById("draft-placement-periods-list");
        const template = document.getElementById("draft-placement-period-row-template");
        if (!list || !template || getPeriodRows().length >= MAX_MANUAL_PERIODS) {
            return null;
        }

        const fragment = template.content.cloneNode(true);
        const row = fragment.querySelector("[data-draft-period-row]");
        const start = row.querySelector("[data-period-start]");
        const end = row.querySelector("[data-period-end]");
        if (start) {
            start.value = period && period.start_date ? period.start_date : "";
        }
        if (end) {
            end.value = period && period.end_date ? period.end_date : "";
        }
        syncPeriodRowDateBounds(row);
        list.appendChild(fragment);
        row.querySelectorAll('input[type="date"]').forEach(syncDateInputVisualState);
        updatePeriodRemoveButtons();
        syncPeriodsJson();
        if (options && options.focusStart && start) {
            start.focus();
        }
        return row;
    }

    function resetPeriodRows(defaultStartDate) {
        const list = document.getElementById("draft-placement-periods-list");
        if (list) {
            list.replaceChildren();
        }
        createPeriodRow({ start_date: defaultStartDate || "", end_date: "" });
        updatePeriodRemoveButtons();
        syncPeriodsJson();
    }

    function renderPreviewPeriods(periods) {
        const list = document.getElementById("draft-placement-preview-periods");
        if (!list) {
            return;
        }
        list.replaceChildren();
        if (!Array.isArray(periods) || !periods.length) {
            list.hidden = true;
            return;
        }
        periods.forEach(function (period) {
            const row = document.createElement("div");
            row.className = "schedule-draft-placement-preview__period";
            const label = document.createElement("strong");
            label.textContent = period.period_label || "Период";
            const meta = document.createElement("span");
            meta.textContent = [
                period.chargeable_days_label || "",
                period.risk_label ? "риск " + period.risk_label.toLowerCase() : "",
                period.can_place === false ? "нельзя поставить" : "",
            ].filter(Boolean).join(" · ");
            row.append(label, meta);
            list.appendChild(row);
        });
        list.hidden = false;
    }

    function setModalTitle(modal, title, subtitle) {
        if (!modal) {
            return;
        }
        setText(modal.querySelector(".app-modal__title"), title || "");
        setText(modal.querySelector(".app-modal__subtitle"), subtitle || "");
    }

    function setModalState(container, message, iconName) {
        if (!container) {
            return;
        }
        const icon = iconName || "hourglass_top";
        container.innerHTML = [
            '<div class="schedule-draft-modal-state">',
            '<span class="material-icons-sharp" aria-hidden="true">' + icon + "</span>",
            "<p>" + (message || "Загрузка...") + "</p>",
            "</div>",
        ].join("");
    }

    function fetchJson(url) {
        return fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }).then(function (response) {
            return response.json().then(function (payload) {
                if (!response.ok || payload.ok === false) {
                    throw new Error(payload.message || "Не удалось загрузить данные.");
                }
                return payload;
            });
        });
    }

    function dayCalculationCard(label, value, detail, tone) {
        const card = document.createElement("article");
        card.className = "schedule-draft-day-calculation__card";
        if (tone) {
            card.classList.add("schedule-draft-day-calculation__card--" + tone);
        }
        const labelNode = document.createElement("span");
        labelNode.textContent = label || "";
        const valueNode = document.createElement("strong");
        valueNode.textContent = value || "0 д.";
        card.append(labelNode, valueNode);
        if (detail) {
            const detailNode = document.createElement("small");
            detailNode.textContent = detail;
            card.appendChild(detailNode);
        }
        return card;
    }

    function renderDayCalculation(container, payload, options) {
        if (!container) {
            return;
        }
        const mode = options && options.mode ? options.mode : "full";
        container.replaceChildren();
        container.classList.remove("is-loading", "is-error");
        container.classList.add("is-ready");

        const body = document.createElement("div");
        body.className = "schedule-draft-day-calculation__body";
        if (mode === "manual") {
            body.classList.add("schedule-draft-day-calculation__body--manual");
        }

        const summary = document.createElement("div");
        summary.className = "schedule-draft-day-calculation__summary";
        const cards = mode === "manual"
            ? [
                ["Нужно поставить", payload.open_required_days_label, "текущая задача HR", "open"],
                ["Из них срочно", payload.deadline_blocking_days_label, payload.nearest_deadline_label ? "до " + payload.nearest_deadline_label : "срочного срока нет", "mandatory"],
                ["Уже поставлено", payload.placed_days_label, "есть в черновике", "placed"],
                ["Можно разбить", payload.max_periods_label, "за одно ручное размещение", "total"],
            ]
            : (Array.isArray(payload.breakdown) ? payload.breakdown.map(function (item) {
                return [item.label, item.value, item.detail, item.tone];
            }) : []);
        cards.forEach(function (item) {
            summary.appendChild(dayCalculationCard(item[0], item[1], item[2], item[3]));
        });

        const reason = document.createElement("article");
        reason.className = "schedule-draft-day-calculation__reason";
        const icon = document.createElement("span");
        icon.className = "material-icons-sharp";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = "info";
        const reasonText = document.createElement("p");
        reasonText.textContent = payload.reason_text || payload.action_text || "Расчёт построен по текущему черновику.";
        reason.append(icon, reasonText);

        body.append(summary, reason);
        container.appendChild(body);
    }

    function setDayCalculationState(container, message, iconName) {
        if (!container) {
            return;
        }
        container.classList.remove("is-ready");
        setModalState(container, message, iconName || "calculate");
    }

    function loadDayCalculation(url, container, options) {
        if (!url || !container) {
            return Promise.reject(new Error("Нет ссылки на расчёт."));
        }
        container.hidden = false;
        container.classList.add("is-loading");
        setDayCalculationState(container, "Загружаю расчёт дней.", "calculate");
        return fetchJson(url)
            .then(function (payload) {
                renderDayCalculation(container, payload, options || {});
                return payload;
            });
    }

    function openDayCalculationModal(trigger) {
        const modal = document.getElementById("schedule-draft-day-calculation-modal");
        const content = modal ? modal.querySelector("[data-day-calculation-content]") : null;
        const url = trigger ? trigger.dataset.calculationUrl || "" : "";
        if (!modal || !content || !url) {
            return;
        }
        setModalTitle(
            modal,
            trigger.dataset.calculationEmployee ? "Расчёт: " + trigger.dataset.calculationEmployee : "Расчёт дней",
            "Почему система считает именно столько дней отпуска.",
        );
        if (window.appModal && typeof window.appModal.open === "function") {
            window.appModal.open(modal);
        }
        loadDayCalculation(url, content, { mode: "full" })
            .catch(function (error) {
                content.classList.remove("is-loading", "is-ready");
                content.classList.add("is-error");
                setModalState(content, error.message || "Не удалось загрузить расчёт дней.", "error");
            });
    }

    function resetManualDayCalculation() {
        const panel = document.getElementById("draft-placement-day-calculation");
        if (!panel) {
            return;
        }
        panel.hidden = true;
        panel.replaceChildren();
        panel.classList.remove("is-loading", "is-ready", "is-error");
    }

    function loadManualDayCalculation(trigger) {
        const panel = document.getElementById("draft-placement-day-calculation");
        const url = trigger ? trigger.dataset.manualCalculationUrl || trigger.dataset.calculationUrl || "" : "";
        if (!panel || !url) {
            resetManualDayCalculation();
            return;
        }
        loadDayCalculation(url, panel, { mode: "manual" })
            .catch(function () {
                panel.hidden = false;
                panel.classList.remove("is-loading", "is-ready");
                panel.classList.add("is-error");
                setModalState(panel, "Расчёт дней не загрузился. Форму можно заполнить вручную.", "info");
            });
    }

    function updateRisk(payload) {
        const risk = document.getElementById("draft-placement-risk");
        if (!risk) {
            return;
        }

        const hasRiskText = payload.risk_short_reason || payload.risk_recommended_action || Number(payload.risk_score) > 0;
        risk.hidden = !hasRiskText;
        if (!hasRiskText) {
            return;
        }

        const riskLabel = payload.risk_score
            ? payload.risk_label + " · " + payload.risk_score + "%"
            : payload.risk_label;
        setText(document.getElementById("draft-placement-risk-label"), riskLabel || "Низкий");
        setText(document.getElementById("draft-placement-risk-reason"), payload.risk_short_reason || "");
        setText(document.getElementById("draft-placement-risk-action"), payload.risk_recommended_action || "");
        risk.classList.toggle("is-conflict", Boolean(payload.risk_is_conflict));
        risk.classList.toggle("is-high", payload.risk_label === "Высокий");
    }

    function setUrgentSubmitEnabled(form, isEnabled) {
        const button = form ? form.querySelector("[data-urgent-submit]") : null;
        if (button) {
            button.disabled = !isEnabled;
            button.classList.toggle("is-disabled", !isEnabled);
        }
        if (form) {
            form.dataset.urgentCanSubmit = isEnabled ? "true" : "false";
        }
    }

    function setUrgentHint(form, message, state) {
        const hint = form ? form.querySelector("[data-urgent-hint]") : null;
        if (!hint) {
            return;
        }
        hint.textContent = message || "Выберите предложенный период или укажите даты вручную.";
        hint.classList.remove("is-success", "is-warning", "is-error");
        if (state) {
            hint.classList.add("is-" + state);
        }
    }

    function setUrgentPreviewState(form, state) {
        const panel = form ? form.querySelector("[data-urgent-preview]") : null;
        if (!panel) {
            return;
        }
        panel.hidden = false;
        panel.classList.remove("is-idle", "is-loading", "is-ready", "is-warning", "is-error");
        panel.classList.add("is-" + state);
    }

    function resetUrgentPreview(form, hidePanel) {
        const panel = form ? form.querySelector("[data-urgent-preview]") : null;
        const risk = form ? form.querySelector("[data-urgent-risk]") : null;
        abortUrgentPreviewRequest();
        if (panel) {
            panel.classList.remove("is-loading", "is-ready", "is-warning", "is-error");
            panel.classList.add("is-idle");
            panel.hidden = Boolean(hidePanel);
        }
        setText(form ? form.querySelector("[data-urgent-period]") : null, "Выберите даты");
        setText(form ? form.querySelector("[data-urgent-calendar-days]") : null, null);
        setText(form ? form.querySelector("[data-urgent-chargeable-days]") : null, null);
        if (risk) {
            risk.hidden = true;
            risk.classList.remove("is-high", "is-conflict");
        }
    }

    function updateUrgentRisk(form, payload) {
        const risk = form ? form.querySelector("[data-urgent-risk]") : null;
        if (!risk) {
            return;
        }
        const hasRiskText = payload.risk_short_reason || payload.risk_recommended_action || Number(payload.risk_score) > 0;
        risk.hidden = !hasRiskText;
        risk.classList.toggle("is-conflict", Boolean(payload.risk_is_conflict));
        risk.classList.toggle("is-high", !payload.risk_is_conflict && (payload.risk_label === "Высокий" || payload.risk_level === "high"));
        if (!hasRiskText) {
            return;
        }

        const riskLabel = payload.risk_score
            ? payload.risk_label + " · " + payload.risk_score + "%"
            : payload.risk_label;
        setText(form.querySelector("[data-urgent-risk-label]"), riskLabel || "Низкий");
        setText(form.querySelector("[data-urgent-risk-reason]"), payload.risk_short_reason || "");
        setText(form.querySelector("[data-urgent-risk-action]"), payload.risk_recommended_action || "");
    }

    function applyUrgentPreviewPayload(form, payload) {
        if (!form) {
            return;
        }
        setText(form.querySelector("[data-urgent-period]"), payload.period_label || "Выбранный период");
        setText(form.querySelector("[data-urgent-calendar-days]"), formatNumber(payload.calendar_days));
        setText(form.querySelector("[data-urgent-chargeable-days]"), formatNumber(payload.chargeable_days));
        updateUrgentRisk(form, payload);

        const isWarning = Boolean(payload.risk_is_conflict) || payload.risk_label === "Высокий";
        if (payload.can_submit) {
            setUrgentPreviewState(form, isWarning ? "warning" : "ready");
            setUrgentHint(form, payload.message || "Период можно отправить руководителю.", isWarning ? "warning" : "success");
            setUrgentSubmitEnabled(form, true);
            return;
        }

        setUrgentPreviewState(form, "error");
        setUrgentHint(form, payload.message || "Проверьте выбранный период.", "error");
        setUrgentSubmitEnabled(form, false);
    }

    function getUrgentDateValues(form) {
        const startField = form ? form.querySelector('[name="manual_start_date"]') : null;
        const endField = form ? form.querySelector('[name="manual_end_date"]') : null;
        return {
            startField: startField,
            endField: endField,
            startDate: startField ? startField.value : "",
            endDate: endField ? endField.value : "",
        };
    }

    function clearUrgentSystemOptions(form) {
        if (!form) {
            return;
        }
        form.querySelectorAll('input[name="selected_option"]').forEach(function (radio) {
            radio.checked = false;
        });
    }

    function clearUrgentManualDates(form) {
        if (!form) {
            return;
        }
        form.querySelectorAll('input[type="date"]').forEach(function (input) {
            input.value = "";
            syncDateInputVisualState(input);
        });
    }

    function validateUrgentManualDatesLocally(form) {
        const values = getUrgentDateValues(form);
        if (!values.startDate && !values.endDate) {
            resetUrgentPreview(form, true);
            setUrgentHint(form, "Выберите предложенный период или укажите даты вручную.", "");
            setUrgentSubmitEnabled(form, false);
            return false;
        }
        if (!values.startDate || !values.endDate) {
            resetUrgentPreview(form, false);
            setUrgentPreviewState(form, "error");
            setUrgentHint(form, "Укажите дату начала и дату окончания.", "error");
            setUrgentSubmitEnabled(form, false);
            return false;
        }
        if (values.endDate < values.startDate) {
            resetUrgentPreview(form, false);
            setUrgentPreviewState(form, "error");
            setUrgentHint(form, "Дата окончания не может быть раньше даты начала.", "error");
            setUrgentSubmitEnabled(form, false);
            return false;
        }
        return true;
    }

    function requestUrgentPreview(form) {
        if (!form || !validateUrgentManualDatesLocally(form)) {
            return;
        }

        const previewUrl = form.dataset.urgentPreviewUrl || "";
        const values = getUrgentDateValues(form);
        if (!previewUrl) {
            resetUrgentPreview(form, false);
            setUrgentPreviewState(form, "error");
            setUrgentHint(form, "Проверка недоступна. Обновите страницу и попробуйте ещё раз.", "error");
            setUrgentSubmitEnabled(form, false);
            return;
        }

        abortUrgentPreviewRequest();
        const requestId = urgentPreviewRequestId;
        urgentPreviewController = new AbortController();
        setUrgentPreviewState(form, "loading");
        setUrgentHint(form, "Проверяем дни, срок использования и риск состава...", "");
        setUrgentSubmitEnabled(form, false);

        const url = new URL(previewUrl, window.location.origin);
        url.searchParams.set("start_date", values.startDate);
        url.searchParams.set("end_date", values.endDate);
        url.searchParams.set("required_days", form.querySelector('[name="required_days"]').value || "");
        url.searchParams.set("deadline", form.querySelector('[name="deadline"]').value || "");

        fetch(url.toString(), {
            method: "GET",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            credentials: "same-origin",
            signal: urgentPreviewController.signal,
        })
            .then(function (response) {
                return response.json().catch(function () {
                    return {
                        can_submit: false,
                        message: "Не удалось разобрать ответ проверки.",
                    };
                }).then(function (payload) {
                    if (!response.ok && !payload.message) {
                        payload.message = "Не удалось проверить период.";
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                if (requestId !== urgentPreviewRequestId) {
                    return;
                }
                urgentPreviewController = null;
                applyUrgentPreviewPayload(form, payload);
            })
            .catch(function (error) {
                if (error && error.name === "AbortError") {
                    return;
                }
                if (requestId !== urgentPreviewRequestId) {
                    return;
                }
                urgentPreviewController = null;
                resetUrgentPreview(form, false);
                setUrgentPreviewState(form, "error");
                setUrgentHint(form, "Не удалось проверить период. Попробуйте ещё раз.", "error");
                setUrgentSubmitEnabled(form, false);
            });
    }

    function applyUrgentSystemOption(target) {
        const form = target ? target.closest(".schedule-draft-urgent-closure-form") : null;
        if (!form || !target.checked) {
            return;
        }
        abortUrgentPreviewRequest();
        clearUrgentManualDates(form);
        resetUrgentPreview(form, true);
        const isWarning = target.dataset.riskConflict === "true" || target.dataset.riskHigh === "true";
        setUrgentHint(form, target.dataset.optionMessage || "Период можно отправить руководителю.", isWarning ? "warning" : "success");
        setUrgentSubmitEnabled(form, true);
    }

    function syncUrgentDemoControls(form) {
        if (!form) {
            return;
        }
        const managerCheckbox = form.querySelector("[data-urgent-demo-manager]");
        const employeeCheckbox = form.querySelector("[data-urgent-demo-employee]");
        const responseGroup = form.querySelector("[data-urgent-demo-response-group]");
        const responseInputs = form.querySelectorAll("[data-urgent-demo-response]");
        const canEmployeeReply = Boolean(managerCheckbox && managerCheckbox.checked);
        if (employeeCheckbox) {
            employeeCheckbox.disabled = !canEmployeeReply;
            if (!canEmployeeReply) {
                employeeCheckbox.checked = false;
            }
        }
        const canChooseResponse = canEmployeeReply && Boolean(employeeCheckbox && employeeCheckbox.checked);
        responseInputs.forEach(function (input) {
            input.disabled = !canChooseResponse;
        });
        if (responseGroup) {
            responseGroup.classList.toggle("is-disabled", !canChooseResponse);
        }
    }

    function resetUrgentForm(form) {
        if (!form) {
            return;
        }
        abortUrgentPreviewRequest();
        form.reset();
        form.querySelectorAll('input[type="date"]').forEach(syncDateInputVisualState);
        syncUrgentDemoControls(form);
        resetUrgentPreview(form, true);
        setUrgentHint(form, "Выберите предложенный период или укажите даты вручную.", "");
        setUrgentSubmitEnabled(form, false);
    }

    function restoreUrgentModalFromQuery() {
        const params = new URLSearchParams(window.location.search);
        const modalId = params.get("open_modal") || "";
        if (!modalId || modalId.indexOf("urgent-closure-") !== 0) {
            return;
        }

        const modal = document.getElementById(modalId);
        if (!modal) {
            return;
        }
        const errorMessage = params.get("modal_error") || "Период не отправлен. Проверьте даты и попробуйте ещё раз.";
        window.requestAnimationFrame(function () {
            if (window.appModal && typeof window.appModal.open === "function") {
                window.appModal.open(modal);
            }
            const form = modal.querySelector(".schedule-draft-urgent-closure-form");
            setUrgentHint(form, errorMessage, "error");
            setUrgentSubmitEnabled(form, false);
        });

        params.delete("open_modal");
        params.delete("modal_error");
        const nextQuery = params.toString();
        const nextUrl = window.location.pathname + (nextQuery ? "?" + nextQuery : "") + window.location.hash;
        window.history.replaceState({}, "", nextUrl);
    }

    function applyPreviewPayload(payload) {
        const form = getForm();
        if (!form) {
            return;
        }

        setPreviewValue("draft-placement-calendar-days", payload.calendar_days);
        setPreviewValue("draft-placement-chargeable-days", payload.chargeable_days);
        setPreviewValue("draft-placement-remaining-days", payload.remaining_after_placement);
        setPreviewValue("draft-placement-merged-days", Array.isArray(payload.periods) ? payload.periods.length : null);
        setText(
            document.getElementById("draft-placement-merged-period"),
            Array.isArray(payload.periods) && payload.periods.length
                ? payload.periods.length + " период(а)"
                : (payload.will_merge ? payload.merged_period_label : "Без объединения"),
        );
        renderPreviewPeriods(payload.periods || []);
        updateRisk(payload);

        const isWarning = Boolean(payload.risk_is_conflict)
            || payload.risk_label === "Высокий"
            || Boolean(payload.short_gap_warning)
            || Boolean(payload.will_merge);

        if (payload.can_submit) {
            setPreviewState(isWarning ? "warning" : "ready");
            setHint(payload.message || "Период можно поставить в черновик.", isWarning ? "warning" : "success");
            form.dataset.previewCanSubmit = "true";
            setSubmitEnabled(true);
            return;
        }

        setPreviewState("error");
        setHint(payload.message || "Проверьте выбранный период.", "error");
        form.dataset.previewCanSubmit = "false";
        setSubmitEnabled(false);
    }

    function requestPreview() {
        const form = getForm();
        if (!form) {
            return;
        }

        const periods = syncPeriodsJson();
        const hasAnyValue = periods.some(function (period) {
            return period.start_date || period.end_date;
        });
        if (!hasAnyValue) {
            resetPreview();
            return;
        }
        const hasIncomplete = periods.some(function (period) {
            return !period.start_date || !period.end_date;
        });
        if (hasIncomplete) {
            resetPreview();
            setPreviewState("error");
            setHint("Заполните дату начала и окончания для каждого периода.", "error");
            setSubmitEnabled(false);
            return;
        }

        const previewUrl = form.dataset.packagePreviewUrl || form.dataset.previewUrl || "";
        if (!previewUrl) {
            setPreviewState("error");
            setHint("Проверка недоступна. Обновите страницу и попробуйте ещё раз.", "error");
            setSubmitEnabled(false);
            return;
        }

        abortPreviewRequest();
        const requestId = previewRequestId;
        previewController = new AbortController();
        setPreviewState("loading");
        setSubmitEnabled(false);
        setHint("Проверяем даты, дни, остаток и риск состава...", "");

        const csrf = form.querySelector('input[name="csrfmiddlewaretoken"]');
        fetch(previewUrl, {
            method: form.dataset.packagePreviewUrl ? "POST" : "GET",
            body: form.dataset.packagePreviewUrl ? JSON.stringify({ periods: periods }) : null,
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": csrf ? csrf.value : "",
            },
            credentials: "same-origin",
            signal: previewController.signal,
        })
            .then(function (response) {
                return response.json().catch(function () {
                    return {
                        can_submit: false,
                        message: "Не удалось разобрать ответ проверки.",
                    };
                }).then(function (payload) {
                    if (!response.ok && !payload.message) {
                        payload.message = "Не удалось проверить период.";
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                if (requestId !== previewRequestId) {
                    return;
                }
                previewController = null;
                applyPreviewPayload(payload);
            })
            .catch(function (error) {
                if (error && error.name === "AbortError") {
                    return;
                }
                if (requestId !== previewRequestId) {
                    return;
                }
                previewController = null;
                setPreviewState("error");
                setHint("Не удалось проверить период. Попробуйте ещё раз.", "error");
                setSubmitEnabled(false);
            });
    }

    function resetSuggestionsPanel() {
        const panel = document.getElementById("draft-placement-suggestions-panel");
        const list = document.getElementById("draft-placement-suggestions-list");
        if (panel) {
            panel.hidden = true;
            panel.classList.remove("is-loading", "is-error", "is-ready");
        }
        if (list) {
            list.replaceChildren();
        }
        setText(document.getElementById("draft-placement-suggestions-status"), "");
        resetPreferencePanel();
    }

    function resetPreferencePanel() {
        const panel = document.getElementById("draft-placement-preference-panel");
        if (!panel) {
            return;
        }
        panel.hidden = true;
        panel.classList.remove("is-ready", "is-blocked");
        setText(document.getElementById("draft-placement-preference-title"), "");
        setText(document.getElementById("draft-placement-preference-period"), "");
        setText(document.getElementById("draft-placement-preference-status"), "");
        setText(document.getElementById("draft-placement-preference-reason"), "");
    }

    function renderPreferenceOption(option) {
        const panel = document.getElementById("draft-placement-preference-panel");
        if (!panel) {
            return;
        }
        if (!option) {
            resetPreferencePanel();
            return;
        }
        panel.hidden = false;
        panel.classList.toggle("is-ready", option.can_apply !== false);
        panel.classList.toggle("is-blocked", option.can_apply === false);
        setText(
            document.getElementById("draft-placement-preference-title"),
            option.preference_match_label || "Запасной период",
        );
        setText(
            document.getElementById("draft-placement-preference-period"),
            option.full_period_label || option.period_label || [option.start_date, option.end_date].filter(Boolean).join(" - "),
        );
        setText(
            document.getElementById("draft-placement-preference-status"),
            option.status_label || (option.can_apply === false ? "Не подходит" : "Учтено"),
        );
        setText(
            document.getElementById("draft-placement-preference-reason"),
            option.reason || option.explanation || option.message || "",
        );
    }

    function renderSuggestionOption(option) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "schedule-draft-suggestion";
        if (Number(option.rank || 0) === 1) {
            button.classList.add("schedule-draft-suggestion--best");
        }
        if (option.is_preference_candidate) {
            button.classList.add("schedule-draft-suggestion--preference");
        }
        if (option.can_apply === false) {
            button.disabled = true;
            button.classList.add("schedule-draft-suggestion--blocked");
        }
        const periods = Array.isArray(option.periods) && option.periods.length
            ? option.periods
            : [{ start_date: option.start_date || "", end_date: option.end_date || "" }];
        const periodLabel = function (period) {
            return period.period_label
                || period.full_period_label
                || [period.start_date, period.end_date].filter(Boolean).join(" - ");
        };
        const periodLabels = periods.map(periodLabel).filter(Boolean);
        button.dataset.suggestionPeriods = JSON.stringify(periods.map(function (period) {
            return {
                start_date: period.start_date || "",
                end_date: period.end_date || "",
            };
        }));

        const main = document.createElement("span");
        main.className = "schedule-draft-suggestion__main";
        const title = document.createElement("strong");
        title.textContent = option.kind_label
            ? option.kind_label + " · " + (option.chargeable_days_label || "")
            : (option.period_label || "Период");
        const meta = document.createElement("small");
        meta.textContent = option.explanation || option.message || option.period_label || "";
        main.append(title, meta);

        const score = document.createElement("span");
        score.className = "schedule-draft-suggestion__score";
        score.textContent = option.score_label ? "Оценка " + option.score_label : "Без оценки";

        const risk = document.createElement("span");
        risk.className = "schedule-draft-suggestion__risk schedule-draft-suggestion__risk--" + (option.risk_tone || "low");
        risk.textContent = (option.risk_label || "Низкий") + " · " + (option.risk_score || 0) + "%";

        button.append(main, score, risk);
        if (Number(option.rank || 0) === 1) {
            const badge = document.createElement("span");
            badge.className = "schedule-draft-suggestion__badge schedule-draft-suggestion__badge--best";
            badge.textContent = "Лучший вариант";
            button.appendChild(badge);
        }
        if (option.preference_match_label) {
            const badge = document.createElement("span");
            badge.className = "schedule-draft-suggestion__badge";
            badge.textContent = option.preference_match_label;
            button.appendChild(badge);
        }
        if (periodLabels.length) {
            const chips = document.createElement("span");
            chips.className = "schedule-draft-suggestion__periods";
            periodLabels.forEach(function (label) {
                const chip = document.createElement("span");
                chip.textContent = label;
                chips.appendChild(chip);
            });
            button.appendChild(chips);
        }
        return button;
    }

    function loadManualSuggestions(trigger, options) {
        const panel = document.getElementById("draft-placement-suggestions-panel");
        const list = document.getElementById("draft-placement-suggestions-list");
        const url = trigger ? trigger.dataset.manualSuggestionsUrl || trigger.dataset.suggestionsUrl || "" : "";
        if (!panel || !list || !url) {
            return;
        }

        panel.hidden = false;
        panel.classList.remove("is-error", "is-ready");
        panel.classList.add("is-loading");
        list.replaceChildren();
        resetPreferencePanel();
        setText(document.getElementById("draft-placement-suggestions-title"), "Подбираем даты");
        setText(document.getElementById("draft-placement-suggestions-status"), "Загрузка...");

        const requestUrl = new URL(url, window.location.origin);
        if (options && options.limit) {
            requestUrl.searchParams.set("limit", String(options.limit));
        }

        fetchJson(requestUrl.toString())
            .then(function (payload) {
                panel.classList.remove("is-loading", "is-error");
                panel.classList.add("is-ready");
                renderPreferenceOption(payload.preference_option || null);
                setText(document.getElementById("draft-placement-suggestions-title"), payload.needed_label || "Подходящие периоды");
                setText(
                    document.getElementById("draft-placement-suggestions-status"),
                    payload.safe_candidates
                        ? "Показано " + (payload.shown_candidates || 0) + " лучших из " + payload.safe_candidates
                        : "Нет безопасных вариантов",
                );
                list.replaceChildren();
                const options = Array.isArray(payload.options) ? payload.options : [];
                if (!options.length) {
                    setModalState(list, "Система не нашла безопасных дат для быстрого предложения.", "info");
                    return;
                }
                options.forEach(function (option) {
                    list.appendChild(renderSuggestionOption(option));
                });
                if (payload.has_more_options) {
                    const more = document.createElement("button");
                    more.type = "button";
                    more.className = "app-modal__button app-modal__button--secondary schedule-draft-suggestions__more";
                    more.textContent = "Показать ещё";
                    more.addEventListener("click", function () {
                        loadManualSuggestions(trigger, { limit: payload.safe_candidates || 6 });
                    });
                    list.appendChild(more);
                }
            })
            .catch(function (error) {
                panel.classList.remove("is-loading", "is-ready");
                panel.classList.add("is-error");
                setText(document.getElementById("draft-placement-suggestions-status"), "Ошибка");
                setModalState(list, error.message || "Не удалось загрузить предложения.", "error");
            });
    }

    function applySuggestion(button) {
        const form = getForm();
        if (!form || !button || button.disabled) {
            return;
        }
        let periods = [];
        try {
            periods = JSON.parse(button.dataset.suggestionPeriods || "[]");
        } catch (error) {
            periods = [];
        }
        if (!periods.length) {
            return;
        }
        const list = document.getElementById("draft-placement-periods-list");
        if (list) {
            list.replaceChildren();
        }
        periods.slice(0, MAX_MANUAL_PERIODS).forEach(function (period) {
            createPeriodRow(period);
        });
        updatePeriodRemoveButtons();
        syncPeriodsJson();
        requestPreview();
    }

    function openPlacementModal(trigger, options) {
        const modal = document.getElementById("schedule-draft-manual-modal");
        const form = getForm();
        if (!modal || !form || !trigger) {
            return;
        }

        form.action = trigger.dataset.manualActionUrl || "";
        form.dataset.previewUrl = trigger.dataset.manualPreviewUrl || "";
        form.dataset.packagePreviewUrl = trigger.dataset.manualPackagePreviewUrl || "";
        form.dataset.suggestionsUrl = trigger.dataset.manualSuggestionsUrl || trigger.dataset.suggestionsUrl || "";
        form.dataset.calculationUrl = trigger.dataset.manualCalculationUrl || trigger.dataset.calculationUrl || "";
        form.dataset.planningYear = trigger.dataset.manualYear || "";
        form.dataset.dateMin = trigger.dataset.manualDateMin || "";
        form.dataset.dateMax = trigger.dataset.manualDateMax || "";
        form.dataset.previewCanSubmit = "false";
        form.reset();
        const nextField = document.getElementById("draft-placement-next-url");
        if (nextField) {
            nextField.value = trigger.dataset.manualNextUrl || window.location.pathname + window.location.search;
        }

        setText(document.getElementById("schedule-draft-manual-modal-title"), trigger.dataset.manualEmployee || "Распределить отпуск");
        setText(modal.querySelector(".app-modal__subtitle"), trigger.dataset.manualSubtitle || "Выберите период и проверьте размещение.");
        setText(document.getElementById("draft-placement-employee"), trigger.dataset.manualEmployee || "");
        setText(document.getElementById("draft-placement-subtitle"), trigger.dataset.manualSubtitle || "");
        setText(document.getElementById("draft-placement-needed"), trigger.dataset.manualNeeded || "");
        setText(document.getElementById("draft-placement-status"), trigger.dataset.manualStatus || "");
        setText(document.getElementById("draft-placement-primary"), trigger.dataset.manualPrimary || "");
        setText(document.getElementById("draft-placement-backup"), trigger.dataset.manualBackup || "");
        setText(document.getElementById("draft-placement-placed"), trigger.dataset.manualPlaced || "");
        setText(document.getElementById("draft-placement-target"), trigger.dataset.manualTarget || "");
        setText(
            document.getElementById("draft-placement-reason"),
            [trigger.dataset.manualReason, trigger.dataset.manualDetail].filter(Boolean).join(" "),
        );
        resetPreview();
        resetSuggestionsPanel();
        resetManualDayCalculation();
        resetPeriodRows(form.dataset.dateMin || "");

        if (window.appModal && typeof window.appModal.open === "function") {
            window.appModal.open(modal);
        }
        loadManualDayCalculation(trigger);
        loadManualSuggestions(trigger, options && options.limit ? { limit: options.limit } : null);
    }

    function decodeHashId(hash) {
        if (!hash || hash.charAt(0) !== "#") {
            return "";
        }
        try {
            return decodeURIComponent(hash.slice(1));
        } catch (error) {
            return hash.slice(1);
        }
    }

    function focusManualTaskCard(card) {
        if (!card) {
            return;
        }
        if (!card.hasAttribute("tabindex")) {
            card.setAttribute("tabindex", "-1");
        }
        card.classList.remove("is-task-focus");
        // Restart the highlight animation when the same task is clicked twice.
        void card.offsetWidth;
        card.classList.add("is-task-focus");
        try {
            card.focus({ preventScroll: true });
        } catch (error) {
            card.focus();
        }
        window.setTimeout(function () {
            card.classList.remove("is-task-focus");
        }, 1800);
    }

    function scrollToManualTask(link) {
        const targetId = decodeHashId(link ? link.getAttribute("href") : "");
        if (!targetId) {
            return false;
        }

        const target = document.getElementById(targetId);
        if (!target) {
            return false;
        }

        const panelScroll = target.closest(".schedule-draft-panel__scroll");
        if (panelScroll) {
            const panelRect = panelScroll.getBoundingClientRect();
            const targetRect = target.getBoundingClientRect();
            const top = panelScroll.scrollTop + targetRect.top - panelRect.top - 12;
            panelScroll.scrollTo({
                top: Math.max(0, top),
                behavior: "auto",
            });
        } else {
            target.scrollIntoView({
                block: "start",
                behavior: "auto",
            });
        }

        focusManualTaskCard(target);
        return true;
    }

    function feedbackIcon(iconName) {
        const icon = document.createElement("span");
        icon.className = "material-icons-sharp";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = iconName || "fact_check";
        return icon;
    }

    function updateFeedbackSummary(block, summary) {
        const head = block.querySelector(".schedule-draft-feedback__head");
        if (!head) {
            return;
        }

        let total = head.querySelector("strong");
        const totalCount = summary && Number(summary.total) ? Number(summary.total) : 0;
        if (totalCount > 0) {
            if (!total) {
                total = document.createElement("strong");
                head.appendChild(total);
            }
            total.textContent = String(totalCount);
        } else if (total) {
            total.remove();
        }

        let summaryNode = block.querySelector(".schedule-draft-feedback__summary");
        const items = summary && Array.isArray(summary.items) ? summary.items : [];
        if (!items.length) {
            if (summaryNode) {
                summaryNode.remove();
            }
            return;
        }

        if (!summaryNode) {
            summaryNode = document.createElement("div");
            summaryNode.className = "schedule-draft-feedback__summary";
            head.insertAdjacentElement("afterend", summaryNode);
        }
        summaryNode.replaceChildren();
        items.forEach(function (item) {
            const chip = document.createElement("span");
            chip.className = "schedule-draft-feedback__chip schedule-draft-feedback__chip--" + (item.tone || "positive");
            chip.appendChild(feedbackIcon(item.icon));
            chip.appendChild(document.createTextNode((item.summary_label || item.label || "Отзыв") + " "));
            const count = document.createElement("b");
            count.textContent = String(item.count || 0);
            chip.appendChild(count);
            summaryNode.appendChild(chip);
        });
    }

    function updateCurrentFeedback(block, current, form) {
        let currentNode = block.querySelector(".schedule-draft-feedback__current");
        if (!current) {
            if (currentNode) {
                currentNode.remove();
            }
            return;
        }

        if (!currentNode) {
            currentNode = document.createElement("p");
            currentNode.className = "schedule-draft-feedback__current";
            block.insertBefore(currentNode, form || null);
        }

        currentNode.replaceChildren(document.createTextNode("Ваш отзыв: "));
        const label = document.createElement("b");
        label.textContent = current.summary_label || current.label || "сохранён";
        currentNode.appendChild(label);
        if (current.comment) {
            currentNode.appendChild(document.createTextNode(". Комментарий: " + current.comment));
        }
    }

    function setFeedbackStatus(form, message, state) {
        let status = form.querySelector(".schedule-draft-feedback__status");
        if (!status) {
            status = document.createElement("p");
            status.className = "schedule-draft-feedback__status";
            form.appendChild(status);
        }
        status.textContent = message || "";
        status.classList.remove("is-success", "is-error", "is-loading");
        if (state) {
            status.classList.add("is-" + state);
        }
    }

    function setFeedbackSaving(form, isSaving) {
        form.classList.toggle("is-saving", isSaving);
        form.querySelectorAll("button, input[type='text']").forEach(function (control) {
            control.disabled = isSaving;
        });
    }

    function updateFeedbackButtons(form, decision) {
        form.querySelectorAll(".schedule-draft-feedback__button").forEach(function (button) {
            button.classList.toggle("is-active", button.value === decision);
        });
    }

    function updateFeedbackBlock(form, feedback) {
        const block = form.closest(".schedule-draft-feedback");
        if (!block || !feedback) {
            return;
        }
        updateFeedbackSummary(block, feedback.summary || {});
        updateCurrentFeedback(block, feedback.current, form);
        updateFeedbackButtons(form, feedback.current ? feedback.current.decision : "");
    }

    function openReviewModal(trigger) {
        const modal = document.getElementById("schedule-draft-review-modal");
        const content = modal ? modal.querySelector("[data-draft-review-content]") : null;
        const url = trigger ? trigger.dataset.reviewUrl || "" : "";
        if (!modal || !content || !url) {
            return;
        }
        setModalTitle(modal, "Проверка модуля", "Загружаю выбранный период и альтернативы.");
        setModalState(content, "Загружаю проверку модуля.", "hourglass_top");
        if (window.appModal && typeof window.appModal.open === "function") {
            window.appModal.open(modal);
        }

        fetchJson(url)
            .then(function (payload) {
                setModalTitle(modal, payload.title || "Проверка модуля", payload.subtitle || "");
                content.innerHTML = payload.html || "";
            })
            .catch(function (error) {
                setModalTitle(modal, "Проверка модуля", "Данные не загрузились.");
                setModalState(content, error.message || "Не удалось загрузить проверку.", "error");
            });
    }

    function renderAutoPreviewOption(option) {
        const article = document.createElement("article");
        article.className = "schedule-draft-auto-option";

        const main = document.createElement("div");
        main.className = "schedule-draft-auto-option__main";
        const employee = document.createElement("strong");
        employee.textContent = option.employee_name || "Сотрудник";
        const period = document.createElement("span");
        period.textContent = [option.period_label, option.chargeable_days_label].filter(Boolean).join(" · ");
        const department = document.createElement("small");
        department.textContent = option.department_name || "";
        main.append(employee, period, department);

        const meta = document.createElement("div");
        meta.className = "schedule-draft-auto-option__meta";
        const score = document.createElement("span");
        score.textContent = option.score_label ? "Оценка " + option.score_label : "Без оценки";
        const risk = document.createElement("span");
        risk.className = "schedule-draft-auto-option__risk schedule-draft-auto-option__risk--" + (option.risk_tone || "low");
        risk.textContent = (option.risk_label || "Низкий") + " · " + (option.risk_score || 0) + "%";
        meta.append(score, risk);

        article.append(main, meta);
        if (Array.isArray(option.periods) && option.periods.length > 1) {
            const periods = document.createElement("div");
            periods.className = "schedule-draft-suggestion__periods";
            option.periods.forEach(function (periodOption) {
                const chip = document.createElement("span");
                chip.textContent = periodOption.period_label || periodOption.full_period_label || "";
                periods.appendChild(chip);
            });
            article.appendChild(periods);
        }
        if (option.calculation_note || option.proposal_note) {
            const calculation = document.createElement("p");
            calculation.className = "schedule-draft-auto-option__calculation";
            calculation.textContent = [option.calculation_note, option.proposal_note].filter(Boolean).join(" ");
            article.appendChild(calculation);
        }
        return article;
    }

    function renderAutoPreview(payload) {
        const content = document.querySelector("[data-draft-auto-preview-content]");
        const submit = document.querySelector("[data-draft-auto-submit]");
        if (!content) {
            return;
        }
        content.replaceChildren();

        const summary = document.createElement("div");
        summary.className = "schedule-draft-auto-preview__summary";
        [
            ["Будет добавлено", payload.placed_count || 0],
            ["Останется вручную", payload.unresolved_count || 0],
            ["Высокий риск", payload.high_risk_count || 0],
            ["Без варианта", payload.blocked_count || 0],
        ].forEach(function (item) {
            const card = document.createElement("article");
            const label = document.createElement("span");
            label.textContent = item[0];
            const value = document.createElement("strong");
            value.textContent = String(item[1]);
            card.append(label, value);
            summary.appendChild(card);
        });
        content.appendChild(summary);

        const options = Array.isArray(payload.options) ? payload.options : [];
        if (options.length) {
            const list = document.createElement("div");
            list.className = "schedule-draft-auto-preview__list";
            options.forEach(function (option) {
                list.appendChild(renderAutoPreviewOption(option));
            });
            content.appendChild(list);
            if (payload.has_more_options) {
                const note = document.createElement("p");
                note.className = "schedule-draft-auto-preview__note";
                note.textContent = "Показаны первые варианты. При подтверждении система заново проверит все незакрытые дни.";
                content.appendChild(note);
            }
        } else {
            setModalState(content, "Система не нашла безопасных дат, чтобы добрать незакрытые дни.", "info");
        }

        if (submit) {
            submit.disabled = !Number(payload.placed_count || 0);
        }
    }

    function clearAutoPlacePoll() {
        if (autoPlacePollTimer) {
            window.clearTimeout(autoPlacePollTimer);
            autoPlacePollTimer = null;
        }
    }

    function setAutoPlaceSubmitDisabled(disabled) {
        const submit = document.querySelector("[data-draft-auto-submit]");
        if (submit) {
            submit.disabled = Boolean(disabled);
        }
    }

    function renderAutoPlaceJobState(payload) {
        const content = document.querySelector("[data-draft-auto-preview-content]");
        if (!content) {
            return;
        }
        const status = payload.status || "running";
        const percent = Math.max(0, Math.min(100, Number(payload.progress_percent || 0)));
        const wrapper = document.createElement("div");
        wrapper.className = "schedule-draft-auto-job schedule-draft-auto-job--" + status;

        const head = document.createElement("div");
        head.className = "schedule-draft-auto-job__head";
        const title = document.createElement("strong");
        title.textContent = payload.stage_label || "Добрать незакрытые дни";
        const percentLabel = document.createElement("span");
        percentLabel.textContent = Math.round(percent) + "%";
        head.append(title, percentLabel);

        const track = document.createElement("div");
        track.className = "schedule-draft-auto-job__track";
        const bar = document.createElement("span");
        bar.style.width = percent + "%";
        track.appendChild(bar);

        const message = document.createElement("p");
        message.className = "schedule-draft-auto-job__message";
        message.textContent = payload.error_message || payload.message || "Подбираю лучшие пакеты, чтобы добрать незакрытые дни.";

        const stats = document.createElement("div");
        stats.className = "schedule-draft-auto-job__stats";
        [
            ["Обработано", (payload.processed_employees || 0) + " / " + (payload.total_employees || 0)],
            ["Добавлено", payload.placed_count || 0],
            ["Вручную", payload.unresolved_count || 0],
        ].forEach(function (item) {
            const chip = document.createElement("span");
            chip.textContent = item[0] + ": " + item[1];
            stats.appendChild(chip);
        });

        wrapper.append(head, track, message, stats);

        if (status === "succeeded") {
            const note = document.createElement("p");
            note.className = "schedule-draft-auto-job__note";
            note.textContent = "Готово. Сейчас обновлю черновик, чтобы показать новые пункты.";
            wrapper.appendChild(note);
        }

        if (status === "failed") {
            const note = document.createElement("p");
            note.className = "schedule-draft-auto-job__note";
            note.textContent = "Данные не изменялись частично: ошибка сохранена в статусе задачи.";
            wrapper.appendChild(note);
            setAutoPlaceSubmitDisabled(false);
        }

        content.replaceChildren(wrapper);
    }

    function reloadAfterAutoPlace(payload) {
        const targetUrl = payload.detail_url || window.location.href;
        window.setTimeout(function () {
            if (targetUrl === window.location.href) {
                window.location.reload();
                return;
            }
            const navigation = getNavigation();
            if (navigation && typeof navigation.navigate === "function" && navigation.navigate(targetUrl, true)) {
                return;
            }
            window.location.href = targetUrl;
        }, 1200);
    }

    function fetchAutoPlaceJobStatus(statusUrl) {
        return fetch(statusUrl, {
            method: "GET",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }).then(function (response) {
            return response.json().then(function (payload) {
                if (!response.ok || payload.ok === false) {
                    throw new Error(payload.message || payload.error_message || "Не удалось получить статус действия «Добрать незакрытые дни».");
                }
                return payload;
            });
        });
    }

    function pollAutoPlaceJob(statusUrl) {
        if (!statusUrl) {
            return;
        }
        clearAutoPlacePoll();
        autoPlacePollTimer = window.setTimeout(function () {
            fetchAutoPlaceJobStatus(statusUrl)
                .then(function (payload) {
                    renderAutoPlaceJobState(payload);
                    if (payload.status === "succeeded") {
                        clearAutoPlacePoll();
                        reloadAfterAutoPlace(payload);
                        return;
                    }
                    if (payload.status === "failed") {
                        clearAutoPlacePoll();
                        const form = document.querySelector(".schedule-draft-auto-confirm-form");
                        if (form) {
                            form.dataset.autoPlaceRunning = "false";
                        }
                        return;
                    }
                    pollAutoPlaceJob(statusUrl);
                })
                .catch(function (error) {
                    clearAutoPlacePoll();
                    const form = document.querySelector(".schedule-draft-auto-confirm-form");
                    if (form) {
                        form.dataset.autoPlaceRunning = "false";
                    }
                    setAutoPlaceSubmitDisabled(false);
                    setModalState(
                        document.querySelector("[data-draft-auto-preview-content]"),
                        error.message || "Не удалось получить статус действия «Добрать незакрытые дни».",
                        "error",
                    );
                });
        }, 1500);
    }

    function submitAutoPlaceForm(form) {
        if (!form || form.dataset.autoPlaceRunning === "true") {
            return;
        }
        form.dataset.autoPlaceRunning = "true";
        setAutoPlaceSubmitDisabled(true);
        clearAutoPlacePoll();
        renderAutoPlaceJobState({
            status: "queued",
            progress_percent: 0,
            stage_label: "Запуск: добрать незакрытые дни",
            message: "Запускаю фоновый подбор лучших пакетов.",
        });

        fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || payload.ok === false) {
                        throw new Error(payload.message || payload.error_message || "Не удалось запустить действие «Добрать незакрытые дни».");
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                renderAutoPlaceJobState(payload);
                pollAutoPlaceJob(payload.status_url);
            })
            .catch(function (error) {
                form.dataset.autoPlaceRunning = "false";
                setAutoPlaceSubmitDisabled(false);
                setModalState(
                    document.querySelector("[data-draft-auto-preview-content]"),
                    error.message || "Не удалось запустить действие «Добрать незакрытые дни».",
                    "error",
                );
            });
    }

    function loadAutoPreview(trigger) {
        const modal = document.getElementById("schedule-draft-auto-modal");
        const content = modal ? modal.querySelector("[data-draft-auto-preview-content]") : null;
        const submit = modal ? modal.querySelector("[data-draft-auto-submit]") : null;
        const url = trigger ? trigger.dataset.autoPreviewUrl || "" : "";
        if (!modal || !content || !url) {
            return;
        }
        clearAutoPlacePoll();
        const form = modal.querySelector(".schedule-draft-auto-confirm-form");
        if (form) {
            form.dataset.autoPlaceRunning = "false";
        }
        if (submit) {
            submit.disabled = true;
        }
        setModalState(content, "Загружаю предпросмотр действия «Добрать незакрытые дни».", "hourglass_top");
        if (window.appModal && typeof window.appModal.open === "function") {
            window.appModal.open(modal);
        }

        fetchJson(url)
            .then(renderAutoPreview)
            .catch(function (error) {
                if (submit) {
                    submit.disabled = true;
                }
                setModalState(content, error.message || "Не удалось загрузить предпросмотр.", "error");
            });
    }

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || !form.classList.contains("schedule-draft-auto-confirm-form") || !window.fetch) {
            return;
        }
        event.preventDefault();
        submitAutoPlaceForm(form);
    });

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || !form.classList.contains("schedule-draft-feedback__form") || !window.fetch) {
            return;
        }

        event.preventDefault();
        const submitter = event.submitter || document.activeElement;
        const formData = new FormData(form);
        if (submitter && submitter.name) {
            formData.set(submitter.name, submitter.value || "");
        }

        setFeedbackSaving(form, true);
        setFeedbackStatus(form, "Сохраняю отзыв...", "loading");
        fetch(form.action, {
            method: "POST",
            body: formData,
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || !payload.ok) {
                        throw new Error(payload.message || "Не удалось сохранить отзыв.");
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                updateFeedbackBlock(form, payload.feedback);
                setFeedbackStatus(form, payload.message || "Отзыв сохранён.", "success");
            })
            .catch(function (error) {
                setFeedbackStatus(form, error.message || "Не удалось сохранить отзыв.", "error");
            })
            .finally(function () {
                setFeedbackSaving(form, false);
            });
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const addButton = target ? target.closest("[data-draft-period-add]") : null;
        if (!addButton || event.defaultPrevented) {
            return;
        }
        event.preventDefault();
        createPeriodRow({}, { focusStart: true });
        requestPreview();
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const removeButton = target ? target.closest("[data-draft-period-remove]") : null;
        if (!removeButton || event.defaultPrevented || removeButton.disabled) {
            return;
        }
        const row = removeButton.closest("[data-draft-period-row]");
        if (row && getPeriodRows().length > 1) {
            event.preventDefault();
            row.remove();
            updatePeriodRemoveButtons();
            syncPeriodsJson();
            requestPreview();
        }
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target ? target.closest("[data-draft-day-calculation-open]") : null;
        if (!trigger || event.defaultPrevented) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        openDayCalculationModal(trigger);
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target ? target.closest("[data-draft-review-open]") : null;
        if (!trigger || event.defaultPrevented) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        openReviewModal(trigger);
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target ? target.closest("[data-draft-auto-open]") : null;
        if (!trigger) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        loadAutoPreview(trigger);
    }, true);

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target ? target.closest("[data-draft-suggestions-open]") : null;
        if (!trigger || event.defaultPrevented) {
            return;
        }
        const card = trigger.closest(".schedule-draft-manual-card");
        const manualTrigger = card ? card.querySelector("[data-draft-manual-open]") : null;
        if (!manualTrigger) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        openPlacementModal(manualTrigger, { loadSuggestions: true });
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const suggestion = target ? target.closest(".schedule-draft-suggestion") : null;
        if (!suggestion || event.defaultPrevented) {
            return;
        }
        event.preventDefault();
        applySuggestion(suggestion);
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const link = target ? target.closest("[data-draft-task-link]") : null;
        if (!link || event.defaultPrevented) {
            return;
        }
        if (!scrollToManualTask(link)) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target ? target.closest("[data-draft-manual-open]") : null;
        if (!trigger || event.defaultPrevented) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        openPlacementModal(trigger);
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.matches("#schedule-draft-placement-form input[type='date'], .schedule-draft-urgent-closure-form input[type='date']")) {
            return;
        }
        openNativeDatePicker(target);
    });

    document.addEventListener("focusin", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.matches("#schedule-draft-placement-form input[type='date'], .schedule-draft-urgent-closure-form input[type='date']")) {
            return;
        }
        openNativeDatePicker(target);
    });

    document.addEventListener("input", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.matches("#schedule-draft-placement-form input[type='date']")) {
            syncDateInputVisualState(target);
            requestPreview();
        }
        if (target && target.matches(".schedule-draft-urgent-closure-form input[type='date']")) {
            const form = target.closest(".schedule-draft-urgent-closure-form");
            if (form && target.value) {
                clearUrgentSystemOptions(form);
            }
            syncDateInputVisualState(target);
            requestUrgentPreview(form);
        }
    });

    document.addEventListener("change", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.matches("#schedule-draft-placement-form input[type='date']")) {
            syncDateInputVisualState(target);
            requestPreview();
        }
        if (target && target.matches(".schedule-draft-urgent-closure-form input[type='date']")) {
            const form = target.closest(".schedule-draft-urgent-closure-form");
            if (form && target.value) {
                clearUrgentSystemOptions(form);
            }
            syncDateInputVisualState(target);
            requestUrgentPreview(form);
        }
        if (target && target.matches('.schedule-draft-urgent-closure-form input[name="selected_option"]')) {
            applyUrgentSystemOption(target);
        }
        if (target && target.matches("[data-urgent-demo-manager], [data-urgent-demo-employee], [data-urgent-demo-response]")) {
            syncUrgentDemoControls(target.closest(".schedule-draft-urgent-closure-form"));
        }
    });

    document.addEventListener("submit", function (event) {
        if (!event.target || event.target.id !== "schedule-draft-placement-form") {
            return;
        }
        const form = event.target;
        syncPeriodsJson();
        if (form.dataset.previewCanSubmit !== "true") {
            event.preventDefault();
            setPreviewState("error");
            setHint("Сначала выберите даты и дождитесь успешной проверки.", "error");
            setSubmitEnabled(false);
        }
    });

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || !form.classList.contains("schedule-draft-urgent-closure-form")) {
            return;
        }

        if (form.dataset.urgentCanSubmit === "true") {
            return;
        }

        event.preventDefault();
        validateUrgentManualDatesLocally(form);
        if (form.dataset.urgentCanSubmit !== "true") {
            setUrgentHint(form, "Выберите предложенный период или дождитесь успешной проверки ручных дат.", "error");
            setUrgentSubmitEnabled(form, false);
        }
    });

    document.addEventListener("app-modal:open", function (event) {
        const modal = event.target instanceof Element ? event.target : null;
        if (!modal || !modal.id || modal.id.indexOf("urgent-closure-") !== 0) {
            return;
        }
        resetUrgentForm(modal.querySelector(".schedule-draft-urgent-closure-form"));
    });

    document.addEventListener("app-modal:close", function (event) {
        if (event.target && event.target.id === "schedule-draft-manual-modal") {
            abortPreviewRequest();
        }
        if (event.target && event.target.id && event.target.id.indexOf("urgent-closure-") === 0) {
            abortUrgentPreviewRequest();
        }
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initScheduleDraftSearch();
            restoreUrgentModalFromQuery();
        }, { once: true });
    } else {
        initScheduleDraftSearch();
        restoreUrgentModalFromQuery();
    }

    document.addEventListener("app:navigation", initScheduleDraftSearch);
})();
