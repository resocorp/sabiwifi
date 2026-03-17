from rest_framework import serializers
from routers.models import Router


class RouterAddSerializer(serializers.Serializer):
    """Reseller submits a serial number to claim a router."""
    serial_number = serializers.CharField(max_length=50)

    def validate_serial_number(self, value):
        value = value.strip().upper()
        if not value:
            raise serializers.ValidationError("Serial number is required.")

        try:
            router = Router.objects.get(serial_number=value)
        except Router.DoesNotExist:
            raise serializers.ValidationError(
                "Serial number not found. Power on the router and wait a few minutes "
                "for it to register, then try again."
            )

        if router.reseller is not None:
            raise serializers.ValidationError("This router is already assigned to another account.")

        return value


class RouterSerializer(serializers.ModelSerializer):
    """Read-only router details for dashboard."""
    is_online = serializers.BooleanField(read_only=True)

    class Meta:
        model = Router
        fields = [
            'id', 'serial_number', 'status', 'location_name',
            'is_online', 'last_seen', 'provision_count', 'created_at',
        ]
        read_only_fields = fields


class RouterSSIDSerializer(serializers.Serializer):
    """Update WiFi SSID via RouterOS API."""
    ssid = serializers.CharField(max_length=32, min_length=1)

    def validate_ssid(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("SSID cannot be empty.")
        return value
