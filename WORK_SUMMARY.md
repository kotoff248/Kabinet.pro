# Kabinet.pro - Work Summary

Updated: 2026-05-18

Этот файл нужен для продолжения разработки или переноса проекта на другой
компьютер. Сначала читать:

1. `WORK_SUMMARY.md`
2. `NEURAL_MODULE_PLAN.md`
3. `ARCHITECTURE.md`
4. `git status --short`

Рабочая папка на текущем компьютере:

`D:\Fedya\Инст\МАГИСТЕРСКАЯ\Kabinet.pro`

Проект называется `Kabinet.pro`. Не использовать прежнее название проекта в
новых текстах, UI и описаниях.

## Главное Сейчас

Система готовится к демонстрации полного создания и утверждения графика
отпусков на 2027 год. Основной сценарий уже реализован end-to-end:

1. HR запускает сбор пожеланий.
2. Демо-сотрудники заполняют пожелания.
3. HR создает черновик графика.
4. HR нажимает `Добрать незакрытые дни`.
5. Нейромодуль выбирает лучшие допустимые периоды через hard rules + v2-score.
6. HR вручную разбирает срочные остатки через `Закрыть в 2026`.
7. HR отправляет график руководителям отделов.
8. Руководители отделов согласуют или возвращают свой отдел.
9. HR дорабатывает возвращенный отдел и повторно отправляет его.
10. После согласования всех отделов HR отправляет график руководителю
    предприятия.
11. Руководитель предприятия утверждает график или возвращает HR с
    комментарием.
12. После утверждения график становится активным, пункты становятся
    `approved`, а сотрудники получают информационные уведомления.

Единое название действия в UI:

`Добрать незакрытые дни`

Не возвращать в интерфейс варианты `Автоматически распределить`,
`нераспределенные дни`, `оставшиеся дни` как название кнопки.

## Свежий Статус

Нейромодуль, seed-история, быстрый сброс, фоновый полный reset, автодобор,
срочные остатки, проверка отделов, доработка отдела, финальное утверждение и
уведомления после утверждения уже собраны в один рабочий сценарий.

Последние важные изменения:

- добавлен `apps/core/services/demo_urgent_closure_cases.py`;
- seed не создает активные urgent-closure approvals, но готовит два
  контролируемых demo cases с дедлайнами `03.01.2027` и `04.01.2027`;
- quick restore снова подготавливает эти urgent-closure cases после очистки
  работы по 2027;
- автодобор работает в фоне, делает до 4 проходов и использует пакетный подбор
  вариантов;
- черновик можно открыть во время фонового добора, прогресс виден на planning и
  draft;
- HR и директор предприятия видят demo reset/restore в `DEBUG`;
- руководители отделов согласуют или возвращают только свой отдел;
- HR дорабатывает возвращенный отдел через замену годового пакета сотрудника;
- финальный график утверждает руководитель предприятия, без обязательного
  уполномоченного лица;
- уполномоченное лицо остается только для отдельных заявок/переносов самого
  руководителя предприятия;
- после утверждения графика сотрудники с утвержденными пунктами получают
  уведомление `schedule_approved`;
- добавлено много-летнее планирование: после утверждения активного года HR или
  директор может открыть следующий плановый год, например 2028 после 2027.

## Что Уже Сделано

### Архитектурная Раскладка После Этапов 1-6

Завершена структурная уборка без изменения пользовательской логики:

- `apps.leave.views` разложен на пакет view-модулей по сценариям;
- `apps.leave.urls` разложен на пакет URL-модулей, `include("apps.leave.urls")`
  продолжает работать;
- нейромодуль находится в `apps/leave/ml/`, JSON-модели лежат в
  `apps/leave/ml/artifacts/`;
- include-шаблоны разложены по подпапкам `shared`, `employees`,
  `leave_detail`, `schedule_draft`, `staffing` и уже существующим зонам;
- CSS лежит в `static/css/base`, `auth`, `layout`, `components`, `pages`;
- JS лежит в `static/js/core`, `auth`, `components`, `pages`, `schedule`,
  `calendar`;
- большой demo seed разложен в `apps/core/services/demo_seed/`, а management
  command остался тонкой Django-точкой входа;
- большой сервис черновика графика разложен в
  `apps/leave/services/schedule_drafts/`;
- модели между Django app не переносились: текущие границы описаны в
  `ARCHITECTURE.md`.

### Исторические ML-следы в seed

`seed_vacation_requests --confirm-reset` создает исторические:

