function initApplicationsPage() {
    const existingController = window.__applicationsPageController;
    if (existingController) {
        existingController.abort();
    }

    const root = document.querySelector("[data-applications-page]");
    if (!root) {
        return;
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__applicationsPageController = controller;

    const statusForms = Array.from(root.querySelectorAll("[data-applications-status-form]"));
    const buttons = Array.from(root.querySelectorAll("[data-applications-status-form] button[name='status']"));
    const taskScopeForms = Array.from(root.querySelectorAll("[data-applications-task-scope-form]"));
    const taskScopeInputs = Array.from(root.querySelectorAll("[data-applications-task-scope-form] input[name='task_scope']"));
    const transferList = document.getElementById("changeRequestsCardsList");
    const requestList = document.getElementById("vacationsCardsList");
    const transferScrollShell = root.querySelector("[data-applications-transfer-scroll]");
    const requestScrollShell = root.querySelector("[data-applications-request-scroll]");
    const departmentSelects = Array.from(root.querySelectorAll("[data-applications-department-filter]"));
    const groupSelects = Array.from(root.querySelectorAll("[data-applications-group-filter]"));
    const vacationTypeSelect = root.querySelector("[data-applications-vacation-type-filter]");
    const searchControls = Array.from(root.querySelectorAll("[data-live-search-form]")).map(function (form) {
        return {
            form: form,
            input: form.querySelector("[data-live-search-input]"),
            toggle: form.querySelector("[data-live-search-toggle]"),
            clear: form.querySelector("[data-live-search-clear]"),
        };
    }).filter(function (control) {
        return Boolean(control.input);
    });
    const scrollStorageKey = "applications:list-scroll-state";
    const searchDebounceMs = 250;
    const defaultStatus = "all";
    const defaultTaskScope = "all";
    const defaultDepartment = "all";
    const defaultGroup = "all";
    const defaultVacationType = "all";

    if (!statusForms.length || !buttons.length || !transferList || !requestList) {
        return;
    }

    const initialSearchControl = searchControls.find(function (control) {
        return control.input.value;
    });

    let currentStatus = (buttons.find(function (button) {
        return button.classList.contains("active");
    }) || buttons[0]).value;
    let currentTaskScope = taskScopeInputs.some(function (input) {
        return input.checked;
    }) ? "mine" : defaultTaskScope;
    let currentSearch = normalizeSearch(initialSearchControl ? initialSearchControl.input.value : new URLSearchParams(window.location.search).get("search"));
    let searchTimer = null;
    let scrollStateTimer = 0;
    let requestSequence = 0;

    function getSelectValue(selects, defaultValue) {
        const selectNodes = selects.filter(Boolean);
        const selectedNode = selectNodes.find(function (select) {
            return select.value && select.value !== defaultValue;
        });
        const fallbackNode = selectNodes[0];
        return selectedNode ? selectedNode.value : (fallbackNode ? fallbackNode.value : defaultValue);
    }

    function getDepartmentValue() {
        return getSelectValue(departmentSelects, defaultDepartment);
    }

    function getGroupValue() {
        return getSelectValue(groupSelects, defaultGroup);
    }

    function getVacationTypeValue() {
        return vacationTypeSelect ? vacationTypeSelect.value : "all";
    }

    function normalizeSearch(value) {
        return (value || "").trim().replace(/\s+/g, " ");
    }

    function rememberListHref() {
        if (
            window.KabinetNavigation
            && typeof window.KabinetNavigation.rememberSectionListHref === "function"
        ) {
            window.KabinetNavigation.rememberSectionListHref("applications", window.location.href);
        }
    }

    function getCurrentListState() {
        return {
            status: currentStatus,
            taskScope: currentTaskScope,
            department: getDepartmentValue(),
            group: getGroupValue(),
            vacationType: getVacationTypeValue(),
            search: currentSearch,
        };
    }

    function getCurrentSearchInputValue() {
        const focusedControl = searchControls.find(function (control) {
            return document.activeElement === control.input;
        });
        if (focusedControl) {
            return focusedControl.input.value;
        }

        const filledControl = searchControls.find(function (control) {
            return control.input.value;
        });
        return filledControl ? filledControl.input.value : currentSearch;
    }

    function setSearchOpen(control, isOpen) {
        if (!control || !control.form) {
            return;
        }

        const shouldOpen = Boolean(isOpen || currentSearch);
        control.form.classList.toggle("is-open", shouldOpen);
        if (control.toggle) {
            control.toggle.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
        }
    }

    function focusSearchInput(control) {
        if (!control || !control.input) {
            return;
        }

        const placeCaret = function () {
            try {
                const caretPosition = control.input.value.length;
                control.input.setSelectionRange(caretPosition, caretPosition);
            } catch (error) {
            }
        };

        control.input.focus({ preventScroll: true });
        placeCaret();
        window.requestAnimationFrame(function () {
            control.input.focus({ preventScroll: true });
            placeCaret();
            window.requestAnimationFrame(function () {
                control.input.focus({ preventScroll: true });
                placeCaret();
            });
        });
    }

    function collapseSearchInput(control) {
        if (!control || !control.form) {
            return;
        }

        const activeElement = document.activeElement;
        if (activeElement && control.form.contains(activeElement) && typeof activeElement.blur === "function") {
            activeElement.blur();
        }
        if (control.input && typeof control.input.blur === "function") {
            control.input.blur();
        }
        const bodyTabIndex = document.body.getAttribute("tabindex");
        document.body.setAttribute("tabindex", "-1");
        document.body.focus({ preventScroll: true });
        if (bodyTabIndex === null) {
            document.body.removeAttribute("tabindex");
        } else {
            document.body.setAttribute("tabindex", bodyTabIndex);
        }
        setSearchOpen(control, false);
    }

    function syncSearchControls(sourceInput) {
        searchControls.forEach(function (control) {
            if (control.input !== sourceInput) {
                control.input.value = currentSearch;
            }
            const hasFocus = control.form.contains(document.activeElement);
            setSearchOpen(control, hasFocus || Boolean(currentSearch));
            if (control.clear) {
                control.clear.hidden = !currentSearch;
            }
        });
    }

    function syncSelectUi(selectNode) {
        if (!selectNode) {
            return;
        }

        const selectWrapper = selectNode.closest("[data-employee-select]");
        if (!selectWrapper) {
            return;
        }

        const valueNode = selectWrapper.querySelector("[data-employee-select-value]");
        const selectedOption = selectNode.options[selectNode.selectedIndex];
        if (valueNode && selectedOption) {
            valueNode.textContent = selectedOption.textContent;
        }

        const triggerNode = selectWrapper.querySelector("[data-employee-select-trigger]");
        if (triggerNode && triggerNode.dataset.employeeSelectLabel) {
            const selectedText = selectedOption ? selectedOption.textContent.trim() : "";
            const labelText = selectedText || (valueNode ? valueNode.textContent.trim() : "");
            const triggerLabel = triggerNode.dataset.employeeSelectLabel + ": " + labelText;
            triggerNode.setAttribute("aria-label", triggerLabel);
            triggerNode.title = triggerLabel;
        }

        const menuNode = selectWrapper.__floatingMenu || selectWrapper.querySelector("[data-employee-select-menu]");
        const optionButtons = menuNode
            ? Array.from(menuNode.querySelectorAll("[data-employee-select-option]"))
            : Array.from(selectWrapper.querySelectorAll("[data-employee-select-option]"));
        optionButtons.forEach(function (optionButton) {
            const isSelected = optionButton.dataset.value === selectNode.value;
            optionButton.classList.toggle("is-selected", isSelected);
            optionButton.setAttribute("aria-selected", isSelected ? "true" : "false");
        });
    }

    function setDepartmentValue(value) {
        departmentSelects.forEach(function (selectNode) {
            selectNode.value = value;
            syncSelectUi(selectNode);
        });
    }

    function setGroupValue(value) {
        groupSelects.forEach(function (selectNode) {
            selectNode.value = value;
            syncSelectUi(selectNode);
        });
    }

    function setVacationTypeValue(value) {
        if (!vacationTypeSelect) {
            return;
        }

        vacationTypeSelect.value = value;
        syncSelectUi(vacationTypeSelect);
    }

    function syncGroupOptionsForDepartment() {
        if (!groupSelects.length) {
            return defaultGroup;
        }

        const departmentValue = getDepartmentValue();
        let selectedGroupValue = getGroupValue();
        let selectedOptionIsAvailable = false;

        groupSelects.forEach(function (groupSelect) {
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
                if (isAvailable && option.value === selectedGroupValue) {
                    selectedOptionIsAvailable = true;
                }
            });

            const selectWrapper = groupSelect.closest("[data-employee-select]");
            const menuNode = selectWrapper
                ? selectWrapper.__floatingMenu || selectWrapper.querySelector("[data-employee-select-menu]")
                : null;
            if (menuNode) {
                menuNode.querySelectorAll("[data-employee-select-option]").forEach(function (optionButton) {
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
        });

        if (!selectedOptionIsAvailable) {
            selectedGroupValue = defaultGroup;
        }
        setGroupValue(selectedGroupValue);
        return selectedGroupValue;
    }

    function readScrollState() {
        try {
            return JSON.parse(sessionStorage.getItem(scrollStorageKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function writeScrollState(selectedVacationId) {
        const state = getCurrentListState();
        state.transferTop = transferScrollShell ? transferScrollShell.scrollTop : 0;
        state.requestTop = requestScrollShell ? requestScrollShell.scrollTop : 0;

        if (selectedVacationId) {
            state.selectedVacationId = selectedVacationId;
        }

        try {
            sessionStorage.setItem(scrollStorageKey, JSON.stringify(state));
        } catch (error) {
        }
    }

    function flushScrollState(selectedVacationId) {
        if (scrollStateTimer) {
            window.clearTimeout(scrollStateTimer);
            scrollStateTimer = 0;
        }
        writeScrollState(selectedVacationId);
    }

    function scheduleScrollStateWrite() {
        if (scrollStateTimer) {
            window.clearTimeout(scrollStateTimer);
        }
        scrollStateTimer = window.setTimeout(function () {
            scrollStateTimer = 0;
            writeScrollState();
        }, 140);
    }

    function clearScrollState() {
        if (scrollStateTimer) {
            window.clearTimeout(scrollStateTimer);
            scrollStateTimer = 0;
        }
        try {
            sessionStorage.removeItem(scrollStorageKey);
        } catch (error) {
        }
    }

    function restoreScrollState() {
        const savedState = readScrollState();
        const currentState = getCurrentListState();
        if (
            !savedState
            || savedState.status !== currentState.status
            || savedState.taskScope !== currentState.taskScope
            || savedState.department !== currentState.department
            || savedState.group !== currentState.group
            || savedState.vacationType !== currentState.vacationType
            || savedState.search !== currentState.search
        ) {
            return;
        }

        requestAnimationFrame(function () {
            if (transferScrollShell) {
                transferScrollShell.scrollTop = Number(savedState.transferTop) || 0;
            }

            if (requestScrollShell) {
                requestScrollShell.scrollTop = Number(savedState.requestTop) || 0;
            }

            if (!savedState.selectedVacationId || !requestScrollShell) {
                return;
            }

            const selectedCard = requestList.querySelector('[data-vacation-id="' + savedState.selectedVacationId + '"]');
            if (!selectedCard) {
                return;
            }

            const shellBounds = requestScrollShell.getBoundingClientRect();
            const cardBounds = selectedCard.getBoundingClientRect();
            if (cardBounds.top < shellBounds.top || cardBounds.bottom > shellBounds.bottom) {
                selectedCard.scrollIntoView({ block: "center", behavior: "auto" });
            }
        });
    }

    function syncHiddenInput(form, name, value, removeWhenDefault) {
        if (!form) {
            return;
        }

        let input = form.querySelector('input[name="' + name + '"]');
        if (removeWhenDefault && (!value || value === "all")) {
            if (input) {
                input.remove();
            }
            return;
        }

        if (!input) {
            input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            form.appendChild(input);
        }
        input.value = value || "";
    }

    function syncHiddenFilterInputs() {
        statusForms.forEach(function (form) {
            syncHiddenInput(form, "task_scope", currentTaskScope, true);
            syncHiddenInput(form, "department", getDepartmentValue(), true);
            syncHiddenInput(form, "group", getGroupValue(), true);
            syncHiddenInput(form, "vacation_type", getVacationTypeValue(), true);
        });

        taskScopeForms.forEach(function (form) {
            syncHiddenInput(form, "status", currentStatus, true);
            syncHiddenInput(form, "department", getDepartmentValue(), true);
            syncHiddenInput(form, "group", getGroupValue(), true);
            syncHiddenInput(form, "vacation_type", getVacationTypeValue(), true);
            syncHiddenInput(form, "search", currentSearch, true);
        });

        searchControls.forEach(function (control) {
            syncHiddenInput(control.form, "task_scope", currentTaskScope, true);
            syncHiddenInput(control.form, "department", getDepartmentValue(), true);
            syncHiddenInput(control.form, "group", getGroupValue(), true);
            syncHiddenInput(control.form, "vacation_type", getVacationTypeValue(), true);
        });
    }

    function setActiveButton(value) {
        statusForms.forEach(function (form) {
            let activeButton = null;
            form.querySelectorAll("button[name='status']").forEach(function (button) {
                const isActive = button.value === value;
                button.classList.toggle("active", isActive);
                if (isActive) {
                    activeButton = button;
                }
            });
            if (window.KabinetSegmented && typeof window.KabinetSegmented.sync === "function") {
                window.KabinetSegmented.sync(form, activeButton);
            }
        });
        syncHiddenFilterInputs();
    }

    function setActiveTaskScopeControl(value) {
        taskScopeForms.forEach(function (form) {
            form.querySelectorAll("input[name='task_scope']").forEach(function (input) {
                input.checked = value === "mine";
            });
        });
        syncHiddenFilterInputs();
    }

    function replaceListHtml(listNode, html) {
        listNode.innerHTML = html || "";
    }

    function renderChangeRequests(html) {
        replaceListHtml(transferList, html);
    }

    function renderVacationRequests(html) {
        replaceListHtml(requestList, html);
    }

    function updateUrl(status, taskScope, department, group, vacationType, search) {
        const params = new URLSearchParams(window.location.search);
        params.set("status", status);
        if (taskScope && taskScope !== "all") {
            params.set("task_scope", taskScope);
        } else {
            params.delete("task_scope");
        }
        if (department && department !== "all") {
            params.set("department", department);
        } else {
            params.delete("department");
        }
        if (group && group !== "all") {
            params.set("group", group);
        } else {
            params.delete("group");
        }
        if (vacationType && vacationType !== "all") {
            params.set("vacation_type", vacationType);
        } else {
            params.delete("vacation_type");
        }
        if (search) {
            params.set("search", search);
        } else {
            params.delete("search");
        }

        const query = params.toString();
        window.history.replaceState({}, "", query ? window.location.pathname + "?" + query : window.location.pathname);
        rememberListHref();
    }

    function resetListScroll() {
        if (transferScrollShell) {
            transferScrollShell.scrollTop = 0;
        }
        if (requestScrollShell) {
            requestScrollShell.scrollTop = 0;
        }
    }

    function fetchApplications() {
        const selectedDepartment = getDepartmentValue();
        const selectedGroup = getGroupValue();
        const selectedVacationType = getVacationTypeValue();
        const url = new URL(window.location.href);
        const requestId = ++requestSequence;
        currentSearch = normalizeSearch(getCurrentSearchInputValue());
        url.searchParams.set("status", currentStatus);
        if (currentTaskScope && currentTaskScope !== "all") {
            url.searchParams.set("task_scope", currentTaskScope);
        } else {
            url.searchParams.delete("task_scope");
        }

        if (selectedDepartment && selectedDepartment !== "all") {
            url.searchParams.set("department", selectedDepartment);
        } else {
            url.searchParams.delete("department");
        }
        if (selectedGroup && selectedGroup !== "all") {
            url.searchParams.set("group", selectedGroup);
        } else {
            url.searchParams.delete("group");
        }
        if (selectedVacationType && selectedVacationType !== "all") {
            url.searchParams.set("vacation_type", selectedVacationType);
        } else {
            url.searchParams.delete("vacation_type");
        }
        if (currentSearch) {
            url.searchParams.set("search", currentSearch);
        } else {
            url.searchParams.delete("search");
        }
        syncSearchControls();

        fetch(url.toString(), {
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
            signal: signal,
        })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                if (requestId !== requestSequence) {
                    return;
                }
                renderChangeRequests(data.change_requests_html);
                renderVacationRequests(data.vacations_html);
                updateUrl(currentStatus, currentTaskScope, selectedDepartment, selectedGroup, selectedVacationType, currentSearch);
                resetListScroll();
                clearScrollState();
            })
            .catch(function (error) {
                if (error.name === "AbortError") {
                    return;
                }
                console.error("Error fetching applications:", error);
            });
    }

    function scheduleSearch() {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(function () {
            clearScrollState();
            fetchApplications();
        }, searchDebounceMs);
    }

    function resetApplicationsSection() {
        window.clearTimeout(searchTimer);
        currentStatus = defaultStatus;
        currentTaskScope = defaultTaskScope;
        currentSearch = "";
        searchControls.forEach(function (control) {
            control.input.value = "";
        });
        setDepartmentValue(defaultDepartment);
        syncGroupOptionsForDepartment();
        setGroupValue(defaultGroup);
        setVacationTypeValue(defaultVacationType);
        setActiveButton(currentStatus);
        setActiveTaskScopeControl(currentTaskScope);
        syncSearchControls();
        clearScrollState();
        fetchApplications();
    }

    function hasDefaultApplicationsFilters() {
        return (
            currentStatus === defaultStatus
            && currentTaskScope === defaultTaskScope
            && getDepartmentValue() === defaultDepartment
            && getGroupValue() === defaultGroup
            && getVacationTypeValue() === defaultVacationType
            && currentSearch === ""
            && !getCurrentSearchInputValue()
        );
    }

    setDepartmentValue(getDepartmentValue());
    syncGroupOptionsForDepartment();
    setVacationTypeValue(getVacationTypeValue());
    setActiveButton(currentStatus);
    setActiveTaskScopeControl(currentTaskScope);
    syncSearchControls();
    rememberListHref();

    buttons.forEach(function (button) {
        button.addEventListener("click", function () {
            window.clearTimeout(searchTimer);
            currentStatus = button.value;
            setActiveButton(currentStatus);
            clearScrollState();
            fetchApplications();
        }, { signal: signal });
    });

    taskScopeInputs.forEach(function (input) {
        input.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            currentTaskScope = input.checked ? "mine" : defaultTaskScope;
            setActiveTaskScopeControl(currentTaskScope);
            clearScrollState();
            fetchApplications();
        }, { signal: signal });
    });

    departmentSelects.forEach(function (departmentSelect) {
        departmentSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            setDepartmentValue(departmentSelect.value);
            syncGroupOptionsForDepartment();
            syncHiddenFilterInputs();
            clearScrollState();
            fetchApplications();
        }, { signal: signal });
    });

    groupSelects.forEach(function (groupSelect) {
        groupSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            setGroupValue(groupSelect.value);
            syncHiddenFilterInputs();
            clearScrollState();
            fetchApplications();
        }, { signal: signal });
    });

    if (vacationTypeSelect) {
        vacationTypeSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            syncSelectUi(vacationTypeSelect);
            syncHiddenFilterInputs();
            clearScrollState();
            fetchApplications();
        }, { signal: signal });
    }

    searchControls.forEach(function (control) {
        control.form.addEventListener("submit", function (event) {
            event.preventDefault();
            window.clearTimeout(searchTimer);
            currentSearch = normalizeSearch(control.input.value);
            control.input.value = currentSearch;
            syncSearchControls();
            clearScrollState();
            fetchApplications();
        }, { signal: signal });

        control.input.addEventListener("input", function () {
            currentSearch = normalizeSearch(control.input.value);
            syncSearchControls(control.input);
            scheduleSearch();
        }, { signal: signal });

        control.form.addEventListener("focusout", function () {
            window.setTimeout(syncSearchControls, 0);
        }, { signal: signal });

        if (control.toggle) {
            control.toggle.addEventListener("pointerdown", function (event) {
                event.preventDefault();
                setSearchOpen(control, true);
                focusSearchInput(control);
            }, { signal: signal });

            control.toggle.addEventListener("click", function () {
                setSearchOpen(control, true);
                focusSearchInput(control);
            }, { signal: signal });
        }

        if (control.clear) {
            control.clear.addEventListener("click", function () {
                if (!control.input.value && !currentSearch) {
                    focusSearchInput(control);
                    return;
                }

                currentSearch = "";
                searchControls.forEach(function (otherControl) {
                    otherControl.input.value = "";
                });
                window.clearTimeout(searchTimer);
                syncSearchControls();
                clearScrollState();
                fetchApplications();
                collapseSearchInput(control);
                window.requestAnimationFrame(function () {
                    collapseSearchInput(control);
                });
                window.setTimeout(function () {
                    collapseSearchInput(control);
                }, 40);
            }, { signal: signal });
        }
    });

    signal.addEventListener("abort", function () {
        window.clearTimeout(searchTimer);
        if (scrollStateTimer) {
            window.clearTimeout(scrollStateTimer);
        }
    }, { once: true });

    document.addEventListener("app:before-navigation", function () {
        flushScrollState();
    }, { signal: signal });

    document.addEventListener("app:section-filters-reset", function (event) {
        if (!event.detail || event.detail.sectionKey !== "applications") {
            return;
        }

        if (hasDefaultApplicationsFilters()) {
            return;
        }

        event.preventDefault();
        resetApplicationsSection();
    }, { signal: signal });

    [transferScrollShell, requestScrollShell].forEach(function (scrollShell) {
        if (!scrollShell) {
            return;
        }
        scrollShell.addEventListener("scroll", scheduleScrollStateWrite, { passive: true, signal: signal });
    });

    requestList.addEventListener("click", function (event) {
        const card = event.target.closest("[data-vacation-id]");
        if (card && requestList.contains(card)) {
            flushScrollState(card.dataset.vacationId);
        }
    }, { capture: true, signal: signal });

    transferList.addEventListener("click", function (event) {
        const card = event.target.closest("[data-change-request-id]");
        if (card && transferList.contains(card)) {
            flushScrollState();
        }
    }, { capture: true, signal: signal });

    requestList.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const card = event.target.closest("[data-vacation-id]");
        if (card && requestList.contains(card)) {
            flushScrollState(card.dataset.vacationId);
        }
    }, { capture: true, signal: signal });

    transferList.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const card = event.target.closest("[data-change-request-id]");
        if (card && transferList.contains(card)) {
            flushScrollState();
        }
    }, { capture: true, signal: signal });

    restoreScrollState();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initApplicationsPage, { once: true });
} else {
    initApplicationsPage();
}

document.addEventListener("app:navigation", initApplicationsPage);
