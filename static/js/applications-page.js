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
    const transferList = document.getElementById("changeRequestsCardsList");
    const requestList = document.getElementById("vacationsCardsList");
    const transferScrollShell = root.querySelector("[data-applications-transfer-scroll]");
    const requestScrollShell = root.querySelector("[data-applications-request-scroll]");
    const departmentSelect = document.getElementById("department");
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

    if (!statusForms.length || !buttons.length || !transferList || !requestList) {
        return;
    }

    const initialSearchControl = searchControls.find(function (control) {
        return control.input.value;
    });

    let currentStatus = (buttons.find(function (button) {
        return button.classList.contains("active");
    }) || buttons[0]).value;
    let currentSearch = normalizeSearch(initialSearchControl ? initialSearchControl.input.value : new URLSearchParams(window.location.search).get("search"));
    let searchTimer = null;
    let requestSequence = 0;

    function getDepartmentValue() {
        return departmentSelect ? departmentSelect.value : "all";
    }

    function normalizeSearch(value) {
        return (value || "").trim().replace(/\s+/g, " ");
    }

    function getCurrentListState() {
        return {
            status: currentStatus,
            department: getDepartmentValue(),
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

        control.input.focus({ preventScroll: true });
        window.requestAnimationFrame(function () {
            control.input.focus({ preventScroll: true });
            window.requestAnimationFrame(function () {
                control.input.focus({ preventScroll: true });
            });
        });
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
        syncHeaderSearchInput();
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

    function clearScrollState() {
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
            || savedState.department !== currentState.department
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

    function syncHiddenDepartmentInputs() {
        statusForms.forEach(function (form) {
            let input = form.querySelector('input[name="department"]');
            const departmentValue = getDepartmentValue();

            if (!departmentValue || departmentValue === "all") {
                if (input) {
                    input.remove();
                }
                return;
            }

            if (!input) {
                input = document.createElement("input");
                input.type = "hidden";
                input.name = "department";
                form.appendChild(input);
            }
            input.value = departmentValue;
        });
    }

    function syncHeaderStatusInput() {
        const statusInput = document.querySelector('#applications-department-form input[name="status"]');
        if (statusInput) {
            statusInput.value = currentStatus;
        }
    }

    function syncHeaderSearchInput() {
        const searchInputNode = document.querySelector('#applications-department-form input[name="search"]');
        if (searchInputNode) {
            searchInputNode.value = currentSearch;
        }
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
        syncHiddenDepartmentInputs();
        syncHeaderStatusInput();
        syncHeaderSearchInput();
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
        const url = new URL(window.location.href);
        const requestId = ++requestSequence;
        currentSearch = normalizeSearch(getCurrentSearchInputValue());
        url.searchParams.set("status", currentStatus);

        if (selectedDepartment && selectedDepartment !== "all") {
            url.searchParams.set("department", selectedDepartment);
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
                renderChangeRequests(data.change_requests_html);
                renderVacationRequests(data.vacations_html);
                updateUrl(currentStatus, selectedDepartment, currentSearch);
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

    setActiveButton(currentStatus);
    syncSearchControls();

    buttons.forEach(function (button) {
        button.addEventListener("click", function () {
            window.clearTimeout(searchTimer);
            currentStatus = button.value;
            setActiveButton(currentStatus);
            clearScrollState();
            fetchApplications();
        }, { signal: signal });
    });

    if (departmentSelect) {
        departmentSelect.addEventListener("change", function () {
            window.clearTimeout(searchTimer);
            syncHiddenDepartmentInputs();
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
                focusSearchInput(control);
            }, { signal: signal });
        }
    });

    signal.addEventListener("abort", function () {
        window.clearTimeout(searchTimer);
    }, { once: true });

    [transferScrollShell, requestScrollShell].forEach(function (scrollShell) {
        if (!scrollShell) {
            return;
        }
        scrollShell.addEventListener("scroll", function () {
            writeScrollState();
        }, { passive: true, signal: signal });
    });

    requestList.addEventListener("click", function (event) {
        const card = event.target.closest("[data-vacation-id]");
        if (card && requestList.contains(card)) {
            writeScrollState(card.dataset.vacationId);
        }
    }, { capture: true, signal: signal });

    requestList.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const card = event.target.closest("[data-vacation-id]");
        if (card && requestList.contains(card)) {
            writeScrollState(card.dataset.vacationId);
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
