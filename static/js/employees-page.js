function initEmployeesPage() {
    const existingController = window.__employeesPageController;
    if (existingController) {
        existingController.abort();
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__employeesPageController = controller;

    const buttons = Array.from(document.querySelectorAll("#employees-status-form button[name='status']"));
    const employeesList = document.getElementById("employees-table-body");
    const employeesScrollShell = document.querySelector(".employee-cards-shell");
    const departmentSelect = document.getElementById("department");
    const employeesCountNode = document.getElementById("employees-count");
    const searchForm = document.querySelector("[data-live-search-form]");
    const searchInput = searchForm ? searchForm.querySelector("[data-live-search-input]") : null;
    const searchToggle = searchForm ? searchForm.querySelector("[data-live-search-toggle]") : null;
    const searchClear = searchForm ? searchForm.querySelector("[data-live-search-clear]") : null;
    const canOpenProfiles = Boolean(document.querySelector(".employee-row-clickable"));
    const segmentedControl = document.getElementById("employees-status-form");
    const scrollStorageKey = "employees:list-scroll-state";
    const searchDebounceMs = 250;
    const defaultStatus = "None";
    const defaultDepartment = "all";

    if (!segmentedControl || !buttons.length || !employeesList) {
        return;
    }

    let currentStatus = (buttons.find(function (button) {
        return button.classList.contains("active");
    }) || buttons[0]).value;
    let currentSearch = normalizeSearch(searchInput ? searchInput.value : new URLSearchParams(window.location.search).get("search"));
    let searchTimer = null;
    let requestSequence = 0;

    function getDepartmentValue() {
        return departmentSelect ? departmentSelect.value : "all";
    }

    function normalizeSearch(value) {
        return (value || "").trim().replace(/\s+/g, " ");
    }

    function rememberListHref() {
        if (
            window.KabinetNavigation
            && typeof window.KabinetNavigation.rememberSectionListHref === "function"
        ) {
            window.KabinetNavigation.rememberSectionListHref("employees", window.location.href);
        }
    }

    function getCurrentListState() {
        return {
            status: currentStatus,
            department: getDepartmentValue(),
            search: currentSearch,
        };
    }

    function syncHiddenFilterInputs() {
        document.querySelectorAll('input[type="hidden"][name="status"]').forEach(function (input) {
            input.value = currentStatus;
        });
        document.querySelectorAll('input[type="hidden"][name="search"]').forEach(function (input) {
            input.value = currentSearch;
        });
    }

    function setSearchOpen(isOpen) {
        if (!searchForm) {
            return;
        }

        const shouldOpen = Boolean(isOpen || currentSearch);
        searchForm.classList.toggle("is-open", shouldOpen);
        if (searchToggle) {
            searchToggle.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
        }
    }

    function focusSearchInput() {
        if (!searchInput) {
            return;
        }

        searchInput.focus({ preventScroll: true });
        window.requestAnimationFrame(function () {
            searchInput.focus({ preventScroll: true });
            window.requestAnimationFrame(function () {
                searchInput.focus({ preventScroll: true });
            });
        });
    }

    function syncSearchControls() {
        if (!searchForm) {
            return;
        }

        const hasFocus = searchForm.contains(document.activeElement);
        setSearchOpen(hasFocus || Boolean(currentSearch));
        if (searchClear) {
            searchClear.hidden = !currentSearch;
        }
        syncHiddenFilterInputs();
    }

    function syncDepartmentSelectUi() {
        if (!departmentSelect) {
            return;
        }

        const selectWrapper = departmentSelect.closest("[data-employee-select]");
        if (!selectWrapper) {
            return;
        }

        const valueNode = selectWrapper.querySelector("[data-employee-select-value]");
        const selectedOption = departmentSelect.options[departmentSelect.selectedIndex];
        if (valueNode && selectedOption) {
            valueNode.textContent = selectedOption.textContent;
        }

        selectWrapper.querySelectorAll("[data-employee-select-option]").forEach(function (optionButton) {
            const isSelected = optionButton.dataset.value === departmentSelect.value;
            optionButton.classList.toggle("is-selected", isSelected);
            optionButton.setAttribute("aria-selected", isSelected ? "true" : "false");
        });
    }

    function setDepartmentValue(value) {
        if (!departmentSelect) {
            return;
        }

        departmentSelect.value = value;
        syncDepartmentSelectUi();
    }

    function readScrollState() {
        try {
            return JSON.parse(sessionStorage.getItem(scrollStorageKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function writeScrollState(selectedEmployeeId) {
        if (!employeesScrollShell) {
            return;
        }

        const state = getCurrentListState();
        state.scrollTop = employeesScrollShell.scrollTop;
        if (selectedEmployeeId) {
            state.selectedEmployeeId = selectedEmployeeId;
        }

        try {
            sessionStorage.setItem(scrollStorageKey, JSON.stringify(state));
        } catch (error) {
            return;
        }
    }

    function clearScrollState() {
        try {
            sessionStorage.removeItem(scrollStorageKey);
        } catch (error) {
            return;
        }
    }

    function restoreScrollState() {
        if (!employeesScrollShell) {
            return;
        }

        const savedState = readScrollState();
        const currentState = getCurrentListState();
        if (
            !savedState
            || savedState.status !== currentState.status
            || savedState.department !== currentState.department
            || savedState.search !== currentState.search
        ) {
            return;
        }

        requestAnimationFrame(function () {
            employeesScrollShell.scrollTop = Number(savedState.scrollTop) || 0;

            if (!savedState.selectedEmployeeId) {
                return;
            }

            const selectedCard = employeesList.querySelector('[data-employee-id="' + savedState.selectedEmployeeId + '"]');
            if (!selectedCard) {
                return;
            }

            const shellBounds = employeesScrollShell.getBoundingClientRect();
            const cardBounds = selectedCard.getBoundingClientRect();
            if (cardBounds.top < shellBounds.top || cardBounds.bottom > shellBounds.bottom) {
                selectedCard.scrollIntoView({ block: "center", behavior: "auto" });
            }
        });
    }

    function setActiveButton(value) {
        let activeButton = null;
        buttons.forEach(function (button) {
            const isActive = button.value === value;
            button.classList.toggle("active", isActive);
            if (isActive) {
                activeButton = button;
            }
        });
        if (window.KabinetSegmented && typeof window.KabinetSegmented.sync === "function") {
            window.KabinetSegmented.sync(segmentedControl, activeButton);
        }
        syncHiddenFilterInputs();
    }

    function renderEmptyState() {
        employeesList.innerHTML = "";
        const empty = document.createElement("div");
        empty.className = "employee-cards-empty";
        empty.innerHTML = '<p class="table-empty">Сотрудники по выбранным фильтрам не найдены.</p>';
        employeesList.appendChild(empty);
    }

    function updateUrl(status, department, search) {
        const params = new URLSearchParams(window.location.search);
        params.set("status", status);
        if (department && department !== "all") {
            params.set("department", department);
        } else {
            params.delete("department");
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

    function createCell(label, value, extraClass) {
        const cell = document.createElement("div");
        cell.className = "employee-card__cell" + (extraClass ? " " + extraClass : "");

        const labelNode = document.createElement("span");
        labelNode.className = "employee-card__label";
        labelNode.textContent = label;

        const valueNode = document.createElement("span");
        valueNode.className = "employee-card__value";
        valueNode.textContent = value;

        cell.appendChild(labelNode);
        cell.appendChild(valueNode);
        return cell;
    }

    function createEmployeeCard(employee) {
        const article = document.createElement("article");
        article.className = "employee-card";

        if (canOpenProfiles) {
            article.classList.add("employee-row", "employee-row-clickable", "is-clickable");
            article.dataset.href = employee.profile_url;
            article.dataset.employeeId = employee.id;
            article.tabIndex = 0;
            article.setAttribute("role", "link");
        }

        const role = document.createElement("div");
        role.className = "employee-card__role employee-card__role--" + (employee.role_variant || "employee");
        role.title = employee.role_label || "Сотрудник";
        role.setAttribute("aria-label", employee.role_label || "Сотрудник");

        const roleIcon = document.createElement("span");
        roleIcon.className = employee.role_icon_type === "symbol" ? "employee-card__role-symbol" : "material-icons-sharp";
        roleIcon.setAttribute("aria-hidden", "true");
        roleIcon.textContent = employee.role_icon || "person";
        role.appendChild(roleIcon);

        const primary = document.createElement("div");
        primary.className = "employee-card__primary";

        const primaryLabel = document.createElement("span");
        primaryLabel.className = "employee-card__label";
        primaryLabel.textContent = "ФИО";
        primary.appendChild(primaryLabel);

        const nameNode = document.createElement("strong");
        nameNode.className = "employee-card__value employee-card__value--name";
        nameNode.textContent = employee.name;
        primary.appendChild(nameNode);

        const positionNode = document.createElement("span");
        positionNode.className = "employee-card__subline";
        positionNode.textContent = employee.position;
        primary.appendChild(positionNode);

        const departmentNode = document.createElement("span");
        departmentNode.className = "employee-card__subline";
        departmentNode.textContent = employee.department_name || "Не указан";
        primary.appendChild(departmentNode);

        const meta = document.createElement("div");
        meta.className = "employee-card__meta";
        meta.appendChild(createCell("Доступный отпуск", employee.available_days + " д.", "employee-card__cell--available"));
        meta.appendChild(createCell("Ближайший отпуск", employee.upcoming_vacation_label || "Не запланирован", "employee-card__cell--upcoming"));

        const status = document.createElement("div");
        status.className = "employee-card__status";
        status.innerHTML = '<span class="employee-card__label">Статус</span>';

        const badge = document.createElement("span");
        badge.className = "employee-status-badge " + (employee.is_working ? "employee-status-badge--working" : "employee-status-badge--vacation");
        badge.textContent = employee.status_label;
        status.appendChild(badge);

        article.appendChild(role);
        article.appendChild(primary);
        article.appendChild(meta);
        article.appendChild(status);
        return article;
    }

    function fetchEmployees() {
        const departmentId = getDepartmentValue();
        const url = new URL(window.location.href);
        const requestId = ++requestSequence;
        currentSearch = normalizeSearch(searchInput ? searchInput.value : currentSearch);
        url.searchParams.set("status", currentStatus);

        if (departmentId && departmentId !== "all") {
            url.searchParams.set("department", departmentId);
        } else {
            url.searchParams.delete("department");
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
                employeesList.innerHTML = "";

                if (!data.employees.length) {
                    renderEmptyState();
                    if (employeesCountNode) {
                        employeesCountNode.textContent = "0";
                    }
                    updateUrl(currentStatus, departmentId, currentSearch);
                    return;
                }

                data.employees.forEach(function (employee) {
                    employeesList.appendChild(createEmployeeCard(employee));
                });

                if (employeesCountNode) {
                    employeesCountNode.textContent = String(data.employees.length);
                }

                updateUrl(currentStatus, departmentId, currentSearch);
                if (employeesScrollShell) {
                    employeesScrollShell.scrollTop = 0;
                }
                clearScrollState();
            })
            .catch(function (error) {
                if (error.name === "AbortError") {
                    return;
                }
                console.error("Error fetching employees:", error);
            });
    }

    function scheduleSearch() {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(function () {
            clearScrollState();
            fetchEmployees();
        }, searchDebounceMs);
    }

    function resetEmployeesSection() {
        window.clearTimeout(searchTimer);
        currentStatus = defaultStatus;
        currentSearch = "";
        if (searchInput) {
            searchInput.value = "";
        }
        setDepartmentValue(defaultDepartment);
        setActiveButton(currentStatus);
        syncSearchControls();
        clearScrollState();
        fetchEmployees();
    }

    setActiveButton(currentStatus);
    syncSearchControls();
    rememberListHref();

    buttons.forEach(function (button) {
        button.addEventListener("click", function () {
            window.clearTimeout(searchTimer);
            currentStatus = button.value;
            setActiveButton(currentStatus);
            clearScrollState();
            fetchEmployees();
        }, { signal: signal });
    });

    if (departmentSelect) {
        departmentSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            clearScrollState();
            fetchEmployees();
        }, { signal: signal });
    }

    if (searchForm && searchInput) {
        searchForm.addEventListener("submit", function (event) {
            event.preventDefault();
            window.clearTimeout(searchTimer);
            currentSearch = normalizeSearch(searchInput.value);
            searchInput.value = currentSearch;
            syncSearchControls();
            clearScrollState();
            fetchEmployees();
        }, { signal: signal });

        searchInput.addEventListener("input", function () {
            currentSearch = normalizeSearch(searchInput.value);
            syncSearchControls();
            scheduleSearch();
        }, { signal: signal });

        searchForm.addEventListener("focusout", function () {
            window.setTimeout(syncSearchControls, 0);
        }, { signal: signal });
    }

    if (searchToggle && searchInput) {
        searchToggle.addEventListener("click", function () {
            setSearchOpen(true);
            focusSearchInput();
        }, { signal: signal });
    }

    if (searchClear && searchInput) {
        searchClear.addEventListener("click", function () {
            if (!searchInput.value && !currentSearch) {
                focusSearchInput();
                return;
            }

            searchInput.value = "";
            currentSearch = "";
            window.clearTimeout(searchTimer);
            syncSearchControls();
            clearScrollState();
            fetchEmployees();
            focusSearchInput();
        }, { signal: signal });
    }

    signal.addEventListener("abort", function () {
        window.clearTimeout(searchTimer);
    }, { once: true });

    document.addEventListener("app:section-sidebar-repeat", function (event) {
        if (!event.detail || event.detail.sectionKey !== "employees") {
            return;
        }

        event.preventDefault();
        resetEmployeesSection();
    }, { signal: signal });

    if (employeesScrollShell) {
        employeesScrollShell.addEventListener("scroll", function () {
            writeScrollState();
        }, { passive: true, signal: signal });
    }

    employeesList.addEventListener("click", function (event) {
        const card = event.target.closest("[data-employee-id]");
        if (card && employeesList.contains(card)) {
            writeScrollState(card.dataset.employeeId);
        }
    }, { capture: true, signal: signal });

    employeesList.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const card = event.target.closest("[data-employee-id]");
        if (card && employeesList.contains(card)) {
            writeScrollState(card.dataset.employeeId);
        }
    }, { capture: true, signal: signal });

    restoreScrollState();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initEmployeesPage, { once: true });
} else {
    initEmployeesPage();
}

document.addEventListener("app:navigation", initEmployeesPage);
