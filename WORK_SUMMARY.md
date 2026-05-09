# Work Summary For Continuing Kabinet.pro

Updated: 2026-05-09

## How To Continue In A New Chat

Start by reading:

1. `AGENTS.md`
2. this file
3. `git status --short`

Workspace:

`D:\Fedya\Инст\МАГИСТЕРСКАЯ\Kabinet.pro`

Project name: `Kabinet.pro`.

The UI/domain copy is Russian. On Windows, terminal output can show valid UTF-8
Russian text as mojibake, so verify files with UTF-8 readers or in the browser
before editing Russian copy.

Do not revert unrelated dirty files. The project often has active UI/domain work
in progress between chats.

## Current Product Direction

Kabinet.pro is a manager cabinet for workforce and vacation planning:

- staffing rules and department workload;
- employee preference collection for the next schedule year;
- deterministic draft vacation schedule generation;
- risk/conflict explanation;
- manager/HR review and manual corrections;
- later AI/ML decision support after the deterministic flow is trustworthy.

Do not add a neural module yet. The current priority is making draft schedule
creation comfortable, explainable, and correct enough before sending it to
department heads.

## Current Important State

The latest active year in the demo flow is `2027`.

The main planning pages are:

- `/calendar/planning/2027/`
- `/calendar/planning/2027/?stage=collection`
- `/calendar/planning/2027/?stage=draft`
- `/preferences/2027/`
- `/preferences/2027/readiness/`
- `/calendar/drafts/2027/`

The user explicitly said we are **not ready to move to sending the draft to the
department head yet**. Before implementing the send-to-review stage, audit and
polish the draft creation experience again.

The demo checkbox/autofill behavior for dissertation showing should remain
enabled. The project is for demonstration and dissertation work, not production
deployment right now.

## Implemented Since The Previous Summary

### Schedule Transfers

Schedule transfer details now have their own page:

- `/applications/transfers/<id>/`

Transfer notifications should lead to the transfer detail page, not only to the
applications list.

The applications list transfer card was changed toward "open detail first,
decide inside detail".

### Navigation And Performance

Navigation and list performance were audited and optimized:

- calendar and staffing pages were reduced from repeated server calculations;
- profile/employees schedule status calculations were bulked up;
- heavy PJAX transitions were improved;
- section memory recognizes newly added planning/detail routes;
- first calendar paint was cleaned up so the page does not briefly render
  unstyled controls.

Do another browser smoke check after any big frontend edit because these pages
use internal scroll containers and PJAX-like replacement.

### Planning Hub

The schedule workflow was moved into a planning hub:

- `calendar/planning/<year>/`
- stages are represented as clickable cells/cards, not a separate sidebar item;
- the old extra slider was intentionally removed because the stage cells already
  work as navigation.

The planning hub should use the shared page header and large panel visual system
from the rest of the app. Avoid page-specific header heights.

### Preference Collection

The preference flow now supports:

- primary vacation period;
- backup vacation period;
- "no preferences";
- saved/filled state with "Изменить" before editing again;
- date fields that open the date picker by clicking the whole input-like block;
- wider period lengths, not only up to 28 days;
- preference readiness page for HR.

Important interpretation:

- primary and backup are alternatives, not two separate vacations;
- if the employee wants a long vacation, they should be able to enter a long
  primary period;
- if the employee gives no preferences, the system may only place safe periods.

### Remainder Policy In Preferences

`VacationPreference.remainder_policy` was added.

The choices are:

- `auto`: "Можно распределить автоматически";
- `approval`: "Сначала согласовать со мной";
- `defer`: "Не планировать сверх указанного периода".

This is used to decide what to do with days beyond the employee's selected
period. This was added because the generator must not blindly consume every
available day if the employee only asked for a smaller period.

Current intended behavior:

- `auto`: the generator can place annual-plan days beyond the chosen period if
  staffing risk allows it;
- `approval`: the chosen period can be placed, but the extra annual-plan part is
  left as "needs separate agreement";
- `defer`: the chosen period can be placed, and the extra part is intentionally
  not planned in this draft.

### Draft Schedule Generator

The draft schedule generator now exists under `apps/leave/services/schedule_drafts.py`.

It creates/updates a draft `VacationSchedule` for the planning year using:

- employee preferences;
- available paid leave balance;
- mandatory/urgent leftover days;
- annual-plan target days;
- staffing and substitution risk logic;
- existing vacation requests and schedule items;
- remainder policy.

Important rule added recently:

- automatic generation should not create standalone paid vacation parts shorter
  than 14 calendar days, except for urgent previous-year closure cases where a
  short remainder may be legally unavoidable.

This is based on the rule that one split part of annual paid leave must be at
least 14 calendar days. The system may still show fewer chargeable days if public
holidays fall inside the calendar period.

