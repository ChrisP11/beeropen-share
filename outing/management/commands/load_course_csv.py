# outing/management/commands/load_course_csv.py
import csv
from typing import Dict
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from outing.models import Course, Hole, TeeBox, TeeYardage, EventSettings


HEADER_MAP = {
    "course": "course",
    "nine": "course",          # allow alias
    "hole": "hole",
    "par": "par",
    "blue": "blue",
    "white": "white",
    "red": "red",
    "handicap": "handicap",
    "hdcp": "handicap",        # allow alias
}

def _norm_headers(headers):
    norm = {}
    for h in headers or []:
        k = (h or "").strip().lower()
        norm[h] = HEADER_MAP.get(k, k)  # map known headers; else keep lower key
    return norm


class Command(BaseCommand):
    help = (
        "Load a course CSV to seed/update Course, Hole, TeeBox, and TeeYardage.\n"
        "CSV columns (case-insensitive): course|nine, hole, par, blue, white, red, handicap\n"
        "Example row: South,1,4,339,319,258,9"
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to the CSV file.")
        parser.add_argument(
            "--course",
            default="Arrowhead GC",
            help='Name of Course to (get or create). Default: "Arrowhead GC"',
        )
        parser.add_argument(
            "--tees",
            default="Blue,White,Red",
            help="Comma list of tee names in the CSV order (default matches columns): Blue,White,Red",
        )
        parser.add_argument(
            "--set-event",
            action="store_true",
            help="After load, set EventSettings.scoring_course (and optionally scoring_tee).",
        )
        parser.add_argument(
            "--tee-for-event",
            default="Blue",
            help='When using --set-event, which tee to set as EventSettings.scoring_tee (default "Blue").',
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report changes without writing to the database.",
        )

    def handle(self, csv_path, course, tees, set_event, tee_for_event, dry_run, **kwargs):
        tee_names = [t.strip() for t in tees.split(",") if t.strip()]
        if not tee_names:
            raise CommandError("No tee names provided via --tees")

        # Open CSV
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    raise CommandError("CSV has no header row.")

                header_map = _norm_headers(reader.fieldnames)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"CSV not found: {csv_path}")

        # Stats
        created = {"course": 0, "hole": 0, "tee": 0, "yard": 0}
        updated = {"hole": 0, "yard": 0}

        # Upsert Course
        course_obj, course_created = Course.objects.get_or_create(
            name=course, defaults={"city": "", "state": ""}
        )
        if course_created:
            created["course"] += 1

        # Ensure TeeBoxes exist (by name, per course)
        tee_objs: Dict[str, TeeBox] = {}
        for name in tee_names:
            t, t_created = TeeBox.objects.get_or_create(course=course_obj, name=name)
            tee_objs[name.lower()] = t
            if t_created:
                created["tee"] += 1

        # Build a lookup for holes we’ll touch
        # Note: We expect 1–18 across your South(1–9) + East(10–18) CSV.
        def get_int(row, key):
            v = (row.get(key) or "").strip()
            try:
                return int(v)
            except Exception:
                return None

        def get_from_row(row, logical_key):
            # Map original header key -> logical key
            for raw, mapped in header_map.items():
                if mapped == logical_key:
                    return row.get(raw)
            return None

        # Transaction so either all rows load or none (unless --dry-run)
        ctx = transaction.atomic() if not dry_run else nullcontext()
        with ctx:
            for row in rows:
                # Normalize per-row values via mapped keys
                r_course = (get_from_row(row, "course") or "").strip()
                hole_num = get_int(row, "hole")
                par_val  = get_int(row, "par")
                hdcp_val = get_int(row, "handicap")  # stored as men_hdcp
                blue_y   = get_int(row, "blue")
                white_y  = get_int(row, "white")
                red_y    = get_int(row, "red")

                if not hole_num:
                    self.stdout.write(self.style.WARNING(f"Skip row (no hole): {row}"))
                    continue
                if not par_val:
                    self.stdout.write(self.style.WARNING(f"Skip row (no par): hole {hole_num}"))
                    continue

                # Upsert Hole
                hole_obj, h_created = Hole.objects.get_or_create(
                    course=course_obj, number=hole_num,
                    defaults={"par": par_val, "men_hdcp": hdcp_val}
                )
                if h_created:
                    created["hole"] += 1
                else:
                    changed = False
                    if hole_obj.par != par_val:
                        hole_obj.par = par_val; changed = True
                    if hdcp_val is not None and hole_obj.men_hdcp != hdcp_val:
                        hole_obj.men_hdcp = hdcp_val; changed = True
                    if changed and not dry_run:
                        hole_obj.save(update_fields=["par", "men_hdcp"])
                        updated["hole"] += 1

                # Yardages per tee
                for name, yards in (("Blue", blue_y), ("White", white_y), ("Red", red_y)):
                    if name.lower() not in tee_objs:  # ignore unknown tee column
                        continue
                    if yards is None:
                        continue
                    tee_obj = tee_objs[name.lower()]
                    ty, y_created = TeeYardage.objects.get_or_create(
                        tee=tee_obj, hole=hole_obj, defaults={"yards": yards}
                    )
                    if y_created:
                        created["yard"] += 1
                    else:
                        if ty.yards != yards and not dry_run:
                            ty.yards = yards
                            ty.save(update_fields=["yards"])
                            updated["yard"] += 1

            # Optionally set EventSettings
            if set_event:
                es = EventSettings.load()
                es.scoring_course = course_obj
                tee_target = tee_objs.get(tee_for_event.strip().lower())
                if tee_target:
                    es.scoring_tee = tee_target
                if not dry_run:
                    es.save(update_fields=["scoring_course", "scoring_tee"])

        # Report
        self.stdout.write(self.style.SUCCESS(f"Course: {'created' if course_created else 'existing'} → {course_obj.name}"))
        self.stdout.write(f"Tees created: {created['tee']}")
        self.stdout.write(f"Holes created: {created['hole']} updated: {updated['hole']}")
        self.stdout.write(f"Yardages created: {created['yard']} updated: {updated['yard']}")
        if set_event:
            self.stdout.write(self.style.SUCCESS("EventSettings updated." if not dry_run else "Would update EventSettings (dry-run)."))


# tiny compat helper for --dry-run context
from contextlib import contextmanager
@contextmanager
def nullcontext():
    yield