- `VacationScheduleGenerationRun`;
- `VacationScheduleCandidate`;
- `VacationScheduleCandidatePackage`;
- `VacationScheduleCandidatePackagePeriod`;
- `VacationScheduleCandidateFeedback`;
- выбранные, отклоненные и заблокированные candidates;
- связи schedule items с selected candidate и AI-полями.

История используется как база для обучения, но будущий draft 2027 не считается
историей.

### v2-нейромодуль

Обучение делается через PyTorch, но обычный сайт PyTorch не импортирует.
Runtime-скоринг работает через JSON-веса и pure Python inference.

Команда обучения:

```powershell
.\.venv\Scripts\python.exe manage.py train_vacation_candidate_model
```

Файлы модели:

- `apps/leave/ml/artifacts/vacation_candidate_mlp_v2.json`
- `apps/leave/ml/artifacts/vacation_candidate_mlp_v2_metrics.json`

Активная версия задается в `.env`:

```env
VACATION_CANDIDATE_SCORER_VERSION=vacation-candidate-mlp-v2
```

Если v2 не найдена или JSON сломан, система безопасно падает на v1/baseline.

### Калибровка оценок

Убрана искусственная подпорка, из-за которой почти все варианты получали около
`68%`. Теперь оценка зависит от:

- совпадения с основным/запасным пожеланием;
- длины периода;
- риска;
- нагрузки отдела;
- запаса состава;
- закрытия срочного остатка;
- того, что часть отпуска может добраться позже.

Ожидаемая шкала:

- хороший primary: примерно `78-90%`;
- нормальный partial primary: примерно `68-82%`;
- хороший backup: примерно `62-78%`;
- допустимый, но рискованный период: примерно `45-65%`;
- blocked: `0%`.

### Оптимальный добор дней

`Добрать незакрытые дни` работает в фоне через `VacationScheduleAutoPlaceJob`.
Кнопка не должна зависать в браузере.

Фоновая команда:

```powershell
.\.venv\Scripts\python.exe manage.py run_schedule_draft_auto_place --job-id <id> --year 2027 --actor-id <hr_id>
```

Автодобор использует умный подбор пакетов:

`auto_place_remaining_schedule_draft(..., use_package_selection=True)`

Он сравнивает допустимые варианты и может выбирать 1/2/3 периода, а не просто
ставить всем одинаковую схему.

Свежая правка: автодобор может делать до 4 проходов. Повторный проход запускается
не только когда были добавлены новые пункты, но и когда система убрала
конфликтующие сгенерированные пункты и после этого остались ручные задачи.

Предпросмотр `Добрать незакрытые дни` и предложения модуля кэшируются в
`static/js/schedule/schedule-draft.js`, но не считаются автоматически при входе на
страницу. Подгрузка стартует только при наведении/фокусе на соответствующую
кнопку, чтобы не тормозить открытие черновика. Это только UX-кэш; серверная
проверка при подтверждении остается обязательной.

### Прогресс фоновых задач

Сделано:

- полный reset демо-БД идет в фоне через `DemoDataResetJob`;
- модалка reset показывает процент и текущий этап;
- повторный клик не запускает второй seed, а возвращает активную задачу;
- `Добрать незакрытые дни` показывает прогресс в модалке;
- прогресс добора дублируется на странице планирования;
- черновик можно открыть во время фонового добора;
- на странице черновика тоже есть плашка прогресса.

### Быстрый сброс

Есть кнопка `Сбросить до начальных настроек`.

Она:

- не запускает полный seed;
- не разлогинивает пользователя;
- быстро очищает работу по 2027;
- откатывает правила состава/нагрузку к snapshot;
- не трогает исторические графики, исторические ML-следы и JSON-модели.

Доступ в `DEBUG`: директор предприятия и HR.

### Срочное закрытие остатков

Текущая логика:

- seed больше не создает заранее активные срочные закрытия и не отправляет их
  руководителю;
- seed подготавливает demo entitlement cases через
  `ensure_demo_urgent_closure_cases`, чтобы минимум две ручные кнопки реально
  появлялись в черновике;
- эти cases строятся через настоящие исторические schedule items: helper
  сокращает исторически использованный объем до 49 дней из 52, а не меняет норму
  отпуска;
- demo cases фиксируются на разных ранних дедлайнах `03.01` и `04.01`, где в
  planning year нет списываемых отпускных дней; после `Добрать незакрытые дни`
  они должны оставаться ручной задачей `Закрыть в 2026`;
- ledger учитывает `must_use_by`: отпуск после дедлайна не закрывает старый
  рабочий год, даже если баланс пересчитывается после автодобора;
