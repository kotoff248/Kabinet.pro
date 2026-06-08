(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    function setCalendarPageState(isActive) {
        document.documentElement.classList.toggle("is-calendar-page", isActive);
        document.body.classList.toggle("is-calendar-page", isActive);

        if (!isActive) {
            document.documentElement.classList.remove("is-calendar-sizing");
        }
    }

    function revealCalendarBoard() {
        document.documentElement.classList.remove("is-calendar-sizing");
    }

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

    function formatDays(value) {
        return value.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
    }

    function readJsonScript(id, fallbackValue) {
        const node = document.getElementById(id);
        if (!node) {
            return fallbackValue;
        }

        try {
            return JSON.parse(node.textContent || JSON.stringify(fallbackValue));
        } catch (error) {
            return fallbackValue;
        }
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

    function resetDocumentScroll() {
        const scrollingElement = document.scrollingElement || document.documentElement;
        if (scrollingElement) {
            scrollingElement.scrollTop = 0;
        }
        document.body.scrollTop = 0;
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
                persistedUrl = sessionStorage.getItem("calendar:last-url");
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

            const persistedTargetUrl = new URL(persistedUrl, window.location.href);
            if (persistedTargetUrl.searchParams.has("from")) {
                persistedTargetUrl.searchParams.delete("from");
                persistedTargetUrl.searchParams.delete("back_url");
                persistedTargetUrl.searchParams.delete("back_label");
            }

            link.href = persistedTargetUrl.href;
        }, { capture: true });
    }

    Calendar.setCalendarPageState = setCalendarPageState;
    Calendar.revealCalendarBoard = revealCalendarBoard;
    Calendar.parseNumber = parseNumber;
    Calendar.formatDays = formatDays;
    Calendar.readJsonScript = readJsonScript;
    Calendar.getDateRange = getDateRange;
    Calendar.toIsoDate = toIsoDate;
    Calendar.resetDocumentScroll = resetDocumentScroll;
    Calendar.bindCalendarNavigationMemory = bindCalendarNavigationMemory;

    setCalendarPageState(true);
})();
