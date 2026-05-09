function initEmployeeFormPage() {
    const existingController = window.__employeeFormPageController;
    if (existingController) {
        existingController.abort();
    }

    document.querySelectorAll(".employee-select__menu--floating").forEach(function (menu) {
        menu.remove();
    });

    const controller = new AbortController();
    const signal = controller.signal;
    window.__employeeFormPageController = controller;

    const employeeSelects = Array.from(document.querySelectorAll("[data-employee-select]"));
    const employeeForms = Array.from(document.querySelectorAll("[data-employee-form]"));

    if (!employeeSelects.length && !employeeForms.length) {
        return;
    }

    function isModalSelect(selectWrapper) {
        return selectWrapper.classList.contains("employee-select--modal");
    }

    function isFloatingSelect(selectWrapper) {
        return isModalSelect(selectWrapper) || selectWrapper.classList.contains("employee-select--floating");
    }

    function getSelectParts(selectWrapper) {
        return {
            nativeSelect: selectWrapper.querySelector("select"),
            trigger: selectWrapper.querySelector("[data-employee-select-trigger]"),
            valueNode: selectWrapper.querySelector("[data-employee-select-value]"),
            menu: selectWrapper.__floatingMenu || selectWrapper.querySelector("[data-employee-select-menu]"),
        };
    }

    function restoreFloatingMenu(selectWrapper) {
        const parts = getSelectParts(selectWrapper);
        const menu = parts.menu;
        if (!menu || !menu.classList.contains("employee-select__menu--floating")) {
            return;
        }

        menu.classList.remove("employee-select__menu--floating", "is-open", "is-above");
        menu.style.top = "";
        menu.style.left = "";
        menu.style.width = "";
        menu.style.maxHeight = "";

        if (menu.__portalPlaceholder && menu.__portalPlaceholder.parentNode) {
            menu.__portalPlaceholder.parentNode.insertBefore(menu, menu.__portalPlaceholder);
            menu.__portalPlaceholder.remove();
            menu.__portalPlaceholder = null;
            menu.__ownerSelectWrapper = null;
            selectWrapper.__floatingMenu = null;
            return;
        }

        selectWrapper.appendChild(menu);
        menu.__ownerSelectWrapper = null;
        selectWrapper.__floatingMenu = null;
    }

    function closeEmployeeSelect(selectWrapper) {
        const parts = getSelectParts(selectWrapper);
        selectWrapper.classList.remove("is-open");

        if (parts.trigger) {
            parts.trigger.setAttribute("aria-expanded", "false");
        }

        if (isFloatingSelect(selectWrapper)) {
            restoreFloatingMenu(selectWrapper);
        }
    }

    function closeEmployeeSelects(exceptSelect) {
        employeeSelects.forEach(function (selectWrapper) {
            if (exceptSelect && selectWrapper === exceptSelect) {
                return;
            }

            closeEmployeeSelect(selectWrapper);
        });
    }

    function syncEmployeeSelect(selectWrapper) {
        if (!selectWrapper) {
            return;
        }

        const parts = getSelectParts(selectWrapper);
        if (!parts.nativeSelect || !parts.trigger || !parts.valueNode) {
            return;
        }

        const selectedOption = parts.nativeSelect.options[parts.nativeSelect.selectedIndex];
        const selectedText = getOptionText(selectedOption);
        if (selectedOption) {
            parts.valueNode.textContent = selectedOption.textContent;
        }

        if (parts.trigger && parts.trigger.dataset.employeeSelectLabel) {
            const labelText = selectedText || parts.valueNode.textContent.trim();
            const triggerLabel = parts.trigger.dataset.employeeSelectLabel + ": " + labelText;
            parts.trigger.setAttribute("aria-label", triggerLabel);
            parts.trigger.title = triggerLabel;
        }

        const forcedDisabled = parts.nativeSelect.dataset.emptyForDepartment === "true";
        parts.trigger.disabled = parts.nativeSelect.disabled || forcedDisabled;
        parts.trigger.setAttribute("aria-expanded", selectWrapper.classList.contains("is-open") ? "true" : "false");
        selectWrapper.classList.toggle("is-disabled", parts.nativeSelect.disabled || forcedDisabled);

        if (parts.menu) {
            parts.menu.querySelectorAll("[data-employee-select-option]").forEach(function (optionButton) {
                const isSelected = optionButton.dataset.value === parts.nativeSelect.value;
                optionButton.classList.toggle("is-selected", isSelected);
                optionButton.setAttribute("aria-selected", isSelected ? "true" : "false");
            });
        }
    }

    function getOptionText(option) {
        return option ? option.textContent.trim() : "";
    }

    function syncPositionSelectForForm(form) {
        const departmentWrapper = form.querySelector("[data-department-select]");
        const positionWrapper = form.querySelector("[data-position-select]");
        if (!departmentWrapper || !positionWrapper) {
            return;
        }

        const departmentParts = getSelectParts(departmentWrapper);
        const positionParts = getSelectParts(positionWrapper);
        if (!departmentParts.nativeSelect || !positionParts.nativeSelect) {
            return;
        }

        const selectedDepartmentId = departmentParts.nativeSelect.value;
        const nativeOptions = Array.from(positionParts.nativeSelect.options);
        const menuOptions = positionParts.menu
            ? Array.from(positionParts.menu.querySelectorAll("[data-employee-select-option]"))
            : [];
        const matchingOptions = nativeOptions.filter(function (option) {
            return option.dataset.departmentId === selectedDepartmentId;
        });
        const currentOption = nativeOptions.find(function (option) {
            return option.value === positionParts.nativeSelect.value;
        });
        const currentMatches = Boolean(currentOption && currentOption.dataset.departmentId === selectedDepartmentId);

        nativeOptions.forEach(function (option) {
            const matches = option.dataset.departmentId === selectedDepartmentId;
            option.hidden = !matches;
            option.disabled = !matches;
        });

        menuOptions.forEach(function (optionButton) {
            const matches = optionButton.dataset.departmentId === selectedDepartmentId;
            optionButton.hidden = !matches;
            optionButton.disabled = !matches;
            optionButton.classList.toggle("is-hidden", !matches);
        });

        if (matchingOptions.length && !currentMatches) {
            positionParts.nativeSelect.value = matchingOptions[0].value;
        } else if (!matchingOptions.length) {
            positionParts.nativeSelect.value = "";
        }

        if (positionParts.valueNode) {
            positionParts.valueNode.textContent = matchingOptions.length
                ? getOptionText(nativeOptions.find(function (option) {
                    return option.value === positionParts.nativeSelect.value;
                }))
                : "Нет должностей для выбранного отдела";
        }

        positionParts.nativeSelect.dataset.emptyForDepartment = matchingOptions.length ? "false" : "true";
        if (positionParts.trigger) {
            positionParts.trigger.disabled = !matchingOptions.length;
        }

        positionWrapper.classList.toggle("is-disabled", !matchingOptions.length);
        positionParts.nativeSelect.setCustomValidity(
            matchingOptions.length ? "" : "Для выбранного отдела нет должностей в справочнике."
        );
        syncEmployeeSelect(positionWrapper);
    }

    function formHasBlocker(form) {
        return Boolean(form.querySelector("[data-form-blocker]"));
    }

    function syncFormSubmitState(form) {
        if (!form) {
            return;
        }

        const submitButton = form.querySelector("[data-employee-submit]");
        if (!submitButton) {
            return;
        }

        submitButton.disabled = formHasBlocker(form) || !form.checkValidity();
    }

    function positionFloatingMenu(selectWrapper) {
        const parts = getSelectParts(selectWrapper);
        if (!parts.trigger || !parts.menu || !parts.menu.classList.contains("employee-select__menu--floating")) {
            return;
        }

        const viewportGap = 12;
        const triggerRect = parts.trigger.getBoundingClientRect();
        const maxPreferredHeight = 240;
        const explicitMinWidth = Number.parseFloat(selectWrapper.dataset.employeeSelectMenuMinWidth || "");
        const minPreferredWidth = Number.isFinite(explicitMinWidth) && explicitMinWidth > 0
            ? explicitMinWidth
            : selectWrapper.classList.contains("employee-select--group") ? 320 : triggerRect.width;
        const maxAvailableWidth = Math.max(160, window.innerWidth - viewportGap * 2);
        const menuWidth = Math.min(Math.max(triggerRect.width, minPreferredWidth), maxAvailableWidth);
        const menuLeft = Math.min(
            Math.max(viewportGap, Math.round(triggerRect.left)),
            Math.max(viewportGap, Math.round(window.innerWidth - viewportGap - menuWidth))
        );

        parts.menu.style.width = Math.round(menuWidth) + "px";
        parts.menu.style.left = menuLeft + "px";
        parts.menu.style.top = "0px";
        parts.menu.style.maxHeight = maxPreferredHeight + "px";

        const naturalHeight = Math.min(parts.menu.scrollHeight, maxPreferredHeight);
        const spaceBelow = window.innerHeight - triggerRect.bottom - viewportGap;
        const spaceAbove = triggerRect.top - viewportGap;
        const shouldOpenAbove = spaceBelow < naturalHeight && spaceAbove > spaceBelow;
        const availableSpace = shouldOpenAbove ? spaceAbove : spaceBelow;
        const finalMaxHeight = Math.max(120, Math.min(maxPreferredHeight, availableSpace));
        const top = shouldOpenAbove
            ? Math.max(viewportGap, Math.round(triggerRect.top - finalMaxHeight - 8))
            : Math.round(triggerRect.bottom + 8);

        parts.menu.classList.toggle("is-above", shouldOpenAbove);
        parts.menu.style.top = top + "px";
        parts.menu.style.maxHeight = finalMaxHeight + "px";
    }

    function openEmployeeSelect(selectWrapper) {
        const parts = getSelectParts(selectWrapper);
        if (!parts.trigger || !parts.menu) {
            return;
        }

        closeEmployeeSelects(selectWrapper);
        selectWrapper.classList.add("is-open");
        parts.trigger.setAttribute("aria-expanded", "true");

        if (isFloatingSelect(selectWrapper) && !parts.menu.classList.contains("employee-select__menu--floating")) {
            const placeholder = document.createComment("employee-select-menu-anchor");
            parts.menu.__portalPlaceholder = placeholder;
            parts.menu.__ownerSelectWrapper = selectWrapper;
            parts.menu.parentNode.insertBefore(placeholder, parts.menu);
            document.body.appendChild(parts.menu);
            parts.menu.classList.add("employee-select__menu--floating");
            selectWrapper.__floatingMenu = parts.menu;
        }

        if (isFloatingSelect(selectWrapper)) {
            parts.menu.classList.add("is-open");
            positionFloatingMenu(selectWrapper);
        }
    }

    function selectEmployeeOption(selectWrapper, optionButton) {
        if (!selectWrapper || !optionButton || optionButton.disabled || optionButton.hidden) {
            return;
        }

        const parts = getSelectParts(selectWrapper);
        if (!parts.nativeSelect) {
            return;
        }

        if (parts.nativeSelect.value !== optionButton.dataset.value) {
            parts.nativeSelect.value = optionButton.dataset.value;
            parts.nativeSelect.dispatchEvent(new Event("change", { bubbles: true }));
        } else {
            syncEmployeeSelect(selectWrapper);
        }

        closeEmployeeSelect(selectWrapper);
    }

    function repositionOpenSelects() {
        employeeSelects.forEach(function (selectWrapper) {
            if (selectWrapper.classList.contains("is-open") && isFloatingSelect(selectWrapper)) {
                positionFloatingMenu(selectWrapper);
            }
        });
    }

    employeeSelects.forEach(function (selectWrapper) {
        const parts = getSelectParts(selectWrapper);
        if (!parts.trigger || !parts.nativeSelect || !parts.menu) {
            return;
        }

        syncEmployeeSelect(selectWrapper);

        parts.trigger.addEventListener("click", function (event) {
            event.stopPropagation();
            if (parts.trigger.disabled) {
                return;
            }

            if (selectWrapper.classList.contains("is-open")) {
                closeEmployeeSelect(selectWrapper);
                return;
            }

            openEmployeeSelect(selectWrapper);
        }, { signal: signal });

        parts.menu.querySelectorAll("[data-employee-select-option]").forEach(function (optionButton) {
            optionButton.addEventListener("click", function (event) {
                event.stopPropagation();
                selectEmployeeOption(selectWrapper, optionButton);
            }, { signal: signal });
        });

        parts.nativeSelect.addEventListener("change", function () {
            syncEmployeeSelect(selectWrapper);
            const form = parts.nativeSelect.closest("[data-employee-form]");
            if (selectWrapper.hasAttribute("data-department-select") && form) {
                syncPositionSelectForForm(form);
            }
            syncFormSubmitState(parts.nativeSelect.closest("[data-employee-form]"));
        }, { signal: signal });
    });

    employeeForms.forEach(function (form) {
        ["input", "change"].forEach(function (eventName) {
            form.addEventListener(eventName, function () {
                syncFormSubmitState(form);
            }, { signal: signal });
        });

        syncPositionSelectForForm(form);
        syncFormSubmitState(form);
    });

    document.addEventListener("click", function (event) {
        const optionButton = event.target.closest(".employee-select__menu--floating [data-employee-select-option]");
        if (!optionButton) {
            return;
        }

        const menu = optionButton.closest(".employee-select__menu--floating");
        if (!menu || !menu.__ownerSelectWrapper) {
            return;
        }

        event.stopPropagation();
        selectEmployeeOption(menu.__ownerSelectWrapper, optionButton);
    }, { capture: true, signal: signal });

    document.addEventListener("click", function (event) {
        if (!event.target.closest("[data-employee-select]") && !event.target.closest(".employee-select__menu--floating")) {
            closeEmployeeSelects();
        }
    }, { signal: signal });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeEmployeeSelects();
        }
    }, { signal: signal });

    window.addEventListener("resize", repositionOpenSelects, { signal: signal });
    window.addEventListener("scroll", repositionOpenSelects, { capture: true, signal: signal });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initEmployeeFormPage, { once: true });
} else {
    initEmployeeFormPage();
}

document.addEventListener("app:navigation", initEmployeeFormPage);