- quick restore вызывает ту же подготовку demo urgent cases после очистки
  работы по 2027;
- baseline snapshot не восстанавливает активные urgent-closure approvals;
- в черновике должна появляться ручная кнопка `Закрыть в 2026`;
- даты для срочного закрытия теперь тоже ранжируются нейромодулем;
- в модалке срочного закрытия показывается `Оценка модуля`;
- варианты срочного закрытия грузятся лениво через endpoint options, чтобы
  открытие страницы черновика не считало risk/neural для этих вариантов заранее.

Важно: если в уже существующей локальной БД остались старые активные срочные
заявки, код сам их не удаляет. Чтобы увидеть ручную кнопку, нужно выполнить
`Сбросить до начальных настроек` или полный reseed. Перед полным reseed всегда
спросить пользователя.

### Согласование графика 2027

Сделано:

- HR отправляет готовый график на проверку руководителям отделов;
- руководитель отдела видит только свой отдел;
- руководитель отдела может согласовать отдел или вернуть его с комментарием;
- при возврате HR получает уведомление и открывает режим `Доработка отдела`;
- в доработке HR меняет весь годовой пакет сотрудника, а не отдельную строку;
- предложения в доработке строятся как замена текущего пакета, поэтому текущие
  дни временно исключаются из баланса/пересечений;
- после доработки HR повторно отправляет только этот отдел;
- после согласования всех отделов HR отправляет график руководителю
  предприятия;
- руководитель предприятия утверждает график или возвращает HR с комментарием;
- если финал возвращен, HR выбирает конкретный отдел для доработки через уже
  существующий режим;
- после финального утверждения `VacationSchedule.status = approved`, planned
  items становятся approved, а этап `Черновик` в planning отображается как
  завершенный, а не как пустой;
- `VacationScheduleAuthorizedApproval` в общем утверждении графика не
  используется.

### Уведомления после утверждения

После финального утверждения создаются информационные уведомления:

- тип `schedule_approved`;
- получают все активные сотрудники, у которых есть утвержденные пункты графика;
- уведомление не требует действия;
- ссылка ведет в годовой календарь и открывает карточку сотрудника с фокусом на
  первый отпуск;
- dedupe key не дает создавать дубли при повторном вызове сервиса.

### Много-летнее Планирование

Сделано:

- добавлена модель `VacationPlanningCycle`;
- система хранит один активный плановый год;
- если циклов еще нет, fallback остается `текущий год + 1`, поэтому демо 2027
  не ломается;
- `/calendar/planning/` и sidebar ведут на активный год;
- после утверждения графика активного года появляется кнопка
  `Начать планирование 2028`;
- старый год становится read-only, новый год открывается пустым;
- start/finish сбора, создание черновика, автодобор и отправка на согласование
  разрешены только для активного года;
- quick restore возвращает активный год к snapshot-году и удаляет future
  planning cycles, созданные в демо.

## Важные Файлы

- `apps/core/management/commands/seed_vacation_requests.py`
- `apps/core/management/commands/train_vacation_candidate_model.py`
- `apps/core/services/demo_seed/`
- `apps/core/services/demo_baseline.py`
- `apps/core/services/demo_urgent_closure_cases.py`
- `apps/core/services/demo_locks.py`
- `apps/core/services/demo_reset_jobs.py`
- `apps/leave/ml/traces.py`
- `apps/leave/services/planning_cycles.py`
- `apps/leave/ml/training.py`
- `apps/leave/ml/runtime.py`
- `apps/leave/ml/scoring.py`
- `apps/leave/services/schedule_drafts/`
- `apps/leave/services/schedule_approvals.py`
- `apps/leave/services/schedule_auto_place_jobs.py`
- `apps/leave/services/schedule_planning.py`
- `apps/leave/services/notifications.py`
- `apps/leave/services/urgent_closures.py`
- `apps/leave/management/commands/run_schedule_draft_auto_place.py`
- `templates/vacation_schedule_draft.html`
- `templates/vacation_schedule_planning.html`
- `templates/includes/page_headers/staffing_enterprise_deputy.html`
- `templates/includes/schedule_draft/schedule_draft_urgent_closure_modal_body.html`
- `static/js/schedule/schedule-draft.js`
- `static/js/schedule/schedule-planning.js`
- `static/css/pages/schedule-draft.css`
- `static/css/pages/schedule-planning.css`
- `static/css/pages/staffing.css`

## Миграции, Которые Нужны На Другом Компьютере

