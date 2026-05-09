(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createBoardController = function (context, dependencies) {
        const resultsContainer = context.resultsContainer;
        const signal = context.signal;
        let calendarMetricsFrame = null;
        let boardScrollPersistTimeout = null;
        let pendingBoardScrollState = null;
        let boundBoardScrollElement = null;
        let boundGridHeadElement = null;
        let boardScrollGlobalListenersBound = false;
        let gridScrollSyncFrame = null;
        let pendingGridScrollSource = null;
        let pendingGridScrollTarget = null;
        let isSyncingGridScroll = false;
        let isFetchingCalendarResults = false;
        let lastPersistedBoardScrollSignature = "";
        const boardScrollPersistDelay = 220;
        const boardScrollEndPersistDelay = 140;

        function stripModalParams(url) {
            url.searchParams.delete("calendar_modal");
            url.searchParams.delete("calendar_month");
            url.searchParams.delete("calendar_modal_focus");
            url.searchParams.delete("calendar_modal_scroll");
            url.searchParams.delete("calendar_employee");
            url.searchParams.delete("calendar_focus_employee");
            url.searchParams.delete("calendar_focus_start");
            url.searchParams.delete("calendar_focus_end");
        }

        function getMemorySafeCalendarUrl(value) {
            const url = new URL(value || window.location.href, window.location.href);
            stripModalParams(url);
            return url.href;
        }

        function persistCalendarUrl(url) {
            try {
                sessionStorage.setItem(context.calendarUrlStorageKey, getMemorySafeCalendarUrl(url));
                sessionStorage.setItem("calendar:path", context.calendarPath);
            } catch (error) {
                return;
            }
        }

        function persistBoardScrollState(scrollState) {
            if (!scrollState) {
                return;
            }

            const state = {
                key: scrollState.key || dependencies.getFiltersStateKey(),
                top: Number(scrollState.top) || 0,
                left: Number(scrollState.left) || 0,
            };
            const signature = state.key + ":" + state.top + ":" + state.left;

            if (signature === lastPersistedBoardScrollSignature) {
                return;
            }

            try {
                sessionStorage.setItem(context.calendarScrollStorageKey, JSON.stringify(state));
                lastPersistedBoardScrollSignature = signature;
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

        function scheduleBoardScrollStatePersist(boardScroll, delay) {
            pendingBoardScrollState = {
                key: dependencies.getCachedFiltersStateKey(),
                top: boardScroll.scrollTop,
                left: boardScroll.scrollLeft,
            };

            clearBoardScrollPersistTimeout();
            boardScrollPersistTimeout = window.setTimeout(
                flushBoardScrollState,
                Number.isFinite(delay) ? delay : boardScrollPersistDelay
            );
        }

        function readPersistedBoardScrollState() {
            try {
                return JSON.parse(sessionStorage.getItem(context.calendarScrollStorageKey) || "null");
            } catch (error) {
                return null;
            }
        }

        function restorePersistedBoardScrollState() {
            const savedState = readPersistedBoardScrollState();
            if (!savedState || savedState.key !== dependencies.getFiltersStateKey()) {
                return;
            }

            requestAnimationFrame(function () {
                restoreBoardScrollState(savedState);
            });
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

        function getCachedBoardScrollElement() {
            if (boundBoardScrollElement && boundBoardScrollElement.isConnected) {
                return boundBoardScrollElement;
            }
            return getBoardScrollElement();
        }

        function getCachedGridHeadElement() {
            if (boundGridHeadElement && boundGridHeadElement.isConnected) {
                return boundGridHeadElement;
            }
            return getCalendarGridHead();
        }

        function syncGridScrollLeftNow(sourceElement, targetElement) {
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

        function scheduleGridScrollLeftSync(sourceElement, targetElement) {
            if (!sourceElement || !targetElement || sourceElement === targetElement) {
                return;
            }

            pendingGridScrollSource = sourceElement;
            pendingGridScrollTarget = targetElement;

            if (gridScrollSyncFrame) {
                return;
            }

            gridScrollSyncFrame = window.requestAnimationFrame(function () {
                const source = pendingGridScrollSource;
                const target = pendingGridScrollTarget;
                gridScrollSyncFrame = null;
                pendingGridScrollSource = null;
                pendingGridScrollTarget = null;

                if (
                    !source
                    || !target
                    || !source.isConnected
                    || !target.isConnected
                ) {
                    return;
                }

                syncGridScrollLeftNow(source, target);
            });
        }

        function syncGridHeaderScroll() {
            const boardScroll = getCachedBoardScrollElement();
            const gridHead = getCachedGridHeadElement();
            if (!boardScroll || !gridHead) {
                return;
            }

            syncGridScrollLeftNow(boardScroll, gridHead);
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
                    scheduleGridScrollLeftSync(boardScroll, boundGridHeadElement);
                    scheduleBoardScrollStatePersist(boardScroll);
                }, { passive: true, signal: signal });
                boardScroll.addEventListener("scrollend", function () {
                    scheduleBoardScrollStatePersist(boardScroll, boardScrollEndPersistDelay);
                }, { passive: true, signal: signal });
            }

            if (gridHead && gridHead !== boundGridHeadElement) {
                boundGridHeadElement = gridHead;
                gridHead.addEventListener("scroll", function () {
                    const currentBoardScroll = boundBoardScrollElement;
                    scheduleGridScrollLeftSync(gridHead, currentBoardScroll);
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
                document.addEventListener("app:before-navigation", flushBoardScrollState, { signal: signal });
                signal.addEventListener("abort", function () {
                    flushBoardScrollState();
                    if (gridScrollSyncFrame) {
                        window.cancelAnimationFrame(gridScrollSyncFrame);
                    }
                    gridScrollSyncFrame = null;
                    pendingGridScrollSource = null;
                    pendingGridScrollTarget = null;
                    pendingBoardScrollState = null;
                }, { once: true });
            }

            syncGridHeaderScroll();
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

        function getViewModeFromUrl(url) {
            try {
                return new URL(url, window.location.href).searchParams.get("view") || "month";
            } catch (error) {
                return "month";
            }
        }

        function prepareScrollStateForRequest(scrollState, requestUrl) {
            if (!scrollState) {
                return null;
            }

            const currentViewMode = getViewModeFromUrl(window.location.href);
            const nextViewMode = getViewModeFromUrl(requestUrl);
            if (currentViewMode !== nextViewMode) {
                return Object.assign({}, scrollState, { left: 0 });
            }

            return scrollState;
        }

        function scrollBoardToTop() {
            const boardScroll = getBoardScrollElement();
            if (!boardScroll) {
                return;
            }

            const currentLeft = boardScroll.scrollLeft;
            if (typeof boardScroll.scrollTo === "function") {
                boardScroll.scrollTo({
                    top: 0,
                    left: currentLeft,
                    behavior: "smooth",
                });
            } else {
                boardScroll.scrollTop = 0;
            }

            syncGridHeaderScroll();
            persistBoardScrollState({
                key: dependencies.getCachedFiltersStateKey(),
                top: 0,
                left: currentLeft,
            });
        }

        function syncCalendarBoardMetrics() {
            const boardShell = getCalendarBoardShell();
            const boardScroll = getBoardScrollElement();

            if (!boardShell || !boardScroll) {
                return;
            }

            const scrollbarWidth = Math.max(0, boardScroll.offsetWidth - boardScroll.clientWidth);
            boardShell.style.setProperty("--calendar-scrollbar-width", scrollbarWidth + "px");
            syncGridHeaderScroll();
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
            dependencies.updateDetailsData(payload.calendar_details);
            dependencies.updateMonthDetailsData(payload.calendar_month_details);
            dependencies.bindRows();
            dependencies.bindMonthTotals();
            bindBoardScrollMemory();
            scheduleCalendarBoardMetricsSync();
        }

        function requestCalendarResults() {
            if (!resultsContainer || isFetchingCalendarResults) {
                return;
            }

            flushBoardScrollState();
            const requestUrl = dependencies.buildFiltersUrl();
            const boardScrollState = getBoardScrollState();
            const restoreScrollState = prepareScrollStateForRequest(boardScrollState, requestUrl);
            persistBoardScrollState(restoreScrollState);
            isFetchingCalendarResults = true;
            resultsContainer.classList.add("is-loading");
            dependencies.closeCustomSelects();
            dependencies.closeCalendarDetailDrawer();
            dependencies.closeCalendarMonthSummaryDrawer();

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
                    persistBoardScrollState(restoreScrollState);
                    requestAnimationFrame(function () {
                        restoreBoardScrollState(restoreScrollState);
                    });
                })
                .catch(function () {
                    dependencies.submitFilters();
                })
                .finally(function () {
                    isFetchingCalendarResults = false;
                    resultsContainer.classList.remove("is-loading");
                });
        }

        function init() {
            dependencies.bindRows();
            Calendar.bindCalendarNavigationMemory(context.calendarUrlStorageKey);
            bindBoardScrollMemory();
            syncCalendarBoardMetrics();
            Calendar.revealCalendarBoard();
            Calendar.resetDocumentScroll();
            window.addEventListener("resize", scheduleCalendarBoardMetricsSync, { signal: signal });
            persistCalendarUrl(window.location.href);
            restorePersistedBoardScrollState();
        }

        return {
            init: init,
            requestCalendarResults: requestCalendarResults,
            flushBoardScrollState: flushBoardScrollState,
            scrollBoardToTop: scrollBoardToTop,
        };
    };
})();
