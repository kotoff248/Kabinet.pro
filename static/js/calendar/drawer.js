(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createDrawerController = function (context, dependencies) {
        const signal = context.signal;
        let focusHighlightTimeout = null;
        let currentUpcomingAnchor = null;
        let currentDetailEmployeeId = null;
        const detailRequests = new Map();
        const calendarRowSelector = ".timeline-row[data-employee-id], .year-row[data-employee-id]";
        const calendarNavigationSources = [
            "profile",
            "employees",
            "applications",
            "departments",
            "analytics",
            "staffing",
            "notifications",
            "calendar",
        ];

        function stripModalParams(url) {
            url.searchParams.delete("calendar_modal");
            url.searchParams.delete("calendar_month");
            url.searchParams.delete("calendar_modal_focus");
            url.searchParams.delete("calendar_modal_scroll");
            url.searchParams.delete("calendar_employee");
            url.searchParams.delete("calendar_focus_employee");
            url.searchParams.delete("calendar_focus_start");
            url.searchParams.delete("calendar_focus_end");
            url.searchParams.delete("calendar_detail_employee");
            url.searchParams.delete("calendar_detail_month");
        }

        function getDetailDialog() {
            return context.detailModal
                ? context.detailModal.querySelector(".app-modal__dialog")
                : null;
        }

        function getDetailScrollTop() {
            const dialog = getDetailDialog();
            return dialog ? Math.max(0, Math.round(dialog.scrollTop || 0)) : 0;
        }

        function buildDetailReturnHref(scrollTop) {
            const url = new URL(window.location.href);
            stripModalParams(url);
            url.searchParams.set("calendar_modal", "employee_detail");
            url.searchParams.set("calendar_employee", currentDetailEmployeeId || "");
            if (Number(scrollTop) > 0) {
                url.searchParams.set("calendar_modal_scroll", String(Math.round(Number(scrollTop))));
            }
            return url.pathname + url.search + url.hash;
        }

        function getCalendarNavigationSource() {
            const url = new URL(window.location.href);
            const source = url.searchParams.get("from") || "";
            return calendarNavigationSources.indexOf(source) === -1 ? "calendar" : source;
        }

        function buildDetailLinkedHref(targetUrl, scrollTop) {
            if (!targetUrl) {
                return "#";
            }

            const url = new URL(targetUrl, window.location.href);
            url.searchParams.set("from", getCalendarNavigationSource());
            url.searchParams.set("back_url", buildDetailReturnHref(scrollTop));
            url.searchParams.set("back_label", "К графику");
            return url.href;
        }

        function buildProfileHref(profileUrl, scrollTop) {
            return buildDetailLinkedHref(profileUrl, scrollTop);
        }

        function syncDetailsDataNode() {
            if (context.detailsDataNode) {
                context.detailsDataNode.textContent = JSON.stringify(context.detailsData || {});
            }
        }

        function buildEmployeeDetailUrl(employeeId) {
            const url = new URL(window.location.href);
            stripModalParams(url);
            url.searchParams.set("calendar_detail_employee", employeeId);
            return url.pathname + url.search + url.hash;
        }

        function fetchEmployeeDetail(employeeId) {
            const key = String(employeeId || "");
            if (!key) {
                return Promise.resolve(null);
            }
            if (context.detailsData && context.detailsData[key]) {
                return Promise.resolve(context.detailsData[key]);
            }
            if (detailRequests.has(key)) {
                return detailRequests.get(key);
            }

            const request = fetch(buildEmployeeDetailUrl(key), {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
                signal: signal,
            })
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Failed to load calendar employee detail.");
                    }
                    return response.json();
                })
                .then(function (payload) {
                    const detail = payload ? payload.calendar_detail : null;
                    if (!detail) {
                        return null;
                    }
                    context.detailsData = context.detailsData || {};
                    context.detailsData[key] = detail;
                    syncDetailsDataNode();
                    return detail;
                })
                .catch(function () {
                    return null;
                })
                .finally(function () {
                    detailRequests.delete(key);
                });

            detailRequests.set(key, request);
            return request;
        }

        function syncCurrentDetailHistoryState(scrollTop) {
            if (!currentDetailEmployeeId) {
                return;
            }

            window.history.replaceState({}, "", buildDetailReturnHref(scrollTop));
        }

        function syncProfileReturnState(link, updateHistory) {
            if (!link) {
                return;
            }

            const profileUrl = link.dataset.calendarProfileUrl || link.href;
            if (!profileUrl || profileUrl === "#") {
                return;
            }

            const scrollTop = getDetailScrollTop();
            link.href = buildProfileHref(profileUrl, scrollTop);
            if (updateHistory) {
                syncCurrentDetailHistoryState(scrollTop);
            }
        }

        function syncDetailLinkReturnState(link, updateHistory) {
            if (!link) {
                return;
            }

            const targetUrl = link.dataset.calendarDetailReturnUrl || link.href;
            if (!targetUrl || targetUrl === "#") {
                return;
            }

            const scrollTop = getDetailScrollTop();
            link.href = buildDetailLinkedHref(targetUrl, scrollTop);
            if (updateHistory) {
                syncCurrentDetailHistoryState(scrollTop);
            }
        }

        function clearModalReturnParams() {
            const url = new URL(window.location.href);
            if (!url.searchParams.has("calendar_modal")) {
                return;
            }

            stripModalParams(url);
            window.history.replaceState({}, "", url.pathname + url.search + url.hash);
        }

        function getBoardScrollElement() {
            return document.querySelector("[data-calendar-grid-body]") || document.querySelector(".calendar-board-scroll");
        }

        function scrollRowIntoBoardView(row, options) {
            const scrollOptions = options || {};
            const behavior = scrollOptions.behavior || "auto";
            const boardScroll = getBoardScrollElement();
            if (!boardScroll) {
                if (typeof row.scrollIntoView === "function") {
                    row.scrollIntoView({ behavior: behavior, block: "center", inline: "nearest" });
                }
                return;
            }

            const boardRect = boardScroll.getBoundingClientRect();
            const rowRect = row.getBoundingClientRect();
            const rowOffset = rowRect.top - boardRect.top;
            const centeredOffset = Math.max(0, (boardScroll.clientHeight - rowRect.height) / 2);
            const targetTop = Math.max(0, boardScroll.scrollTop + rowOffset - centeredOffset);

            if (typeof boardScroll.scrollTo === "function") {
                boardScroll.scrollTo({
                    top: targetTop,
                    left: boardScroll.scrollLeft,
                    behavior: behavior,
                });
                return;
            }

            boardScroll.scrollTop = targetTop;
        }

        function scheduleFocusAnchorInCalendar(anchor, options) {
            window.requestAnimationFrame(function () {
                window.requestAnimationFrame(function () {
                    focusAnchorInCalendar(anchor, options);
                });
            });
        }

        function clearEntryHighlights() {
            document.querySelectorAll(".is-calendar-entry-highlight").forEach(function (element) {
                element.classList.remove("is-calendar-entry-highlight");
            });
        }

        function dateToComparable(value) {
            return String(value || "");
        }

        function getMonthNumber(value) {
            const parts = String(value || "").split("-");
            return Number(parts[1]) || 0;
        }

        function escapeSelectorValue(value) {
            if (window.CSS && typeof CSS.escape === "function") {
                return CSS.escape(value);
            }

            return String(value).replace(/"/g, '\\"');
        }

        function getCalendarRowSelector(employeeId) {
            const escapedEmployeeId = escapeSelectorValue(employeeId);
            return '.timeline-row[data-employee-id="' + escapedEmployeeId + '"], '
                + '.year-row[data-employee-id="' + escapedEmployeeId + '"]';
        }

        function getDirectCollapseBody(section) {
            if (!section) {
                return null;
            }

            return Array.from(section.children).find(function (child) {
                return child.hasAttribute("data-calendar-collapse-body");
            }) || null;
        }

        function getCollapseKey(section) {
            if (!section) {
                return "";
            }

            const level = section.dataset.calendarCollapseLevel || "group";
            const id = section.dataset.calendarCollapseId || "0";
            const name = section.dataset.calendarCollapseName || "";
            return level + ":" + id + ":" + name;
        }

        function forgetCollapsedSection(section) {
            try {
                const storageKey = context.calendarCollapseStorageKey || "calendar:collapse-state";
                const rawValue = sessionStorage.getItem(storageKey);
                const parsedValue = JSON.parse(rawValue || "[]");
                if (!Array.isArray(parsedValue)) {
                    return;
                }

                const collapseKey = getCollapseKey(section);
                const nextValue = parsedValue.filter(function (storedKey) {
                    return storedKey !== collapseKey;
                });
                sessionStorage.setItem(storageKey, JSON.stringify(nextValue));
            } catch (error) {
                return;
            }
        }

        function expandCollapsedAncestorsForFocus(row) {
            const sections = [];
            let node = row ? row.parentElement : null;
            while (node) {
                if (node.matches && node.matches("[data-calendar-collapse-section]")) {
                    sections.unshift(node);
                }
                node = node.parentElement;
            }

            sections.forEach(function (section) {
                if (!section.classList.contains("is-collapsed")) {
                    return;
                }

                const toggle = section.querySelector("[data-calendar-collapse-toggle]");
                const body = getDirectCollapseBody(section);
                section.classList.remove("is-collapsed");
                if (toggle) {
                    toggle.setAttribute("aria-expanded", "true");
                    toggle.removeAttribute("title");
                }
                if (body) {
                    body.setAttribute("aria-hidden", "false");
                }
                forgetCollapsedSection(section);
            });
        }

        function setTooltip(node, title, text, variant) {
            if (!node) {
                return;
            }

            if (!text) {
                delete node.dataset.scheduleStatusTooltip;
                delete node.dataset.scheduleStatusVariant;
                delete node.dataset.tooltipTitle;
                delete node.dataset.tooltipText;
                return;
            }

            node.dataset.scheduleStatusTooltip = "";
            node.dataset.scheduleStatusVariant = variant || "empty";
            node.dataset.tooltipTitle = title || "";
            node.dataset.tooltipText = text;
        }

        function focusAnchorInCalendar(anchor, options) {
            if (!anchor) {
                return;
            }

            const focusOptions = options || {};
            const employeeId = String(anchor.employee_id || "");
            const startDate = dateToComparable(anchor.start_date);
            const endDate = dateToComparable(anchor.end_date);
            if (!employeeId || !startDate || !endDate) {
                return;
            }

            const row = document.querySelector(getCalendarRowSelector(employeeId));
            if (!row) {
                return;
            }

            expandCollapsedAncestorsForFocus(row);

            if (focusOptions.closeDetail !== false) {
                closeDetailModal();
            }
            clearEntryHighlights();

            row.classList.add("is-calendar-entry-highlight");
            const dateCells = Array.from(row.querySelectorAll("[data-calendar-date]"));
            if (dateCells.length) {
                dateCells.forEach(function (cell) {
                    const cellDate = dateToComparable(cell.dataset.calendarDate);
                    if (cellDate >= startDate && cellDate <= endDate) {
                        cell.classList.add("is-calendar-entry-highlight");
                    }
                });
            } else {
                const startMonth = getMonthNumber(startDate);
                const endMonth = getMonthNumber(endDate);
                row.querySelectorAll("[data-calendar-month]").forEach(function (cell) {
                    const month = Number(cell.dataset.calendarMonth || 0);
                    if (month >= startMonth && month <= endMonth) {
                        cell.classList.add("is-calendar-entry-highlight");
                    }
                });
            }

            scrollRowIntoBoardView(row, { behavior: focusOptions.behavior || "smooth" });

            if (focusHighlightTimeout) {
                window.clearTimeout(focusHighlightTimeout);
            }
            focusHighlightTimeout = window.setTimeout(
                clearEntryHighlights,
                Number(focusOptions.highlightDuration) || 1900
            );
        }

        function updateIssueSummary(detail) {
            if (!context.detailIssue) {
                return;
            }

            const riskDetails = detail.risk_details || {};
            const status = riskDetails.status || (detail.has_conflict ? "conflict" : (detail.has_high_risk ? "risk" : "clear"));
            const hasConflict = status === "conflict" || Boolean(detail.has_conflict);
            const hasHighRisk = status === "risk" || Boolean(detail.has_high_risk);
            context.detailIssue.classList.toggle("calendar-drawer__issue--conflict", hasConflict);
            context.detailIssue.classList.toggle("calendar-drawer__issue--risk", !hasConflict && hasHighRisk);
            context.detailIssue.classList.toggle("calendar-drawer__issue--clear", !hasConflict && !hasHighRisk);

            const icon = context.detailIssue.querySelector(".calendar-drawer__issue-icon");
            if (icon) {
                icon.innerHTML = hasConflict
                    ? '<span class="calendar-drawer__issue-symbol" aria-hidden="true">⚔</span>'
                    : '<span class="material-icons-sharp" aria-hidden="true">' + (hasHighRisk ? "bolt" : "check_circle") + '</span>';
            }
            if (context.detailIssueLabel) {
                context.detailIssueLabel.textContent = riskDetails.label || detail.issue_label || "Проблем нет";
            }
            if (context.detailIssueDescription) {
                context.detailIssueDescription.textContent = riskDetails.summary || detail.issue_description || "В выбранном периоде критичных проблем не найдено.";
            }
            setTooltip(
                context.detailIssue,
                riskDetails.label || detail.issue_label || "Проблем нет",
                riskDetails.summary || detail.issue_description || "В выбранном периоде критичных проблем не найдено.",
                hasConflict ? "conflict" : (hasHighRisk ? "risk" : "planned")
            );
            renderRiskReasons(riskDetails.problems || riskDetails.reasons || []);
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
                        link.href = buildProfileHref(employee.profile_url, getDetailScrollTop());
                        link.dataset.appLink = "";
                        link.dataset.calendarDetailProfileReturn = "";
                        link.dataset.calendarProfileUrl = employee.profile_url;
                        link.setAttribute("aria-label", "Открыть профиль сотрудника " + employee.name);
                        setTooltip(
                            link,
                            "Профиль сотрудника",
                            "Открыть профиль сотрудника " + employee.name + ".",
                            "info"
                        );
                        link.textContent = employee.name;
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

        function renderRiskReasons(problems) {
            if (!context.detailIssueReasons) {
                return;
            }

            const safeProblems = Array.isArray(problems) ? problems : [];
            context.detailIssueReasons.innerHTML = "";
            context.detailIssueReasons.classList.toggle("is-empty", !safeProblems.length);

            safeProblems.forEach(function (problem) {
                const item = document.createElement("article");
                item.className = "calendar-drawer__issue-problem calendar-drawer__issue-problem--" + (problem.kind || "risk");

                const head = document.createElement("div");
                head.className = "calendar-drawer__issue-problem-head";
                if (problem.period_label) {
                    const period = document.createElement("span");
                    period.className = "calendar-drawer__issue-period";
                    period.textContent = problem.period_label;
                    head.appendChild(period);
                }
                const title = document.createElement("strong");
                title.textContent = problem.title || "Риск состава";
                head.appendChild(title);

                const text = document.createElement("p");
                text.textContent = problem.text || problem.summary || "";

                const meta = document.createElement("div");
                meta.className = "calendar-drawer__issue-problem-meta";
                [problem.impact_label, problem.substitution_label].forEach(function (label) {
                    if (!label) {
                        return;
                    }
                    const chip = document.createElement("span");
                    chip.textContent = label;
                    meta.appendChild(chip);
                });

                item.appendChild(head);
                item.appendChild(text);
                if (meta.childElementCount) {
                    item.appendChild(meta);
                }
                if (
                    (Array.isArray(problem.affected_employees) && problem.affected_employees.length)
                    || (Array.isArray(problem.affected_names) && problem.affected_names.length)
                ) {
                    const affected = document.createElement("div");
                    affected.className = "calendar-drawer__issue-affected";
                    const affectedLabel = document.createElement("span");
                    affectedLabel.textContent = "Отсутствуют:";
                    const affectedNames = document.createElement("strong");
                    affectedNames.className = "calendar-drawer__affected-list";
                    if (renderAffectedEmployees(affectedNames, problem, "calendar-drawer__affected-link")) {
                        affected.appendChild(affectedLabel);
                        affected.appendChild(affectedNames);
                        item.appendChild(affected);
                    }
                }
                context.detailIssueReasons.appendChild(item);
            });
        }

        function updateProfileLink(detail) {
            if (!context.detailProfileLink) {
                return;
            }

            const link = context.detailProfileLink;
            const roleVariant = detail.role_variant || "employee";
            const roleLabel = detail.role_label || "";
            const employeeName = detail.employee_name || "сотрудника";

            Array.from(link.classList).forEach(function (className) {
                if (className.indexOf("calendar-drawer__profile-link--") === 0) {
                    link.classList.remove(className);
                }
            });
            link.classList.add("calendar-drawer__profile-link--" + roleVariant);

            if (detail.profile_url) {
                link.href = buildProfileHref(detail.profile_url, getDetailScrollTop());
                link.dataset.calendarDetailProfileReturn = "";
                link.dataset.calendarProfileUrl = detail.profile_url;
                link.classList.remove("is-hidden");
            } else {
                link.href = "#";
                delete link.dataset.calendarDetailProfileReturn;
                delete link.dataset.calendarProfileUrl;
                link.classList.add("is-hidden");
            }

            link.setAttribute(
                "aria-label",
                "Открыть профиль сотрудника " + employeeName + (roleLabel ? ". " + roleLabel : "")
            );
            setTooltip(
                link,
                roleLabel || "Профиль",
                "Открыть профиль сотрудника " + employeeName + ".",
                "info"
            );
            link.innerHTML = "";

            const icon = document.createElement("span");
            icon.className = detail.role_icon_type === "symbol"
                ? "calendar-drawer__profile-symbol"
                : "material-icons-sharp";
            icon.setAttribute("aria-hidden", "true");
            icon.textContent = detail.role_icon || "person";
            link.appendChild(icon);
        }

        function renderManagementBadges(badges) {
            if (!context.detailManagementBadges) {
                return;
            }

            const safeBadges = Array.isArray(badges) ? badges : [];
            context.detailManagementBadges.innerHTML = "";
            context.detailManagementBadges.classList.toggle("is-empty", !safeBadges.length);

            safeBadges.forEach(function (badge) {
                const item = document.createElement("span");
                item.className = "calendar-drawer__employee-badge calendar-drawer__employee-badge--" + (badge.variant || "employee");

                const icon = document.createElement("span");
                icon.className = badge.icon_type === "symbol"
                    ? "calendar-drawer__employee-badge-symbol"
                    : "material-icons-sharp";
                icon.setAttribute("aria-hidden", "true");
                icon.textContent = badge.icon || "verified_user";

                const label = document.createElement("span");
                label.textContent = badge.label || "";

                item.appendChild(icon);
                item.appendChild(label);
                context.detailManagementBadges.appendChild(item);
            });
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
            icon.className = badge.icon_type === "symbol"
                ? "calendar-drawer__employee-badge-symbol"
                : "material-icons-sharp";
            icon.setAttribute("aria-hidden", "true");
            icon.textContent = badge.icon || "person_add";
            badgeNode.appendChild(icon);

            return badgeNode;
        }

        function renderDetailName(detail) {
            if (!context.detailName) {
                return;
            }

            context.detailName.innerHTML = "";
            context.detailName.appendChild(document.createTextNode(detail.employee_name || "Сотрудник"));
            const badge = createNewHireBadge(detail.employee_new_hire_badge);
            if (badge) {
                context.detailName.appendChild(badge);
            }
        }

        function appendRiskLine(container, item) {
            const risk = document.createElement("span");
            risk.className = "calendar-drawer__entry-risk";
            if (item.has_conflict) {
                risk.classList.add("calendar-drawer__entry-risk--conflict");
            } else if (item.has_high_risk) {
                risk.classList.add("calendar-drawer__entry-risk--high");
            }

            const reason = item.risk_short_reason || item.conflict_summary || "";
            if (item.has_conflict) {
                risk.textContent = "Конфликт состава";
            } else {
                risk.textContent = "Риск: " + (item.risk_label || "Низкий")
                    + (item.risk_score ? " · " + item.risk_score + "%" : "")
                    + (reason ? " · " + reason : "");
            }
            setTooltip(
                risk,
                item.has_conflict ? "Конфликт состава" : "Риск записи",
                item.has_conflict
                    ? (item.conflict_summary || "Запись пересекается с правилом состава и требует проверки.")
                    : (reason || "Оценка учитывает нагрузку отдела и пересечения отпусков."),
                item.has_conflict ? "conflict" : (item.has_high_risk ? "risk" : "planned")
            );
            container.appendChild(risk);
        }

        function createEntryStage(item) {
            if (!item || !item.stage_label) {
                return null;
            }

            const stage = document.createElement("span");
            stage.className = "calendar-drawer__entry-stage calendar-drawer__entry-stage--" + (item.stage || "upcoming");
            stage.setAttribute("aria-label", "Этап: " + item.stage_label);
            setTooltip(
                stage,
                "Этап: " + item.stage_label,
                "Показывает, на каком этапе находится запись: черновик, план, утверждение или фактическое отсутствие.",
                item.stage === "approved" ? "planned" : (item.stage === "draft" ? "medium" : "info")
            );

            const icon = document.createElement("span");
            icon.className = "material-icons-sharp";
            icon.setAttribute("aria-hidden", "true");
            icon.textContent = item.stage_icon || "event";

            const label = document.createElement("span");
            label.textContent = item.stage_label;

            stage.appendChild(icon);
            stage.appendChild(label);
            return stage;
        }

        function renderEntriesSafe(container, entries, emptyText) {
            if (!container) {
                return;
            }

            const safeEntries = Array.isArray(entries) ? entries : [];
            container.innerHTML = "";
            if (!safeEntries.length) {
                const placeholder = document.createElement("p");
                placeholder.className = "calendar-detail-placeholder";
                placeholder.textContent = emptyText;
                container.appendChild(placeholder);
                return;
            }

            safeEntries.forEach(function (item) {
                const article = document.createElement("article");
                article.className = "calendar-drawer__entry calendar-drawer__entry--stage-" + (item.stage || "upcoming") + " status-" + item.status;

                const main = document.createElement("div");
                main.className = "calendar-drawer__entry-main";
                const strong = document.createElement("strong");
                strong.textContent = item.period_label;
                const meta = document.createElement("span");
                meta.className = "calendar-drawer__entry-meta";
                const type = document.createElement("span");
                type.textContent = (item.source_label ? item.source_label + " • " : "") + item.vacation_type_label;
                setTooltip(
                    type,
                    "Источник",
                    "Показывает, откуда появилась запись: график, заявка, перенос или ручное действие.",
                    "info"
                );
                const stage = createEntryStage(item);
                meta.appendChild(type);
                if (stage) {
                    meta.appendChild(stage);
                }
                main.appendChild(strong);
                main.appendChild(meta);
                appendRiskLine(main, item);

                const side = document.createElement("div");
                side.className = "calendar-drawer__entry-side";
                const status = document.createElement("span");
                status.textContent = item.status_label;
                setTooltip(
                    status,
                    "Статус",
                    "Текущий статус записи или связанной заявки.",
                    item.status === "approved" ? "planned" : (item.status === "rejected" ? "empty" : "medium")
                );
                const days = document.createElement("strong");
                days.textContent = item.days + " д.";
                setTooltip(days, "Дней", "Количество календарных дней этой записи.", "info");
                side.appendChild(status);
                side.appendChild(days);

                if (item.detail_url) {
                    const detailAction = document.createElement("a");
                    detailAction.className = "calendar-drawer__entry-action calendar-drawer__entry-action--link";
                    detailAction.href = buildDetailLinkedHref(item.detail_url, getDetailScrollTop());
                    detailAction.dataset.appLink = "";
                    detailAction.dataset.calendarDetailReturn = "";
                    detailAction.dataset.calendarDetailReturnUrl = item.detail_url;
                    detailAction.textContent = item.detail_label || "Открыть заявку";
                    setTooltip(
                        detailAction,
                        item.detail_label || "Открыть заявку",
                        "Открывает детали заявки или записи согласования.",
                        "info"
                    );
                    side.appendChild(detailAction);
                }

                if (item.can_request_transfer && item.transfer_url) {
                    const action = document.createElement("button");
                    action.type = "button";
                    action.className = "calendar-drawer__entry-action";
                    action.dataset.transferOpen = "";
                    action.dataset.transferUrl = item.transfer_url;
                    action.dataset.transferPreviewUrl = item.transfer_preview_url || "";
                    action.dataset.transferTitle = item.transfer_title || item.period_label;
                    action.dataset.transferEmployeeId = item.transfer_employee_id || (item.anchor ? item.anchor.employee_id : "");
                    action.dataset.transferExcludeScheduleItem = item.transfer_exclude_schedule_item_id || (
                        item.anchor && item.anchor.source_kind === "schedule" ? item.anchor.source_id : ""
                    );
                    action.dataset.transferYear = item.transfer_year || (
                        item.anchor && item.anchor.start_date ? String(item.anchor.start_date).slice(0, 4) : ""
                    );
                    action.dataset.transferActionLabel = item.transfer_action_label || "";
                    action.dataset.transferSubmitLabel = item.transfer_submit_label || "";
                    action.dataset.transferHint = item.transfer_hint || "";
                    action.dataset.transferModalTitle = item.transfer_modal_title || "";
                    action.dataset.transferModalSubtitle = item.transfer_modal_subtitle || "";
                    action.textContent = item.transfer_action_label || "Запросить перенос";
                    setTooltip(
                        action,
                        item.transfer_action_label || "Запросить перенос",
                        item.transfer_hint || "Открывает форму запроса переноса выбранного периода.",
                        "medium"
                    );
                    side.appendChild(action);
                }

                const focusAction = document.createElement("button");
                focusAction.type = "button";
                focusAction.className = "calendar-drawer__entry-action calendar-drawer__entry-action--ghost";
                focusAction.dataset.calendarFocusEntry = "";
                if (item.anchor) {
                    focusAction.dataset.employeeId = item.anchor.employee_id;
                    focusAction.dataset.startDate = item.anchor.start_date;
                    focusAction.dataset.endDate = item.anchor.end_date;
                }
                focusAction.textContent = "Показать в графике";
                setTooltip(
                    focusAction,
                    "Показать в графике",
                    "Подсвечивает эту запись на календарной доске и прокручивает к ней.",
                    "info"
                );
                side.appendChild(focusAction);

                article.appendChild(main);
                article.appendChild(side);
                container.appendChild(article);
            });
        }

        function openDetailModal() {
            if (!context.detailModal) {
                return;
            }
            if (!window.appModal || typeof window.appModal.open !== "function") {
                window.requestAnimationFrame(openDetailModal);
                return;
            }

            dependencies.closeVacationModal();
            dependencies.closeCustomSelects();
            window.appModal.open(context.detailModal);
        }

        function closeDetailModal() {
            if (!context.detailModal) {
                return;
            }
            if (!window.appModal || typeof window.appModal.close !== "function") {
                return;
            }

            window.appModal.close(context.detailModal);
        }

        function updateDetailCard(employeeId) {
            const detail = context.detailsData[String(employeeId)];
            if (!detail) {
                fetchEmployeeDetail(employeeId).then(function (fetchedDetail) {
                    if (fetchedDetail) {
                        updateDetailCard(employeeId);
                    }
                });
                return;
            }
            currentDetailEmployeeId = String(employeeId);

            context.rows.forEach(function (row) {
                row.classList.toggle("is-active", row.dataset.employeeId === String(employeeId));
            });

            renderDetailName(detail);
            if (context.detailPosition) {
                context.detailPosition.textContent = detail.position || "Должность не указана";
            }
            if (context.detailDepartment) {
                context.detailDepartment.textContent = detail.department || "Не указан";
            }
            if (context.detailGroup) {
                context.detailGroup.textContent = detail.production_group || "Не указана";
            }
            updateProfileLink(detail);
            renderManagementBadges(detail.employee_management_badges);
            context.detailPeriod.textContent = detail.selected_period_label;
            context.detailSchedule.textContent = detail.selected_schedule_days + " д.";
            context.detailRequests.textContent = detail.selected_request_days + " д.";
            context.detailChanged.textContent = detail.selected_changed_days + " д.";
            context.detailUpcoming.textContent = detail.upcoming_label;
            context.detailUpcomingStatus.textContent = detail.upcoming_status || "";
            currentUpcomingAnchor = detail.upcoming_anchor || null;
            if (context.detailUpcomingAction) {
                context.detailUpcomingAction.classList.toggle("is-hidden", !currentUpcomingAnchor);
            }
            updateIssueSummary(detail);

            const primaryEntries = Array.isArray(detail.primary_entries) ? detail.primary_entries : (detail.selected_entries || []);
            const secondaryEntries = Array.isArray(detail.secondary_entries) ? detail.secondary_entries : (detail.year_entries || []);
            const hasSecondary = !detail.is_year_view && secondaryEntries.length > 0;
            if (context.primaryTitle) {
                context.primaryTitle.textContent = detail.primary_entries_title || (detail.is_year_view ? "Записи за год" : "Отпуска в выбранном месяце");
            }
            if (context.secondaryTitle) {
                context.secondaryTitle.textContent = detail.secondary_entries_title || "Остальные записи за год";
            }
            if (context.secondarySection) {
                context.secondarySection.classList.toggle("is-hidden", !hasSecondary);
            }
            if (context.detailContentGrid) {
                context.detailContentGrid.classList.toggle("calendar-drawer__content-grid--single", !hasSecondary);
            }

            renderEntriesSafe(
                context.primaryList || context.selectedList,
                primaryEntries,
                detail.primary_entries_empty || (detail.is_year_view ? "За этот год записей пока нет." : "В выбранном месяце отпусков нет.")
            );
            renderEntriesSafe(
                context.secondaryList || context.yearList,
                secondaryEntries,
                detail.secondary_entries_empty || "Других записей за год нет."
            );
            openDetailModal();
        }

        function restoreDetailDrawerScroll(scrollTop) {
            const numericScrollTop = Number(scrollTop);
            if (!Number.isFinite(numericScrollTop) || numericScrollTop <= 0) {
                return;
            }

            window.requestAnimationFrame(function () {
                window.requestAnimationFrame(function () {
                    const dialog = getDetailDialog();
                    if (dialog) {
                        dialog.scrollTop = numericScrollTop;
                    }
                });
            });
        }

        function restoreDetailDrawerFromUrl() {
            const url = new URL(window.location.href);
            if (url.searchParams.get("calendar_modal") !== "employee_detail") {
                return false;
            }

            const employeeId = url.searchParams.get("calendar_employee");
            const focusAnchor = getEntryFocusAnchorFromUrl(url);
            if (!employeeId) {
                clearModalReturnParams();
                return false;
            }

            if (!context.detailsData[String(employeeId)]) {
                fetchEmployeeDetail(employeeId).then(function (detail) {
                    if (!detail) {
                        clearModalReturnParams();
                        return;
                    }
                    updateDetailCard(employeeId);
                    restoreDetailDrawerScroll(url.searchParams.get("calendar_modal_scroll"));
                    scheduleFocusAnchorInCalendar(focusAnchor, {
                        behavior: "auto",
                        closeDetail: false,
                        highlightDuration: 7000,
                    });
                    clearModalReturnParams();
                });
                return true;
            }

            updateDetailCard(employeeId);
            restoreDetailDrawerScroll(url.searchParams.get("calendar_modal_scroll"));
            scheduleFocusAnchorInCalendar(focusAnchor, {
                behavior: "auto",
                closeDetail: false,
                highlightDuration: 7000,
            });
            clearModalReturnParams();
            return true;
        }

        function getEntryFocusAnchorFromUrl(url) {
            const employeeId = url.searchParams.get("calendar_focus_employee");
            const startDate = url.searchParams.get("calendar_focus_start");
            const endDate = url.searchParams.get("calendar_focus_end");
            if (!employeeId || !startDate || !endDate) {
                return null;
            }

            return {
                employee_id: employeeId,
                start_date: startDate,
                end_date: endDate,
            };
        }

        function restoreEntryFocusFromUrl() {
            const url = new URL(window.location.href);
            const focusAnchor = getEntryFocusAnchorFromUrl(url);
            if (!focusAnchor) {
                return false;
            }

            scheduleFocusAnchorInCalendar(focusAnchor, {
                behavior: "auto",
                highlightDuration: 7000,
            });

            stripModalParams(url);
            window.history.replaceState({}, "", url.pathname + url.search + url.hash);
            return true;
        }

        function bindRows() {
            context.rows = Array.from(document.querySelectorAll(calendarRowSelector));
            context.rows.forEach(function (row) {
                row.addEventListener("click", function (event) {
                    if (event.target.closest("a, button")) {
                        return;
                    }
                    updateDetailCard(row.dataset.employeeId);
                }, { signal: signal });
                row.addEventListener("keydown", function (event) {
                    if (event.target.closest("a, button")) {
                        return;
                    }
                    if (event.key !== "Enter" && event.key !== " ") {
                        return;
                    }

                    event.preventDefault();
                    updateDetailCard(row.dataset.employeeId);
                }, { signal: signal });
            });
        }

        if (context.detailUpcomingAction) {
            context.detailUpcomingAction.addEventListener("click", function () {
                focusAnchorInCalendar(currentUpcomingAnchor);
            }, { signal: signal });
        }

        if (context.detailModal) {
            context.detailModal.addEventListener("click", function (event) {
                const target = event.target instanceof Element ? event.target : null;
                const action = target ? target.closest("[data-calendar-focus-entry]") : null;
                if (!action || !context.detailModal.contains(action)) {
                    return;
                }

                event.preventDefault();
                focusAnchorInCalendar({
                    employee_id: action.dataset.employeeId,
                    start_date: action.dataset.startDate,
                    end_date: action.dataset.endDate,
                });
            }, { signal: signal });
        }

        ["pointerdown", "focus", "click"].forEach(function (eventName) {
            document.addEventListener(eventName, function (event) {
                const target = event.target instanceof Element ? event.target : null;
                if (!target || !context.detailModal || !context.detailModal.contains(target)) {
                    return;
                }

                const detailLink = target.closest("[data-calendar-detail-return]");
                if (detailLink) {
                    syncDetailLinkReturnState(detailLink, eventName === "click");
                    return;
                }

                const profileLink = target.closest("[data-calendar-detail-profile-return]");
                if (profileLink) {
                    syncProfileReturnState(profileLink, eventName === "click");
                }
            }, { capture: true, signal: signal });
        });

        function updateDetailsData(nextDetailsData) {
            context.detailsData = nextDetailsData || {};
            syncDetailsDataNode();
        }

        return {
            bindRows: bindRows,
            updateDetailsData: updateDetailsData,
            closeDetailModal: closeDetailModal,
            closeCalendarDetailDrawer: closeDetailModal,
            restoreDetailDrawerFromUrl: restoreDetailDrawerFromUrl,
            restoreEntryFocusFromUrl: restoreEntryFocusFromUrl,
        };
    };
})();
