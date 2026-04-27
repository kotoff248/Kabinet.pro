# AGENTS.md

## Project Snapshot

This repository is `Kabinet.pro`, a Django 5 vacation management application for
employees, HR, department heads, enterprise heads, and an authorized approval
service role.

Current workspace path:

`D:\Fedya\Инст\МАГИСТЕРСКАЯ\Kabinet.pro`

The UI and domain text are Russian. Keep existing Russian user-facing copy and
watch for Windows console mojibake when reading files from PowerShell; do not
rewrite text just because terminal output looks garbled.

The product/project name is `Kabinet.pro`. Do not call it `OtpuskPro` in new
documentation, UI text, commit messages, or summaries unless quoting an old
file that still uses the previous name.

## Product Direction

The long-term product goal is a manager's cabinet for workforce and vacation
planning. Vacation planning is the central workflow, but the product should grow
toward a broader leadership workspace: approvals, staffing visibility,
department workload, risks, analytics, and planning decisions.

AI/ML is an intended future part of the product, especially for schedule
recommendations, risk scoring, workload prediction, conflict detection, and
decision support. Do not add AI just for decoration. Introduce it only when the
data model, business rules, fallback behavior, and user-facing explanation are
clear enough to test and trust.

## Stack

- Python / Django `5.0.6`
- PostgreSQL via `psycopg[binary]`
- `holidays` for holiday-aware vacation day calculations
- Server-rendered Django templates in `templates/`
- Static CSS/JS in `static/`
- Main settings module: `config.settings.postgres`
- Base settings auto-load `.env`

## Local Setup And Commands

Use the project virtual environment when available:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Create `.env` from `.env.example`. Required database variables:

- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_HOST`
- `DB_PORT`

Useful checks:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test apps.accounts apps.employees apps.leave apps.core --keepdb
```

If the test database schema is stale, rerun tests once without `--keepdb`.

Demo data:

```powershell
.\.venv\Scripts\python.exe manage.py seed_vacation_requests
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --seed-value 42
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --fast
```

There is also a helper script:

```powershell
.\run_postgres.ps1
```

## Main Apps And Routes

- `apps.accounts`: login/logout, role checks, auth helpers.
- `apps.employees`: employees, departments, profile pages, employee forms.
- `apps.leave`: vacation calendar, vacation requests, schedule transfers,
  approvals, analytics, leave ledger.
- `apps.core`: shared project hooks and management commands.

Important routes:

- `/` - login page
- `/main/` - current user's cabinet/profile
- `/employees/` - employee registry
- `/employee/<id>/` - employee profile
- `/departments/` - departments
- `/calendar/` - vacation calendar
- `/calendar/schedule-items/<id>/transfer/` - schedule transfer request
- `/applications/` - approval board
- `/applications/<id>/` - vacation request detail
- `/applications/transfers/<id>/approve/` and `/reject/` - transfer decisions
- `/analytics/` - analytics dashboard

## Roles And Access Rules

Employee roles live in `apps.employees.models.Employees`:

- `employee`
- `hr`
- `department_head`
- `enterprise_head`
- `authorized_person`

Regular employees use the employee side of login. HR, department heads,
enterprise heads, and authorized person use the management side. The authorized
person is a service role and should have only applications/approval access.

Approval rules:

- regular employee request -> department head
- HR or department head request -> enterprise head
- enterprise head request -> authorized person
- users cannot approve their own requests

Centralize access and approval logic in `apps.accounts.services` and
`apps.leave.services`; do not duplicate permission rules in templates or JS.

## Vacation Domain

Default annual paid leave norm is 52 calendar days: 28 basic days plus 24 Far
North/additional days.

Core leave models:

- `VacationRequest`: paid/unpaid/study requests and approval state
- `VacationSchedule`: yearly vacation schedule
- `VacationScheduleItem`: concrete scheduled paid leave period
- `VacationScheduleChangeRequest`: transfer request for an existing schedule item
- `VacationEntitlementPeriod`: working-year entitlement
- `VacationEntitlementAllocation`: allocation/reservation of paid leave days
- workload/staffing/preference models for risk and planning

Paid annual leave should be represented primarily as schedule items. Approved
paid requests may create manual schedule items and must not double-count balance.
Unpaid and study leave do not reduce paid leave balance.

Paid leave chargeable days exclude Russian public holidays. Balance allocation
uses the oldest available working year first and must not silently corrupt data;
raise validation errors for invalid paid leave.

Employee "working/on vacation" status is calculated from active approved
requests and active schedule items, not stored as a manual flag.

## Frontend Structure

Templates are in `templates/`; shared fragments are in `templates/includes/`.
Authenticated pages extend `templates/base.html`. The login page is standalone
and uses `templates/login.html`, `static/css/login_style.css`, and
`static/js/script.js`.

Important frontend files:

- `static/css/main.css`: global layout, sidebar, shared tokens, profile pages
- `static/css/calendar.css`: calendar board, month/year views, calendar modals
- `static/css/employees.css`: employees, departments, profile management UI
- `static/css/applications.css`: approval board, transfer cards, status filters
- `static/js/base.js`: sidebar, app-container replacement, modals
- `static/js/calendar.js`: filtering, calendar rendering, detail drawer, modals
- `static/js/employees-page.js`: employee filters and card rendering
- `static/js/applications-page.js`: approval filters and AJAX updates
- `static/js/profile-sections.js`: section navigation and scroll memory

When editing frontend behavior:

- preserve backend-provided URLs such as `profile_url`; avoid hardcoded URL
  concatenation in JS
- preserve custom selects and segmented controls used by calendar, employees,
  and applications pages
- keep page-specific classes such as calendar body/html state from leaking across
  PJAX-like navigation in `base.js`
- verify significant UI changes with Playwright MCP when possible

## Current Worktree State

As of 2026-04-27 the worktree is intentionally dirty. Do not revert or overwrite
these changes unless explicitly asked.

Current modified areas include:

- leave logic and tests: `apps/leave/models.py`, `apps/leave/services.py`,
  `apps/leave/views.py`, `apps/leave/tests.py`
- seed/tests: `apps/core/management/commands/seed_vacation_requests.py`,
  `apps/core/tests.py`
- employees forms/views/tests: `apps/employees/forms.py`,
  `apps/employees/views.py`, `apps/employees/tests.py`
- shared UI/CSS/JS across employees, calendar, applications, base layout
- templates for applications, base, departments, employees, calendar toolbar and
  board, employee forms/personal info, page controls

Current untracked files include:

- `apps/leave/migrations/0008_vacationscheduleitem_created_from_vacation_request.py`
- `apps/leave/migrations/0009_convert_approved_paid_requests_to_schedule_items.py`
- `templates/includes/calendar/board_content.html`
- `templates/includes/search_control.html`

Treat scratch/generated folders as local artifacts unless the user says
otherwise: `.downloads/`, `.tmp/`, `.playwright-mcp/`, `.run/`, `output/`.

## Agent Working Rules

- Read the relevant code before changing it.
- Keep edits scoped to the requested behavior.
- Do not revert user changes or clean the dirty worktree without permission.
- Add migrations when model fields or data shape changes require them.
- Update focused tests for behavior changes in access rules, leave balance,
  approvals, schedule items, calendar rendering, or AJAX partials.
- Run `manage.py check` after backend changes when feasible.
- Run targeted Django tests for touched apps when feasible.
- Use Context7 MCP for up-to-date framework/library documentation.
- Use Playwright MCP for browser checks of local UI flows.
- Do not commit, stage, or push unless the user explicitly asks.
