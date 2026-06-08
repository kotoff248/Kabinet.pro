(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createControlsController = function (context, dependencies) {
        const filtersForm = context.filtersForm;
        const segmentedControl = context.segmentedControl;
        const viewInputs = context.viewInputs;
        const issueSegmentedControl = context.issueSegmentedControl;
        const issueInputs = Array.from(context.issueInputs || []);
        const sortSegmentedControl = context.sortSegmentedControl;
        const sortInputs = Array.from(context.sortInputs || []);
        const yearSelect = context.yearSelect;
        const monthSelect = context.monthSelect;
        const departmentSelect = context.departmentSelect;
        const groupSelect = context.groupSelect;
        const searchWrapper = context.searchWrapper;
        const searchInput = context.searchInput;
        const searchToggle = context.searchToggle;
        const searchClear = context.searchClear;
        const monthFilter = context.monthFilter;
        const stepButtons = context.stepButtons;
        const customSelects = context.customSelects;
        const legend = context.legend;
        const legendToggle = context.legendToggle;
        const legendPopover = context.legendPopover;
        const signal = context.signal;
        const preservedContextSources = [
            "schedule_planning",
            "profile",
            "employees",
            "applications",
            "departments",
            "analytics",
            "staffing",
            "notifications",
            "calendar",
        ];
        const searchDebounceMs = 250;
        let searchTimer = null;

        function syncFilterSelectLayerState() {
            const boardCard = filtersForm ? filtersForm.closest(".calendar-board-card") : null;
            if (!boardCard) {
                return;
            }

            const hasOpenFilterSelect = Array.from(filtersForm.querySelectorAll("[data-filter-select]")).some(function (selectWrapper) {
                return selectWrapper.classList.contains("is-open");
            });
            boardCard.classList.toggle("has-open-select", hasOpenFilterSelect);
        }

        function submitFilters() {
            if (typeof dependencies.flushBoardScrollState === "function") {
                dependencies.flushBoardScrollState();
            }

            syncContextHiddenInputs();
            if (monthSelect && monthSelect.disabled) {
                monthSelect.disabled = false;
            }
            filtersForm.submit();
        }

        function getCurrentPlanningContextParams() {
            const currentParams = new URL(window.location.href).searchParams;
            const source = currentParams.get("from");
            if (preservedContextSources.indexOf(source) === -1) {
                return [];
            }

            return ["from", "back_url", "back_label"]
                .filter(function (name) {
                    return currentParams.has(name);
                })
                .map(function (name) {
                    return {
                        name: name,
                        value: currentParams.get(name),
                    };
                });
        }

        function appendPlanningContextParams(params) {
            getCurrentPlanningContextParams().forEach(function (entry) {
                if (!params.has(entry.name)) {
                    params.set(entry.name, entry.value);
                }
            });
        }

        function syncContextHiddenInputs() {
            const contextParams = getCurrentPlanningContextParams();
            filtersForm.querySelectorAll("[data-calendar-context-param]").forEach(function (input) {
                input.remove();
            });
            contextParams.forEach(function (entry) {
                const input = document.createElement("input");
                input.type = "hidden";
                input.name = entry.name;
                input.value = entry.value;
                input.dataset.calendarContextParam = "true";
                filtersForm.appendChild(input);
            });
        }

        function buildFiltersUrl() {
            const params = new URLSearchParams(new FormData(filtersForm));
            appendPlanningContextParams(params);
            return window.location.pathname + "?" + params.toString();
        }

        function getFiltersStateKey() {
            context.currentFiltersStateKey = new URLSearchParams(new FormData(filtersForm)).toString();
            return context.currentFiltersStateKey;
        }

        function getCachedFiltersStateKey() {
            return context.currentFiltersStateKey || getFiltersStateKey();
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

        function syncIssueSegmentedState() {
            if (!issueInputs.length) {
                return;
            }

            let activeItem = null;
            issueInputs.forEach(function (input) {
                const item = input.closest(".segmented-control__item");
                if (!item) {
                    return;
                }

                const isActive = input.checked;
                item.classList.toggle("is-active", isActive);
                if (isActive) {
                    activeItem = item;
                }
            });
            if (issueSegmentedControl && window.KabinetSegmented && typeof window.KabinetSegmented.sync === "function") {
                window.KabinetSegmented.sync(issueSegmentedControl, activeItem);
            }
        }

        function syncSortSegmentedState() {
            if (!sortInputs.length) {
                return;
            }

            let activeItem = null;
            sortInputs.forEach(function (input) {
                const item = input.closest(".segmented-control__item");
                if (!item) {
                    return;
                }

                const isActive = input.checked;
                item.classList.toggle("is-active", isActive);
                if (isActive) {
                    activeItem = item;
                }
            });
            if (sortSegmentedControl && window.KabinetSegmented && typeof window.KabinetSegmented.sync === "function") {
                window.KabinetSegmented.sync(sortSegmentedControl, activeItem);
            }
        }

        function normalizeSearchValue(value) {
            return (value || "").replace(/\s+/g, " ").trim();
        }

        function syncSearchState() {
            if (!searchWrapper || !searchInput) {
                return;
            }

            const hasSearch = Boolean(normalizeSearchValue(searchInput.value));
            searchWrapper.classList.toggle("is-open", hasSearch || document.activeElement === searchInput);
            if (searchToggle) {
                searchToggle.setAttribute("aria-expanded", searchWrapper.classList.contains("is-open") ? "true" : "false");
            }
            if (searchClear) {
                searchClear.hidden = !hasSearch;
            }
        }

        function clearSearchTimer() {
            if (!searchTimer) {
                return;
            }

            window.clearTimeout(searchTimer);
            searchTimer = null;
        }

        function requestCalendarResultsFromSearch() {
            clearSearchTimer();
            searchTimer = window.setTimeout(function () {
                searchTimer = null;
                if (searchInput) {
                    searchInput.value = normalizeSearchValue(searchInput.value);
                }
                syncSearchState();
                dependencies.requestCalendarResults();
            }, searchDebounceMs);
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
            syncFilterSelectLayerState();
        }

        function closeLegend() {
            if (!legend || !legendToggle || !legendPopover) {
                return;
            }

            legend.classList.remove("is-open");
            legendToggle.setAttribute("aria-expanded", "false");
            legendPopover.hidden = true;
        }

        function toggleLegend() {
            if (!legend || !legendToggle || !legendPopover) {
                return;
            }

            const willOpen = !legend.classList.contains("is-open");
            closeCustomSelects();
            legend.classList.toggle("is-open", willOpen);
            legendToggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
            legendPopover.hidden = !willOpen;
        }

        function syncCustomSelect(selectWrapper) {
            if (!selectWrapper) {
                return;
            }

            const nativeSelect = selectWrapper.querySelector("select");
            const trigger = selectWrapper.querySelector("[data-select-trigger]");
            const valueNode = selectWrapper.querySelector("[data-select-value]");
            const selectedOption = nativeSelect ? nativeSelect.options[nativeSelect.selectedIndex] : null;

            if (valueNode && selectedOption) {
                valueNode.textContent = selectedOption.textContent;
            }

            if (trigger && nativeSelect) {
                trigger.disabled = nativeSelect.disabled;
                trigger.setAttribute("aria-expanded", selectWrapper.classList.contains("is-open") ? "true" : "false");
            }

            if (nativeSelect) {
                selectWrapper.classList.toggle("is-disabled", nativeSelect.disabled);
            }
            selectWrapper.querySelectorAll("[data-select-option]").forEach(function (optionButton) {
                const isSelected = nativeSelect ? optionButton.dataset.value === nativeSelect.value : false;
                optionButton.classList.toggle("is-selected", isSelected);
                optionButton.setAttribute("aria-selected", isSelected ? "true" : "false");
            });
        }

        function syncGroupOptionsForDepartment() {
            if (!groupSelect) {
                return;
            }

            const departmentValue = departmentSelect ? departmentSelect.value : "all";
            let selectedOptionIsAvailable = false;

            Array.from(groupSelect.options).forEach(function (option) {
                const optionDepartmentId = option.dataset.departmentId || "";
                const isAvailable = (
                    option.value === "all"
                    || !departmentValue
                    || departmentValue === "all"
                    || optionDepartmentId === departmentValue
                );
                option.hidden = !isAvailable;
                option.disabled = !isAvailable;
                if (isAvailable && option.value === groupSelect.value) {
                    selectedOptionIsAvailable = true;
                }
            });

            const groupWrapper = groupSelect.closest("[data-filter-select]");
            if (groupWrapper) {
                groupWrapper.querySelectorAll("[data-select-option]").forEach(function (optionButton) {
                    const optionDepartmentId = optionButton.dataset.departmentId || "";
                    const isAvailable = (
                        optionButton.dataset.value === "all"
                        || !departmentValue
                        || departmentValue === "all"
                        || optionDepartmentId === departmentValue
                    );
                    optionButton.hidden = !isAvailable;
                    optionButton.disabled = !isAvailable;
                    optionButton.classList.toggle("is-hidden", !isAvailable);
                });
            }

            if (!selectedOptionIsAvailable) {
                groupSelect.value = "all";
            }
            syncCustomSelectFromNative(groupSelect);
        }

        function syncCustomSelectFromNative(selectElement) {
            if (!selectElement) {
                return;
            }
            syncCustomSelect(selectElement.closest("[data-filter-select], [data-modal-select]"));
        }

        function syncMonthFilterState() {
            const activeView = filtersForm.querySelector("input[name='view']:checked");
            if (!activeView || !monthSelect || !monthFilter) {
                return;
            }

            const isMonthMode = activeView.value === "month";
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

        function getAdjacentYearOptionIndex(direction) {
            if (!yearSelect) {
                return -1;
            }

            const currentYear = Number(yearSelect.value);
            const options = Array.from(yearSelect.options)
                .map(function (option, index) {
                    return {
                        index: index,
                        value: Number(option.value),
                    };
                })
                .filter(function (option) {
                    return Number.isFinite(option.value);
                });
            const candidates = options.filter(function (option) {
                return direction < 0 ? option.value < currentYear : option.value > currentYear;
            });
            if (!candidates.length) {
                return -1;
            }

            return candidates.reduce(function (best, option) {
                if (!best) {
                    return option;
                }

                if (direction < 0) {
                    return option.value > best.value ? option : best;
                }
                return option.value < best.value ? option : best;
            }, null).index;
        }

        function stepYear(direction) {
            const nextIndex = getAdjacentYearOptionIndex(direction);
            if (nextIndex < 0 || !yearSelect) {
                return;
            }

            yearSelect.selectedIndex = nextIndex;
            syncCustomSelectFromNative(yearSelect);
            dependencies.requestCalendarResults();
        }

        function stepSelect(selectElement, direction) {
            if (!selectElement) {
                return;
            }

            const nextIndex = selectElement.selectedIndex + direction;
            if (nextIndex < 0 || nextIndex >= selectElement.options.length) {
                return;
            }

            selectElement.selectedIndex = nextIndex;
            syncCustomSelectFromNative(selectElement);
            dependencies.requestCalendarResults();
        }

        function stepMonth(direction) {
            if (!monthSelect || !yearSelect) {
                return;
            }

            const nextMonthIndex = monthSelect.selectedIndex + direction;

            if (nextMonthIndex >= 0 && nextMonthIndex < monthSelect.options.length) {
                monthSelect.selectedIndex = nextMonthIndex;
                syncCustomSelectFromNative(monthSelect);
                dependencies.requestCalendarResults();
                return;
            }

            const nextYearIndex = getAdjacentYearOptionIndex(direction);
            if (nextYearIndex < 0) {
                return;
            }

            yearSelect.selectedIndex = nextYearIndex;
            monthSelect.selectedIndex = direction > 0 ? 0 : monthSelect.options.length - 1;
            syncCustomSelectFromNative(yearSelect);
            syncCustomSelectFromNative(monthSelect);
            dependencies.requestCalendarResults();
        }

        function initCustomSelects() {
            customSelects.forEach(function (selectWrapper) {
                const trigger = selectWrapper.querySelector("[data-select-trigger]");
                const nativeSelect = selectWrapper.querySelector("select");

                syncCustomSelect(selectWrapper);

                if (!trigger || !nativeSelect) {
                    return;
                }

                nativeSelect.addEventListener("change", function () {
                    syncCustomSelect(selectWrapper);
                    if (nativeSelect === departmentSelect) {
                        syncGroupOptionsForDepartment();
                    }
                    if (selectWrapper.hasAttribute("data-filter-select")) {
                        dependencies.requestCalendarResults();
                    }
                }, { signal: signal });

                trigger.addEventListener("click", function (event) {
                    event.stopPropagation();
                    if (trigger.disabled) {
                        return;
                    }

                    const willOpen = !selectWrapper.classList.contains("is-open");
                    closeCustomSelects(selectWrapper);
                    selectWrapper.classList.toggle("is-open", willOpen);
                    trigger.setAttribute("aria-expanded", willOpen ? "true" : "false");
                    syncFilterSelectLayerState();
                }, { signal: signal });

                selectWrapper.querySelectorAll("[data-select-option]").forEach(function (optionButton) {
                    optionButton.addEventListener("click", function (event) {
                        event.stopPropagation();
                        nativeSelect.value = optionButton.dataset.value;
                        syncCustomSelect(selectWrapper);
                        if (nativeSelect === departmentSelect) {
                            syncGroupOptionsForDepartment();
                        }
                        closeCustomSelects();
                        if (selectWrapper.hasAttribute("data-filter-select")) {
                            dependencies.requestCalendarResults();
                        } else {
                            nativeSelect.dispatchEvent(new Event("change", { bubbles: true }));
                        }
                    }, { signal: signal });
                });
            });
        }

        function bindFilterControls() {
            syncViewSegmentedState();
            syncIssueSegmentedState();
            syncSortSegmentedState();
            syncSearchState();
            syncMonthFilterState();
            syncGroupOptionsForDepartment();

            viewInputs.forEach(function (input) {
                input.addEventListener("change", function () {
                    syncViewSegmentedState();
                    syncMonthFilterState();
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            });

            if (yearSelect) {
                yearSelect.addEventListener("change", function () {
                    syncCustomSelectFromNative(yearSelect);
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            }

            if (monthSelect) {
                monthSelect.addEventListener("change", function () {
                    syncCustomSelectFromNative(monthSelect);
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            }

            if (departmentSelect) {
                departmentSelect.addEventListener("change", function () {
                    syncCustomSelectFromNative(departmentSelect);
                    syncGroupOptionsForDepartment();
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            }

            if (groupSelect) {
                groupSelect.addEventListener("change", function () {
                    syncCustomSelectFromNative(groupSelect);
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            }

            issueInputs.forEach(function (input) {
                input.addEventListener("change", function () {
                    syncIssueSegmentedState();
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            });

            sortInputs.forEach(function (input) {
                input.addEventListener("change", function () {
                    syncSortSegmentedState();
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            });

            if (sortSegmentedControl) {
                sortSegmentedControl.querySelectorAll(".segmented-control__item").forEach(function (item) {
                    item.addEventListener("click", function () {
                        const input = item.querySelector("input[name='sort']");
                        if (!input || item.classList.contains("is-active")) {
                            return;
                        }

                        input.checked = true;
                        syncSortSegmentedState();
                        dependencies.requestCalendarResults();
                    }, { signal: signal });
                });
            }

            if (searchToggle && searchWrapper && searchInput) {
                function focusSearchInput() {
                    searchInput.focus({ preventScroll: true });
                    try {
                        const caretPosition = searchInput.value.length;
                        searchInput.setSelectionRange(caretPosition, caretPosition);
                    } catch (error) {
                        // Some input types do not support selection ranges.
                    }
                }

                searchToggle.addEventListener("click", function () {
                    const shouldOpen = !searchWrapper.classList.contains("is-open");
                    if (shouldOpen) {
                        searchWrapper.classList.add("is-open");
                        if (searchToggle) {
                            searchToggle.setAttribute("aria-expanded", "true");
                        }
                        focusSearchInput();
                        window.requestAnimationFrame(focusSearchInput);
                    } else {
                        syncSearchState();
                    }
                }, { signal: signal });
            }

            if (searchInput) {
                searchInput.addEventListener("input", function () {
                    syncSearchState();
                    requestCalendarResultsFromSearch();
                }, { signal: signal });
                searchInput.addEventListener("blur", syncSearchState, { signal: signal });
                searchInput.addEventListener("keydown", function (event) {
                    if (event.key !== "Enter") {
                        return;
                    }

                    event.preventDefault();
                    clearSearchTimer();
                    searchInput.value = normalizeSearchValue(searchInput.value);
                    syncSearchState();
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            }

            if (searchClear && searchInput) {
                searchClear.addEventListener("click", function () {
                    clearSearchTimer();
                    searchInput.value = "";
                    syncSearchState();
                    searchInput.focus();
                    dependencies.requestCalendarResults();
                }, { signal: signal });
            }

            stepButtons.forEach(function (button) {
                button.addEventListener("click", function () {
                    const direction = Number(button.dataset.direction || 0);
                    if (!direction) {
                        return;
                    }

                    closeCustomSelects();

                    if (button.dataset.stepControl === "year") {
                        stepYear(direction);
                        return;
                    }

                    if (monthSelect && !monthSelect.disabled) {
                        stepMonth(direction);
                    }
                }, { signal: signal });
            });

            if (legendToggle) {
                legendToggle.addEventListener("click", function (event) {
                    event.stopPropagation();
                    toggleLegend();
                }, { signal: signal });
            }
        }

        function init() {
            initCustomSelects();

            document.addEventListener("click", function (event) {
                if (!event.target.closest("[data-filter-select], [data-modal-select]")) {
                    closeCustomSelects();
                }
                if (legend && !event.target.closest("[data-calendar-legend]")) {
                    closeLegend();
                }
            }, { signal: signal });

            document.addEventListener("keydown", function (event) {
                if (event.key === "Escape") {
                    closeCustomSelects();
                    closeLegend();
                }
            }, { signal: signal });

            bindFilterControls();
            signal.addEventListener("abort", clearSearchTimer, { once: true });
        }

        return {
            init: init,
            submitFilters: submitFilters,
            buildFiltersUrl: buildFiltersUrl,
            getFiltersStateKey: getFiltersStateKey,
            getCachedFiltersStateKey: getCachedFiltersStateKey,
            closeCustomSelects: closeCustomSelects,
            syncFormNavigationFields: syncFormNavigationFields,
        };
    };
})();
