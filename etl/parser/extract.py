"""Parse the 'Touren ambulant' weekly print-layout (.xlsx) into one-row-per-visit
records. Geometric column detection: each day's time / name / PLZ columns are
found by content, then visits are read off the PLZ anchors."""
import openpyxl, re

TIME = re.compile(r'^\s*([01]?\d|2[0-3]):[0-5]\d\s*$')
PLZ = re.compile(r'^\s*(1\d{4})\s+Berlin\s*$', re.I)
HDR = re.compile(r'^(Mo|Di|Mi|Do|Fr|Sa|So)\.\s*(\d{2})\.(\d{2})\.(\d{2})')
PLAN = re.compile(r'Wochentourenplan für\s+(.*?)\s+(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*\((.*?)\)')
TOURHEAD = re.compile(r'^(\d+)\s*-\s*([A-Z]+-[A-Z]+\s*\d+)')
PHONE = re.compile(r'^[\d ]{5,}$')
ACTIVITY = {'vorbereitung', 'telefonat', 'pflegedoku', 'nachbereitung', 'dienstbesprechung'}


def hhmm_to_min(s):
    h, m = s.strip().split(':'); return int(h) * 60 + int(m)


def min_to_hhmm(m):
    return f'{m // 60:02d}:{m % 60:02d}'


def clean_name(frags):
    s = ' '.join(f.strip() for f in frags if f and f.strip())
    s = re.sub(r'\s+', ' ', s).replace(' ,', ',').replace('- ', '-').strip().rstrip(',').strip()
    return s


