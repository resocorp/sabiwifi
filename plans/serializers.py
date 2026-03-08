from rest_framework import serializers
from django.utils.text import slugify
from plans.models import ServicePlan, Subscription


class ServicePlanSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating service plans."""
    speed_display = serializers.CharField(read_only=True)
    duration_display = serializers.CharField(read_only=True)
    data_cap_display = serializers.CharField(read_only=True)
    active_subscribers = serializers.SerializerMethodField()

    class Meta:
        model = ServicePlan
        fields = [
            'id', 'name', 'slug', 'download_mbps', 'upload_mbps',
            'duration_days', 'duration_hours', 'data_cap_gb', 'max_devices',
            'price_ngn', 'is_trial', 'is_active', 'is_system_created',
            'speed_display', 'duration_display', 'data_cap_display',
            'active_subscribers', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'is_system_created', 'created_at', 'updated_at']

    def get_active_subscribers(self, obj):
        return Subscription.objects.filter(plan=obj, status='active').count()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Plan name is required.")
        return value

    def validate(self, data):
        # Check paid plan guard
        request = self.context.get('request')
        if request and hasattr(request.user, 'reseller'):
            reseller = request.user.reseller
            price = data.get('price_ngn', 0)
            if price and price > 0 and not reseller.payment_verified:
                raise serializers.ValidationError({
                    'price_ngn': (
                        'To create paid plans, add your bank account first. '
                        'Subscribers can\'t pay until you have a verified account.'
                    )
                })
        return data

    def create(self, validated_data):
        reseller = self.context['request'].user.reseller
        validated_data['reseller'] = reseller
        validated_data['slug'] = slugify(validated_data['name'])

        # Ensure unique slug per reseller
        original_slug = validated_data['slug']
        counter = 1
        while ServicePlan.objects.filter(reseller=reseller, slug=validated_data['slug']).exists():
            validated_data['slug'] = f'{original_slug}-{counter}'
            counter += 1

        # Set is_trial flag
        if validated_data.get('price_ngn', 0) == 0:
            validated_data['is_trial'] = True

        return super().create(validated_data)


class SubscriptionSerializer(serializers.ModelSerializer):
    """Read-only subscription details."""
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_speed = serializers.CharField(source='plan.speed_display', read_only=True)
    plan_data_cap = serializers.CharField(source='plan.data_cap_display', read_only=True)
    subscriber_phone = serializers.CharField(source='subscriber.phone', read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'subscriber_phone', 'plan_name', 'plan_speed', 'plan_data_cap',
            'start_date', 'expiry_date', 'status', 'created_at',
        ]
        read_only_fields = fields
