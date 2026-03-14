from rest_framework import authentication, exceptions
from accounts.models import Reseller


class ResellerTokenAuthentication(authentication.BaseAuthentication):
    """
    Custom DRF authentication for reseller API token.
    Token is passed in the Authorization header: Token <token>
    """
    keyword = 'Token'

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request)
        if not auth_header:
            return None

        auth_parts = auth_header.split()
        if len(auth_parts) != 2 or auth_parts[0].lower() != b'token':
            return None

        token = auth_parts[1].decode('utf-8')

        try:
            from rest_framework.authtoken.models import Token
            token_obj = Token.objects.select_related('user', 'user__reseller').get(key=token)
        except Token.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid token.')

        if not token_obj.user.is_active:
            raise exceptions.AuthenticationFailed('User account is disabled.')

        return (token_obj.user, token_obj)
