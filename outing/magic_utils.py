import hashlib, secrets
from datetime import timedelta
from django.utils.timezone import now
from django.urls import reverse

from .models import MagicLoginToken

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def create_magic_link(request, user, ttl_seconds: int = 15 * 60, sent_to: str = "") -> str:
    """
    Creates a single-use token row and returns a full URL to send via SMS.
    """
    raw = secrets.token_urlsafe(32)
    tok = MagicLoginToken.objects.create(
        user=user,
        token_hash=_hash(raw),
        expires_at=now() + timedelta(seconds=ttl_seconds),
        sent_to=sent_to,
    )
    path = reverse("magic_login", args=[tok.pk, raw])
    return request.build_absolute_uri(path)

def validate_token(token_id: int, raw: str) -> MagicLoginToken | None:
    try:
        tok = MagicLoginToken.objects.select_related("user").get(pk=token_id)
    except MagicLoginToken.DoesNotExist:
        return None
    if tok.used_at is not None or tok.expires_at <= now():
        return None
    if tok.token_hash != _hash(raw):
        return None
    return tok
