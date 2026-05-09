from django.contrib import admin

from .models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationRequestHistory,
    VacationSchedule,
    VacationScheduleAuthorizedApproval,
    VacationScheduleChangeRequest,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)


admin.site.register(VacationRequest)
admin.site.register(VacationRequestHistory)
admin.site.register(VacationSchedule)
admin.site.register(VacationScheduleItem)
admin.site.register(VacationEntitlementPeriod)
admin.site.register(VacationEntitlementAllocation)
admin.site.register(VacationScheduleDepartmentApproval)
admin.site.register(VacationScheduleEnterpriseApproval)
admin.site.register(VacationScheduleAuthorizedApproval)
admin.site.register(VacationScheduleChangeRequest)
admin.site.register(VacationUrgentClosureRequest)
admin.site.register(VacationPreference)
admin.site.register(VacationPreferenceCollection)
admin.site.register(DepartmentWorkload)
admin.site.register(DepartmentStaffingRule)
