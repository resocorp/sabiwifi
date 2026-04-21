from django.contrib import admin

from tickets.models import Ticket, TicketEvent


class TicketEventInline(admin.TabularInline):
    model = TicketEvent
    extra = 0
    readonly_fields = ('kind', 'actor', 'from_status', 'to_status', 'note',
                       'metadata', 'created_at')
    can_delete = False


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ('id', 'reseller', 'type', 'status', 'priority', 'subject',
                    'assigned_staff', 'sla_due_at', 'created_at')
    list_filter = ('status', 'type', 'priority', 'ai_handled', 'reseller')
    search_fields = ('subject', 'body', 'resolution_note')
    raw_id_fields = ('reseller', 'lead', 'subscriber', 'conversation',
                     'installation_order', 'assigned_staff')
    readonly_fields = ('created_at', 'updated_at', 'first_response_at',
                       'resolved_at', 'closed_at')
    inlines = [TicketEventInline]


@admin.register(TicketEvent)
class TicketEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'ticket', 'kind', 'actor', 'created_at')
    list_filter = ('kind',)
    raw_id_fields = ('ticket',)
    readonly_fields = ('created_at',)
