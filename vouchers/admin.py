from django.contrib import admin
from vouchers.models import VoucherBatch, Voucher, RefillCardBatch, RefillCard


class VoucherInline(admin.TabularInline):
    model = Voucher
    extra = 0
    readonly_fields = ['pin', 'status', 'subscriber', 'activated_at', 'expires_at', 'created_at']
    fields = ['pin', 'status', 'subscriber', 'activated_at', 'expires_at']


@admin.register(VoucherBatch)
class VoucherBatchAdmin(admin.ModelAdmin):
    list_display = ['name', 'reseller', 'plan', 'quantity', 'status', 'created_at']
    list_filter = ['status', 'reseller']
    inlines = [VoucherInline]


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ['pin', 'reseller', 'plan', 'status', 'subscriber', 'activated_at', 'expires_at']
    list_filter = ['status', 'reseller']
    search_fields = ['pin']
    readonly_fields = ['created_at']


class RefillCardInline(admin.TabularInline):
    model = RefillCard
    extra = 0
    readonly_fields = ['pin', 'status', 'redeemed_by', 'redeemed_at', 'created_at']
    fields = ['pin', 'value_ngn', 'status', 'redeemed_by', 'redeemed_at']


@admin.register(RefillCardBatch)
class RefillCardBatchAdmin(admin.ModelAdmin):
    list_display = ['name', 'reseller', 'value_ngn', 'quantity', 'status', 'created_at']
    list_filter = ['status', 'reseller']
    inlines = [RefillCardInline]


@admin.register(RefillCard)
class RefillCardAdmin(admin.ModelAdmin):
    list_display = ['pin', 'reseller', 'value_ngn', 'status', 'redeemed_by', 'redeemed_at']
    list_filter = ['status', 'reseller']
    search_fields = ['pin']
