(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createMonthSummaryController = function (context) {
        let currentMonthNumber = null;
        const monthDetailRequests = new Map();

        function stripModalParams(url) {
            url.searchParams.delete("calendar_modal");
            url.searchParams.delete("calendar_month");
            url.searchParams.delete("calendar_modal_focus");
            url.searchParams.delete("calendar_modal_scroll");
            url.searchParams.delete("calendar_employee");
            url.searchParams.delete("calendar_detail_employee");
            url.searchParams.delete("calendar_detail_month");
        }

        function getMonthSummaryDialog() {
            return context.monthSummaryModal
                ? context.monthSummaryModal.querySelector(".app-modal__dialog")
                : null;
        }

        function getMonthSummaryScrollTop() {
            const dialog = getMonthSummaryDialog();
            return dialog ? Math.max(0, Math.round(dialog.scrollTop || 0)) : 0;
        }

        function buildMonthSummaryReturnHref(focusTarget, scrollTop) {
            const url = new URL(window.location.href);
            stripModalParams(url);
            url.searchParams.set("calendar_modal", "month_summary");
            url.searchParams.set("calendar_month", currentMonthNumber || "");
            if (focusTarget) {
                url.searchParams.set("calendar_modal_focus", focusTarget);
            }
            if (Number(scrollTop) > 0) {
                url.searchParams.set("calendar_modal_scroll", String(Math.round(Number(scrollTop))));
            }
            return url.pathname + url.search + url.hash;
        }

        function syncCurrentMonthSummaryHistoryState(focusTarget) {
            if (!currentMonthNumber) {
                return;
            }

            window.history.replaceState(
                {},
                "",
                buildMonthSummaryReturnHref(focusTarget, getMonthSummaryScrollTop())
            );
        }

        function buildProfileHref(profileUrl, focusTarget, scrollTop) {
            if (!profileUrl) {
                return "#";
            }

            const url = new URL(profileUrl, window.location.href);
            url.searchParams.set("from", "calendar");
            url.searchParams.set("back_url", buildMonthSummaryReturnHref(focusTarget, scrollTop));
            url.searchParams.set("back_label", "К графику");
            return url.href;
        }

        function syncProfileReturnHref(link, profileUrl, focusTarget, updateHistory) {
            if (!link || !profileUrl) {
                return;
            }
            link.href = buildProfileHref(profileUrl, focusTarget, getMonthSummaryScrollTop());
            if (updateHistory) {
                syncCurrentMonthSummaryHistoryState(focusTarget);
            }
        }

        function bindProfileReturnState(link, profileUrl, focusTarget) {
            if (!link || !profileUrl) {
                return;
            }
            syncProfileReturnHref(link, profileUrl, focusTarget, false);
            ["pointerdown", "focus", "click"].forEach(function (eventName) {
                link.addEventListener(eventName, function () {
                    syncProfileReturnHref(link, profileUrl, focusTarget, eventName === "click");
                }, { capture: true });
            });
        }

        function clearModalReturnParams() {
            const url = new URL(window.location.href);
            if (!url.searchParams.has("calendar_modal")) {
                return;
            }

            stripModalParams(url);
            window.history.replaceState({}, "", url.pathname + url.search + url.hash);
        }

        function getMonthDetail(monthNumber) {
            return context.monthDetailsData[String(monthNumber)] || null;
        }

        function syncMonthDetailsDataNode() {
            if (context.monthDetailsDataNode) {
                context.monthDetailsDataNode.textContent = JSON.stringify(context.monthDetailsData || {});
            }
        }

        function buildMonthDetailUrl(monthNumber) {
            const url = new URL(window.location.href);
            stripModalParams(url);
            url.searchParams.set("calendar_detail_month", monthNumber);
            return url.pathname + url.search + url.hash;
        }

        function fetchMonthDetail(monthNumber) {
            const key = String(monthNumber || "");
            if (!key) {
                return Promise.resolve(null);
            }
            const cachedDetail = getMonthDetail(key);
            if (cachedDetail) {
                return Promise.resolve(cachedDetail);
            }
            if (monthDetailRequests.has(key)) {
                return monthDetailRequests.get(key);
            }

            const request = fetch(buildMonthDetailUrl(key), {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
                signal: context.signal,
            })
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Failed to load calendar month detail.");
                    }
                    return response.json();
                })
                .then(function (payload) {
                    const detail = payload ? payload.calendar_month_detail : null;
                    if (!detail) {
                        return null;
                    }
                    context.monthDetailsData = context.monthDetailsData || {};
                    context.monthDetailsData[key] = detail;
                    syncMonthDetailsDataNode();
                    return detail;
                })
                .catch(function () {
                    return null;
                })
                .finally(function () {
                    monthDetailRequests.delete(key);
                });

            monthDetailRequests.set(key, request);
            return request;
        }

        function setText(node, value) {
            if (node) {
                node.textContent = value;
            }
        }

        function formatEmployeeCount(value) {
            const count = Number(value) || 0;
            const mod100 = count % 100;
            const mod10 = count % 10;
            if ((mod100 < 11 || mod100 > 14) && mod10 === 1) {
                return count + " сотрудник";
            }
            if ((mod100 < 11 || mod100 > 14) && mod10 >= 2 && mod10 <= 4) {
                return count + " сотрудника";
            }
            return count + " сотрудников";
        }

        function formatDays(value) {
            return (Number(value) || 0) + " д.";
        }

        function clearNode(node) {
            if (node) {
                node.innerHTML = "";
            }
        }

        function setTooltip(node, title, text, variant) {
            if (!node || !text) {
                return;
            }

            node.dataset.scheduleStatusTooltip = "";
            node.dataset.scheduleStatusVariant = variant || "empty";
            node.dataset.tooltipTitle = title || "";
            node.dataset.tooltipText = text;
        }

        function createEmpty(text) {
            const placeholder = document.createElement("p");
            placeholder.className = "calendar-month-drawer__empty";
            placeholder.textContent = text;
            return placeholder;
        }

        function renderAffectedEmployees(target, problem, linkClass) {
            const employees = Array.isArray(problem.affected_employees) ? problem.affected_employees : [];
            if (employees.length) {
                employees.forEach(function (employee, index) {
                    if (index > 0) {
                        target.appendChild(document.createTextNode(", "));
                    }

                    if (employee.profile_url) {
                        const link = document.createElement("a");
                        link.className = linkClass;
                        link.dataset.appLink = "";
                        link.title = "Открыть профиль сотрудника " + employee.name;
                        link.setAttribute("aria-label", "Открыть профиль сотрудника " + employee.name);
                        link.textContent = employee.name;
                        bindProfileReturnState(link, employee.profile_url, "issues");
                        target.appendChild(link);
                    } else {
                        target.appendChild(document.createTextNode(employee.name || ""));
                    }
                });
                if (problem.extra_affected_count) {
                    target.appendChild(document.createTextNode(" + еще " + problem.extra_affected_count));
                }
                return true;
            }

            if (Array.isArray(problem.affected_names) && problem.affected_names.length) {
                target.textContent = problem.affected_names.join(", ")
                    + (problem.extra_affected_count ? " + еще " + problem.extra_affected_count : "");
                return true;
            }
            return false;
        }

        function renderDays(detail) {
            const container = context.monthSummaryDays;
            clearNode(container);
            if (!container) {
                return;
            }

            (detail.days || []).forEach(function (day) {
                const item = document.createElement("div");
                item.className = "calendar-month-drawer__day calendar-month-drawer__day--" + (day.status || "free");
                if (day.is_weekend) {
                    item.classList.add("is-weekend");
                }
                setTooltip(
                    item,
                    day.has_conflict ? "Конфликт дня" : (day.has_high_risk ? "Высокий риск дня" : "День месяца"),
                    day.date_iso + " · " + formatEmployeeCount(day.employee_count)
                        + (day.has_conflict ? " · есть конфликт состава" : (day.has_high_risk ? " · есть высокий риск" : "")),
                    day.has_conflict ? "conflict" : (day.has_high_risk ? "risk" : (day.employee_count ? "planned" : "empty"))
                );

                const top = document.createElement("span");
                top.className = "calendar-month-drawer__day-top";
                const number = document.createElement("strong");
                number.textContent = day.day;
                const weekday = document.createElement("span");
                weekday.textContent = day.weekday || "";
                top.appendChild(number);
                top.appendChild(weekday);

                const count = document.createElement("span");
                count.className = "calendar-month-drawer__day-count";
                count.textContent = day.employee_count ? formatEmployeeCount(day.employee_count) : "нет";

                if (day.has_conflict || day.has_high_risk) {
                    const marker = document.createElement("span");
                    marker.className = "calendar-month-drawer__day-marker";
                    marker.textContent = day.has_conflict ? "⚔" : "bolt";
                    if (!day.has_conflict) {
                        marker.classList.add("material-icons-sharp");
                    }
                    item.appendChild(marker);
                }

                item.appendChild(top);
                item.appendChild(count);
                container.appendChild(item);
            });
        }

        function renderProblems(detail) {
            const container = context.monthSummaryProblems;
            clearNode(container);
            if (!container) {
                return;
            }

            const problems = Array.isArray(detail.problems) ? detail.problems : [];
            if (!problems.length) {
                container.appendChild(createEmpty("В этом месяце критичных проблем не найдено."));
                return;
            }

            problems.forEach(function (problem) {
                const item = document.createElement("article");
                item.className = "calendar-month-drawer__problem calendar-month-drawer__problem--" + (problem.kind || "risk");

                const head = document.createElement("div");
                head.className = "calendar-month-drawer__problem-head";
                const period = document.createElement("span");
                period.textContent = problem.period_label || "";
                const title = document.createElement("strong");
                title.textContent = problem.title || "Риск состава";
                head.appendChild(period);
                head.appendChild(title);

                const text = document.createElement("p");
                text.textContent = problem.text || "";
                item.appendChild(head);
                item.appendChild(text);

                if (problem.impact_label || problem.substitution_label) {
                    const meta = document.createElement("div");
                    meta.className = "calendar-month-drawer__problem-meta";
                    [problem.impact_label, problem.substitution_label].forEach(function (label) {
                        if (!label) {
                            return;
                        }
                        const chip = document.createElement("span");
                        chip.textContent = label;
                        meta.appendChild(chip);
                    });
                    item.appendChild(meta);
                }

                if (
                    (Array.isArray(problem.affected_employees) && problem.affected_employees.length)
                    || (Array.isArray(problem.affected_names) && problem.affected_names.length)
                ) {
                    const affected = document.createElement("div");
                    affected.className = "calendar-month-drawer__affected";
                    const label = document.createElement("span");
                    label.textContent = "Отсутствуют:";
                    const names = document.createElement("strong");
                    names.className = "calendar-month-drawer__affected-list";
                    if (renderAffectedEmployees(names, problem, "calendar-month-drawer__affected-link")) {
                        affected.appendChild(label);
                        affected.appendChild(names);
                        item.appendChild(affected);
                    }
                }

                container.appendChild(item);
            });
        }

        function renderGroups(detail) {
            const container = context.monthSummaryGroups;
            clearNode(container);
            if (!container) {
                return;
            }

            const groups = Array.isArray(detail.absence_groups) ? detail.absence_groups : [];
            if (!groups.length) {
                container.appendChild(createEmpty("В этом месяце отсутствующих сотрудников нет."));
                return;
            }

            groups.forEach(function (group) {
                const item = document.createElement("article");
                item.className = "calendar-month-drawer__group";
                const employeeCount = Number(group.employee_count) || 0;
                if (employeeCount <= 2) {
                    item.classList.add("calendar-month-drawer__group--compact");
                } else if (employeeCount >= 5) {
                    item.classList.add("calendar-month-drawer__group--large");
                } else {
                    item.classList.add("calendar-month-drawer__group--medium");
                }

                const head = document.createElement("div");
                head.className = "calendar-month-drawer__group-head";
                const title = document.createElement("strong");
                title.textContent = group.department + " · " + group.production_group;
                const meta = document.createElement("span");
                meta.textContent = formatEmployeeCount(group.employee_count) + " · " + formatDays(group.days);
                head.appendChild(title);
                head.appendChild(meta);
                item.appendChild(head);

                const list = document.createElement("div");
                list.className = "calendar-month-drawer__employees";
                (group.employees || []).forEach(function (employee) {
                    const hasProfile = Boolean(employee.profile_url);
                    const employeeItem = hasProfile
                        ? document.createElement("a")
                        : document.createElement("div");
                    employeeItem.className = "calendar-month-drawer__employee";
                    if (hasProfile) {
                        employeeItem.classList.add("calendar-month-drawer__employee--link");
                        employeeItem.dataset.appLink = "";
                        employeeItem.title = "Открыть профиль сотрудника " + employee.employee_name;
                        employeeItem.setAttribute("aria-label", "Открыть профиль сотрудника " + employee.employee_name);
                        bindProfileReturnState(employeeItem, employee.profile_url, "groups");
                    }
                    const name = document.createElement("strong");
                    name.className = "calendar-month-drawer__employee-name";
                    name.textContent = employee.employee_name;
                    const entries = document.createElement("span");
                    entries.textContent = (employee.entries || [])
                        .map(function (entry) {
                            return entry.period_label + " · " + entry.source_label;
                        })
                        .join("; ");
                    employeeItem.appendChild(name);
                    employeeItem.appendChild(entries);
                    list.appendChild(employeeItem);
                });
                item.appendChild(list);
                container.appendChild(item);
            });
        }

        function renderMonthSummary(detail) {
            setText(context.monthSummaryTitle, detail.title || "Месяц");
            setText(
                context.monthSummarySubtitle,
                "Обзор по текущим фильтрам годового графика: " + formatEmployeeCount(detail.employee_count) + "."
            );
            setText(context.monthSummaryEmployees, formatEmployeeCount(detail.employee_count));
            setText(context.monthSummaryDaysTotal, formatDays(detail.busy_days));
            setText(context.monthSummaryRisks, detail.risk_count || 0);
            setText(context.monthSummaryConflicts, detail.conflict_count || 0);
            renderDays(detail);
            renderProblems(detail);
            renderGroups(detail);
        }

        function openMonthSummary(monthNumber, focusTarget) {
            if (!context.monthSummaryModal) {
                return;
            }
            const detail = getMonthDetail(monthNumber);
            if (!detail) {
                fetchMonthDetail(monthNumber).then(function (fetchedDetail) {
                    if (fetchedDetail) {
                        openMonthSummary(monthNumber, focusTarget);
                    }
                });
                return;
            }

            currentMonthNumber = String(monthNumber);
            renderMonthSummary(detail);
            window.appModal.open(context.monthSummaryModal);

            if (focusTarget === "issues" && context.monthSummaryIssuesSection) {
                window.requestAnimationFrame(function () {
                    context.monthSummaryIssuesSection.scrollIntoView({ behavior: "smooth", block: "start" });
                });
            } else if (focusTarget === "groups" && context.monthSummaryGroups) {
                window.requestAnimationFrame(function () {
                    const section = context.monthSummaryGroups.closest(".calendar-month-drawer__section");
                    (section || context.monthSummaryGroups).scrollIntoView({ behavior: "smooth", block: "start" });
                });
            }
        }

        function restoreMonthSummaryScroll(scrollTop) {
            const numericScrollTop = Number(scrollTop);
            if (!Number.isFinite(numericScrollTop) || numericScrollTop <= 0) {
                return;
            }

            window.requestAnimationFrame(function () {
                window.requestAnimationFrame(function () {
                    const dialog = getMonthSummaryDialog();
                    if (dialog) {
                        dialog.scrollTop = numericScrollTop;
                    }
                });
            });
        }

        function restoreMonthSummaryFromUrl() {
            const url = new URL(window.location.href);
            if (url.searchParams.get("calendar_modal") !== "month_summary") {
                return false;
            }

            const monthNumber = url.searchParams.get("calendar_month");
            if (!monthNumber) {
                clearModalReturnParams();
                return false;
            }

            const scrollTop = url.searchParams.get("calendar_modal_scroll");
            openMonthSummary(monthNumber, scrollTop ? "" : (url.searchParams.get("calendar_modal_focus") || ""));
            restoreMonthSummaryScroll(scrollTop);
            clearModalReturnParams();
            return true;
        }

        function buildMonthUrl(issue) {
            const detail = getMonthDetail(currentMonthNumber);
            if (!detail) {
                return null;
            }

            const url = new URL(window.location.href);
            stripModalParams(url);
            url.searchParams.set("view", "month");
            url.searchParams.set("year", detail.year);
            url.searchParams.set("month", detail.month_number);
            url.searchParams.set("issue", issue || "all");
            return url;
        }

        function navigateToMonth(issue) {
            const url = buildMonthUrl(issue);
            if (!url) {
                return;
            }

            syncCurrentMonthSummaryHistoryState("");
            if (context.monthSummaryModal) {
                window.appModal.close(context.monthSummaryModal);
            }
            if (
                window.KabinetNavigation
                && typeof window.KabinetNavigation.navigate === "function"
                && window.KabinetNavigation.navigate(url.href, true)
            ) {
                return;
            }
            window.location.href = url.href;
        }

        function bindMonthTotals() {
            context.monthTotalButtons = Array.from(document.querySelectorAll("[data-calendar-month-summary-open]"));
        }

        document.addEventListener("click", function (event) {
            const target = event.target instanceof Element ? event.target : null;
            const button = target ? target.closest("[data-calendar-month-summary-open]") : null;
            if (!button) {
                return;
            }

            const focusTarget = target.closest("[data-calendar-month-summary-focus]") ? "issues" : "";
            openMonthSummary(button.dataset.calendarMonthSummaryOpen, focusTarget);
        }, { signal: context.signal });

        function updateMonthDetailsData(nextMonthDetailsData) {
            context.monthDetailsData = nextMonthDetailsData || {};
            syncMonthDetailsDataNode();
        }

        if (context.monthSummaryOpenAction) {
            context.monthSummaryOpenAction.addEventListener("click", function () {
                navigateToMonth("all");
            }, { signal: context.signal });
        }
        if (context.monthSummaryConflictsAction) {
            context.monthSummaryConflictsAction.addEventListener("click", function () {
                navigateToMonth("conflict");
            }, { signal: context.signal });
        }

        function closeMonthSummaryDrawer() {
            if (context.monthSummaryModal) {
                window.appModal.close(context.monthSummaryModal);
            }
        }

        return {
            bindMonthTotals: bindMonthTotals,
            closeMonthSummaryDrawer: closeMonthSummaryDrawer,
            restoreMonthSummaryFromUrl: restoreMonthSummaryFromUrl,
            updateMonthDetailsData: updateMonthDetailsData,
        };
    };
})();
