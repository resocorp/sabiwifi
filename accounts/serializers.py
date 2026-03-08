from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from accounts.models import Reseller
import re


class ResellerSignupSerializer(serializers.Serializer):
    """One-step reseller signup: business name, owner name, email, phone, password."""
    business_name = serializers.CharField(max_length=200)
    owner_name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20)
    password = serializers.CharField(min_length=8, write_only=True)

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        # Normalize: strip spaces, ensure +234 prefix
        value = re.sub(r'\s+', '', value)
        if value.startswith('0'):
            value = '+234' + value[1:]
        elif value.startswith('234'):
            value = '+' + value
        elif not value.startswith('+234'):
            raise serializers.ValidationError("Phone number must be a Nigerian number (+234).")

        # Validate format: +234 followed by 10 digits
        if not re.match(r'^\+234\d{10}$', value):
            raise serializers.ValidationError("Invalid phone number format. Expected +234XXXXXXXXXX.")

        if Reseller.objects.filter(phone=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate_business_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Business name is required.")
        return value

    def create(self, validated_data):
        # Create Django User
        user = User.objects.create_user(
            username=validated_data['email'].lower(),
            email=validated_data['email'].lower(),
            password=validated_data['password'],
            first_name=validated_data['owner_name'],
        )

        # Create Reseller
        reseller = Reseller.objects.create(
            user=user,
            name=validated_data['business_name'],
            owner_name=validated_data['owner_name'],
            email=validated_data['email'].lower(),
            phone=validated_data['phone'],
            branding={
                'portal_title': validated_data['business_name'],
                'welcome_text': 'Welcome! Get connected in seconds.',
                'bg_image': '',
                'logo': '',
                'primary_color': '#0052CC',
                'template': 'modern',
            },
        )
        return reseller


class ResellerLoginSerializer(serializers.Serializer):
    """Reseller login with email + password."""
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        email = data.get('email', '').lower().strip()
        password = data.get('password', '')

        user = authenticate(username=email, password=password)
        if user is None:
            raise serializers.ValidationError("Invalid email or password.")

        if not user.is_active:
            raise serializers.ValidationError("This account has been disabled.")

        if not hasattr(user, 'reseller'):
            raise serializers.ValidationError("No reseller account found.")

        data['user'] = user
        data['reseller'] = user.reseller
        return data


class ResellerProfileSerializer(serializers.ModelSerializer):
    """Read-only reseller profile for dashboard context."""
    class Meta:
        model = Reseller
        fields = [
            'id', 'name', 'slug', 'owner_name', 'email', 'phone',
            'location', 'status', 'branding', 'payment_verified',
            'created_at',
        ]
        read_only_fields = fields
