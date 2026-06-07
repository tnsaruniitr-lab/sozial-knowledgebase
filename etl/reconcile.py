"""Reconcile name variants → patient_id, so budget/invoice/tour rows that didn't
match (stray dots, diacritics, spacing) now join. Safe: only adds an alias when a
source name strong-normalizes to exactly ONE master patient. Run, then re-run the
loaders (they're idempotent) to pick up the new matches.
"""
import os, re, csv, unicodedata, sys
import pandas as pd, psycopg2, warnings
from psycopg2.extras import execute_values
warnings.filterwarnings("ignore")
DATA = os.path.join(os.path.dirname(__file__), "data")
DB = os.environ.get("DATABASE_URL")
csv.field_size_limit(10**7)


def norm(s):
    return " ".join(str(s).strip().split()).rstrip(",").lower()


def strong(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def src_names():
    names = set()
    for fn in ("39-june.xls", "45-june.xls"):
        p = os.path.join(DATA, fn)
        if os.path.exists(p):
            r = pd.read_excel(p, header=None)
            names |= {str(r.iloc[i, 0]) for i in range(3, len(r)) if "," in str(r.iloc[i, 0])}
    p = os.path.join(DATA, "bill.xls")
    if os.path.exists(p):
        r = pd.read_excel(p, header=None)
        names |= {str(r.iloc[i, 2]) for i in range(1, len(r)) if "," in str(r.iloc[i, 2])}
    for fn in ("tours.csv", "tours-april.csv"):
        p = os.path.join(DATA, fn)
        if os.path.exists(p):
            for row in csv.DictReader(open(p)):
                names.add(row["patientName"])
    return {n for n in names if n and n != "nan"}


def main():
    c = psycopg2.connect(DB); cur = c.cursor()
    cur.execute("select patient_id, full_name from patients")
    pats = cur.fetchall()
    strong_idx, dupe = {}, set()
    for pid, full in pats:
        s = strong(full)
        if s in strong_idx:
            dupe.add(s)
        else:
            strong_idx[s] = pid
    for s in dupe:
        strong_idx.pop(s, None)            # drop ambiguous strong-keys
    cur.execute("select alias_norm from patient_aliases")
    have = {r[0] for r in cur.fetchall()}
    add, still = [], 0
    for nm in src_names():
        k = norm(nm)
        if k in have:
            continue
        pid = strong_idx.get(strong(nm))
        if pid:
            add.append((k, pid, "reconcile"))
            have.add(k)
        else:
            still += 1
    if add:
        execute_values(cur, "insert into patient_aliases (alias_norm,patient_id,source) values %s on conflict (alias_norm) do nothing", add)
    c.commit()
    print(f"added {len(add)} aliases; {still} source names still unmatched (not in master)")
    cur.close(); c.close()


if __name__ == "__main__":
    main()
