from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import DemoDataResetJob
from apps.core.services.demo_baseline import DemoBaselineMissingError
from apps.core.services.demo_reset_jobs import (
    DEMO_SEED_PRESET_FAST,
    demo_data_reset_job_payload,
    get_or_create_demo_data_reset_job,
)


class DemoResetJobStaleProcessTests(TestCase):
    def test_get_or_create_marks_dead_running_process_failed(self):
        stale_job = DemoDataResetJob.objects.create(
            token="stale-reset-token",
            status=DemoDataResetJob.STATUS_RUNNING,
            seed_value=17,
            preset=DemoDataResetJob.PRESET_STANDARD,
            progress_percent=78,
            process_id=999999,
            started_at=timezone.now(),
        )

        with patch("apps.core.services.demo_reset_jobs._demo_seed_process_is_active", return_value=False):
            job, created = get_or_create_demo_data_reset_job(seed_value=42, preset=DEMO_SEED_PRESET_FAST)

        stale_job.refresh_from_db()
        self.assertEqual(stale_job.status, DemoDataResetJob.STATUS_FAILED)
        self.assertTrue(created)
        self.assertNotEqual(job.id, stale_job.id)
        self.assertEqual(job.preset, DemoDataResetJob.PRESET_FAST)

    def test_payload_marks_dead_running_process_failed(self):
        stale_job = DemoDataResetJob.objects.create(
            token="stale-status-token",
            status=DemoDataResetJob.STATUS_RUNNING,
            seed_value=17,
            preset=DemoDataResetJob.PRESET_FAST,
            progress_percent=78,
            process_id=999999,
            started_at=timezone.now(),
        )

        with patch("apps.core.services.demo_reset_jobs._demo_seed_process_is_active", return_value=False):
            payload = demo_data_reset_job_payload(stale_job)

        stale_job.refresh_from_db()
        self.assertEqual(stale_job.status, DemoDataResetJob.STATUS_FAILED)
        self.assertEqual(payload["status"], DemoDataResetJob.STATUS_FAILED)
        self.assertTrue(payload["error_message"])


class SeedVacationRequestsFastBaselineTests(TestCase):
    def test_fast_seed_uses_baseline_restore_when_available(self):
        stdout = StringIO()
        with (
            patch(
                "apps.core.management.commands.seed_vacation_requests.reset_demo_to_baseline",
                return_value={"planning_year": 2027, "departments": 5},
            ) as restore_baseline,
            patch("apps.core.management.commands.seed_vacation_requests.DemoVacationSeedRunner") as seed_runner,
        ):
            call_command("seed_vacation_requests", confirm_reset=True, fast=True, stdout=stdout)

        restore_baseline.assert_called_once_with(ignore_reset_job_id=None)
        seed_runner.assert_not_called()
        self.assertIn("Быстрая демо-база восстановлена", stdout.getvalue())

    def test_fast_seed_falls_back_to_rebuild_without_baseline(self):
        stdout = StringIO()
        with (
            patch(
                "apps.core.management.commands.seed_vacation_requests.reset_demo_to_baseline",
                side_effect=DemoBaselineMissingError,
            ) as restore_baseline,
            patch("apps.core.management.commands.seed_vacation_requests.DemoVacationSeedRunner") as seed_runner,
        ):
            seed_runner.return_value.run.return_value = None
            call_command("seed_vacation_requests", confirm_reset=True, fast=True, stdout=stdout)

        restore_baseline.assert_called_once_with(ignore_reset_job_id=None)
        seed_runner.return_value.run.assert_called_once()
