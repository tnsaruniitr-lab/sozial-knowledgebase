"""Parse the early-April week files (1–26) into a load_tours-compatible CSV.
27 Apr onward is already loaded, so we keep dates < 2026-04-27 and drop WG tours.
Output: etl/data/tours-april.csv  ->  feed to etl/load_tours.py
"""
import os, re, csv
from collections import defaultdict
import extract
from extract import hhmm_to_min, min_to_hhmm

HERE = os.path.dirname(__file__)
FILES = [f"{HERE}/conv/tour-apr{n}-new.xlsx" for n in (1, 8, 15, 22)]
OUT = os.path.join(HERE, "..", "data", "tours-april.csv")
SPLIT_GAP = 150
CODE = re.compile(r'[KP]\d{1,3}[a-z]?\d?')
HEADER = ["tourId", "nurseName", "visitSequence", "visitId", "patientName", "latitude",
          "longitude", "shiftStart", "shiftEnd", "visitTime", "visitDurationMin",
          "travelTimeMin", "dateOfService", "codes"]


def slug(s):
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-')


def main():
    visits = []
    for f in FILES:
        for p in extract.load_pages(f):
            visits += extract.parse_page(p)
    # early April only, exclude WG
    visits = [v for v in visits if v['date'] < '2026-04-27' and not v['is_wg']]
    tours = defaultdict(list)
    for v in visits:
        tours[(v['nurse'], v['date'])].append(v)
    rows = []
    for (nurse, date), dayvs in sorted(tours.items()):
        dayvs = sorted(dayvs, key=lambda x: (hhmm_to_min(x['arrive']) if x['arrive'] else 0, x['seq']))
        segs, cur = [], [dayvs[0]]
        for a, b in zip(dayvs, dayvs[1:]):
            gap = (hhmm_to_min(b['arrive']) - hhmm_to_min(a['arrive'])) if (a['arrive'] and b['arrive']) else 0
            (segs.append(cur) or (cur := [b])) if gap > SPLIT_GAP else cur.append(b)
        segs.append(cur)
        for si, seg in enumerate(segs):
            suffix = '' if len(segs) == 1 else f'_r{si + 1}'
            tid = f'actual_{date}_{slug(nurse)}{suffix}'
            arr = [hhmm_to_min(v['arrive']) for v in seg if v['arrive']]
            dep = [hhmm_to_min(v['depart']) for v in seg if v['depart']]
            ss = min_to_hhmm(min(arr)) if arr else ''
            se = min_to_hhmm(max(dep)) if dep else ss
            travel = 0
            for a, b in zip(seg, seg[1:]):
                if a['depart'] and b['arrive']:
                    travel += max(0, hhmm_to_min(b['arrive']) - hhmm_to_min(a['depart']))
            for seq, v in enumerate(seg, 1):
                rows.append({
                    "tourId": tid, "nurseName": nurse, "visitSequence": seq,
                    "visitId": f"{tid}_v{seq}", "patientName": v['name'],
                    "latitude": "", "longitude": "", "shiftStart": ss, "shiftEnd": se,
                    "visitTime": v['arrive'], "visitDurationMin": v['duration'],
                    "travelTimeMin": travel, "dateOfService": date,
                    "codes": ';'.join(CODE.findall(v.get('codes', ''))),
                })
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
    dates = sorted(set(r["dateOfService"] for r in rows))
    print(f"wrote {len(rows)} early-April rows / {len(tours)} nurse-days -> {OUT}")
    print(f"date span {dates[0]}..{dates[-1]} ({len(dates)} days)")


if __name__ == "__main__":
    main()
