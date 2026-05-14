import argparse

from django.core.management.base import BaseCommand, CommandError

from apps.leave.services.candidate_training import (
    CandidateTrainingDataError,
    CandidateTrainingDependencyError,
    train_candidate_mlp_model,
)


class Command(BaseCommand):
    help = "Обучает v2 нейромодуль подбора отпусков на historical candidates/feedback."

    def add_arguments(self, parser):
        parser.add_argument("--output-version", default="vacation-candidate-mlp-v2")
        parser.add_argument("--epochs", type=int, default=250)
        parser.add_argument("--lr", type=float, default=0.01)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--min-examples", type=int, default=30)
        parser.add_argument("--output-dir", default=None, help=argparse.SUPPRESS)

    def handle(self, *args, **options):
        try:
            result = train_candidate_mlp_model(
                output_version=options["output_version"],
                output_dir=options.get("output_dir"),
                epochs=options["epochs"],
                lr=options["lr"],
                seed=options["seed"],
                min_examples=options["min_examples"],
            )
        except (CandidateTrainingDataError, CandidateTrainingDependencyError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Обучение vacation candidate MLP завершено."))
        self.stdout.write(f"Примеров: {result.examples_count}")
        self.stdout.write(f"Баланс классов: {result.class_balance}")
        for split_name in ("train", "val", "test"):
            self.stdout.write(f"{split_name}: {result.metrics.get(split_name)}")
        self.stdout.write(f"loss: {result.metrics.get('training_loss')}")
        self.stdout.write(f"Модель: {result.model_path}")
        self.stdout.write(f"Метрики: {result.metrics_path}")
