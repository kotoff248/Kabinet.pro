(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    function collectCalendarContext(filtersForm, signal) {
        const monthSelect = filtersForm.querySelector("select[name='month']");
        const balanceNode = document.getElementById("calendar-balance");
        const chargePreview = Calendar.readJsonScript("calendar-charge-preview", {});

        const context = {
            signal: signal,
            filtersForm: filtersForm,
            segmentedControl: filtersForm.querySelector(".calendar-segmented"),
            viewInputs: filtersForm.querySelectorAll("input[name='view']"),
            yearSelect: filtersForm.querySelector("select[name='year']"),
            monthSelect: monthSelect,
            monthFilter: monthSelect ? monthSelect.closest(".calendar-filter") : null,
            stepButtons: filtersForm.querySelectorAll("[data-step-control]"),
            customSelects: Array.from(document.querySelectorAll("[data-filter-select], [data-modal-select]")),
            resultsContainer: document.querySelector("[data-calendar-results]"),
            detailsDataNode: document.getElementById("calendar-details-data"),
            calendarScrollStorageKey: "calendar:board-scroll-state",
            calendarUrlStorageKey: "calendar:last-url",
            calendarPath: window.location.pathname,
            rows: Array.from(document.querySelectorAll("[data-employee-id]")),
            detailsData: Calendar.readJsonScript("calendar-details-data", {}),
            currentFiltersStateKey: null,

            detailModal: document.getElementById("calendar-detail-drawer"),
            detailName: document.getElementById("calendar-detail-name"),
            detailMeta: document.getElementById("calendar-detail-meta"),
            detailPeriod: document.getElementById("calendar-detail-period"),
            detailSchedule: document.getElementById("calendar-detail-schedule"),
            detailRequests: document.getElementById("calendar-detail-requests"),
            detailChanged: document.getElementById("calendar-detail-changed"),
            detailUpcoming: document.getElementById("calendar-detail-upcoming"),
            detailUpcomingStatus: document.getElementById("calendar-detail-upcoming-status"),
            selectedList: document.getElementById("calendar-selected-list"),
            yearList: document.getElementById("calendar-year-list"),

            modal: document.getElementById("vacation-modal"),
            transferModal: document.getElementById("schedule-transfer-modal"),
            transferForm: document.getElementById("schedule-transfer-form"),
            transferCurrentPeriod: document.getElementById("transfer-current-period"),
            startDateInput: document.getElementById("start_date"),
            endDateInput: document.getElementById("end_date"),
            submitButton: document.getElementById("submit-vacation-btn"),
            countDays: document.getElementById("count_days"),
            chargeableDaysNode: document.getElementById("chargeable_days"),
            remainingBalance: document.getElementById("remaining_balance"),
            balanceNode: balanceNode,
            vacationTypeSelect: document.getElementById("type_vacation_select"),
            vacationFormHint: document.getElementById("vacation-form-hint"),
            chargePreviewNode: document.getElementById("calendar-charge-preview"),
            chargePreview: chargePreview,
            holidayDates: new Set(chargePreview.holiday_dates || []),
        };

        context.availableBalance = Calendar.parseNumber(
            balanceNode ? balanceNode.dataset.balance : undefined,
            Calendar.parseNumber(chargePreview.available_balance, 0)
        );

        return context;
    }

    function initCalendarPage() {
        const previousController = window.__calendarPageController;
        if (previousController) {
            previousController.abort();
        }

        const filtersForm = document.getElementById("calendar-filters-form");
        if (!filtersForm) {
            Calendar.setCalendarPageState(false);
            return;
        }

        Calendar.setCalendarPageState(true);

        const controller = new AbortController();
        const signal = controller.signal;
        window.__calendarPageController = controller;

        const context = collectCalendarContext(filtersForm, signal);
        let boardController = null;
        let formsController = null;

        const controlsController = Calendar.createControlsController(context, {
            requestCalendarResults: function () {
                if (boardController) {
                    boardController.requestCalendarResults();
                }
            },
            flushBoardScrollState: function () {
                if (boardController) {
                    boardController.flushBoardScrollState();
                }
            },
        });

        const drawerController = Calendar.createDrawerController(context, {
            closeCustomSelects: function () {
                controlsController.closeCustomSelects();
            },
            closeVacationModal: function () {
                if (formsController) {
                    formsController.closeVacationModal();
                }
            },
        });

        boardController = Calendar.createBoardController(context, {
            buildFiltersUrl: function () {
                return controlsController.buildFiltersUrl();
            },
            getFiltersStateKey: function () {
                return controlsController.getFiltersStateKey();
            },
            getCachedFiltersStateKey: function () {
                return controlsController.getCachedFiltersStateKey();
            },
            closeCustomSelects: function () {
                controlsController.closeCustomSelects();
            },
            closeCalendarDetailDrawer: function () {
                drawerController.closeCalendarDetailDrawer();
            },
            updateDetailsData: function (nextDetailsData) {
                drawerController.updateDetailsData(nextDetailsData);
            },
            bindRows: function () {
                drawerController.bindRows();
            },
            submitFilters: function () {
                controlsController.submitFilters();
            },
        });

        formsController = Calendar.createFormsController(context, {
            closeCustomSelects: function () {
                controlsController.closeCustomSelects();
            },
            closeDetailModal: function () {
                drawerController.closeDetailModal();
            },
            syncFormNavigationFields: function (form) {
                controlsController.syncFormNavigationFields(form);
            },
        });

        controlsController.init();
        formsController.init();
        boardController.init();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initCalendarPage, { once: true });
    } else {
        initCalendarPage();
    }

    document.addEventListener("app:navigation", initCalendarPage);
})();
