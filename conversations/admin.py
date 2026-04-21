from django.contrib import admin

from conversations.models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ('created_at', 'sent_at', 'external_message_id')
    fields = ('direction', 'source', 'body', 'delivery_status', 'created_at')
    ordering = ('created_at',)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'reseller', 'channel', 'display_contact', 'state',
        'unread_count', 'last_message_at',
    )
    list_filter = ('channel', 'state')
    search_fields = ('external_thread_id', 'contact_phone', 'contact_display_name')
    raw_id_fields = ('reseller', 'lead', 'subscriber', 'assigned_staff')
    inlines = [MessageInline]
    readonly_fields = ('created_at', 'updated_at', 'last_message_at')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'direction', 'source', 'delivery_status', 'created_at')
    list_filter = ('direction', 'source', 'delivery_status')
    search_fields = ('body', 'external_message_id', 'sender_phone')
    raw_id_fields = ('conversation', 'sent_by')
    readonly_fields = ('created_at', 'sent_at', 'external_message_id')
