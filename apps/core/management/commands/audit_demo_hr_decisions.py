import json
from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.employees.models import Employees
from apps.leave.models import (
    VacationPreference,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleItem,
)


SCHEDULE_STATUSES = [VacationSchedule.STATUS_ARCHIVED, VacationSchedule.STATUS_APPROVED]
TRACE_ITEM_STATUSES = [VacationScheduleItem.STATUS_APPROVED, VacationScheduleItem.STATUS_TRANSFERRED]


def _pct(part, total):
    total = float(total or 0)
    return round(float(part or 0) / total * 100.0, 1) if total else 0.0


def _avg(values):
    values = [float(value) for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else 0.0


def _features(candidate):
    return candidate.features if isinstance(candidate.features, dict) else {}


def _feature(candidate, key, default=None):
    return _features(candidate).get(key, default)


def _group_key(candidate):
    event_key = _feature(candidate, "historical_decision_event_key")
    if event_key:
        return f"event:{event_key}"
    return f"legacy:{candidate.schedule_id}:{candidate.employee_id}:{candidate.generation_run_id}"


class Command(BaseCommand):
    help = "Audit demo HR decision traces used for candidate/package ML experiments."

    def add_arguments(self, parser):
        parser.add_argument("--max-year", type=int, default=None)
        parser.add_argument(
            "--output-dir",
            default="outputs/experimental_research/demo_hr_decision_quality",
            help="Directory for JSON/TXT audit files.",
        )

    def handle(self, *args, **options):
        max_year = options["max_year"] or timezone.localdate().year
        output_dir = Path(options["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        candidates = list(
            VacationScheduleCandidate.objects.select_related("schedule", "employee")
            .filter(
                schedule__year__lte=max_year,
                schedule__status__in=SCHEDULE_STATUSES,
                decision__in=[
                    VacationScheduleCandidate.DECISION_SELECTED,
                    VacationScheduleCandidate.DECISION_REJECTED,
                    VacationScheduleCandidate.DECISION_BLOCKED,
                ],
            )
            .order_by("schedule__year", "generation_run_id", "employee_id", "decision_rank", "id")
        )

        candidate_audit = self._audit_candidates(candidates)
        preference_audit = self._audit_preferences(max_year)
        report = {
            "max_year": max_year,
            "candidate_audit": candidate_audit,
            "preference_audit": preference_audit,
        }

        json_path = output_dir / "demo_hr_decision_quality_report.json"
        txt_path = output_dir / "demo_hr_decision_quality_summary.txt"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        txt_path.write_text(self._summary_text(report, json_path), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"Demo HR decision audit written: {txt_path}"))
        self.stdout.write(
            "selected_top={top:.1f}%, preference_exact={pref:.1f}%, multi_selected={multi}".format(
                top=candidate_audit["selected_top_among_passed_percent"],
                pref=preference_audit["any_exact_match_percent"],
                multi=candidate_audit["groups_multi_selected"],
            )
        )

    def _audit_candidates(self, candidates):
        groups = defaultdict(list)
        for candidate in candidates:
            groups[_group_key(candidate)].append(candidate)

        top_selected = 0
        selected_not_top = 0
        selected_below_best_by_5pp = 0
        groups_multi_selected = 0
        groups_with_passed_rejected = 0
        score_gaps = []
        selected_scores = []
        rejected_scores = []

        for group in groups.values():
            selected = [candidate for candidate in group if candidate.decision == VacationScheduleCandidate.DECISION_SELECTED]
            passed = [candidate for candidate in group if candidate.passed_hard_rules]
            rejected_passed = [
                candidate
                for candidate in group
                if candidate.decision == VacationScheduleCandidate.DECISION_REJECTED and candidate.passed_hard_rules
            ]
            if len(selected) > 1:
                groups_multi_selected += 1
            if rejected_passed:
                groups_with_passed_rejected += 1
            selected_scores.extend(float(candidate.score or 0) for candidate in selected if candidate.passed_hard_rules)
            rejected_scores.extend(float(candidate.score or 0) for candidate in rejected_passed)

            if selected and passed:
                best_passed = max(passed, key=lambda candidate: float(candidate.score or 0))
                selected_best_score = max(float(candidate.score or 0) for candidate in selected)
                best_score = float(best_passed.score or 0)
                gap = round(best_score - selected_best_score, 3)
                score_gaps.append(gap)
                if gap <= 0.01:
                    top_selected += 1
                else:
                    selected_not_top += 1
                    if gap >= 5.0:
                        selected_below_best_by_5pp += 1

        selected_candidates = [
            candidate for candidate in candidates if candidate.decision == VacationScheduleCandidate.DECISION_SELECTED
        ]
        blocked_candidates = [
            candidate for candidate in candidates if candidate.decision == VacationScheduleCandidate.DECISION_BLOCKED
        ]
        return {
            "total_candidates": len(candidates),
            "groups": len(groups),
            "groups_multi_selected": groups_multi_selected,
            "groups_multi_selected_percent": _pct(groups_multi_selected, len(groups)),
            "groups_with_passed_rejected": groups_with_passed_rejected,
            "groups_with_passed_rejected_percent": _pct(groups_with_passed_rejected, len(groups)),
            "selected_top_among_passed": top_selected,
            "selected_top_among_passed_percent": _pct(top_selected, len(groups)),
            "selected_not_top": selected_not_top,
            "selected_below_best_by_5pp": selected_below_best_by_5pp,
            "selected_below_best_by_5pp_percent": _pct(selected_below_best_by_5pp, len(groups)),
            "avg_best_minus_selected_gap_pp": _avg(score_gaps),
            "avg_selected_score": _avg(selected_scores),
            "avg_rejected_passed_score": _avg(rejected_scores),
            "selected_by_kind": dict(Counter(candidate.kind for candidate in selected_candidates)),
            "selected_risk_levels": dict(Counter(_feature(candidate, "risk_level", "") for candidate in selected_candidates)),
            "block_reasons": dict(Counter(candidate.block_reason_key or "unknown" for candidate in blocked_candidates)),
        }

    def _audit_preferences(self, max_year):
        preferences = list(
            VacationPreference.objects.filter(
                year__lte=max_year,
                status=VacationPreference.STATUS_FILLED,
                start_date__isnull=False,
                end_date__isnull=False,
            ).order_by("employee_id", "year", "priority", "id")
        )
        preference_pairs = defaultdict(dict)
        for preference in preferences:
            preference_pairs[(preference.employee_id, preference.year)][preference.priority] = preference

        items = list(
            VacationScheduleItem.objects.select_related("schedule", "employee")
            .filter(
                schedule__year__lte=max_year,
                schedule__status__in=SCHEDULE_STATUSES,
                vacation_type="paid",
                status__in=TRACE_ITEM_STATUSES,
            )
            .exclude(employee__role__in=Employees.SERVICE_ROLES)
            .order_by("schedule__year", "employee_id", "start_date", "id")
        )

        primary_matches = set()
        backup_matches = set()
        any_matches = set()
        item_years_with_preferences = 0
        for item in items:
            key = (item.employee_id, item.schedule.year)
            pair = preference_pairs.get(key) or {}
            if not pair:
                continue
            item_years_with_preferences += 1
            primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
            backup = pair.get(VacationPreference.PRIORITY_BACKUP)
            if primary and item.start_date == primary.start_date and item.end_date == primary.end_date:
                primary_matches.add(key)
                any_matches.add(key)
            if backup and item.start_date == backup.start_date and item.end_date == backup.end_date:
                backup_matches.add(key)
                any_matches.add(key)

        employee_years_with_preferences = len(preference_pairs)
        return {
            "employee_years_with_preferences": employee_years_with_preferences,
            "primary_exact_matches": len(primary_matches),
            "backup_exact_matches": len(backup_matches),
            "any_exact_matches": len(any_matches),
            "any_exact_match_percent": _pct(len(any_matches), employee_years_with_preferences),
            "schedule_items_in_employee_years_with_preferences": item_years_with_preferences,
            "schedule_items_with_preferences_percent": _pct(item_years_with_preferences, len(items)),
        }

    def _summary_text(self, report, json_path):
        candidates = report["candidate_audit"]
        preferences = report["preference_audit"]
        lines = [
            "Качество демо-истории HR-решений",
            "======================================",
            f"Кандидатов: {candidates['total_candidates']}",
            f"Групп выбора: {candidates['groups']}",
            (
                "Групп с несколькими selected: "
                f"{candidates['groups_multi_selected']} ({candidates['groups_multi_selected_percent']}%)"
            ),
            (
                "Выбранный вариант лучший среди прошедших hard rules: "
                f"{candidates['selected_top_among_passed']} ({candidates['selected_top_among_passed_percent']}%)"
            ),
            (
                "Отставание selected от лучшей допустимой альтернативы >= 5 п.п.: "
                f"{candidates['selected_below_best_by_5pp']} ({candidates['selected_below_best_by_5pp_percent']}%)"
            ),
            f"Средний разрыв best_passed - selected: {candidates['avg_best_minus_selected_gap_pp']} п.п.",
            f"Средний score selected: {candidates['avg_selected_score']}%",
            f"Средний score rejected passed: {candidates['avg_rejected_passed_score']}%",
            "",
            "Пожелания сотрудников против фактического графика:",
            f"- Employee-year с заполненными пожеланиями: {preferences['employee_years_with_preferences']}",
            f"- Точное совпадение с primary: {preferences['primary_exact_matches']}",
            f"- Точное совпадение с backup: {preferences['backup_exact_matches']}",
            f"- Любое точное совпадение: {preferences['any_exact_matches']} ({preferences['any_exact_match_percent']}%)",
            "",
            f"JSON: {json_path}",
        ]
        return "\n".join(lines)