Просто выполнить:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
```

Недавние миграции:

- `apps/core/migrations/0004_demobaselinesnapshot.py`
- `apps/core/migrations/0005_demodataresetjob.py`
- `apps/core/migrations/0006_alter_notification_event_type.py`
- `apps/leave/migrations/0021_vacationscheduleautoplacejob.py`
- `apps/leave/migrations/0022_vacationrequest_ai_support.py`
- `apps/leave/migrations/0023_vacationrequest_decision_ai_support.py`
- `apps/leave/migrations/0024_backfill_vacationrequest_decision_ai.py`
- `apps/leave/migrations/0025_vacationplanningcycle.py`

## Перенос На Другой Компьютер

1. Скопировать проект.
2. Создать/проверить `.env` по `.env.example`.
3. Установить зависимости:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Если зависает PyTorch, не ждать молча: установить PyTorch вручную и продолжить.

4. Применить миграции:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
```

5. Проверить активную модель:

```env
VACATION_CANDIDATE_SCORER_VERSION=vacation-candidate-mlp-v2
```

6. Если БД не переносится, создать демо-данные:

```powershell
.\.venv\Scripts\python.exe manage.py seed_vacation_requests --confirm-reset --seed-value 42
```

Перед reseed существующей демо-БД спросить пользователя.

7. Если v2 JSON не скопирован, обучить:

```powershell
.\.venv\Scripts\python.exe manage.py train_vacation_candidate_model
```

8. Запустить сервер:

```powershell
.\scripts\django_server.ps1 -Action restart -Port 8001 -ReadyTimeoutSeconds 10
```

9. Открыть и проверить:

- `/staffing/`
- `/calendar/planning/2027/`
- `/calendar/planning/2027/?stage=draft`
- `/calendar/drafts/2027/`

После открытия проверить отдельно:

- HR/директор видят компактные кнопки `Пересоздать демо-данные` и
  `Сбросить до начальных настроек`;
- после seed или quick restore в черновике есть ручные срочные кейсы
  `Закрыть в 2026`, а не заранее отправленное согласование;
- `Добрать незакрытые дни` запускается в фоне и показывает прогресс;
- карточки черновика не ломаются на ширинах ноутбука.
- отделы можно отправить на проверку, вернуть и доработать;
- директор предприятия может утвердить финальный график;
- после утверждения сотрудники получают уведомления о графике.

## Последние Проверки

Актуальная проверка от 2026-05-17:

```powershell
.\.venv\Scripts\python.exe manage.py check
node --check static/js/schedule/schedule-draft.js
node --check static/js/schedule/schedule-planning.js
.\.venv\Scripts\python.exe manage.py test apps.core.tests.test_seed_vacation_data apps.leave.tests.test_schedule_draft_auto apps.leave.tests.test_urgent_closures apps.leave.tests.test_preferences apps.core.tests.test_notifications --keepdb
.\.venv\Scripts\python.exe manage.py test apps.leave.tests.test_preferences apps.leave.tests.test_schedule_draft_auto apps.core.tests.test_seed_vacation_data apps.employees.tests.test_staffing --keepdb
```

Результат: 122 теста прошли успешно.

При проверке seed fast-режима был закреплен важный обучающий след:
исторический пункт, который реально был перенесен руководителем, теперь дает
`reject` feedback для ML-обучения. Это сохраняет в seed не только `agree` и
`needs_change`, но и отрицательные человеческие решения.

Для полного smoke перед показом рекомендуется дополнительно пройти сценарий в
браузере:

```powershell
.\scripts\django_server.ps1 -Action restart -Port 8001 -ReadyTimeoutSeconds 10
```

Открыть `/staffing/`, `/calendar/planning/2027/`,
`/calendar/planning/2027/?stage=draft`, `/calendar/drafts/2027/`.

## Текущее Demo-Состояние

После quick restore от 2026-05-17 локальная БД находится в стартовой точке
показа:

- baseline snapshot `initial_demo_state` существует;
- активных фоновых задач full reset нет;
- активных фоновых задач `Добрать незакрытые дни` нет;
- графика 2027 еще нет;
- сбора пожеланий 2027 еще нет;
- department/enterprise approvals 2027 нет;
- active urgent-closure approvals 2027 нет;
- есть ровно два ручных urgent-closure demo candidates:
  `03.01.2027` и `04.01.2027`, по 3 дня;
- исторические ML-следы есть: generation runs, candidates, feedback.
- `.env` выбирает `vacation-candidate-mlp-v2`;
- JSON-модель v2 и metrics-файл присутствуют в `apps/leave/ml/artifacts/`.

