# outing/management/commands/load_archive_rosters.py
import csv
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from outing.models import ArchiveEvent  # <- your app/model

# Map common header variants to model fields (case-insensitive)
FIELD_ALIASES = {
    "year": {"year", "Year"},
    "kind": {"kind", "Kind"},
    "swag": {"swag", "Swag"},

    "p1_first_name": {"player 1 first name", "p1_first_name", "p1 first"},
    "p1_last_name":  {"player 1 last name",  "p1_last_name",  "p1 last"},
    "p2_first_name": {"player 2 first name", "p2_first_name", "p2 first"},
    "p2_last_name":  {"player 2 last name",  "p2_last_name",  "p2 last"},
    "p3_first_name": {"player 3 first name", "p3_first_name", "p3 first"},
    "p3_last_name":  {"player 3 last name",  "p3_last_name",  "p3 last"},
    "p4_first_name": {"player 4 first name", "p4_first_name", "p4 first"},
    "p4_last_name":  {"player 4 last name",  "p4_last_name",  "p4 last"},
}

# Your model stores kind as one of: open / local / ito
# Normalize CSV values accordingly.
def normalize_kind(k: str) -> str:
    if not k:
        return ""
    k = k.strip().lower()
    # accept common variants
    if k in {"open", "the open"}:
        return "open"
    if k in {"local"}:
        return "local"
    if k in {"ito"}:
        return "ito"
    # For special labels like "darrenito", "goosenito", "sand valley"
    # these should be "open" in kind, with their label in `swag`.
    return "open"

def normalize_header_map(headers):
    lower = {h.strip().lower(): h for h in headers}
    mapping = {}
    for model_field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            key = alias.lower()
            if key in lower:
                mapping[model_field] = lower[key]
                break
    return mapping

class Command(BaseCommand):
    help = "Populate ArchiveEvent swag + winner fields from a CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to the winners CSV (e.g. 'Beer Open 2025 - Sheet2.csv')")
        parser.add_argument("--dry-run", action="store_true", help="Show changes without saving.")
        parser.add_argument(
            "--match-by",
            default="year,kind",
            help="Comma list of keys to match ArchiveEvent (default: year,kind)."
        )

    def handle(self, *args, **opts):
        csv_path = Path(opts["csv_path"])
        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")

        match_by = [s.strip().lower() for s in opts["match_by"].split(",") if s.strip()]
        with csv_path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise CommandError("CSV has no header row.")

            header_map = normalize_header_map(reader.fieldnames)
            if "year" not in header_map:
                raise CommandError("CSV missing required 'year' column.")

            updated = not_found = rows = 0
            for row in reader:
                rows += 1

                # Build lookup
                try:
                    year = int((row.get(header_map["year"]) or "").strip())
                except ValueError:
                    self.stderr.write(f"[row {rows}] Bad year: {row.get(header_map['year'])!r}")
                    continue

                csv_kind_raw = (row.get(header_map.get("kind", "")) or "").strip()
                kind_norm = normalize_kind(csv_kind_raw)

                lookup = {}
                if "year" in match_by:
                    lookup["year"] = year
                if "kind" in match_by:
                    lookup["kind"] = kind_norm

                try:
                    evt = ArchiveEvent.objects.get(**lookup)
                except ArchiveEvent.DoesNotExist:
                    not_found += 1
                    self.stderr.write(f"[row {rows}] No ArchiveEvent for {lookup} (CSV kind={csv_kind_raw!r})")
                    continue
                except ArchiveEvent.MultipleObjectsReturned:
                    not_found += 1
                    self.stderr.write(f"[row {rows}] Multiple ArchiveEvents for {lookup} — refine --match-by.")
                    continue

                def get(field):
                    col = header_map.get(field)
                    return (row.get(col).strip() if col and row.get(col) is not None else None)

                # If CSV kind was a special label, prefer putting it into swag.
                swag_val = get("swag")
                if not swag_val and csv_kind_raw and csv_kind_raw.lower() not in {"open", "local", "ito"}:
                    swag_val = csv_kind_raw

                evt.swag = swag_val or evt.swag
                evt.p1_first_name = get("p1_first_name") or evt.p1_first_name
                evt.p1_last_name  = get("p1_last_name")  or evt.p1_last_name
                evt.p2_first_name = get("p2_first_name") or evt.p2_first_name
                evt.p2_last_name  = get("p2_last_name")  or evt.p2_last_name
                evt.p3_first_name = get("p3_first_name") or evt.p3_first_name
                evt.p3_last_name  = get("p3_last_name")  or evt.p3_last_name
                evt.p4_first_name = get("p4_first_name") or evt.p4_first_name
                evt.p4_last_name  = get("p4_last_name")  or evt.p4_last_name

                if opts["dry_run"]:
                    self.stdout.write(f"[DRY-RUN] Would update {lookup} (swag={evt.swag})")
                else:
                    evt.save()
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Processed {rows} rows: updated={updated}, not_found={not_found}"
        ))
