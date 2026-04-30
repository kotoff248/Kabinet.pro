function initProfileScheduleFilters() {
    const previousController = window.__profileScheduleFiltersController;
    if (previousController) {
        previousController.abort();
    }

    const schedules = Array.from(document.querySelectorAll("[data-profile-schedule]"));
    if (!schedules.length) {
        return;
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__profileScheduleFiltersController = controller;
    const filterStorageKey = "profile-schedule-filters:" + window.location.pathname;

    function syncSegmented(inputs) {
        if (!inputs.length) {
            return;
        }

        const checkedIndex = Math.max(0, inputs.findIndex(function (input) {
            return input.checked;
        }));
        const control = inputs[0].closest(".segmented-control");
        if (control) {
            control.dataset.segmentedIndex = String(checkedIndex);
        }

        inputs.forEach(function (input, index) {
            const item = input.closest(".segmented-control__item");
            if (item) {
                item.classList.toggle("is-active", index === checkedIndex);
            }
        });
    }

    function cardHasYear(card, selectedYear) {
        const years = (card.dataset.years || "").split(/\s+/).filter(Boolean);
        return years.indexOf(String(selectedYear)) !== -1;
    }

    function setHidden(element, isHidden) {
        if (!element) {
            return;
        }
        element.hidden = isHidden;
    }

    function getYearOptions(select) {
        if (!select) {
            return [];
        }

        return Array.from(select.options)
            .map(function (option, index) {
                return {
                    index: index,
                    value: Number(option.value),
                };
            })
            .filter(function (option) {
                return Number.isFinite(option.value);
            });
    }

    function getAdjacentYearIndex(select, direction) {
        if (!select) {
            return -1;
        }

        const options = getYearOptions(select);
        if (!options.length) {
            return -1;
        }

        const currentYear = Number(select.value);
        if (!Number.isFinite(currentYear)) {
            return direction < 0 ? options[0].index : -1;
        }

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

    function updateYearButtons(select, stepButtons) {
        stepButtons.forEach(function (button) {
            const direction = Number(button.dataset.profileScheduleYearStep || 0);
            button.disabled = getAdjacentYearIndex(select, direction) < 0;
        });
    }

    function getYearSelectWrapper(select) {
        return select ? select.closest("[data-profile-schedule-year-select]") : null;
    }

    function closeYearSelect(wrapper) {
        if (!wrapper) {
            return;
        }

        wrapper.classList.remove("is-open");
        const trigger = wrapper.querySelector("[data-profile-schedule-year-trigger]");
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
        }
    }

    function closeYearSelects(exceptWrapper) {
        document.querySelectorAll("[data-profile-schedule-year-select]").forEach(function (wrapper) {
            if (wrapper !== exceptWrapper) {
                closeYearSelect(wrapper);
            }
        });
    }

    function toggleYearSelect(wrapper) {
        if (!wrapper) {
            return;
        }

        const willOpen = !wrapper.classList.contains("is-open");
        closeYearSelects(wrapper);
        wrapper.classList.toggle("is-open", willOpen);

        const trigger = wrapper.querySelector("[data-profile-schedule-year-trigger]");
        if (trigger) {
            trigger.setAttribute("aria-expanded", willOpen ? "true" : "false");
        }
    }

    function syncYearSelect(select) {
        const wrapper = getYearSelectWrapper(select);
        if (!wrapper || !select) {
            return;
        }

        const valueNode = wrapper.querySelector("[data-profile-schedule-year-value]");
        const selectedOption = select.options[select.selectedIndex] || null;
        if (valueNode && selectedOption) {
            valueNode.textContent = selectedOption.textContent;
        }

        wrapper.querySelectorAll("[data-profile-schedule-year-option]").forEach(function (optionButton) {
            const isSelected = optionButton.dataset.value === select.value;
            optionButton.classList.toggle("is-selected", isSelected);
            optionButton.setAttribute("aria-selected", isSelected ? "true" : "false");
        });
    }

    function readFilterState() {
        try {
            return JSON.parse(sessionStorage.getItem(filterStorageKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function writeFilterState(schedule) {
        const yearSelect = schedule.querySelector("[data-profile-schedule-year]");
        const checkedType = Array.from(schedule.querySelectorAll("[data-profile-schedule-type]")).find(function (input) {
            return input.checked;
        });
        const state = {
            year: yearSelect ? yearSelect.value : schedule.dataset.profileScheduleCurrentYear,
            type: checkedType ? checkedType.value : "all",
        };

        try {
            sessionStorage.setItem(filterStorageKey, JSON.stringify(state));
        } catch (error) {
        }
    }

    function clearFilterState() {
        try {
            sessionStorage.removeItem(filterStorageKey);
        } catch (error) {
        }
    }

    function hasYearOption(select, value) {
        if (!select) {
            return false;
        }

        return Array.from(select.options).some(function (option) {
            return option.value === String(value);
        });
    }

    function applyStoredFilterState(schedule) {
        const state = readFilterState();
        if (!state) {
            return;
        }

        const yearSelect = schedule.querySelector("[data-profile-schedule-year]");
        if (yearSelect && state.year !== undefined && hasYearOption(yearSelect, state.year)) {
            yearSelect.value = String(state.year);
        }

        const typeInput = Array.from(schedule.querySelectorAll("[data-profile-schedule-type]")).find(function (input) {
            return input.value === String(state.type || "all");
        });
        if (typeInput) {
            typeInput.checked = true;
        }
    }

    function isDefaultFilterState(schedule) {
        const yearSelect = schedule.querySelector("[data-profile-schedule-year]");
        const checkedType = Array.from(schedule.querySelectorAll("[data-profile-schedule-type]")).find(function (input) {
            return input.checked;
        });
        const selectedYear = yearSelect ? yearSelect.value : schedule.dataset.profileScheduleCurrentYear;
        const selectedType = checkedType ? checkedType.value : "all";

        return selectedYear === String(schedule.dataset.profileScheduleCurrentYear || "") && selectedType === "all";
    }

    function resetFilterState(schedule) {
        const yearSelect = schedule.querySelector("[data-profile-schedule-year]");
        const defaultYear = schedule.dataset.profileScheduleCurrentYear || "";
        if (yearSelect && hasYearOption(yearSelect, defaultYear)) {
            yearSelect.value = defaultYear;
        }

        const defaultType = Array.from(schedule.querySelectorAll("[data-profile-schedule-type]")).find(function (input) {
            return input.value === "all";
        });
        if (defaultType) {
            defaultType.checked = true;
        }

        syncYearSelect(yearSelect);
        updateSchedule(schedule, { persist: false });
    }

    function updateSchedule(schedule, options) {
        const updateOptions = options || {};
        const typeInputs = Array.from(schedule.querySelectorAll("[data-profile-schedule-type]"));
        const yearSelect = schedule.querySelector("[data-profile-schedule-year]");
        const vacationCards = Array.from(schedule.querySelectorAll("[data-schedule-entry-card]"));
        const transferCards = Array.from(schedule.querySelectorAll("[data-schedule-transfer-card]"));
        const transferGroup = schedule.querySelector("[data-schedule-transfer-group]");
        const emptyState = schedule.querySelector("[data-profile-schedule-empty]");
        const title = schedule.querySelector("[data-profile-schedule-title]");
        const counter = schedule.querySelector("[data-profile-schedule-counter]");
        const stepButtons = Array.from(schedule.querySelectorAll("[data-profile-schedule-year-step]"));
        const checkedType = typeInputs.find(function (input) {
            return input.checked;
        });
        const selectedType = checkedType ? checkedType.value : "all";
        const selectedYear = yearSelect ? yearSelect.value : schedule.dataset.profileScheduleCurrentYear;
        const isAllYears = selectedYear === "all";
        let visibleVacationCount = 0;
        let visibleTransferCount = 0;

        syncSegmented(typeInputs);

        vacationCards.forEach(function (card) {
            const matchesPeriod = isAllYears || cardHasYear(card, selectedYear);
            const matchesType = selectedType === "all" || card.dataset.vacationType === selectedType;
            const isVisible = matchesPeriod && matchesType;
            setHidden(card, !isVisible);
            if (isVisible) {
                visibleVacationCount += 1;
            }
        });

        transferCards.forEach(function (card) {
            const isVisible = isAllYears || cardHasYear(card, selectedYear);
            setHidden(card, !isVisible);
            if (isVisible) {
                visibleTransferCount += 1;
            }
        });

        setHidden(transferGroup, visibleTransferCount === 0);
        setHidden(emptyState, visibleVacationCount !== 0);

        if (title) {
            title.textContent = isAllYears ? "Отпуска за всё время" : "Отпуска на " + selectedYear + " год";
        }

        if (counter) {
            counter.textContent = String(visibleVacationCount);
        }

        syncYearSelect(yearSelect);
        updateYearButtons(yearSelect, stepButtons);
        if (updateOptions.persist !== false) {
            writeFilterState(schedule);
        }
    }

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.closest("[data-profile-schedule-year-select]")) {
            closeYearSelects();
        }
    }, { signal: signal });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeYearSelects();
        }
    }, { signal: signal });

    schedules.forEach(function (schedule) {
        const yearSelect = schedule.querySelector("[data-profile-schedule-year]");
        const typeInputs = Array.from(schedule.querySelectorAll("[data-profile-schedule-type]"));
        const stepButtons = Array.from(schedule.querySelectorAll("[data-profile-schedule-year-step]"));
        const yearSelectWrapper = schedule.querySelector("[data-profile-schedule-year-select]");
        const yearTrigger = schedule.querySelector("[data-profile-schedule-year-trigger]");
        const yearOptions = Array.from(schedule.querySelectorAll("[data-profile-schedule-year-option]"));
        const refresh = function () {
            updateSchedule(schedule);
        };

        applyStoredFilterState(schedule);

        typeInputs.forEach(function (input) {
            input.addEventListener("change", refresh, { signal: signal });
        });

        if (yearSelect) {
            yearSelect.addEventListener("change", function () {
                syncYearSelect(yearSelect);
                refresh();
            }, { signal: signal });
        }

        if (yearTrigger) {
            yearTrigger.addEventListener("click", function (event) {
                event.stopPropagation();
                toggleYearSelect(yearSelectWrapper);
            }, { signal: signal });
        }

        yearOptions.forEach(function (optionButton) {
            optionButton.addEventListener("click", function (event) {
                event.stopPropagation();
                if (!yearSelect) {
                    return;
                }

                yearSelect.value = optionButton.dataset.value;
                syncYearSelect(yearSelect);
                closeYearSelect(yearSelectWrapper);
                yearSelect.dispatchEvent(new Event("change", { bubbles: true }));
            }, { signal: signal });
        });

        stepButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                if (!yearSelect || button.disabled) {
                    return;
                }

                const direction = Number(button.dataset.profileScheduleYearStep || 0);
                const nextIndex = getAdjacentYearIndex(yearSelect, direction);
                if (nextIndex < 0) {
                    return;
                }

                yearSelect.selectedIndex = nextIndex;
                syncYearSelect(yearSelect);
                closeYearSelect(yearSelectWrapper);
                yearSelect.dispatchEvent(new Event("change", { bubbles: true }));
            }, { signal: signal });
        });

        refresh();
    });

    document.addEventListener("app:section-filters-reset", function (event) {
        if (!event.detail || event.detail.sectionKey !== "profile") {
            return;
        }

        const hasFiltersToReset = schedules.some(function (schedule) {
            return !isDefaultFilterState(schedule);
        });
        if (!hasFiltersToReset) {
            return;
        }

        event.preventDefault();
        schedules.forEach(resetFilterState);
        clearFilterState();
    }, { signal: signal });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initProfileScheduleFilters, { once: true });
} else {
    initProfileScheduleFilters();
}

document.addEventListener("app:navigation", initProfileScheduleFilters);
