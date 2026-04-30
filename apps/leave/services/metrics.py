from .ledger import rebuild_employee_leave_ledger

VACATION_METRIC_SYNC_ENABLED = True

def is_vacation_metric_sync_enabled():
    return VACATION_METRIC_SYNC_ENABLED

def set_vacation_metric_sync_enabled(enabled):
    global VACATION_METRIC_SYNC_ENABLED
    previous_value = VACATION_METRIC_SYNC_ENABLED
    VACATION_METRIC_SYNC_ENABLED = enabled
    return previous_value

def sync_employee_vacation_metrics(employee):
    if employee is None or not VACATION_METRIC_SYNC_ENABLED:
        return

    rebuild_employee_leave_ledger(employee)
