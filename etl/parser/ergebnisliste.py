"""Parser for nCara/NOVENTI 'Tagestourenplan (Ergebnisliste)' PDF exports.

Layout (fixed x-bands, two time-rows per visit):
    Fahrzeit(~45) | Beginn(~83) | Ende(~116) | Dauer(~152) | patient/codes(~183) | Notizen(>=440)
    row 1 = ACTUAL (recorded execution)  ·  row 2 = PLANNED
    'þ <code> ...' lines = services performed; notes wrap in the Notizen column.
    Pseudo-blocks 'Vorbereitung' and 'Fahrtenbuch [...]' are prep/logbook, not visits.

Output: one canonical tours CSV (the app's / load_tours.py format) with extra
columns (per-leg actual+planned travel, planned times, notes) that the existing
consumers simply ignore. lat/lng joined from the KB patients via aliases.

Usage:  python3 ergebnisliste.py <pdf> [more.pdf ...] [-o out.csv]
"""
import os, re, sys, csv, unicodedata
import fitz  # pymupdf

# x-band boundaries (points). Header row confirms: Fahrzeit 45, Beginn 83,
# Ende 116, Dauer 152, main 183, Notizen 442.
X_FZ, X_BEG, X_END, X_DUR, X_NOTE = 62, 99, 135, 168, 438
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
TOUR_RE = re.compile(r"Tour: (.+?) am \w+, (\d\d)\.(\d\d)\.(\d\d)$")
CODE_RE = re.compile(r"^([PKH]\d+\w*)\b(.*)$")
PHONE_RE = re.compile(r"^0?1?[\d\s/-]{6,}$")
PID_RE = re.compile(r"^\[/(\d+)\]$")


def norm(s):
    return " ".join(str(s).strip().split()).rstrip(",").lower()


