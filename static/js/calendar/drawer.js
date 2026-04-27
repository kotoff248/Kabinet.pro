(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createDrawerController = function (context, dependencies) {
        const signal = context.signal;

        function renderEntriesSafe(container, entries, emptyText) {
            if (!container) {
                return;
            }

            container.innerHTML = "";
            if (!entries.length) {
                const placeholder = document.createElement("p");
                placeholder.className = "calendar-detail-placeholder";
                placeholder.textContent = emptyText;
                container.appendChild(placeholder);
                return;
            }

            entries.forEach(function (item) {
                const article = document.createElement("article");
                article.className = "calendar-drawer__entry status-" + item.status;

                const main = document.createElement("div");
                main.className = "calendar-drawer__entry-main";
                const strong = document.createElement("strong");
                strong.textContent = item.period_label;
                const type = document.createElement("span");
                type.textContent = (item.source_label ? item.source_label + " • " : "") + item.vacation_type_label;
                main.appendChild(strong);
                main.appendChild(type);

                const side = document.createElement("div");
                side.className = "calendar-drawer__entry-side";
                const status = document.createElement("span");
                status.textContent = item.status_label;
                const days = document.createElement("strong");
                days.textContent = item.days + " д.";
                side.appendChild(status);
                side.appendChild(days);

                if (item.can_request_transfer && item.transfer_url) {
                    const action = document.createElement("button");
                    action.type = "button";
                    action.className = "calendar-drawer__entry-action";
                    action.dataset.transferOpen = "";
                    action.dataset.transferUrl = item.transfer_url;
                    action.dataset.transferTitle = item.transfer_title || item.period_label;
                    action.textContent = "Запросить перенос";
                    side.appendChild(action);
                }

                article.appendChild(main);
                article.appendChild(side);
                container.appendChild(article);
            });
        }

        function openDetailModal() {
            if (!context.detailModal) {
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

            window.appModal.close(context.detailModal);
        }

        function updateDetailCard(employeeId) {
            const detail = context.detailsData[String(employeeId)];
            if (!detail) {
                return;
            }

            context.rows.forEach(function (row) {
                row.classList.toggle("is-active", row.dataset.employeeId === String(employeeId));
            });

            context.detailName.textContent = detail.employee_name;
            context.detailMeta.textContent = detail.position + " • " + detail.department;
            context.detailPeriod.textContent = detail.selected_period_label;
            context.detailSchedule.textContent = detail.selected_schedule_days + " д.";
            context.detailRequests.textContent = detail.selected_request_days + " д.";
            context.detailChanged.textContent = detail.selected_changed_days + " д.";
            context.detailUpcoming.textContent = detail.upcoming_label;
            context.detailUpcomingStatus.textContent = detail.upcoming_status || "";
            renderEntriesSafe(context.selectedList, detail.selected_entries || [], "В выбранном периоде отпусков нет.");
            renderEntriesSafe(context.yearList, detail.year_entries || [], "За этот год записей пока нет.");
            openDetailModal();
        }

        function bindRows() {
            context.rows = Array.from(document.querySelectorAll("[data-employee-id]"));
            context.rows.forEach(function (row) {
                row.addEventListener("click", function () {
                    updateDetailCard(row.dataset.employeeId);
                }, { signal: signal });
            });
        }

        function updateDetailsData(nextDetailsData) {
            context.detailsData = nextDetailsData || {};
            if (context.detailsDataNode) {
                context.detailsDataNode.textContent = JSON.stringify(context.detailsData);
            }
        }

        return {
            bindRows: bindRows,
            updateDetailsData: updateDetailsData,
            closeDetailModal: closeDetailModal,
            closeCalendarDetailDrawer: closeDetailModal,
        };
    };
})();
