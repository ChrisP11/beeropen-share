import csv
from django.core.management.base import BaseCommand, CommandError
from outing.models import Course, Hole, TeeBox, TeeYardage, EventSettings

def norm(s): return (s or "").strip()

class Command(BaseCommand):
    help = "Load course holes + tee yardages from CSV. Supports normal tees or a combo tee with a 'tee designation' column."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--course", required=True, help='Course name, e.g. "Arrowhead GC"')
        parser.add_argument("--tee", help='Tee name to load (e.g. "Blue", "White", "Red"). If omitted and a "blue/white" column is present, a combo tee is assumed.')
        parser.add_argument("--combo-name", default="Blue/White", help='Name for the combo tee (default: "Blue/White")')
        parser.add_argument("--set-event", action="store_true", help="Also set EventSettings.scoring_course/scoring_tee")
        parser.add_argument("--tee-for-event", help="Override tee for EventSettings if desired")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        path        = opts["csv_path"]
        course_name = opts["course"].strip()
        fixed_tee   = (opts.get("tee") or "").strip()
        combo_name  = opts["combo_name"].strip()
        dry         = bool(opts["dry_run"])

        # Read CSV
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            # normalize header keys (lower, no spaces/slashes)
            keymap = {k: k for k in (reader.fieldnames or [])}
            def kfind(*candidates):
                # find a header key matching any candidate (case/space insensitive)
                want = [(c.lower().replace(" ", "").replace("/", "")) for c in candidates]
                for k in keymap:
                    kk = k.lower().replace(" ", "").replace("/", "")
                    if kk in want:
                        return keymap[k]
                return None

            k_course = kfind("course")          # South / East (optional)
            k_hole   = kfind("hole","number")
            k_par    = kfind("par")

            # Either normal tee (Blue/White/Red) or combo mode ("blue/white" + "tee designation")
            k_yards_combo = kfind("bluewhite","blue/white")
            k_designation = kfind("teedesignation","designation")

            # For normal single-tee CSVs
            k_yards_single = None
            if not k_yards_combo:
                # if user passed --tee, look for that column name directly
                if fixed_tee:
                    k_yards_single = kfind(fixed_tee)
                    if not k_yards_single:
                        raise CommandError(f"Could not find a '{fixed_tee}' column in CSV.")
                else:
                    # try Blue/White/Red columns in that order
                    for guess in ("blue","white","red"):
                        k_yards_single = kfind(guess)
                        if k_yards_single: 
                            fixed_tee = guess.title()
                            break
                    if not k_yards_single:
                        raise CommandError("CSV must contain either a 'blue/white' column (combo) or a single tee column like 'Blue', 'White', or 'Red'.")

            # Upsert course
            course, _ = Course.objects.get_or_create(name=course_name)
            self.stdout.write(f"Course: {course.name}")

            # Determine tee to write
            if k_yards_combo:
                tee_name = combo_name
            else:
                tee_name = fixed_tee

            tee, _ = TeeBox.objects.get_or_create(course=course, name=tee_name)

            holes_created = holes_updated = yards_created = yards_updated = 0

            for row in reader:
                hole_str = norm(row.get(k_hole))
                par_str  = norm(row.get(k_par))
                if not hole_str:
                    continue

                hole_no = int(hole_str)
                par     = int(par_str) if par_str.isdigit() else None

                hole, created = Hole.objects.get_or_create(course=course, number=hole_no, defaults={"par": par or 4})
                if created: holes_created += 1
                elif par and hole.par != par:
                    hole.par = par
                    hole.save(update_fields=["par"])
                    holes_updated += 1

                # Yardages
                if k_yards_combo:
                    yards_str = norm(row.get(k_yards_combo))
                    desig_str = norm(row.get(k_designation) or "").lower()
                    yards = int(yards_str) if yards_str.isdigit() else None
                    if yards is None:
                        continue
                    ty, created = TeeYardage.objects.get_or_create(tee=tee, hole=hole, defaults={"yards": yards, "designation": desig_str})
                    if created:
                        yards_created += 1
                    else:
                        changed = False
                        if ty.yards != yards:
                            ty.yards = yards; changed = True
                        if ty.designation != desig_str:
                            ty.designation = desig_str; changed = True
                        if changed and not dry:
                            ty.save()
                        yards_updated += int(changed)
                else:
                    yards_str = norm(row.get(k_yards_single))
                    yards = int(yards_str) if yards_str.isdigit() else None
                    if yards is None:
                        continue
                    ty, created = TeeYardage.objects.get_or_create(tee=tee, hole=hole, defaults={"yards": yards})
                    if created:
                        yards_created += 1
                    else:
                        if ty.yards != yards and not dry:
                            ty.yards = yards
                            ty.save(update_fields=["yards"])
                        yards_updated += int(ty.yards == yards)

            self.stdout.write(f"Holes created: {holes_created} updated: {holes_updated}")
            self.stdout.write(f"Yardages created: {yards_created} updated: {yards_updated}")

            # Optionally set EventSettings pointers
            if opts.get("set_event"):
                tee_for_event = (opts.get("tee_for_event") or tee_name).strip()
                set_tee = TeeBox.objects.filter(course=course, name__iexact=tee_for_event).first() or tee
                if opts.get("dry_run"):
                    self.stdout.write("Would update EventSettings (dry-run).")
                else:
                    es = EventSettings.load()
                    es.scoring_course = course
                    es.scoring_tee    = set_tee
                    es.save(update_fields=["scoring_course","scoring_tee"])
                    self.stdout.write("EventSettings updated.")
