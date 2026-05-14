from django.db import connection


DEMO_DATA_MUTATION_LOCK_ID = 2026051401


def try_demo_data_mutation_lock():
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [DEMO_DATA_MUTATION_LOCK_ID])
        return bool(cursor.fetchone()[0])