### Draft UI

The draft page exists:

- `/calendar/drafts/<year>/`

It shows:

- items already placed by the system;
- manual placement rows;
- urgent/blocking leftovers;
- role-colored employee avatars and management badges;
- clean modals for manual placement and urgent closure;
- scroll memory around forms/modals.

The draft cards intentionally separate:

- employee role color;
- risk/conflict/status colors;
- urgent/blocking labels.

Do not recolor employee avatars based on risk.

### Manual Placement

Manual placement uses a modal instead of overcrowding each card.

The modal previews:

- selected dates;
- chargeable/calendar days;
- risk level;
- conflicts;
- server-side validation messages before final POST.

The row/card should stay compact; detailed checks belong inside the modal.

### Urgent Previous-Year Closure

`VacationUrgentClosureRequest` was added for cases where days must be closed
before a deadline outside the target schedule year.

Routes include:

- `/calendar/drafts/<year>/urgent-closures/<employee_id>/preview/`
- `/calendar/drafts/<year>/urgent-closures/<employee_id>/create/`
- `/applications/urgent-closures/<id>/`
- manager approve;
- employee accept/propose;
- HR finalize/reject.

Intended flow:

1. HR sees a blocking urgent leftover in the draft.
2. HR clicks "Закрыть остаток".
3. The system suggests safe periods before the deadline.
4. HR can pick a suggested period or manually enter another period.
5. The request goes to the department head and then to the employee.
6. HR finalizes it into the actual schedule for the previous/current year.
7. The planning draft recalculates blockers.

This is intentionally separate from the 2027 draft because, for example, a
leftover that must be used before `03.01.2027` cannot realistically be solved by
a 2027 annual schedule item. It must be handled in 2026 before the deadline.

## Current Demo Snapshot Before This Handoff

The local demo database was migrated through:

- `apps/core/migrations/0003_alter_notification_event_type.py`
- `apps/leave/migrations/0013_vacationurgentclosurerequest_and_more.py`
- `apps/leave/migrations/0014_vacationpreference_remainder_policy.py`

The existing 2027 draft was rebuilt from the current collection after the latest
generator changes.

Observed browser state after rebuild:

- `/preferences/2027/`: saved answer shows remainder policy; no console errors.
- `/preferences/2027/readiness/`: about 80% readiness; 86/108 answered; no console errors.
- `/calendar/drafts/2027/`: placed 145, manual 51, blocking 45, remaining annual-plan days 1387, blocking days 136; no console errors.
- `/calendar/planning/2027/?stage=draft`: metrics match the draft; no console errors.

There were no generated draft items shorter than 14 calendar days after rebuild.

The database is demo-only. If moving to another computer, it is fine to recreate
it with migrations and the seed command.

## Tests Last Run

Last successful checks after the latest planning/preference changes:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test apps.leave.tests.test_preferences apps.leave.tests.test_urgent_closures --keepdb
node --check static/js/vacation-preferences.js
node --check static/js/schedule-draft.js
```

Browser smoke checks were done on:

- `/preferences/2027/`
- `/preferences/2027/readiness/`
- `/calendar/drafts/2027/`
- `/calendar/planning/2027/?stage=draft`

## What The Next Agent Should Do Next

Before implementing "send draft to department head", do a focused UX/domain audit
of draft creation:

1. Reopen `/calendar/planning/2027/?stage=draft` and `/calendar/drafts/2027/`.
2. Check whether HR can understand why each employee is in manual placement.
3. Check whether the "remaining annual plan" number is useful or too noisy.
4. Check whether urgent closures are clearly separated from normal 2027 planning.
5. Check whether employees with `approval` or `defer` remainder policy are
   represented clearly.
6. Check whether the manual placement modal has enough context and direct links
   to the employee/profile/calendar.
7. Check whether HR needs bulk actions/filters before the draft can be reviewed.
8. Re-check large-screen behavior and internal scroll containers.

Likely next implementation slice:

- add filters/grouping to the draft page for:
  - urgent blockers;
  - employees without preferences;
  - employees needing separate remainder approval;
  - staffing/risk issues;
  - departments/groups;
- improve explanations for "why manual";
- add quick navigation from draft rows to employee profile/calendar context;
- only after that, implement the review stage and notifications to department
  heads.

Do not send the draft to department heads until this usability layer is checked.

## Important Commands

Run local server:

```powershell
.\scripts\django_server.ps1 -Action restart -Port 8001 -ReadyTimeoutSeconds 10
.\scripts\django_server.ps1 -Action status -Port 8001
.\scripts\django_server.ps1 -Action stop -Port 8001
```

Run checks:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test apps.accounts apps.employees apps.leave apps.core --keepdb
```

Recreate demo data:

```powershell
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset --fast
```

Demo users intentionally share password `1234`.