Это чистая стартовая точка для демонстрации: можно запускать сбор пожеланий,
создавать черновик, добирать дни, затем проходить согласование отделов и финал.

## Текущий ML-Статус На 2026-05-18

Этап 1 улучшения нейромодуля выполнен: генерация вариантов отпусков стала шире
и качественнее, а ручной режим и автодобор получают больше допустимых пакетов
перед оценкой.

Этап 2 выполнен: добавлен `vacation-package-ranker-v3`, отдельный нейромодуль
для оценки годового пакета отпусков целиком. Он не заменяет v2-кандидатный
скорер, а работает поверх него:

- v2 оценивает каждый отдельный период;
- v3 оценивает весь пакет сотрудника: 1, 2, 3 или больше периодов в режиме
  доработки;
- hard rules остаются главнее обеих моделей;
- runtime не импортирует PyTorch, читает JSON-артефакт;
- PyTorch используется только командой обучения.

Добавлены файлы:

- `apps/leave/ml/package_runtime.py`
- `apps/leave/ml/package_scoring.py`
- `apps/leave/ml/package_training.py`
- `apps/core/management/commands/train_vacation_package_model.py`
- `apps/leave/ml/artifacts/vacation_package_ranker_v3.json`
- `apps/leave/ml/artifacts/vacation_package_ranker_v3_metrics.json`

Команда обучения:

```powershell
.\.venv\Scripts\python.exe manage.py train_vacation_package_model
```

Текущий артефакт v3 обучен на 1198 historical candidate packages за
2021-2026 годы.

Этап 3 выполнен: `Добрать незакрытые дни` в пакетном режиме использует
глобальный оптимизатор автодобора. Он больше не обязан жадно фиксировать первого
сотрудника и только потом считать следующего:

- сотрудники обрабатываются безопасными группами: сначала срочные дедлайны,
  затем отдел/производственная группа;
- внутри группы работает beam search с лимитами, чтобы держать несколько лучших
  промежуточных комбинаций;
- общий score учитывает v3 package score, закрытие потребности, пожелания,
  срочные дедлайны, риск, мелкие периоды и остаток вручную;
- выбранные пакеты сохраняются через прежние generation runs/candidates/packages;
- rejected alternatives сохраняются для ML-следов;
- короткий остаток сначала пытается продлить соседний отпуск, а не создавать
  отдельный странный кусок;
- если оптимизатор не нашел вариант для группы, есть fallback на прежний
  последовательный package-placement для этой группы.

## Текущее Dirty-Состояние

Рабочее дерево специально грязное. Это накопленные изменения текущих этапов,
их не откатывать без прямой просьбы пользователя.

Основные измененные зоны:

- demo reset/progress;
- planning progress;
- draft progress;
- auto-place jobs;
- urgent closures;
- demo urgent closure cases;
- seed historical ML traces / baseline snapshot;
- department review, department rework, enterprise final approval;
- final `schedule_approved` employee notifications;
- draft/staffing responsive CSS;
- tests for seed, staffing, preferences, auto-place, notifications and urgent
  closures.
- architecture documentation and path cleanup after stages 1-6.

## Что Делать Дальше

После этапа 3 осталось одно крупное ML-направление:

1. Этап 4: кнопка переобучения нейромодуля после утверждения графика. Она
   должна понятно показывать, какие годы вошли в обучение, и не запускаться,
   если утвержденного нового графика еще нет.

Перед демонстрацией все равно нужно:

1. Проверить clean demo state через quick restore или read-only диагностику.
2. Убедиться, что `.env` выбирает `vacation-candidate-mlp-v2`, а package ranker
   v3 доступен через JSON-артефакт.
3. В браузере пройти демонстрационный сценарий HR -> руководители отделов ->
   директор -> уведомление сотруднику.
4. При необходимости добавить презентационные улучшения: экспорт/печать
   утвержденного графика, короткий отчет по работе нейромодуля, финальную
   инструкцию для защиты.

Крупная обязательная бизнес-логика для основного сценария уже реализована.

## Правила Для Дальнейшей Разработки

- Не делать полный reseed без вопроса пользователю.
- Если надо работать с demo-БД, спрашивать: пересоздавать/сбрасывать или нет.
- Не запускать долгий `pip install` молча.
- Не ослаблять hard rules ради красивой оценки модели.
- Нейромодуль выбирает только среди допустимых вариантов.
- Русский UI проверять как UTF-8, не чинить “кракозябры” только по выводу PowerShell.
- Не коммитить и не stage-ить без просьбы пользователя.
