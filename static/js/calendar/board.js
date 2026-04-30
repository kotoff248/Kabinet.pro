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
        let isSyncingGridScroll = false;
        let isFetchingCalendarResults = false;
        const boardScrollPersistDelay = 140;

        function persistCalendarUrl(url) {
            try {
                sessionStorage.setItem(context.calendarUrlStorageKey, url || window.location.href);
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

            try {
                sessionStorage.setItem(context.calendarScrollStorageKey, JSON.stringify(state));
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
                key: dependencies.getCachedFiltersStateKey(),
                top: boardScroll.scrollTop,
                left: boardScroll.scrollLeft,
            };

            clearBoardScrollPersistTimeout();
            boardScrollPersistTimeout = window.setTimeout(flushBoardScrollState, boardScrollPersistDelay);
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
            const yearHeaderGrid = boardShell
                ? boardShell.querySelector("[data-calendar-grid-head] .year-grid--head")
                : null;
            const headerGrid = boardShell
                ? boardShell.querySelector("[data-calendar-grid-head] .year-grid--head, [data-calendar-grid-head] .timeline-grid--head")
                : null;
            const employeeHead = headerGrid
                ? headerGrid.querySelector(".year-head--employee, .timeline-head--employee")
                : null;

            if (!boardShell || !boardScroll) {
                return;
            }

            const scrollbarWidth = Math.max(0, boardScroll.offsetWidth - boardScroll.clientWidth);
            boardShell.style.setProperty("--calendar-scrollbar-width", scrollbarWidth + "px");
            syncGridHeaderScroll();

            if (window.matchMedia("(max-width: 900px)").matches || !headerGrid || !employeeHead) {
                boardShell.style.removeProperty("--calendar-row-height");
                boardShell.style.removeProperty("--calendar-year-tile-size");
                return;
            }

            const rootFontSize = parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16;
            const gridStyles = window.getComputedStyle(headerGrid);
            const parsedColumnGap = parseFloat(gridStyles.columnGap || gridStyles.gap || "0");
            const columnGap = yearHeaderGrid
                ? (Number.isFinite(parsedColumnGap) ? parsedColumnGap : 0)
                : getVirtualYearColumnGap(rootFontSize);
            const gridWidth = headerGrid.clientWidth;
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

        function getVirtualYearColumnGap(rootFontSize) {
            if (window.matchMedia("(max-width: 1100px)").matches) {
                return 0.18 * rootFontSize;
            }

            const viewportGap = window.innerWidth * 0.0032;
            return Math.min(Math.max(0.16 * rootFontSize, viewportGap), 0.42 * rootFontSize);
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
            dependencies.bindRows();
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
            persistBoardScrollState(boardScrollState);
            isFetchingCalendarResults = true;
            resultsContainer.classList.add("is-loading");
            dependencies.closeCustomSelects();
            dependencies.closeCalendarDetailDrawer();

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
