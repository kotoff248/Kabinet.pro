import argparse

from django.core.management.base import BaseCommand, CommandError

from apps.leave.ml.package_training import (
    PackageTrainingDataError,
    PackageTrainingDependencyError,
    train_package_ranker_model,
)


class Command(BaseCommand):
    help = "Обучает v3 нейромодуль ранжирования годовых пакетов отпусков."

    def add_arguments(self, parser):
        parser.add_argument("--output-version", default="vacation-package-ranker-v3")
        parser.add_argument("--epochs", type=int, default=250)
        parser.add_argument("--lr", type=float, default=0.01)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--min-examples", type=int, default=20)
        parser.add_argument("--max-schedule-year", type=int, default=None)
        parser.add_argument("--output-dir", default=None, help=argparse.SUPPRESS)

    def handle(self, *args, **options):
        try:
            result = train_package_ranker_model(
                output_version=options["output_version"],
                output_dir=options.get("output_dir"),
                epochs=options["epochs"],
                lr=options["lr"],
                seed=options["seed"],
                min_examples=options["min_examples"],
                max_schedule_year=options.get("max_schedule_year"),
            )
        except (PackageTrainingDataError, PackageTrainingDependencyError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Обучение vacation package ranker завершено."))
        self.stdout.write(f"Пакетов: {result.examples_count}")
        self.stdout.write(f"Баланс классов: {result.class_balance}")
        for split_name in ("train", "val", "test"):
            self.stdout.write(f"{split_name}: {result.metrics.get(split_name)}")
        self.stdout.write(f"loss: {result.metrics.get('training_loss')}")
        self.stdout.write(f"Модель: {result.model_path}")
        self.stdout.write(f"Метрики: {result.metrics_path}")
