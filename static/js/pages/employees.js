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
    const groupSelect = document.getElementById("production-group");
    const scheduleStatusSelect = document.getElementById("schedule-status");
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
    const defaultGroup = "all";
    const defaultScheduleStatus = "all";

    if (!segmentedControl || !buttons.length || !employeesList) {
        return;
    }

    let currentStatus = (buttons.find(function (button) {
        return button.classList.contains("active");
    }) || buttons[0]).value;
    let currentSearch = normalizeSearch(searchInput ? searchInput.value : new URLSearchParams(window.location.search).get("search"));
    let searchTimer = null;
    let scrollStateTimer = 0;
    let requestSequence = 0;

    function getDepartmentValue() {
        return departmentSelect ? departmentSelect.value : "all";
    }

    function getGroupValue() {
        return groupSelect ? groupSelect.value : "all";
    }

    function getScheduleStatusValue() {
        return scheduleStatusSelect ? scheduleStatusSelect.value : "all";
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
            group: getGroupValue(),
            scheduleStatus: getScheduleStatusValue(),
            search: currentSearch,
        };
    }

    function syncHiddenInput(form, name, value, removeWhenDefault) {
        if (!form) {
            return;
        }

        let input = form.querySelector('input[type="hidden"][name="' + name + '"]');
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
        document.querySelectorAll('input[type="hidden"][name="status"]').forEach(function (input) {
            input.value = currentStatus;
        });
        document.querySelectorAll('input[type="hidden"][name="search"]').forEach(function (input) {
            input.value = currentSearch;
        });

        const filterForms = [segmentedControl, searchForm].filter(Boolean);
        filterForms.forEach(function (form) {
            syncHiddenInput(form, "department", getDepartmentValue(), true);
            syncHiddenInput(form, "group", getGroupValue(), true);
            syncHiddenInput(form, "schedule_status", getScheduleStatusValue(), true);
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
        if (!departmentSelect) {
            return;
        }

        departmentSelect.value = value;
        syncSelectUi(departmentSelect);
    }

    function setGroupValue(value) {
        if (!groupSelect) {
            return;
        }

        groupSelect.value = value;
        syncSelectUi(groupSelect);
    }

    function setScheduleStatusValue(value) {
        if (!scheduleStatusSelect) {
            return;
        }

        scheduleStatusSelect.value = value;
        syncSelectUi(scheduleStatusSelect);
    }

    function syncGroupOptionsForDepartment() {
        if (!groupSelect) {
            return;
        }

        const departmentValue = getDepartmentValue();
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

        if (!selectedOptionIsAvailable) {
            groupSelect.value = defaultGroup;
        }
        syncSelectUi(groupSelect);
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

    function flushScrollState(selectedEmployeeId) {
        if (scrollStateTimer) {
            window.clearTimeout(scrollStateTimer);
            scrollStateTimer = 0;
        }
        writeScrollState(selectedEmployeeId);
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
            || savedState.group !== currentState.group
            || savedState.scheduleStatus !== currentState.scheduleStatus
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

    function updateUrl(status, department, group, scheduleStatus, search) {
        const params = new URLSearchParams(window.location.search);
        params.set("status", status);
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
        if (scheduleStatus && scheduleStatus !== "all") {
            params.set("schedule_status", scheduleStatus);
        } else {
            params.delete("schedule_status");
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

    function applyTooltip(node, options) {
        if (!node || !options) {
            return node;
        }
        node.dataset.scheduleStatusTooltip = "";
        node.dataset.scheduleStatusVariant = options.variant || "info";
        node.dataset.tooltipTitle = options.title || "";
        node.dataset.tooltipText = options.text || "";
        return node;
    }

    function createCell(label, value, extraClass, tooltip) {
        const cell = document.createElement("div");
        cell.className = "employee-card__cell" + (extraClass ? " " + extraClass : "");
        applyTooltip(cell, tooltip);

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

    function createOrgRow(employee) {
        const row = document.createElement("span");
        row.className = "employee-card__org-row";

        const departmentNode = document.createElement("span");
        departmentNode.className = "employee-card__org-item employee-card__org-item--department";

        const departmentIcon = document.createElement("span");
        departmentIcon.className = "material-icons-sharp";
        departmentIcon.setAttribute("aria-hidden", "true");
        departmentIcon.textContent = "apartment";

        const departmentLabel = document.createElement("span");
        departmentLabel.textContent = "Отдел: " + (employee.department_name || "Не указан");

        const separator = document.createElement("span");
        separator.className = "employee-card__org-separator";
        separator.setAttribute("aria-hidden", "true");

        const groupNode = document.createElement("span");
        groupNode.className = "employee-card__org-item employee-card__org-item--group";

        const groupIcon = document.createElement("span");
        groupIcon.className = "material-icons-sharp";
        groupIcon.setAttribute("aria-hidden", "true");
        groupIcon.textContent = "workspaces";

        const groupLabel = document.createElement("span");
        groupLabel.textContent = "Группа: " + (employee.production_group_label || "Не указана");

        departmentNode.appendChild(departmentIcon);
        departmentNode.appendChild(departmentLabel);
        groupNode.appendChild(groupIcon);
        groupNode.appendChild(groupLabel);
        row.appendChild(departmentNode);
        row.appendChild(separator);
        row.appendChild(groupNode);
        return row;
    }

    function createManagementBadge(badge) {
        const badgeNode = document.createElement("span");
        badgeNode.className = "employee-card__management-badge employee-card__management-badge--" + (badge.variant || "employee");

        const icon = document.createElement("span");
        icon.className = badge.icon_type === "symbol" ? "employee-card__management-symbol" : "material-icons-sharp";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = badge.icon || "verified_user";

        const label = document.createElement("span");
        label.textContent = badge.label || "";

        badgeNode.appendChild(icon);
        badgeNode.appendChild(label);
        return badgeNode;
    }

    function createNewHireBadge(badge) {
        if (!badge) {
            return null;
        }

        const badgeNode = document.createElement("span");
        badgeNode.className = "new-hire-badge";
        badgeNode.dataset.scheduleStatusTooltip = "";
        badgeNode.dataset.scheduleStatusVariant = badge.variant || "medium";
        badgeNode.dataset.tooltipTitle = badge.tooltip_title || badge.label || "Новичок";
        badgeNode.dataset.tooltipText = badge.tooltip_text || "";
        badgeNode.title = "";
        badgeNode.setAttribute("aria-label", badge.label || badgeNode.dataset.tooltipTitle);
        badgeNode.setAttribute("role", "img");

        const icon = document.createElement("span");
        icon.className = badge.icon_type === "symbol" ? "employee-card__management-symbol" : "material-icons-sharp";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = badge.icon || "person_add";
        badgeNode.appendChild(icon);

        return badgeNode;
    }

    function createPositionRow(employee) {
        const row = document.createElement("span");
        row.className = "employee-card__position-row";

        const positionNode = document.createElement("span");
        positionNode.className = "employee-card__subline employee-card__subline--position";
        positionNode.textContent = employee.position;
        row.appendChild(positionNode);

        (employee.management_badges || []).forEach(function (badge) {
            row.appendChild(createManagementBadge(badge));
        });

        return row;
    }

    function createScheduleBadge(scheduleStatus) {
        if (!scheduleStatus) {
            return null;
        }

        const badge = scheduleStatus.calendar_url ? document.createElement("a") : document.createElement("span");
        badge.className = "employee-schedule-badge employee-schedule-badge--" + (scheduleStatus.variant || "empty");
        badge.dataset.scheduleStatusTooltip = "";
        badge.dataset.scheduleStatusVariant = scheduleStatus.variant || "empty";
        badge.dataset.tooltipTitle = scheduleStatus.tooltip_title || scheduleStatus.label || "";
        badge.dataset.tooltipText = scheduleStatus.tooltip_text || "";
        badge.setAttribute("aria-label", "Открыть график сотрудника: " + (scheduleStatus.label || ""));

        if (scheduleStatus.calendar_url) {
            badge.href = scheduleStatus.calendar_url;
            badge.setAttribute("data-app-link", "");
        }

        const icon = document.createElement("span");
        icon.className = scheduleStatus.icon_type === "symbol" ? "employee-schedule-badge__symbol" : "material-icons-sharp";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = scheduleStatus.icon || "event_busy";

        const label = document.createElement("span");
        label.textContent = scheduleStatus.short_label || scheduleStatus.label || "Нет отпуска";

        badge.appendChild(icon);
        badge.appendChild(label);
        return badge;
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
        role.setAttribute("aria-label", employee.role_label || "Сотрудник");
        applyTooltip(role, {
            variant: "info",
            title: employee.role_label || "Сотрудник",
            text: "Роль сотрудника определяет доступные разделы и маршрут согласования заявок.",
        });

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
        nameNode.textContent = employee.name || "";
        const newHireBadge = createNewHireBadge(employee.new_hire_badge);
        if (newHireBadge) {
            nameNode.appendChild(newHireBadge);
        }
        primary.appendChild(nameNode);

        primary.appendChild(createPositionRow(employee));

        primary.appendChild(createOrgRow(employee));

        const meta = document.createElement("div");
        meta.className = "employee-card__meta";
        meta.appendChild(createCell("Доступный отпуск", employee.available_days + " д.", "employee-card__cell--available", {
            variant: Number(employee.available_days || 0) ? "planned" : "empty",
            title: "Доступный отпуск",
            text: "Текущий свободный остаток оплачиваемого отпуска сотрудника.",
        }));
        meta.appendChild(createCell("Ближайший отпуск", employee.upcoming_vacation_label || "Не запланирован", "employee-card__cell--upcoming", {
            variant: "info",
            title: "Ближайший отпуск",
            text: "Самая ближайшая запись отпуска или отсутствия сотрудника.",
        }));

        const status = document.createElement("div");
        status.className = "employee-card__status";
        status.innerHTML = '<span class="employee-card__label">Статус</span>';
        applyTooltip(status, {
            variant: employee.is_working ? "planned" : "info",
            title: "Текущий статус",
            text: employee.is_working
                ? "Сотрудник сейчас в работе по графику и утверждённым заявкам."
                : "Сотрудник сейчас находится в отпуске или другом утверждённом отсутствии.",
        });

        const statusStack = document.createElement("span");
        statusStack.className = "employee-card__status-stack";

        const badge = document.createElement("span");
        badge.className = "employee-status-badge " + (employee.is_working ? "employee-status-badge--working" : "employee-status-badge--vacation");
        badge.textContent = employee.status_label;
        statusStack.appendChild(badge);

        const scheduleBadge = createScheduleBadge(employee.schedule_status);
        if (scheduleBadge) {
            statusStack.appendChild(scheduleBadge);
        }
        status.appendChild(statusStack);

        article.appendChild(role);
        article.appendChild(primary);
        article.appendChild(meta);
        article.appendChild(status);
        return article;
    }

    function fetchEmployees() {
        const departmentId = getDepartmentValue();
        const groupId = getGroupValue();
        const scheduleStatus = getScheduleStatusValue();
        const url = new URL(window.location.href);
        const requestId = ++requestSequence;
        currentSearch = normalizeSearch(searchInput ? searchInput.value : currentSearch);
        url.searchParams.set("status", currentStatus);

        if (departmentId && departmentId !== "all") {
            url.searchParams.set("department", departmentId);
        } else {
            url.searchParams.delete("department");
        }
        if (groupId && groupId !== "all") {
            url.searchParams.set("group", groupId);
        } else {
            url.searchParams.delete("group");
        }
        if (scheduleStatus && scheduleStatus !== "all") {
            url.searchParams.set("schedule_status", scheduleStatus);
        } else {
            url.searchParams.delete("schedule_status");
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
                    updateUrl(currentStatus, departmentId, groupId, scheduleStatus, currentSearch);
                    return;
                }

                data.employees.forEach(function (employee) {
                    employeesList.appendChild(createEmployeeCard(employee));
                });

                if (employeesCountNode) {
                    employeesCountNode.textContent = String(data.employees.length);
                }

                updateUrl(currentStatus, departmentId, groupId, scheduleStatus, currentSearch);
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
        syncGroupOptionsForDepartment();
        setGroupValue(defaultGroup);
        setScheduleStatusValue(defaultScheduleStatus);
        setActiveButton(currentStatus);
        syncSearchControls();
        clearScrollState();
        fetchEmployees();
    }

    syncGroupOptionsForDepartment();
    syncSelectUi(scheduleStatusSelect);
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
            syncGroupOptionsForDepartment();
            clearScrollState();
            fetchEmployees();
        }, { signal: signal });
    }

    if (groupSelect) {
        groupSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            syncSelectUi(groupSelect);
            syncHiddenFilterInputs();
            clearScrollState();
            fetchEmployees();
        }, { signal: signal });
    }

    if (scheduleStatusSelect) {
        scheduleStatusSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            syncSelectUi(scheduleStatusSelect);
            syncHiddenFilterInputs();
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
        if (scrollStateTimer) {
            window.clearTimeout(scrollStateTimer);
        }
    }, { once: true });

    document.addEventListener("app:before-navigation", function () {
        flushScrollState();
    }, { signal: signal });

    document.addEventListener("app:section-sidebar-repeat", function (event) {
        if (!event.detail || event.detail.sectionKey !== "employees") {
            return;
        }

        event.preventDefault();
        resetEmployeesSection();
    }, { signal: signal });

    if (employeesScrollShell) {
        employeesScrollShell.addEventListener("scroll", scheduleScrollStateWrite, { passive: true, signal: signal });
    }

    employeesList.addEventListener("click", function (event) {
        const card = event.target.closest("[data-employee-id]");
        if (card && employeesList.contains(card)) {
            flushScrollState(card.dataset.employeeId);
        }
    }, { capture: true, signal: signal });

    employeesList.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const card = event.target.closest("[data-employee-id]");
        if (card && employeesList.contains(card)) {
            flushScrollState(card.dataset.employeeId);
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
