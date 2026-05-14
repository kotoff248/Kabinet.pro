# Work Summary For Continuing Kabinet.pro

Updated: 2026-05-14

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

## Fresh Handoff: 2026-05-14

This is the most important current context for continuing on another computer.

The project is now focused on the **2027 vacation schedule demo with a real
candidate-based neural module**. The user wants the dissertation demo to look
convincing: HR collects preferences, creates a draft, uses the neural module to
place/inspect vacations, then later sends the schedule through approval.

Current active wording for the auto-fill action is:

**`Добрать незакрытые дни`**

Use this wording consistently in UI, modal titles, progress messages and user
explanations. Avoid mixing it with `Автоматически распределить`,
`нераспределенные дни`, or `оставшиеся дни` in user-facing action names.

### What Was Implemented Recently

1. Historical ML traces in seed:
   - `seed_vacation_requests --confirm-reset` now creates historical
     generation runs, candidates, candidate packages, selected/rejected/blocked
     examples and feedback for archived/approved historical schedules.
   - Historical schedule items are connected to selected candidates and AI
     fields.
   - Staffing/risk features are stored so the neural module can learn from
     coverage, reserves, department load and conflicts.

2. v2 neural model training:
   - PyTorch was added only for training.
   - Runtime scoring stays pure Python through exported JSON.
   - Training command:

   ```powershell
   .\.venv\Scripts\python.exe manage.py train_vacation_candidate_model
   ```

   - Model files:
     - `apps/leave/ml_models/vacation_candidate_mlp_v2.json`
     - `apps/leave/ml_models/vacation_candidate_mlp_v2_metrics.json`
   - Active model is selected through `.env`:

   ```env
   VACATION_CANDIDATE_SCORER_VERSION=vacation-candidate-mlp-v2
   ```

   - If v2 JSON is missing or invalid, scorer falls back safely to v1/baseline.

3. v2 score calibration:
   - Removed the artificial runtime floor that made many values look like the
     same `68%`.
   - Targets now depend on candidate quality: preference match, period length,
     risk, department load, staffing reserve, urgent balance and partial-period
     context.
   - Expected normal range:
     - good primary preference: about `78-90%`;
     - normal partial primary: about `68-82%`;
     - good backup: about `62-78%`;
     - acceptable but risky: about `45-65%`;
     - blocked: `0%`.

4. Quick demo reset:
   - Full reseed is still available, but it can be slow.
   - A fast button `Сбросить до начальных настроек` was added near full demo
     reset.
   - It uses `DemoBaselineSnapshot` and returns the demo to the point before
     2027 preference collection/draft work.
   - It does not delete historical schedules, historical ML traces, users,
     JSON models or metrics.

5. Full demo reset with progress:
   - Full `Пересоздать демо-данные` no longer freezes the page.
   - It starts a background Python process and stores progress in
     `DemoDataResetJob`.
   - The modal shows progress, current stage, success/error and login link.

6. Smart package recommendations:
   - Manual placement suggestions now rank package options, not just one period.
   - Suggestions can compare 1/2/3-period packages and mark the best option.
   - The list is scrollable inside the modal.

7. Background `Добрать незакрытые дни`:
   - Full auto-fill for all remaining employees now starts as a background job,
     so the browser does not hang.
   - Model: `VacationScheduleAutoPlaceJob`.
   - Command:

   ```powershell
   .\.venv\Scripts\python.exe manage.py run_schedule_draft_auto_place --job-id <id> --year 2027 --actor-id <hr_id>
   ```

   - The job still uses smart package selection:
     `auto_place_remaining_schedule_draft(..., use_package_selection=True)`.
   - UI shows progress and reloads the draft after success.
   - Repeat clicks are guarded: if a job is already queued/running for the same
     schedule, the view returns that existing job instead of starting another
     background process.

8. Repeat-click guards for demo reset actions:
   - Full `Пересоздать демо-данные` reuses an active `DemoDataResetJob` instead
     of launching a second seed process.
   - `Сбросить до начальных настроек` uses a PostgreSQL advisory lock and is
     blocked while full demo reset is queued/running.
   - The quick-restore form disables its buttons immediately after submit.

### Current Important Files