def load_pages(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    grid = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    starts = []
    for i, row in enumerate(grid):
        for v in row:
            if v and str(v).startswith('Wochentourenplan'):
                starts.append((i, str(v))); break
    starts.append((len(grid), None))
    pages = []
    for k in range(len(starts) - 1):
        r0, title = starts[k]; r1 = starts[k + 1][0]
        m = PLAN.search(title)
        if not m:
            continue
        pages.append({'r0': r0, 'r1': r1, 'nurse': m.group(1).strip(),
                      'role': m.group(4).strip(), 'grid': grid})
    return pages


def cell(grid, r, c):
    if 0 <= r < len(grid) and 0 <= c - 1 < len(grid[r]):
        v = grid[r][c - 1]
        return '' if v is None else str(v).strip()
    return ''


def find_day_columns(grid, r0, r1, hcol):
    win = [c for c in range(max(1, hcol - 4), hcol)]
    score = {c: {'t': 0, 'p': 0, 'n': 0} for c in win}
    for r in range(r0, r1):
        for c in win:
            v = cell(grid, r, c)
            if not v:
                continue
            if TIME.match(v):
                score[c]['t'] += 1
            elif PLZ.match(v):
                score[c]['p'] += 1
            elif re.match(r'^[A-ZÄÖÜ].+,', v) or re.match(r'^\d+\s*-\s*[A-Z]', v):
                score[c]['n'] += 1
    tcol = max(win, key=lambda c: score[c]['t'])
    if score[tcol]['t'] == 0:
        return None
    ncol = tcol + 1
    cand = [c for c in win if c not in (tcol, ncol)]
    pcol = max(cand, key=lambda c: score[c]['p']) if cand else tcol - 1
    return tcol, ncol, pcol


def is_name_token(v):
    return bool(v) and not TOURHEAD.match(v) and v.lower() not in ACTIVITY \
        and not v.lower().startswith(('spät wg', 'früh wg', 'wg ', 'tag wg', 'nacht wg'))


def grab_name(grid, ncol, street_row, floor):
    frags = []
    for r in range(street_row, max(floor, street_row - 6), -1):
        v = cell(grid, r, ncol)
        if not v:
            if frags:
                break
            continue
        if not is_name_token(v):
            break
        frags.insert(0, v)
    return clean_name(frags)


def parse_day(grid, r0, r1, date, tcol, ncol, pcol):
    plz_rows = [r for r in range(r0, r1) if PLZ.match(cell(grid, r, pcol))]
    if not plz_rows:
        return [], None, None, '', ''
    times = [(r, cell(grid, r, tcol)) for r in range(r0, r1) if TIME.match(cell(grid, r, tcol))]
    shift_start = times[0][1] if times else ''
    shift_end = times[-1][1] if times else ''
    is_wg = len(times) <= 2 and len(plz_rows) >= 3
    tour_no = vehicle = None
    for r in range(r0, r1):
        tm = TOURHEAD.match(cell(grid, r, ncol))
        if tm:
            tour_no = tour_no or tm.group(1); vehicle = vehicle or tm.group(2); break
    wg_dur = 0
    if is_wg and shift_start and shift_end:
        wg_dur = max(0, round((hhmm_to_min(shift_end) - hhmm_to_min(shift_start)) / len(plz_rows)))
    visits = []
    for i, Rp in enumerate(plz_rows):
        prev = plz_rows[i - 1] if i > 0 else r0 - 1
        nxt = plz_rows[i + 1] if i + 1 < len(plz_rows) else r1
        street = ''; street_row = Rp
        for r in range(Rp - 1, prev, -1):
            v = cell(grid, r, pcol)
            if v and not PLZ.match(v):
                street = v; street_row = r; break
        ns_row = None
        for r in range(nxt - 1, Rp, -1):
            v = cell(grid, r, pcol)
            if v and not PLZ.match(v):
                ns_row = r; break
        below = [cell(grid, r, pcol) for r in range(Rp + 1, ns_row if ns_row else nxt)
                 if cell(grid, r, pcol)]
        phone = below[0] if below and PHONE.match(below[0]) else ''
        codes = ' '.join(below[1:] if phone else below).strip()
        name = grab_name(grid, ncol, street_row, prev)
        if is_wg:
            arrive, depart, dur = shift_start, shift_end, wg_dur
        else:
            near = [t for (r, t) in times if prev < r <= Rp]
            arrive = near[-2] if len(near) >= 2 else (near[-1] if near else '')
            depart = near[-1] if near else arrive
            dur = max(0, hhmm_to_min(depart) - hhmm_to_min(arrive)) if arrive and depart else 0
        if not street:
            continue
        if not name:
            name = 'WG-Bewohner' if is_wg else 'Unbekannt'
        visits.append({
            'date': date, 'name': name, 'street': street.strip(),
            'plz': PLZ.match(cell(grid, Rp, pcol)).group(1),
            'address': f'{PLZ.match(cell(grid, Rp, pcol)).group(1)} Berlin, {street.strip()}',
            'phone': phone, 'codes': codes, 'arrive': arrive, 'depart': depart,
            'duration': dur, 'is_wg': is_wg,
        })
    arr = [hhmm_to_min(v['arrive']) for v in visits if v['arrive']]
    dep = [hhmm_to_min(v['depart']) for v in visits if v['depart']]
    if arr:
        shift_start = min_to_hhmm(min(arr))
        shift_end = min_to_hhmm(max(dep)) if dep else shift_start
    return visits, tour_no, vehicle, shift_start, shift_end


def parse_page(page):
    grid, r0, r1 = page['grid'], page['r0'], page['r1']
    hrow = next((r for r in range(r0, min(r0 + 6, r1))
                 if any(HDR.match(cell(grid, r, c)) for c in range(1, 40))), None)
    if hrow is None:
        return []
    headers = []
    for c in range(1, 40):
        m = HDR.match(cell(grid, hrow, c))
        if m:
            dd, mm, yy = m.group(2), m.group(3), m.group(4)
            headers.append((c, f'20{yy}-{mm}-{dd}'))
    claims = {}
    for hcol, date in headers:
        cols = find_day_columns(grid, hrow + 1, r1, hcol)
        if not cols:
            continue
        tcol = cols[0]; dist = abs(tcol - (hcol - 2))
        if tcol not in claims or dist < claims[tcol][0]:
            claims[tcol] = (dist, date, cols)
    out = []
    for tcol, (dist, date, cols) in claims.items():
        visits, tour_no, vehicle, sstart, send = parse_day(grid, hrow + 1, r1, date, *cols)
        for seq, v in enumerate(visits, 1):
            v.update({'nurse': page['nurse'], 'role': page['role'], 'tour_no': tour_no,
                      'vehicle': vehicle, 'seq': seq, 'shift_start': sstart, 'shift_end': send})
            out.append(v)
    return out
