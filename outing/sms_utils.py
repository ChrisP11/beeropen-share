import re
from typing import Iterable, List, Dict
from django.conf import settings

_DIGITS = re.compile(r"\D+")

def _normalize_us_phone(raw: str) -> str | None:
    """Return +1XXXXXXXXXX if possible, else None."""
    if not raw:
        return None
    d = _DIGITS.sub("", raw)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    if raw.startswith("+"):           # already E.164-ish
        return raw
    return None

def prepare_recipients(phones: Iterable[str]) -> List[str]:
    """Normalize, de-duplicate (preserve order)."""
    seen, out = set(), []
    for p in phones:
        n = _normalize_us_phone(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out

def have_twilio_creds() -> bool:
    sid = getattr(settings, "TWILIO_ACCOUNT_SID", "") or ""
    tok = getattr(settings, "TWILIO_AUTH_TOKEN", "") or ""
    frm = getattr(settings, "TWILIO_FROM", "") or ""
    return bool(sid and tok and frm)

def broadcast(numbers: List[str], body: str, dry_run: bool = False) -> Dict[str, object]:
    """
    Send SMS to numbers. Returns:
      {"sent": [list of E.164 numbers], "errors": [(num, "msg"), ...], "invalid": []}
    """
    sent: List[str] = []
    errors: List[tuple] = []
    invalid: List[str] = []

    nums = prepare_recipients(numbers)
    if dry_run:
        return {"sent": nums, "errors": [], "invalid": invalid}

    if not have_twilio_creds():
        return {"sent": [], "errors": [("config", "Missing TWILIO_* settings")], "invalid": invalid}

    from twilio.rest import Client
    client = Client(getattr(settings, "TWILIO_ACCOUNT_SID"), getattr(settings, "TWILIO_AUTH_TOKEN"))
    from_ = getattr(settings, "TWILIO_FROM")

    for to in nums:
        try:
            client.messages.create(from_=from_, to=to, body=body)
            sent.append(to)
        except Exception as e:
            errors.append((to, str(e)))

    return {"sent": sent, "errors": errors, "invalid": invalid}
