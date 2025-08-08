import re
from typing import Iterable, List, Tuple, Dict
from django.conf import settings

def _normalize_us_phone(raw: str) -> str | None:
    """Very simple US/E.164 normalizer. Returns +1XXXXXXXXXX or None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.startswith("+"):              # already E.164?
        return raw
    return None

def prepare_recipients(phones: Iterable[str]) -> List[str]:
    normalized = [_normalize_us_phone(p) for p in phones if p]
    return sorted(set([p for p in normalized if p]))

def have_twilio_creds() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM)

def broadcast(numbers: List[str], body: str, dry_run: bool = False) -> Dict[str, object]:
    """Send SMS to numbers. Returns counts and per-number errors."""
    sent, errors, invalid = 0, [], []
    if not have_twilio_creds():
        return {"sent": 0, "errors": [("config", "Missing TWILIO_* settings")], "invalid": []}

    if dry_run:
        return {"sent": len(numbers), "errors": [], "invalid": []}

    from twilio.rest import Client
    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    for to in numbers:
        try:
            client.messages.create(from_=settings.TWILIO_FROM, to=to, body=body)
            sent += 1
        except Exception as e:
            errors.append((to, str(e)))
    return {"sent": sent, "errors": errors, "invalid": invalid}