def slug(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def hhmm_min(t):
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def lines_of(page):
    """Group words into visual lines (y-clusters), each a list of (x, text)."""
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    out, cur, cury = [], [], None
    for x0, y0, x1, y1, txt, *_ in words:
        if cury is None or y0 - cury > 4:
            if cur:
                out.append((cury, cur))
            cur, cury = [], y0
        cur.append((x0, txt))
        cury = max(cury, y0)
    if cur:
        out.append((cury, cur))
    return [(y, sorted(c)) for y, c in out]


def fields(cells):
    """Split a line's cells into the fixed columns."""
    f = {"fz": None, "beg": None, "end": None, "dur": None, "main": [], "note": []}
    for x, t in cells:
        if x < X_FZ and TIME_RE.match(t):
            f["fz"] = t
        elif x < X_BEG and TIME_RE.match(t):
            f["beg"] = t
        elif x < X_END and TIME_RE.match(t):
            f["end"] = t
        elif x < X_DUR and TIME_RE.match(t):
            f["dur"] = t
        elif x < X_NOTE:
            f["main"].append((x, t))
        else:
            f["note"].append(t)
    return f


SKIP_PREFIXES = (
    "Sozialstation", "Wilhelmsruher Damm 142", "Tel.030", "IK:", "Seite",
    "Tagestourenplan", "Tagestourplan", "Zeiten", "Kennzeichen", "Km Stand",
    "Mandant:", "©", "Gedruckt", "Hiermit erkläre", "Datum", "Unterschrift",
    "Fahrzeit", "461102130",
)


class Visit:
    def __init__(self):
        self.ident = []      # raw identity lines (name, address, phone)
        self.pid_hint = None
        self.a = {}          # actual: fz/beg/end/dur
        self.p = {}          # planned
        self.codes = []
        self.notes = []

    @property
    def has_actual(self):
        return bool(self.a)

    def identity(self):
        full = " ".join(self.ident).strip()
        return full

    def patient_and_address(self):
        full = self.identity()
        parts = [p.strip() for p in full.split(",")]
        phone = None
        if parts and (PHONE_RE.match(parts[-1].replace(" ", "")) or parts[-1].replace(" ", "").isdigit()):
            phone = parts.pop()
        if full.startswith("WG "):
            return full, "", phone, True
        name = ", ".join(parts[:2]) if len(parts) >= 2 else full
        addr = ", ".join(parts[2:]) if len(parts) > 2 else ""
        return name, addr, phone, False


def parse_pdf(path):
    doc = fitz.open(path)
    tours = []          # {label,date,nurse,period,visits[],prep,end_seen}
    cur = None
    visit = None
    skip_next_timerow = False

    def close_visit():
        nonlocal visit
        if visit and visit.has_actual and visit.ident:
            cur["visits"].append(visit)
        visit = None

    for pno in range(doc.page_count):
        for y, cells in lines_of(doc[pno]):
            text = " ".join(t for _, t in cells)
            m = TOUR_RE.search(text)
            if m:
                close_visit()
                label = m.group(1).strip()
                date = f"20{m.group(4)}-{m.group(3)}-{m.group(2)}"
                if cur and cur["label"] == label and cur["date"] == date:
                    continue          # page-break repeat of the same tour header
                lm = re.match(r"(Früh|Spät|Nacht)?\s*\d*\s*(PFK|PHK|PK)?,?\s*(.+)$", label)
                nurse = lm.group(3).strip() if lm else label
                period = (lm.group(1) or "").strip() if lm else ""
                cur = {"label": label, "date": date, "nurse": nurse,
                       "period": period, "visits": [], "prep_start": None, "complete": False}
                tours.append(cur)
                continue
            if cur is None:
                continue
            if "Gesamt-km" in text:
                close_visit()
                cur["complete"] = True
                continue
            if any(text.startswith(p) for p in SKIP_PREFIXES):
                continue
            if text.startswith("Tour:"):       # repeated sub-header without date
                continue

            f = fields(cells)
            timerow = any(f[k] for k in ("fz", "beg", "end", "dur"))
            main_txt = " ".join(t for _, t in f["main"]).strip()

            # pseudo-blocks (prep, logbook, admin time): consume their actual
            # row now and their planned row next — they are not patient visits.
            PSEUDO = ("Vorbereitung", "Fahrtenbuch", "Koordinationszeit",
                      "Pause", "Übergabe", "Besprechung", "Teamsitzung", "Büro")
            if any(main_txt.startswith(p) for p in PSEUDO):
                if main_txt.startswith("Vorbereitung") and f["beg"]:
                    cur["prep_start"] = f["beg"]
                close_visit()
                skip_next_timerow = True
                continue

            is_code = bool(f["main"]) and f["main"][0][1] == "þ"
            if is_code and visit:
                # one line can hold two 'þ CODE label' groups
                segs = re.split(r"\s*þ\s*", main_txt)
                for s in segs:
                    s = s.strip()
                    if not s:
                        continue
                    cm = CODE_RE.match(s)
                    if cm:
                        visit.codes.append(cm.group(1))

            if timerow:
                if skip_next_timerow and not main_txt:
                    skip_next_timerow = False
                elif visit and visit.has_actual and not visit.p and not (main_txt and not is_code):
                    visit.p = {k: f[k] for k in ("fz", "beg", "end", "dur")}
                else:
                    close_visit()
                    visit = Visit()
                    visit.a = {k: f[k] for k in ("fz", "beg", "end", "dur")}
                    if main_txt and not is_code:
                        visit.ident.append(main_txt)
            elif f["main"] and not is_code:
                pm = PID_RE.match(main_txt)
                if visit and pm:
                    visit.pid_hint = pm.group(1)
                elif visit and (not visit.p or not visit.ident):
                    # identity continuation (wrapped address / phone)
                    visit.ident.append(main_txt)
            for n in f["note"]:
                if visit and n != "-":
                    visit.notes.append(n)
    close_visit()
    return tours


def load_coords():
    """patient name (norm) -> (lat,lng,patient_id) from the KB, via aliases."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        envf = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(envf):
            for ln in open(envf):
                if ln.startswith("DATABASE_URL="):
                    url = ln.split("=", 1)[1].strip()
    if not url:
        print("WARN: no DATABASE_URL — emitting without coordinates")
        return {}
    import psycopg2
    c = psycopg2.connect(url); cur = c.cursor()
    cur.execute("""select a.alias_norm, p.lat, p.lng, p.patient_id
                   from patient_aliases a join patients p using (patient_id)""")
    out = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    cur.execute("select full_name, lat, lng, patient_id from patients")
    for full, lat, lng, pid in cur.fetchall():
        out.setdefault(norm(full), (lat, lng, pid))
    c.close()
    return out


def emit_csv(tours, out_path):
    coords = load_coords()
    cols = ["tourId", "nurseName", "visitSequence", "visitId", "patientName",
            "patientAddress", "latitude", "longitude", "shiftStart", "shiftEnd",
            "visitTime", "visitDurationMin", "travelTimeMin", "dateOfService", "codes",
            "travelToVisitMin", "plannedTime", "plannedDurationMin",
            "plannedTravelMin", "notes", "wg"]
    n_rows = n_coord = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for t in tours:
            vs = t["visits"]
            if not vs:
                continue
            tid = f"actual_{t['date']}_{slug(t['label'])}"
            starts = [v.a.get("beg") for v in vs if v.a.get("beg")]
            ends = [v.a.get("end") for v in vs if v.a.get("end")]
            shift_start = t["prep_start"] or (starts[0] if starts else "")
            shift_end = ends[-1] if ends else ""
            total_travel = sum(hhmm_min(v.a["fz"]) or 0 for v in vs if v.a.get("fz"))
            for i, v in enumerate(vs, 1):
                name, addr, phone, wg = v.patient_and_address()
                lat = lng = ""
                hit = coords.get(norm(name))
                if hit and hit[0] is not None:
                    lat, lng = hit[0], hit[1]
                    n_coord += 1
                a_dur = hhmm_min(v.a.get("dur") or "") if v.a.get("dur") else None
                p_dur = hhmm_min(v.p.get("dur") or "") if v.p.get("dur") else None
                w.writerow({
                    "tourId": tid, "nurseName": t["nurse"], "visitSequence": i,
                    "visitId": f"{tid}_v{i}", "patientName": name,
                    "patientAddress": addr, "latitude": lat, "longitude": lng,
                    "shiftStart": shift_start, "shiftEnd": shift_end,
                    "visitTime": v.a.get("beg") or v.p.get("beg") or "",
                    "visitDurationMin": a_dur if a_dur is not None else (p_dur or 0),
                    "travelTimeMin": total_travel, "dateOfService": t["date"],
                    "codes": ";".join(v.codes),
                    "travelToVisitMin": hhmm_min(v.a["fz"]) if v.a.get("fz") else "",
                    "plannedTime": v.p.get("beg") or "",
                    "plannedDurationMin": p_dur if p_dur is not None else "",
                    "plannedTravelMin": hhmm_min(v.p["fz"]) if v.p.get("fz") else "",
                    "notes": " ".join(v.notes), "wg": "1" if wg else "",
                })
                n_rows += 1
    return n_rows, n_coord


def main():
    args = [a for a in sys.argv[1:]]
    out = None
    if "-o" in args:
        i = args.index("-o"); out = args[i + 1]; del args[i:i + 2]
    if not args:
        sys.exit("usage: ergebnisliste.py <pdf> [...] [-o out.csv]")
    all_tours = []
    for p in args:
        ts = parse_pdf(p)
        all_tours.extend(ts)
        print(f"\n=== {os.path.basename(p)} → {len(ts)} tours ===")
        for t in ts:
            vs = t["visits"]
            care = sum(hhmm_min(v.a["dur"]) or 0 for v in vs if v.a.get("dur"))
            legs = sum(hhmm_min(v.a["fz"]) or 0 for v in vs if v.a.get("fz"))
            miss_t = sum(1 for v in vs if not v.a.get("beg"))
            wg = sum(1 for v in vs if v.identity().startswith("WG "))
            noc = sum(1 for v in vs if not v.codes)
            flag = "" if t["complete"] else "  ⚠ NO END FOOTER (truncated?)"
            print(f"  {t['date']} {t['label']:42s} {len(vs):3d} visits | care {care:4d}m | legs {legs:3d}m"
                  f" | no-clock {miss_t} | wg {wg} | no-code {noc}{flag}")
    if out:
        n, nc = emit_csv(all_tours, out)
        print(f"\nwrote {out}: {n} rows, {nc} with coordinates")


if __name__ == "__main__":
    main()
