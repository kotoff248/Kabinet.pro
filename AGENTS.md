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

## Encoding And Russian Text

Most project text shown to users is Russian. Be careful with encoding on
Windows: PowerShell or tool output may display valid UTF-8 Russian text as
mojibake such as `РџСЂ...`, `РЎ...`, `Ð...`, or `Ñ...`.

Rules for Russian text:

- Do not assume Russian text is corrupted only because terminal output looks
  corrupted.
- Before editing Russian copy, inspect the actual file with an explicit UTF-8
  reader or verify the rendered page in the browser.
- When creating or editing files that contain Russian text, write UTF-8 and avoid
  accidental Windows-1251/OEM recoding.
- After changing Russian copy, check for obvious mojibake patterns in the edited
  file and in the browser-rendered UI.
- Do not mass-replace suspicious sequences like `Р` or `С`; those are also real
  Cyrillic letters. Fix only confirmed corrupted text.

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

When an agent starts Django for browser checks, do not run `manage.py runserver`
as a foreground command. Use the guarded helper instead:

```powershell
.\scripts\django_server.ps1 -Action restart -Port 8001 -ReadyTimeoutSeconds 10
.\scripts\django_server.ps1 -Action status -Port 8001
.\scripts\django_server.ps1 -Action stop -Port 8001
```

The helper starts Django in the background with `--noreload`, waits briefly for
the listening port, and stops both the starter PID and the listener PID. On
Windows, `.venv\Scripts\python.exe` may spawn a base Python child process, so
the `Start-Process` PID is not always the PID that owns the port.

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
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset --seed-value 42
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset --fast
```

The seed command deletes and rebuilds demo enterprise data, so the
`--confirm-reset` flag is required. Demo users intentionally share password
`1234` for dissertation/testing convenience.

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

`apps.leave` service logic is split under `apps/leave/services/`; use
module-specific imports for new internal code and keep `apps.leave.services`
as a compatibility facade.

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

- `static/css/base/`: foundation styles, tokens, fonts, document defaults
- `static/css/layout/`: app shell, sidebar, compact/mobile responsive layout
- `static/css/components/`: shared buttons, modals, panels, cards, controls
- `static/css/pages/`: page styles for profile, employees, applications,
  analytics, vacation detail, and calendar submodules
- `static/css/login_style.css`: standalone login page styling
- `static/js/base.js`: sidebar, app-container replacement, modals
- `static/js/calendar.js` and `static/js/calendar/`: calendar entrypoint,
  state, controls, board, drawer, and forms
- `static/js/employees-page.js`: employee filters and card rendering
- `static/js/applications-page.js`: approval filters and AJAX updates
- `static/js/profile-sections.js`: section navigation and scroll memory

When editing frontend behavior:

- preserve backend-provided URLs such as `profile_url`; avoid hardcoded URL
  concatenation in JS
- authenticated in-app links that move between pages, cards, drawers, modals, or
  newly added feature pages should use the existing PJAX-style navigation:
  render links with `data-app-link` when possible, or call
  `window.KabinetNavigation.navigate(...)` from JS. Do not introduce ordinary
  full-page reloads for internal same-origin navigation unless the flow truly
  requires it, such as logout or a destructive POST redirect.
- preserve custom selects and segmented controls used by calendar, employees,
  and applications pages
- new authenticated pages should use the shared `includes/page_header.html`
  / `.page-hero` header pattern; do not add page-specific desktop/tablet header
  heights. Desktop and tablet headers should stay visually identical across
  pages, with long subtitles truncated instead of increasing the header height.
- on mobile, page headers may grow naturally and clamp/wrap descriptive text,
  but the sidebar/navigation must remain visible and usable.
- large page panels should reuse the common panel visual system: the same dark
  gradient, decorative overlay, border, shadow, radius, spacing, and hover
  behavior as the `Личные данные` profile panel.
- inner content inside large panels should align to the same left/right visual
  edge as existing department, employee, application, and notification cards;
  if a panel has no scroll gutter, use the shared content edge spacing token
  rather than adding a page-specific padding value.
- section headings inside large panels, such as employee lists, departments,
  applications, profile blocks, and calendar boards, should start on the same
  visual x/y rhythm as existing `Личные данные` / department panel headings;
  on desktop, the heading top gap should match the left visual content edge.
- scrollable lists and calendar boards inside large panels should extend to the
  panel boundary so clipping happens at the big panel edge, with any extra
  breathing room handled inside the scroll content rather than outside it.
- keep page-specific classes such as calendar body/html state from leaking across
  PJAX-like navigation in `base.js`
- verify significant UI changes and page screenshots with Playwright MCP.

## Current Worktree State

Check `git status` at the start of a task. The worktree may be dirty between
chats; do not revert or overwrite user changes unless explicitly asked.

Test packages currently live under `apps/leave/tests/`,
`apps/employees/tests/`, and `apps/core/tests/`. `apps/accounts/tests.py`
remains a compact single-file login flow test module.

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

## Documentation Hygiene

Keep `AGENTS.md` concise and high-signal. Add only stable rules that help future
agents avoid mistakes across chats. Do not record every fix, investigation, or
temporary plan here; put detailed notes in `WORK_SUMMARY.md`, `ARCHITECTURE.md`,
or feature-specific docs instead.
