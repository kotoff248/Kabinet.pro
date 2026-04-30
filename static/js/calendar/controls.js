(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createControlsController = function (context, dependencies) {
        const filtersForm = context.filtersForm;
        const segmentedControl = context.segmentedControl;
        const viewInputs = context.viewInputs;
        const yearSelect = context.yearSelect;
        const monthSelect = context.monthSelect;
        const monthFilter = context.monthFilter;
        const stepButtons = context.stepButtons;
        const customSelects = context.customSelects;
        const signal = context.signal;

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

            if (monthSelect && monthSelect.disabled) {
                monthSelect.disabled = false;
            }
            filtersForm.submit();
        }

        function buildFiltersUrl() {
            const params = new URLSearchParams(new FormData(filtersForm));
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
            syncMonthFilterState();

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
        }

        function init() {
            initCustomSelects();

            document.addEventListener("click", function (event) {
                if (!event.target.closest("[data-filter-select], [data-modal-select]")) {
                    closeCustomSelects();
                }
            }, { signal: signal });

            document.addEventListener("keydown", function (event) {
                if (event.key === "Escape") {
                    closeCustomSelects();
                }
            }, { signal: signal });

            bindFilterControls();
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
