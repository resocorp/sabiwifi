from django.contrib import admin

from staff.models import StaffMember


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'reseller', 'phone', 'active', 'current_load')
    list_filter = ('role', 'active', 'reseller')
    search_fields = ('name', 'phone', 'whatsapp', 'email')
    raw_id_fields = ('reseller',)
