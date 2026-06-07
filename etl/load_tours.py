"""Load parsed tour rows (sample-tours-ambulant.csv format) into
nurses / tours / visits / visit_services. Joins patients via patient_aliases.
Run after load_structured.py.  Needs DATABASE_URL + etl/data/tours.csv.
"""
import os, csv, re, sys
import psycopg2
from psycopg2.extras import execute_values

csv.field_size_limit(10**7)
DATA = os.path.join(os.path.dirname(__file__), "data")
DB = os.environ.get("DATABASE_URL")


def norm(s):
    return " ".join(str(s).strip().split()).rstrip(",").lower()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")


def num(x):
    try:
        return int(float(x))
    except Exception:
        return None


def main():
    if not DB:
        sys.exit("set DATABASE_URL")
    rows = list(csv.DictReader(open(os.path.join(DATA, "tours.csv"))))
    c = psycopg2.connect(DB); cur = c.cursor()
    cur.execute("select alias_norm, patient_id from patient_aliases")
    amap = dict(cur.fetchall())

    nurses, tours, visits, vserv, codes, coords = {}, {}, [], set(), set(), {}
    unmatched = set()
    for r in rows:
        nid = slug(r["nurseName"])
        nurses[nid] = r["nurseName"]
        tid = r["tourId"]
        if tid not in tours:
            hh = (r.get("shiftStart") or "0").split(":")[0]
            period = "morning" if (hh.isdigit() and int(hh) < 12) else "evening"
            tours[tid] = (r["dateOfService"], nid, period, r.get("shiftStart"), r.get("shiftEnd"))
        pid = amap.get(norm(r["patientName"]))
        if not pid:
            unmatched.add(norm(r["patientName"]))
        visits.append((r["visitId"], tid, pid, r["dateOfService"], num(r["visitSequence"]),
                       r.get("visitTime"), num(r.get("visitDurationMin")), num(r.get("travelTimeMin"))))
        if pid and r.get("latitude"):
            try:
                coords[pid] = (float(r["latitude"]), float(r["longitude"]))
            except Exception:
                pass
        for code in (r.get("codes") or "").split(";"):
            if code:
                codes.add(code)
                vserv.add((r["visitId"], code))

    # ensure every code exists in lk_codes (FK); add unknowns with null price
    execute_values(cur, """insert into lk_codes (code,sgb,kind) values %s
        on conflict (code) do nothing""",
        [(cd, "XI" if cd[0] == "P" else "V", "euro") for cd in codes])
    execute_values(cur, "insert into nurses (nurse_id,name) values %s on conflict (nurse_id) do nothing",
                   [(k, v) for k, v in nurses.items()])
    execute_values(cur, """insert into tours (tour_id,date,nurse_id,period,shift_start,shift_end,source_file)
        values %s on conflict (tour_id) do nothing""",
        [(t, *v, "tour-planner") for t, v in tours.items()])
    execute_values(cur, """insert into visits
        (visit_id,tour_id,patient_id,date,sequence,arrival_time,service_minutes,travel_minutes)
        values %s on conflict (visit_id) do nothing""", visits, page_size=1000)
    execute_values(cur, """insert into visit_services (visit_id,code,quantity)
        values %s on conflict (visit_id,code) do nothing""",
        [(v, cd, 1) for v, cd in vserv], page_size=1000)
    for pid, (la, ln) in coords.items():
        cur.execute("update patients set lat=%s, lng=%s where patient_id=%s and lat is null", (la, ln, pid))
    c.commit()
    print(f"nurses {len(nurses)} | tours {len(tours)} | visits {len(visits)} "
          f"| visit_services {len(vserv)} | codes {len(codes)} | unmatched names {len(unmatched)}")
    cur.close(); c.close()


if __name__ == "__main__":
    main()