- `apps/core/services/demo_locks.py`
- `apps/core/management/commands/seed_vacation_requests.py`
- `apps/core/management/commands/train_vacation_candidate_model.py`
- `apps/core/services/demo_baseline.py`
- `apps/core/services/demo_reset_jobs.py`
- `apps/leave/services/historical_ml_traces.py`
- `apps/leave/services/candidate_training.py`
- `apps/leave/services/candidate_neural.py`
- `apps/leave/services/candidate_scoring.py`
- `apps/leave/services/schedule_drafts.py`
- `apps/leave/services/schedule_auto_place_jobs.py`
- `apps/leave/management/commands/run_schedule_draft_auto_place.py`
- `templates/vacation_schedule_draft.html`
- `templates/vacation_schedule_planning.html`
- `static/js/schedule-draft.js`
- `static/css/pages/schedule-draft.css`

### Migrations Added Recently

- `apps/core/migrations/0004_demobaselinesnapshot.py`
- `apps/core/migrations/0005_demodataresetjob.py`
- `apps/leave/migrations/0021_vacationscheduleautoplacejob.py`

Run migrations on the other computer:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
```

### Transfer / New Computer Checklist

1. Create/update `.env` from `.env.example`.
2. Install requirements. PyTorch can take a long time; if pip hangs, install
   PyTorch manually first, then continue with requirements.
3. Run migrations:

   ```powershell
   .\.venv\Scripts\python.exe manage.py migrate
   ```

4. If the copied database is not available, recreate demo data:

   ```powershell
   .\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset --seed-value 42
   ```

   Ask the user before reseeding if an existing demo DB is already present.

5. Train or reuse v2:
   - If `vacation_candidate_mlp_v2.json` and metrics are copied, no retraining
     is needed.
   - If not copied, run:

   ```powershell
   .\.venv\Scripts\python.exe manage.py train_vacation_candidate_model
   ```

6. For the dissertation demo, set:

   ```env
   VACATION_CANDIDATE_SCORER_VERSION=vacation-candidate-mlp-v2
   ```

7. Start the dev server with the helper:

   ```powershell
   .\scripts\django_server.ps1 -Action restart -Port 8001 -ReadyTimeoutSeconds 10
   ```

8. Browser smoke check:
   - `/calendar/planning/2027/`
   - `/calendar/planning/2027/?stage=draft`
   - `/calendar/drafts/2027/`
   - `/staffing/`

### What To Do Next

Recommended next development direction:

1. Verify the transferred system on the new computer:
   - migrations apply;
   - server starts;
   - v2 scorer loads;
   - pages above open without console errors.

2. Check the `Добрать незакрытые дни` flow visually:
   - button text is consistent on planning and draft pages;
   - modal opens;
   - preview loads;
   - background progress appears after confirmation;
   - page reloads after success.

3. If the user asks to continue product development, the next big feature is
   still the formal schedule approval workflow:
   - HR sends draft to department heads;
   - department heads approve/return;
   - enterprise head approves/returns;
   - authorized person finalizes if required;
   - draft becomes approved schedule.

4. If performance is still a problem, optimize carefully:
   - do not weaken hard rules;
   - do not replace smart package selection with a dumb first-match approach;
   - keep expensive package selection in background jobs;
   - avoid rebuilding full page context when only counters are needed.

### Working Rules For The Next Chat

- The worktree is dirty and contains intentional changes from several stages.
  Do not revert broad files just because they differ from `main`.
- Do not run full reseed automatically. Ask the user first if demo data should
  be recreated.
- Do not run long `pip install` loops silently. If PyTorch/dependencies are
  missing or installation hangs, tell the user exactly what to install.
- Keep Russian UI text UTF-8 and verify it in the browser if terminal output
  looks corrupted.
- Use `Добрать незакрытые дни` as the action name.

## Current Product Direction

Kabinet.pro is a manager cabinet for workforce and vacation planning:

- staffing rules and department workload;
- employee preference collection for the next schedule year;
- deterministic draft vacation schedule generation;
- risk/conflict explanation;
- manager/HR review and manual corrections;
- candidate-based AI/ML decision support for schedule draft generation.

The neural module should be developed through the schedule draft generator, not
as a separate decorative recommendation page. Current direction is documented in
`NEURAL_MODULE_PLAN.md`: generate candidates, apply deterministic hard rules,
store candidate features, then add neural scoring/hybrid selection on top.

The candidate/scoring flow is now implemented enough for the dissertation demo.
The next product direction is the formal approval workflow after HR finishes the
draft: HR sends the draft to department heads, department heads approve or return
their department, then the enterprise head and authorized person complete final
approval.

## Current Important State

The latest active year in the demo flow is `2027`.

The main planning pages are:

- `/calendar/planning/2027/`
- `/calendar/planning/2027/?stage=collection`
- `/calendar/planning/2027/?stage=draft`
- `/preferences/2027/`
- `/preferences/2027/readiness/`
- `/calendar/drafts/2027/`

The user now wants the system to move beyond the HR draft screen. The biggest
missing workflow is **formal schedule approval**:

1. HR completes the draft and sends it to department review.
2. Department heads review only their departments.
3. HR fixes returned departments and resends them.
4. When all departments approve, the enterprise head reviews the whole schedule.
5. The authorized person completes the last approval step if the enterprise head
   is part of the approval chain.
6. The schedule becomes approved, and draft schedule items become active planned
   or approved schedule items.

The demo checkbox/autofill behavior for dissertation showing should remain
enabled. The project is for demonstration and dissertation work, not production
deployment right now.

## Current Demo Data Plan

Important user clarification from 2026-05-13:

- The seed command must **not** automatically run the whole future planning
  scenario. It should create a historically plausible demo database: enterprise
  structure, employees, departments, staffing rules, workload, vacation history,
  transfers, balances and realistic edge cases.
- The dissertation demo should remain interactive. The user wants to personally
  show HR starting the preference collection, using the existing demo autofill
  checkbox, finishing collection, creating the draft and reviewing the result.
- The existing preference-collection demo checkbox already lets employees
  answer automatically. Do not duplicate that checkbox. Improve the quality and
  clarity of its generated answers when needed.
- Keep the preference answers organic: some employees answer, some skip
  preferences, some remain pending; responses should vary by season, department
  workload, comments and remainder policy (`auto`, `approval`, `defer`).
- Improve seed data organicity rather than over-automating the demo:
  fewer visually odd short paid-vacation fragments, calmer leave-balance
  extremes, several clear showcase debt/risk cases, and staffing risks that are
  meaningful but not noisy everywhere.
- Potential future demo accelerator to analyze before implementing: flows that
  require extra role switching after HR acts, especially urgent previous-year
  closure from the draft. It may be useful to add UI checkboxes such as
  "demo: manager answers immediately" and "demo: employee answers immediately"
  around those flows, so the user can show the process without logging into
  every role. These should be UI/demo helpers, not seed-command automation.

## Implemented Since The Previous Summary

### Current Draft/Neural State As Of 2026-05-13

The HR draft screen is now the strongest part of the product. It supports:

- compact draft cards with assigned period, plan status, risk and module score;
- `Проверка модуля` modal with selected candidate, alternatives, hard-rule
  status, score, confidence and feedback buttons;
- AJAX feedback from HR, department heads and enterprise heads;
- `Расчёт` modal explaining why the system thinks a specific number of vacation
  days must be placed;
- manual `Распределить` modal with module suggestions and up to 3 periods;
- package preview endpoint that checks multiple manual periods without saving;
- manual suggestions that include backup preferences when safe;
- `Добрать незакрытые дни` preview and confirm flow;
- automatic top-up that uses the same candidate/scoring architecture;
- lazy manual suggestion cache that is recalculated after draft changes;
- backup preference consideration in both manual placement and auto top-up;
- real neural scoring via `vacation-candidate-mlp-v1` with baseline fallback.

Important current business logic:

- Primary and backup preferences are alternatives, not two separate vacations.
- If the primary preference is safe and close in score to the backup preference,
  the primary preference wins because it is the employee's preferred option.
- The backup preference can still win if the primary period is blocked, high
  risk, marked `avoid`, or meaningfully worse by score.
- The plan target for a filled preference now follows the actually selected
  preference period. Example: if primary is 70 days, backup is 64 days, backup
  is selected, and the remainder policy is `approval`, then the draft target is
  64 days and the extra 6 days are shown as needing separate agreement.
- One part of annual paid leave should be at least 14 calendar days, except
  unavoidable urgent previous-year closure cases.

Recent focused checks that passed:

- `manage.py check`
- `manage.py makemigrations --check --dry-run`
- targeted schedule draft tests for creation, manual placement, auto placement
  and feedback.

### Recommended Next Implementation: Schedule Approval Workflow

Implement the next stage as a centralized enterprise workflow:

1. Add a service module, for example `apps.leave.services.schedule_approvals`,
   rather than putting approval logic directly into views.
2. Add an HR action `Отправить на проверку отделам`.
3. Before sending, block the action if:
   - there is no draft;
   - manual tasks remain;
   - unresolved conflicts remain;
   - urgent blocking leftovers remain;
   - there are no schedule items.
4. On send:
   - create or reset `VacationScheduleDepartmentApproval` rows for departments
     that have draft items;
   - assign each department head;
   - set `VacationSchedule.status = department_review`;
   - keep candidate/audit data intact.
5. Add a department-head review page or stage inside the existing planning hub:
   - department head sees only their department;
   - rows show employee, assigned periods, plan calculation, risk and module
     explanation;
   - actions: `Согласовать отдел` and `Вернуть на доработку`;
   - return must require a comment.
6. If a department is returned:
   - HR can edit affected draft items;
   - schedule remains in review/returned state;
   - the department approval row returns to pending when HR resends.
7. When all departments approve:
   - create `VacationScheduleEnterpriseApproval`;
   - enterprise head reviews the whole schedule and approves or returns.
8. Final approval:
   - create/use `VacationScheduleAuthorizedApproval` if required by the existing
     role chain;
   - when final approval succeeds, set `VacationSchedule.status = approved`;
   - move draft items to active schedule status (`planned` or `approved`, choose
     one consistently with the existing calendar/ledger logic);
   - set `approved_by` and `approved_at`.

Keep the implementation conservative: no neural-module changes are required for
this next step. The approval workflow should consume the draft and its stored
candidate explanations.

### Known Follow-Up: Draft Creation Speed

The user noticed that creating a draft can feel slow on the full demo dataset.
Safe optimization direction:

- do not change the selected vacation periods;
- remove full `build_schedule_draft_page_context(year)` calls from creation
  paths when only counts are needed;
- keep manual suggestions lazy and cache-backed;
- do not prebuild suggestions for every manual task during draft creation;
- reuse candidate scoring results where they were already calculated.

This is a performance task, not a business-logic change.

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

### Neural Module Foundation

The neural module foundation is now part of the draft generator architecture.

Current completed stages:

- candidates are represented explicitly as `DraftGenerationCandidate`;
- preference and auto placement prepare multiple candidate periods;
- hard rules mark candidates as passed/blocked and store block reasons;
- generation runs are saved in `VacationScheduleGenerationRun`;
- considered periods are saved in `VacationScheduleCandidate`;
- selected candidates are linked to created `VacationScheduleItem` records;
- candidate `features` use schema version `1` with employee, period, planning,
  preference and risk fields.
- saved candidates are scored by the active neural model `vacation-candidate-mlp-v1`, which writes
  `score`, `confidence`, `model_version`, `explanation` and a
  `scoring_recommendation` feature.
- schedule draft generation now runs in `hybrid` mode and ranks candidates by
  hard-rule status, score, confidence, risk, coverage and preference context.

Selected schedule items are linked to their chosen candidate and store
`ai_score`, `ai_confidence`, `ai_model_version` and `ai_explanation`. The draft
UI now shows the module score, confidence, recommendation label and explanation
inside placed vacation cards.

Stage 8 is implemented: `VacationScheduleCandidateFeedback` stores HR/manager
feedback for the selected candidate. A feedback entry is linked to the draft
item, selected candidate, generation run and reviewer, and snapshots the score,
confidence, model version and explanation that were visible when the human
decision was made. The draft UI shows feedback counts and lets HR, enterprise
heads and department heads for their own department mark a candidate as agreed,
needing correction, or rejected.

Stage 9 is implemented: `apps.leave.services.candidate_neural` loads the
`apps/leave/ml_models/vacation_candidate_mlp_v1.json` MLP artifact and performs
pure Python feed-forward scoring for valid candidates. The baseline scorer stays
as a safe fallback through `apps.leave.services.candidate_scoring`; fallback
results are marked in `model_version`.

Stage 10 is implemented: the full demo scenario was verified and documented in
`DEMO_NEURAL_MODULE_RESULT.md`. The 2027 demo draft currently has 182 draft
items, 182 selected candidates, 182 neural scores, candidate model version
`vacation-candidate-mlp-v1`, and four demo HR/manager feedback entries.

Use the official user-facing roadmap from `NEURAL_MODULE_PLAN.md` when naming
stages. By that roadmap, stages 1-10 are complete. Remaining:

- No remaining official stages for the neural-module roadmap.

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
