document.documentElement.classList.add("is-calendar-page");
document.body.classList.add("is-calendar-page");

function revealCalendarBoard() {
    document.documentElement.classList.remove("is-calendar-sizing");
}

function initCalendarPage() {
    const previousController = window.__calendarPageController;
    if (previousController) {
        previousController.abort();
    }

    const filtersForm = document.getElementById("calendar-filters-form");
    if (!filtersForm) {
        return;
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__calendarPageController = controller;

    const segmentedControl = filtersForm.querySelector(".calendar-segmented");
    const viewInputs = filtersForm.querySelectorAll("input[name='view']");
    const yearSelect = filtersForm.querySelector("select[name='year']");
    const monthSelect = filtersForm.querySelector("select[name='month']");
    const monthFilter = monthSelect.closest(".calendar-filter");
    const stepButtons = filtersForm.querySelectorAll("[data-step-control]");
    const customSelects = Array.from(document.querySelectorAll("[data-filter-select], [data-modal-select]"));
    const resultsContainer = document.querySelector("[data-calendar-results]");
    const detailsDataNode = document.getElementById("calendar-details-data");
    const calendarScrollStorageKey = "calendar:board-scroll-state";
    const calendarUrlStorageKey = "calendar:last-url";
    const calendarPath = window.location.pathname;
    let rows = Array.from(document.querySelectorAll("[data-employee-id]"));
    let detailsData = JSON.parse(detailsDataNode.textContent || "{}");
    let isFetchingCalendarResults = false;
    let currentFiltersStateKey = null;

    const detailModal = document.getElementById("calendar-detail-drawer");
    const detailName = document.getElementById("calendar-detail-name");
    const detailMeta = document.getElementById("calendar-detail-meta");
    const detailPeriod = document.getElementById("calendar-detail-period");
    const detailSchedule = document.getElementById("calendar-detail-schedule");
    const detailRequests = document.getElementById("calendar-detail-requests");
    const detailChanged = document.getElementById("calendar-detail-changed");
    const detailUpcoming = document.getElementById("calendar-detail-upcoming");
    const detailUpcomingStatus = document.getElementById("calendar-detail-upcoming-status");
    const selectedList = document.getElementById("calendar-selected-list");
    const yearList = document.getElementById("calendar-year-list");
    let calendarMetricsFrame = null;
    let boardScrollPersistTimeout = null;
    let pendingBoardScrollState = null;
    let boundBoardScrollElement = null;
    let boundGridHeadElement = null;
    let boardScrollGlobalListenersBound = false;
    let isSyncingGridScroll = false;
    const boardScrollPersistDelay = 140;

    const modal = document.getElementById("vacation-modal");
    const transferModal = document.getElementById("schedule-transfer-modal");
    const transferForm = document.getElementById("schedule-transfer-form");
    const transferCurrentPeriod = document.getElementById("transfer-current-period");
    const startDateInput = document.getElementById("start_date");
    const endDateInput = document.getElementById("end_date");
    const submitButton = document.getElementById("submit-vacation-btn");
    const countDays = document.getElementById("count_days");
    const chargeableDaysNode = document.getElementById("chargeable_days");
    const remainingBalance = document.getElementById("remaining_balance");
    const balanceNode = document.getElementById("calendar-balance");
    const vacationTypeSelect = document.getElementById("type_vacation_select");
    const vacationFormHint = document.getElementById("vacation-form-hint");
    const chargePreviewNode = document.getElementById("calendar-charge-preview");
    const chargePreview = chargePreviewNode ? JSON.parse(chargePreviewNode.textContent || "{}") : {};
    const holidayDates = new Set(chargePreview.holiday_dates || []);

    function parseNumber(value, fallbackValue) {
        if (typeof value === "number" && Number.isFinite(value)) {
            return value;
        }

        if (typeof value === "string") {
            const normalizedValue = value.replace(/\s+/g, "").replace(",", ".");
            const parsedValue = Number(normalizedValue);
            if (Number.isFinite(parsedValue)) {
                return parsedValue;
            }
        }

        return fallbackValue;
    }

    const availableBalance = parseNumber(
        balanceNode ? balanceNode.dataset.balance : undefined,
        parseNumber(chargePreview.available_balance, 0)
    );

    function formatDays(value) {
        return value.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
    }

    function submitFilters() {
        flushBoardScrollState();

        if (monthSelect.disabled) {
            monthSelect.disabled = false;
        }
        filtersForm.submit();
    }

    function buildFiltersUrl() {
        const params = new URLSearchParams(new FormData(filtersForm));
        return window.location.pathname + "?" + params.toString();
    }

    function getFiltersStateKey() {
        currentFiltersStateKey = new URLSearchParams(new FormData(filtersForm)).toString();
        return currentFiltersStateKey;
    }

    function getCachedFiltersStateKey() {
        return currentFiltersStateKey || getFiltersStateKey();
    }

    function persistCalendarUrl(url) {
        try {
            sessionStorage.setItem(calendarUrlStorageKey, url || window.location.href);
            sessionStorage.setItem("calendar:path", calendarPath);
        } catch (error) {
            return;
        }
    }

    function persistBoardScrollState(scrollState) {
        if (!scrollState) {
            return;
        }

        const state = {
            key: scrollState.key || getFiltersStateKey(),
            top: Number(scrollState.top) || 0,
            left: Number(scrollState.left) || 0,
        };

        try {
            sessionStorage.setItem(calendarScrollStorageKey, JSON.stringify(state));
        } catch (error) {
            return;
        }
    }

    function clearBoardScrollPersistTimeout() {
        if (!boardScrollPersistTimeout) {
            return;
        }

        window.clearTimeout(boardScrollPersistTimeout);
        boardScrollPersistTimeout = null;
    }

    function flushBoardScrollState() {
        clearBoardScrollPersistTimeout();

        if (!pendingBoardScrollState) {
            return;
        }

        persistBoardScrollState(pendingBoardScrollState);
        pendingBoardScrollState = null;
    }

    function scheduleBoardScrollStatePersist(boardScroll) {
        pendingBoardScrollState = {
            key: getCachedFiltersStateKey(),
            top: boardScroll.scrollTop,
            left: boardScroll.scrollLeft,
        };

        clearBoardScrollPersistTimeout();
        boardScrollPersistTimeout = window.setTimeout(flushBoardScrollState, boardScrollPersistDelay);
    }

    function readPersistedBoardScrollState() {
        try {
            return JSON.parse(sessionStorage.getItem(calendarScrollStorageKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function restorePersistedBoardScrollState() {
        const savedState = readPersistedBoardScrollState();
        if (!savedState || savedState.key !== getFiltersStateKey()) {
            return;
        }

        requestAnimationFrame(function () {
            restoreBoardScrollState(savedState);
        });
    }

    function bindCalendarNavigationMemory() {
        if (window.__calendarNavigationMemoryBound) {
            return;
        }

        window.__calendarNavigationMemoryBound = true;
        document.addEventListener("click", function (event) {
            const link = event.target.closest("[data-sidebar-link]");
            if (!link || !link.href) {
                return;
            }

            let persistedPath = null;
            let persistedUrl = null;
            try {
                persistedPath = sessionStorage.getItem("calendar:path");
                persistedUrl = sessionStorage.getItem(calendarUrlStorageKey);
            } catch (error) {
                return;
            }

            if (!persistedPath || !persistedUrl) {
                return;
            }

            const targetUrl = new URL(link.href, window.location.href);
            if (targetUrl.origin !== window.location.origin || targetUrl.pathname !== persistedPath) {
                return;
            }

            link.href = persistedUrl;
        }, { capture: true });
    }

    function getCalendarBoardShell() {
        return resultsContainer
            ? resultsContainer.querySelector(".calendar-board-scroll")
            : null;
    }

    function getCalendarGridHead() {
        const boardShell = getCalendarBoardShell();
        return boardShell
            ? boardShell.querySelector("[data-calendar-grid-head]")
            : null;
    }

    function getBoardScrollElement() {
        const boardShell = getCalendarBoardShell();
        if (!boardShell) {
            return null;
        }

        return boardShell.querySelector("[data-calendar-grid-body]") || boardShell;
    }

    function syncGridScrollLeft(sourceElement, targetElement) {
        if (!sourceElement || !targetElement || isSyncingGridScroll) {
            return;
        }

        if (Math.abs(targetElement.scrollLeft - sourceElement.scrollLeft) < 1) {
            return;
        }

        isSyncingGridScroll = true;
        targetElement.scrollLeft = sourceElement.scrollLeft;
        isSyncingGridScroll = false;
    }

    function syncGridHeaderScroll() {
        const boardScroll = getBoardScrollElement();
        const gridHead = getCalendarGridHead();
        if (!boardScroll || !gridHead) {
            return;
        }

        gridHead.scrollLeft = boardScroll.scrollLeft;
    }

    function bindBoardScrollMemory() {
        if (!resultsContainer) {
            return;
        }

        const boardScroll = getBoardScrollElement();
        if (!boardScroll) {
            return;
        }

        const gridHead = getCalendarGridHead();

        if (boardScroll !== boundBoardScrollElement) {
            boundBoardScrollElement = boardScroll;
            boardScroll.addEventListener("scroll", function () {
                syncGridScrollLeft(boardScroll, getCalendarGridHead());
                scheduleBoardScrollStatePersist(boardScroll);
            }, { passive: true, signal: signal });
            boardScroll.addEventListener("scrollend", flushBoardScrollState, { passive: true, signal: signal });
        }

        if (gridHead && gridHead !== boundGridHeadElement) {
            boundGridHeadElement = gridHead;
            gridHead.addEventListener("scroll", function () {
                const currentBoardScroll = getBoardScrollElement();
                syncGridScrollLeft(gridHead, currentBoardScroll);
                if (currentBoardScroll) {
                    scheduleBoardScrollStatePersist(currentBoardScroll);
                }
            }, { passive: true, signal: signal });
        }

        if (!boardScrollGlobalListenersBound) {
            boardScrollGlobalListenersBound = true;
            window.addEventListener("pagehide", flushBoardScrollState, { signal: signal });
            document.addEventListener("visibilitychange", function () {
                if (document.visibilityState === "hidden") {
                    flushBoardScrollState();
                }
            }, { signal: signal });
            signal.addEventListener("abort", function () {
                clearBoardScrollPersistTimeout();
                pendingBoardScrollState = null;
            }, { once: true });
        }

        syncGridHeaderScroll();
    }

    function bindRows() {
        rows = Array.from(document.querySelectorAll("[data-employee-id]"));
        rows.forEach(function (row) {
            row.addEventListener("click", function () {
                updateDetailCard(row.dataset.employeeId);
            }, { signal: signal });
        });
    }

    function updateDetailsData(nextDetailsData) {
        detailsData = nextDetailsData || {};
        if (detailsDataNode) {
            detailsDataNode.textContent = JSON.stringify(detailsData);
        }
    }

    function getBoardScrollState(options) {
        const shouldIncludeAnchor = !options || options.includeAnchor !== false;
        const boardScroll = getBoardScrollElement();

        if (!boardScroll) {
            return null;
        }

        const anchorState = shouldIncludeAnchor ? getVisibleEmployeeAnchor(boardScroll) : null;

        return {
            top: boardScroll.scrollTop,
            left: boardScroll.scrollLeft,
            anchorEmployeeId: anchorState ? anchorState.employeeId : null,
            anchorOffset: anchorState ? anchorState.offset : 0,
        };
    }

    function getBoardAnchorY(boardScroll) {
        const boardRect = boardScroll.getBoundingClientRect();
        return boardRect.top + 1;
    }

    function getVisibleEmployeeAnchor(boardScroll) {
        const anchorY = getBoardAnchorY(boardScroll);
        const rows = Array.from(boardScroll.querySelectorAll(".timeline-row, .year-row"));
        const anchorRow = rows.find(function (row) {
            const rowRect = row.getBoundingClientRect();
            return rowRect.bottom > anchorY;
        });

        if (!anchorRow) {
            return null;
        }

        return {
            employeeId: anchorRow.dataset.employeeId,
            offset: anchorY - anchorRow.getBoundingClientRect().top,
        };
    }

    function restoreBoardScrollState(scrollState) {
        if (!scrollState || !resultsContainer) {
            return;
        }

        const nextBoardScroll = getBoardScrollElement();
        if (!nextBoardScroll) {
            return;
        }

        nextBoardScroll.scrollLeft = scrollState.left;
        syncGridHeaderScroll();

        if (scrollState.anchorEmployeeId && window.CSS && CSS.escape) {
            const anchorRow = nextBoardScroll.querySelector(
                '[data-employee-id="' + CSS.escape(scrollState.anchorEmployeeId) + '"]'
            );

            if (anchorRow) {
                const anchorY = getBoardAnchorY(nextBoardScroll);
                const rowRect = anchorRow.getBoundingClientRect();
                nextBoardScroll.scrollTop += rowRect.top - (anchorY - scrollState.anchorOffset);
                syncGridHeaderScroll();
                return;
            }
        }

        nextBoardScroll.scrollTop = scrollState.top;
        syncGridHeaderScroll();
    }

    function resetDocumentScroll() {
        const scrollingElement = document.scrollingElement || document.documentElement;
        if (scrollingElement) {
            scrollingElement.scrollTop = 0;
        }
        document.body.scrollTop = 0;
    }

    function syncCalendarBoardMetrics() {
        const boardShell = getCalendarBoardShell();
        const boardScroll = getBoardScrollElement();
        const yearHeaderGrid = boardShell
            ? boardShell.querySelector("[data-calendar-grid-head] .year-grid--head")
            : null;
        const employeeHead = yearHeaderGrid
            ? yearHeaderGrid.querySelector(".year-head--employee")
            : null;

        if (!boardShell || !boardScroll) {
            return;
        }

        const scrollbarWidth = Math.max(0, boardScroll.offsetWidth - boardScroll.clientWidth);
        boardShell.style.setProperty("--calendar-scrollbar-width", scrollbarWidth + "px");
        syncGridHeaderScroll();

        if (window.matchMedia("(max-width: 900px)").matches || !yearHeaderGrid || !employeeHead) {
            boardShell.style.removeProperty("--calendar-row-height");
            boardShell.style.removeProperty("--calendar-year-tile-size");
            return;
        }

        const rootFontSize = parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16;
        const gridStyles = window.getComputedStyle(yearHeaderGrid);
        const parsedColumnGap = parseFloat(gridStyles.columnGap || gridStyles.gap || "0");
        const columnGap = Number.isFinite(parsedColumnGap) ? parsedColumnGap : 0;
        const gridWidth = yearHeaderGrid.clientWidth;
        const employeeWidth = employeeHead.getBoundingClientRect().width;
        const targetHeight = (gridWidth - employeeWidth - (12 * columnGap)) / 12;

        if (!Number.isFinite(targetHeight) || targetHeight <= 0) {
            return;
        }

        const minYearTileSize = window.matchMedia("(max-width: 1100px)").matches
            ? 3.5 * rootFontSize
            : 3.75 * rootFontSize;
        const yearTileSize = Math.max(minYearTileSize, targetHeight);

        boardShell.style.setProperty("--calendar-row-height", yearTileSize + "px");
        boardShell.style.setProperty("--calendar-year-tile-size", yearTileSize + "px");
    }

    function scheduleCalendarBoardMetricsSync() {
        if (calendarMetricsFrame) {
            window.cancelAnimationFrame(calendarMetricsFrame);
        }

        calendarMetricsFrame = window.requestAnimationFrame(function () {
            calendarMetricsFrame = null;
            syncCalendarBoardMetrics();
        });
    }

    function closeCalendarDetailDrawer() {
        if (detailModal) {
            window.appModal.close(detailModal);
        }
    }

    function updateCalendarBoardMeta(payload) {
        const intro = resultsContainer
            ? resultsContainer.querySelector(".calendar-board-card__intro")
            : null;
        const title = intro ? intro.querySelector("h2") : null;
        const description = intro ? intro.querySelector("p") : null;

        if (title && payload.period_label) {
            title.textContent = payload.period_label;
        }
        if (description && payload.period_description) {
            description.textContent = payload.period_description;
        }
    }

    function updateCalendarBoard(payload) {
        const boardShell = getCalendarBoardShell();

        if (!boardShell || typeof payload.board_html !== "string") {
            throw new Error("Calendar board payload is missing.");
        }

        boardShell.innerHTML = payload.board_html;
        updateCalendarBoardMeta(payload);
        updateDetailsData(payload.calendar_details);
        bindRows();
        bindBoardScrollMemory();
        scheduleCalendarBoardMetricsSync();
    }

    function requestCalendarResults() {
        if (!resultsContainer || isFetchingCalendarResults) {
            return;
        }

        flushBoardScrollState();
        const requestUrl = buildFiltersUrl();
        const boardScrollState = getBoardScrollState();
        persistBoardScrollState(boardScrollState);
        isFetchingCalendarResults = true;
        resultsContainer.classList.add("is-loading");
        closeCustomSelects();
        closeCalendarDetailDrawer();

        fetch(requestUrl, {
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Failed to update calendar results.");
                }
                return response.json();
            })
            .then(function (payload) {
                updateCalendarBoard(payload);
                window.history.replaceState({}, "", requestUrl);
                persistCalendarUrl(new URL(requestUrl, window.location.href).href);
                persistBoardScrollState(boardScrollState);
                requestAnimationFrame(function () {
                    restoreBoardScrollState(boardScrollState);
                });
            })
            .catch(function () {
                submitFilters();
            })
            .finally(function () {
                isFetchingCalendarResults = false;
                resultsContainer.classList.remove("is-loading");
            });
    }

    function syncViewSegmentedState() {
        const activeInput = filtersForm.querySelector("input[name='view']:checked");
        if (!activeInput) {
            return;
        }

        let activeItem = null;
        viewInputs.forEach(function (input) {
            const item = input.closest(".segmented-control__item");
            if (item) {
                const isActive = input.checked;
                item.classList.toggle("is-active", isActive);
                if (isActive) {
                    activeItem = item;
                }
            }
        });
        if (window.KabinetSegmented && typeof window.KabinetSegmented.sync === "function") {
            window.KabinetSegmented.sync(segmentedControl, activeItem);
        }
    }

    function closeCustomSelects(exceptSelect) {
        customSelects.forEach(function (selectWrapper) {
            if (exceptSelect && selectWrapper === exceptSelect) {
                return;
            }
            selectWrapper.classList.remove("is-open");
            const trigger = selectWrapper.querySelector("[data-select-trigger]");
            if (trigger) {
                trigger.setAttribute("aria-expanded", "false");
            }
        });
    }

    function syncCustomSelect(selectWrapper) {
        if (!selectWrapper) {
            return;
        }

        const nativeSelect = selectWrapper.querySelector("select");
        const trigger = selectWrapper.querySelector("[data-select-trigger]");
        const valueNode = selectWrapper.querySelector("[data-select-value]");
        const selectedOption = nativeSelect.options[nativeSelect.selectedIndex];

        if (valueNode && selectedOption) {
            valueNode.textContent = selectedOption.textContent;
        }

        if (trigger) {
            trigger.disabled = nativeSelect.disabled;
            trigger.setAttribute("aria-expanded", selectWrapper.classList.contains("is-open") ? "true" : "false");
        }

        selectWrapper.classList.toggle("is-disabled", nativeSelect.disabled);
        selectWrapper.querySelectorAll("[data-select-option]").forEach(function (optionButton) {
            const isSelected = optionButton.dataset.value === nativeSelect.value;
            optionButton.classList.toggle("is-selected", isSelected);
            optionButton.setAttribute("aria-selected", isSelected ? "true" : "false");
        });
    }

    function syncCustomSelectFromNative(selectElement) {
        syncCustomSelect(selectElement.closest("[data-filter-select], [data-modal-select]"));
    }

    function syncMonthFilterState() {
        const isMonthMode = filtersForm.querySelector("input[name='view']:checked").value === "month";
        monthSelect.disabled = !isMonthMode;
        monthFilter.classList.toggle("is-disabled", !isMonthMode);

        if (!isMonthMode) {
            closeCustomSelects();
        }

        stepButtons.forEach(function (button) {
            if (button.dataset.stepControl === "month") {
                button.disabled = !isMonthMode;
            }
        });
        syncCustomSelectFromNative(monthSelect);
    }

    function syncFormNavigationFields(form) {
        if (!form) {
            return;
        }

        const activeView = filtersForm.querySelector("input[name='view']:checked");
        const nextView = form.querySelector("input[name='next_view_mode']");
        const nextYear = form.querySelector("input[name='next_year']");
        const nextMonth = form.querySelector("input[name='next_month']");
        if (nextView && activeView) {
            nextView.value = activeView.value;
        }
        if (nextYear && yearSelect) {
            nextYear.value = yearSelect.value;
        }
        if (nextMonth && monthSelect) {
            nextMonth.value = monthSelect.value;
        }
    }

    function stepSelect(selectElement, direction) {
        const nextIndex = selectElement.selectedIndex + direction;
        if (nextIndex < 0 || nextIndex >= selectElement.options.length) {
            return;
        }

        selectElement.selectedIndex = nextIndex;
        syncCustomSelectFromNative(selectElement);
        requestCalendarResults();
    }

    function stepMonth(direction) {
        const nextMonthIndex = monthSelect.selectedIndex + direction;

        if (nextMonthIndex >= 0 && nextMonthIndex < monthSelect.options.length) {
            monthSelect.selectedIndex = nextMonthIndex;
            syncCustomSelectFromNative(monthSelect);
            requestCalendarResults();
            return;
        }

        const nextYearIndex = yearSelect.selectedIndex + direction;
        if (nextYearIndex < 0 || nextYearIndex >= yearSelect.options.length) {
            return;
        }

        yearSelect.selectedIndex = nextYearIndex;
        monthSelect.selectedIndex = direction > 0 ? 0 : monthSelect.options.length - 1;
        syncCustomSelectFromNative(yearSelect);
        syncCustomSelectFromNative(monthSelect);
        requestCalendarResults();
    }

    function renderEntriesSafe(container, entries, emptyText) {
        container.innerHTML = "";
        if (!entries.length) {
            const placeholder = document.createElement("p");
            placeholder.className = "calendar-detail-placeholder";
            placeholder.textContent = emptyText;
            container.appendChild(placeholder);
            return;
        }

        entries.forEach(function (item) {
            const article = document.createElement("article");
            article.className = "calendar-drawer__entry status-" + item.status;

            const main = document.createElement("div");
            main.className = "calendar-drawer__entry-main";
            const strong = document.createElement("strong");
            strong.textContent = item.period_label;
            const type = document.createElement("span");
            type.textContent = (item.source_label ? item.source_label + " • " : "") + item.vacation_type_label;
            main.appendChild(strong);
            main.appendChild(type);

            const side = document.createElement("div");
            side.className = "calendar-drawer__entry-side";
            const status = document.createElement("span");
            status.textContent = item.status_label;
            const days = document.createElement("strong");
            days.textContent = item.days + " д.";
            side.appendChild(status);
            side.appendChild(days);

            if (item.can_request_transfer && item.transfer_url) {
                const action = document.createElement("button");
                action.type = "button";
                action.className = "calendar-drawer__entry-action";
                action.dataset.transferOpen = "";
                action.dataset.transferUrl = item.transfer_url;
                action.dataset.transferTitle = item.transfer_title || item.period_label;
                action.textContent = "Запросить перенос";
                side.appendChild(action);
            }

            article.appendChild(main);
            article.appendChild(side);
            container.appendChild(article);
        });
    }

    function openDetailModal() {
        if (!detailModal) {
            return;
        }

        closeVacationModal();
        closeCustomSelects();
        window.appModal.open(detailModal);
    }

    function closeDetailModal() {
        if (!detailModal) {
            return;
        }

        window.appModal.close(detailModal);
    }

    function updateDetailCard(employeeId) {
        const detail = detailsData[String(employeeId)];
        if (!detail) {
            return;
        }

        rows.forEach(function (row) {
            row.classList.toggle("is-active", row.dataset.employeeId === String(employeeId));
        });

        detailName.textContent = detail.employee_name;
        detailMeta.textContent = detail.position + " • " + detail.department;
        detailPeriod.textContent = detail.selected_period_label;
        detailSchedule.textContent = detail.selected_schedule_days + " д.";
        detailRequests.textContent = detail.selected_request_days + " д.";
        detailChanged.textContent = detail.selected_changed_days + " д.";
        detailUpcoming.textContent = detail.upcoming_label;
        detailUpcomingStatus.textContent = detail.upcoming_status || "";
        renderEntriesSafe(selectedList, detail.selected_entries || [], "В выбранном периоде отпусков нет.");
        renderEntriesSafe(yearList, detail.year_entries || [], "За этот год записей пока нет.");
        openDetailModal();
    }

    function closeVacationModal() {
        closeCustomSelects();
        if (!modal) {
            return;
        }

        window.appModal.close(modal);
    }

    function openTransferModal(trigger) {
        if (!transferModal || !transferForm || !trigger) {
            return;
        }

        closeCustomSelects();
        closeDetailModal();
        transferForm.action = trigger.dataset.transferUrl || "";
        transferForm.reset();
        syncFormNavigationFields(transferForm);
        if (transferCurrentPeriod) {
            transferCurrentPeriod.textContent = trigger.dataset.transferTitle || "Выбранный отпуск";
        }
        window.appModal.open(transferModal);
    }

    function getDateRange(start, end) {
        const dates = [];
        const cursor = new Date(start.getTime());
        while (cursor <= end) {
            dates.push(new Date(cursor.getTime()));
            cursor.setDate(cursor.getDate() + 1);
        }
        return dates;
    }

    function toIsoDate(value) {
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, "0");
        const day = String(value.getDate()).padStart(2, "0");
        return year + "-" + month + "-" + day;
    }

    function calculateChargeableDays(start, end, vacationType) {
        if (vacationType !== "paid") {
            return 0;
        }

        return getDateRange(start, end).filter(function (currentDate) {
            return !holidayDates.has(toIsoDate(currentDate));
        }).length;
    }

    function updateVacationHint(message, isError) {
        if (!vacationFormHint) {
            return;
        }

        vacationFormHint.textContent = message || vacationFormHint.dataset.defaultHint || "";
        vacationFormHint.classList.toggle("is-error", Boolean(isError));
    }

    function calculateVacationForm() {
        if (!startDateInput || !endDateInput || !submitButton || !countDays || !remainingBalance || !chargeableDaysNode) {
            return;
        }

        const startValue = startDateInput.value;
        const endValue = endDateInput.value;
        const defaultHint = vacationFormHint ? vacationFormHint.dataset.defaultHint : "";

        if (!startValue || !endValue) {
            countDays.textContent = "0 д.";
            chargeableDaysNode.textContent = "0 д.";
            remainingBalance.textContent = formatDays(availableBalance) + " д.";
            updateVacationHint(defaultHint, false);
            submitButton.disabled = true;
            return;
        }

        const start = new Date(startValue + "T00:00:00");
        const end = new Date(endValue + "T00:00:00");
        if (end < start) {
            countDays.textContent = "0 д.";
            chargeableDaysNode.textContent = "0 д.";
            remainingBalance.textContent = formatDays(availableBalance) + " д.";
            updateVacationHint("Дата окончания не может быть раньше даты начала.", true);
            submitButton.disabled = true;
            return;
        }

        const vacationType = vacationTypeSelect ? vacationTypeSelect.value : "paid";
        const calendarDays = Math.floor((end - start) / (1000 * 60 * 60 * 24)) + 1;
        const chargeableDays = calculateChargeableDays(start, end, vacationType);
        const remaining = vacationType === "paid" ? availableBalance - chargeableDays : availableBalance;

        countDays.textContent = calendarDays + " д.";
        chargeableDaysNode.textContent = chargeableDays + " д.";
        remainingBalance.textContent = formatDays(remaining) + " д.";

        if (vacationType !== "paid") {
            updateVacationHint("Неоплачиваемый и учебный отпуск не уменьшают оплачиваемый баланс.", false);
            submitButton.disabled = false;
            return;
        }

        if (remaining < 0) {
            updateVacationHint("Недостаточно доступных дней для этой заявки.", true);
            submitButton.disabled = true;
            return;
        }

        if (chargeableDays === 0) {
            updateVacationHint("В выбранном периоде нет дней, которые спишутся с баланса.", true);
            submitButton.disabled = true;
            return;
        }

        updateVacationHint(defaultHint, false);
        submitButton.disabled = false;
    }

    customSelects.forEach(function (selectWrapper) {
        const trigger = selectWrapper.querySelector("[data-select-trigger]");
        const nativeSelect = selectWrapper.querySelector("select");

        syncCustomSelect(selectWrapper);

        trigger.addEventListener("click", function (event) {
            event.stopPropagation();
            if (trigger.disabled) {
                return;
            }

            const willOpen = !selectWrapper.classList.contains("is-open");
            closeCustomSelects(selectWrapper);
            selectWrapper.classList.toggle("is-open", willOpen);
            trigger.setAttribute("aria-expanded", willOpen ? "true" : "false");
        }, { signal: signal });

        selectWrapper.querySelectorAll("[data-select-option]").forEach(function (optionButton) {
            optionButton.addEventListener("click", function (event) {
                event.stopPropagation();
                nativeSelect.value = optionButton.dataset.value;
                syncCustomSelect(selectWrapper);
                closeCustomSelects();
                if (selectWrapper.hasAttribute("data-filter-select")) {
                    requestCalendarResults();
                } else {
                    nativeSelect.dispatchEvent(new Event("change", { bubbles: true }));
                }
            }, { signal: signal });
        });
    });

    document.addEventListener("click", function (event) {
        if (!event.target.closest("[data-filter-select], [data-modal-select]")) {
            closeCustomSelects();
        }
    }, { signal: signal });

    document.addEventListener("click", function (event) {
        const transferTrigger = event.target.closest("[data-transfer-open]");
        if (!transferTrigger) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        openTransferModal(transferTrigger);
    }, { signal: signal });

    syncViewSegmentedState();
    syncMonthFilterState();

    viewInputs.forEach(function (input) {
        input.addEventListener("change", function () {
            syncViewSegmentedState();
            syncMonthFilterState();
            requestCalendarResults();
        }, { signal: signal });
    });

    yearSelect.addEventListener("change", function () {
        syncCustomSelectFromNative(yearSelect);
        requestCalendarResults();
    }, { signal: signal });

    monthSelect.addEventListener("change", function () {
        syncCustomSelectFromNative(monthSelect);
        requestCalendarResults();
    }, { signal: signal });

    stepButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            const direction = Number(button.dataset.direction || 0);
            if (!direction) {
                return;
            }

            closeCustomSelects();

            if (button.dataset.stepControl === "year") {
                stepSelect(yearSelect, direction);
                return;
            }

            if (!monthSelect.disabled) {
                stepMonth(direction);
            }
        }, { signal: signal });
    });

    bindRows();
    bindCalendarNavigationMemory();
    bindBoardScrollMemory();
    syncCalendarBoardMetrics();
    revealCalendarBoard();
    resetDocumentScroll();
    window.addEventListener("resize", scheduleCalendarBoardMetricsSync, { signal: signal });
    persistCalendarUrl(window.location.href);
    restorePersistedBoardScrollState();

    if (modal) {
        modal.addEventListener("app-modal:open", function () {
            closeDetailModal();
            closeCustomSelects();
            const vacationForm = document.getElementById("vacation-plan-form");
            syncFormNavigationFields(vacationForm);
            calculateVacationForm();
        }, { signal: signal });
    }

    const vacationForm = document.getElementById("vacation-plan-form");
    if (vacationForm) {
        vacationForm.addEventListener("submit", function () {
            syncFormNavigationFields(vacationForm);
        }, { signal: signal });
    }
    if (transferForm) {
        transferForm.addEventListener("submit", function () {
            syncFormNavigationFields(transferForm);
        }, { signal: signal });
    }

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeCustomSelects();
        }
    }, { signal: signal });

    if (startDateInput) {
        startDateInput.addEventListener("change", calculateVacationForm, { signal: signal });
    }
    if (endDateInput) {
        endDateInput.addEventListener("change", calculateVacationForm, { signal: signal });
    }
    if (vacationTypeSelect) {
        vacationTypeSelect.addEventListener("change", calculateVacationForm, { signal: signal });
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCalendarPage, { once: true });
} else {
    initCalendarPage();
}

document.addEventListener("app:navigation", initCalendarPage);
