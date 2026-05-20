from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from apps.leave.models import VacationPreference, VacationSchedule, VacationScheduleCandidate, VacationScheduleCandidatePackage


@dataclass(frozen=True)
class DraftPlacement:
    employee_id: int
    start_date: date
    end_date: date
    item_id: int | None = None


@dataclass
class DraftItemBalance:
    item_id: int
    start_date: date
    end_date: date
    remaining_days: Decimal


@dataclass
class DraftGenerationCandidate:
    employee: object
    start_date: date | None
    end_date: date | None
    kind: str
    source: str
    comment: str
    preference: VacationPreference | None = None
    assessment: dict | None = None
    metadata: dict = field(default_factory=dict)
    stored_candidate: VacationScheduleCandidate | None = None


@dataclass
class DraftGenerationCandidatePackage:
    employee: object
    candidates: list[DraftGenerationCandidate]
    source: str
    explanation: str
    metadata: dict = field(default_factory=dict)
    stored_package: VacationScheduleCandidatePackage | None = None


@dataclass
class DraftGenerationContext:
    year: int
    schedule: VacationSchedule
    eligible_employees: list
    draft_items_by_employee: dict
    preference_pair_by_employee: dict
    preference_state_by_employee: dict
    placements: list
    planning_need_by_employee: dict
    excluded_schedule_item_ids: set = field(default_factory=set)
    planning_static_by_employee: dict = field(default_factory=dict)
    assessment_cache: dict = field(default_factory=dict)
    risk_context_cache: dict = field(default_factory=dict)
