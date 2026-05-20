import argparse

from django.core.management.base import BaseCommand, CommandError

from apps.leave.ml.retraining import (
    VacationNeuralRetrainingError,
    train_vacation_neural_models_for_year,
)


class Command(BaseCommand):
    help = "Переобучает v2/v3 нейромодули на истории и утверждённом графике выбранного года."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--job-id", type=int, default=None)
        parser.add_argument("--candidate-version", default="vacation-candidate-mlp-v2")
        parser.add_argument("--package-version", default="vacation-package-ranker-v3")
        parser.add_argument("--candidate-epochs", type=int, default=250)
        parser.add_argument("--package-epochs", type=int, default=250)
        parser.add_argument("--candidate-lr", type=float, default=0.01)
        parser.add_argument("--package-lr", type=float, default=0.01)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--candidate-min-examples", type=int, default=30)
        parser.add_argument("--package-min-examples", type=int, default=20)
        parser.add_argument("--output-dir", default=None, help=argparse.SUPPRESS)

    def handle(self, *args, **options):
        try:
            payload = train_vacation_neural_models_for_year(
                year=options["year"],
                job_id=options.get("job_id"),
                candidate_version=options["candidate_version"],
                package_version=options["package_version"],
                candidate_epochs=options["candidate_epochs"],
                package_epochs=options["package_epochs"],
                candidate_lr=options["candidate_lr"],
                package_lr=options["package_lr"],
                seed=options["seed"],
                candidate_min_examples=options["candidate_min_examples"],
                package_min_examples=options["package_min_examples"],
                output_dir=options.get("output_dir"),
            )
        except VacationNeuralRetrainingError as exc:
            raise CommandError(str(exc)) from exc

        source = payload["source"]
        self.stdout.write(self.style.SUCCESS("Переобучение нейромодуля завершено."))
        self.stdout.write(f"Годы обучения: {source.get('years', [])}")
        self.stdout.write(f"Графики: {source.get('schedule_ids', [])}")
        self.stdout.write(f"v2 examples: {payload['candidate']['examples_count']}")
        self.stdout.write(f"v3 packages: {payload['package']['examples_count']}")
        self.stdout.write(f"v2 model: {payload['candidate']['model_path']}")
        self.stdout.write(f"v3 model: {payload['package']['model_path']}")
