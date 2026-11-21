from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed


TOKEN_LIFETIME = timedelta(hours=1)


def token_has_expired(token):
    return timezone.now() >= token.created + TOKEN_LIFETIME


@transaction.atomic
def get_login_token(user):
    # Serialize logins so concurrent requests agree on the replacement token.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    token = Token.objects.filter(user=user).first()
    if token is not None:
        if not token_has_expired(token):
            return token
        token.delete()

    return Token.objects.create(user=user)


class BearerAuthentication(TokenAuthentication):
    keyword = 'Bearer'

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if token_has_expired(token):
            raise AuthenticationFailed('Token has expired')
        return user, token
