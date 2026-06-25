from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import DemoDataResetJob
from apps.core.services.demo_reset_jobs import (
    DEMO_SEED_PRESET_FAST,
    demo_data_reset_job_payload,
    get_or_create_demo_data_reset_job,
)
from apps.core.services.demo_seed.constants import (
    DEMO_SEED_PRESET_STANDARD,
    FAST_EMPLOYEE_COUNTS,
    STANDARD_EMPLOYEE_COUNTS,
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


class SeedVacationRequestsPresetTests(TestCase):
    def test_fast_seed_rebuilds_fast_preset_from_scratch(self):
        stdout = StringIO()
        with patch("apps.core.management.commands.seed_vacation_requests.DemoVacationSeedRunner") as seed_runner:
            seed_runner.return_value.run.return_value = None
            call_command("seed_vacation_requests", confirm_reset=True, fast=True, stdout=stdout)

        seed_runner.return_value.run.assert_called_once()
        _, kwargs = seed_runner.return_value.run.call_args
        self.assertTrue(kwargs["fast"])
        self.assertEqual(kwargs["preset"], DEMO_SEED_PRESET_FAST)
        self.assertEqual(kwargs["employee_counts"], FAST_EMPLOYEE_COUNTS)

    def test_standard_seed_rebuilds_standard_preset_from_scratch(self):
        stdout = StringIO()
        with patch("apps.core.management.commands.seed_vacation_requests.DemoVacationSeedRunner") as seed_runner:
            seed_runner.return_value.run.return_value = None
            call_command("seed_vacation_requests", confirm_reset=True, stdout=stdout)

        seed_runner.return_value.run.assert_called_once()
        _, kwargs = seed_runner.return_value.run.call_args
        self.assertFalse(kwargs["fast"])
        self.assertEqual(kwargs["preset"], DEMO_SEED_PRESET_STANDARD)
        self.assertEqual(kwargs["employee_counts"], STANDARD_EMPLOYEE_COUNTS)
