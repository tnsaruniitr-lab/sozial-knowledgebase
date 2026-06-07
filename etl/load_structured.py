"""Load the STRUCTURED care sources into the sozial-knowledgebase Postgres.

Covers: patients (pg.xls), LK prices (lk_codes), budgets (39-june/45-june),
invoices (bill.xls). Tours/visits (the print-layout parser) is a follow-up.

Usage:
  export DATABASE_URL='postgresql://…eu-central-1.pooler.supabase.com:5432/postgres'
  # place source files in etl/data/ : pg.xls, bill.xls, 39-june.xls, 45-june.xls
  python etl/load_structured.py
Idempotent: every table upserts on its natural key, so re-running is safe.
"""
import os, re, sys, warnings
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

warnings.filterwarnings("ignore")
DATA = os.path.join(os.path.dirname(__file__), "data")
DB = os.environ.get("DATABASE_URL")
STD36 = {796, 1497, 1859, 2299}  # §36 Sachleistung standard amounts

# Real €/unit derived from tour1 (Apr 2026 billed ÷ qty); K = Berlin HKP placeholders.
PRICES = {
    "P01": ("XI", 27.09, "erweiterte kleine Körperpflege"),
    "P02": ("XI", 18.06, "kleine Körperpflege"),
    "P03a": ("XI", 40.67, "erweiterte große Körperpflege"),
    "P03b": ("XI", 54.17, "große Körperpflege m. Abw."),
    "P04": ("XI", 36.12, "große Körperpflege"),
    "P05": ("XI", 9.03, "Lagern/Betten"),
    "P06": ("XI", 22.62, "Hilfe bei der Nahrungsaufnahme"),
    "P07a": ("XI", 7.19, "Darm-/Blasenentleerung"),
    "P07b": ("XI", 18.06, "Darm-/Blasenentleerung m. Intimpflege"),
    "P09": ("XI", 54.17, "Begleitung außer Haus"),
    "P11a": ("XI", 7.89, "Aufräumen der Wohnung"),
    "P11b": ("XI", 23.67, "Reinigen der Wohnung"),
    "P12": ("XI", 42.08, "Wäsche wechseln/waschen"),
    "P13": ("XI", 21.04, "Einkaufen"),
    "P14": ("XI", 23.67, "warme Mahlzeit zubereiten"),
    "P15": ("XI", 7.89, "sonstige Mahlzeit zubereiten"),
    "P16a": ("XI", 61.36, "Erstbesuch"),
    "P16b": ("XI", 26.30, "Folgebesuch"),
    "P18": ("XI", 77.32, "Pflegeeinsatz §37.3"),
    "P19a": ("XI", 200.04, "Versorgung WG (PG4/5)"),
    "P19b": ("XI", 100.02, "Versorgung WG (m. Abw.)"),
    "P20": ("XI", 8.77, "Betreuungsmaßnahmen"),
    # SGB V Behandlungspflege (billed to Krankenkasse; placeholders)
    "K10": ("V", 3.00, "Blutdruckmessung"), "K11": ("V", 2.73, "Blutzuckermessung"),
    "K17": ("V", 5.52, "Inhalation"), "K18b": ("V", 4.57, "Injektion s.c."),
    "K18c": ("V", 4.57, "Insulininjektion"), "K26a": ("V", 3.75, "Medikamentengabe"),
    "K26c": ("V", 3.75, "Augentropfen"), "K26i": ("V", 4.00, "Schmerzpflaster"),
    "K26j1": ("V", 4.00, "Med. Dosette richten"), "K27": ("V", 9.00, "PEG-Versorgung"),
    "K28": ("V", 9.00, "Stomabehandlung"), "K29": ("V", 12.00, "Trachealkanüle"),
    "K31b1": ("V", 5.33, "Kompressionsverband anlegen"),
    "K31b2": ("V", 2.66, "Kompressionsverband ablegen"),
    "K31c1": ("V", 9.33, "Kompressionsstrümpfe anziehen"),
    "K31c2": ("V", 9.33, "Kompressionsstrümpfe ausziehen"),
}


def norm(s):
    return " ".join(str(s).strip().split()).rstrip(",").lower()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")


def cell(v):
    return None if (v is None or (isinstance(v, float) and pd.isna(v)) or str(v) == "nan") else v


def conn():
    if not DB:
        sys.exit("Set DATABASE_URL (Supabase Frankfurt connection string).")
    return psycopg2.connect(DB)


def f(path):
    return os.path.join(DATA, path)


def load_prices(cur):
    rows = [(c, lbl, sgb, "euro", None, eur, "tour1 Apr2026 / Berlin HKP")
            for c, (sgb, eur, lbl) in PRICES.items()]
    execute_values(cur, """insert into lk_codes (code,label,sgb,kind,value,euro_per_unit,source)
        values %s on conflict (code) do update set
        euro_per_unit=excluded.euro_per_unit, label=excluded.label, sgb=excluded.sgb""", rows)
    return len(rows)


def load_patients(cur):
    raw = pd.read_excel(f("pg.xls"), sheet_name=0, header=None)
    cols = [str(x).strip() for x in raw.iloc[1].tolist()]
    ci = lambda n: next((i for i, c in enumerate(cols) if c.lower() == n.lower()), None)
    iN, iV, iG, iGeb, iPG, iStr, iPlz, iOrt, iAuf, iAuf2 = (
        ci("Nachname"), ci("Vorname"), ci("Geschlecht"), ci("geb. am"), ci("Pflegegrad"),
        ci("Strasse"), ci("PLZ"), ci("Ort"), ci("Aufnahme"), ci("Auftragstyp"))
    rows, aliases, seen = [], [], {}
    for i in range(2, len(raw)):
        nn, vn = cell(raw.iloc[i, iN]), cell(raw.iloc[i, iV])
        if not nn and not vn:
            continue
        full = f"{nn or ''}, {vn or ''}".strip().strip(",").strip()
        pid = slug(full) or f"p{i}"
        if pid in seen:                       # disambiguate slug collisions
            seen[pid] += 1; pid = f"{pid}-{seen[pid]}"
        else:
            seen[pid] = 1
        pg = cell(raw.iloc[i, iPG]); auf = cell(raw.iloc[i, iAuf2]) or ""
        pgv = int(float(pg)) if pg is not None else None
        elig = bool(pgv and pgv >= 2 and "XI" in str(auf))
        rows.append((pid, str(nn or ""), str(vn or ""), full,
                     None, str(cell(raw.iloc[i, iG]) or "") or None, pgv, str(auf) or None, elig,
                     str(cell(raw.iloc[i, iStr]) or "") or None, str(cell(raw.iloc[i, iPlz]) or "") or None,
                     str(cell(raw.iloc[i, iOrt]) or "") or None, None))
        aliases.append((norm(full), pid, "pg.xls"))
    execute_values(cur, """insert into patients
        (patient_id,nachname,vorname,full_name,geburtsdatum,geschlecht,pflegegrad,auftragstyp,
         sachleistung_eligible,strasse,plz,ort,kostentraeger) values %s
        on conflict (patient_id) do update set pflegegrad=excluded.pflegegrad,
         auftragstyp=excluded.auftragstyp, sachleistung_eligible=excluded.sachleistung_eligible""", rows)
    execute_values(cur, """insert into patient_aliases (alias_norm,patient_id,source) values %s
        on conflict (alias_norm) do nothing""", aliases)
    return len(rows)


def _alias_map(cur):
    cur.execute("select alias_norm, patient_id from patient_aliases")
    return dict(cur.fetchall())


def load_budgets(cur):
    amap = _alias_map(cur)
    months = {11: "2026-03", 13: "2026-04", 15: "2026-05", 17: "2026-06"}
    out, miss = [], 0
    for fn, para in [("39-june.xls", "39"), ("45-june.xls", "45")]:
        if not os.path.exists(f(fn)):
            continue
        raw = pd.read_excel(f(fn), sheet_name=0, header=None)
        for i in range(3, len(raw)):
            nm = cell(raw.iloc[i, 0])
            if not nm or "," not in str(nm):
                continue
            pid = amap.get(norm(nm))
            if not pid:
                miss += 1; continue
            for bc, month in months.items():
                b = cell(raw.iloc[i, bc])
                if b is None:
                    continue
                m = cell(raw.iloc[i, bc + 1])
                out.append((pid, para, month, float(b),
                            float(m) if m is not None else None, fn))
    execute_values(cur, """insert into budgets
        (patient_id,paragraph,month,budget_eur,minderung_eur,source_file) values %s
        on conflict (patient_id,paragraph,month) do update set
         budget_eur=excluded.budget_eur, minderung_eur=excluded.minderung_eur""", out)
    return len(out), miss


def load_invoices(cur):
    amap = _alias_map(cur)
    raw = pd.read_excel(f("bill.xls"), sheet_name=0, header=None)
    out, miss = [], 0
    for i in range(1, len(raw)):
        nm = cell(raw.iloc[i, 2]); s = cell(raw.iloc[i, 19])
        if not nm or "," not in str(nm) or s is None:
            continue
        stapel = str(cell(raw.iloc[i, 16]) or "")
        if "Eingang" in stapel:               # skip payment-receipt rows
            continue
        pid = amap.get(norm(nm))
        if not pid:
            miss += 1; continue
        rg = cell(raw.iloc[i, 17]) or f"{i}"
        bud = cell(raw.iloc[i, 20]); para = "36" if (bud and float(bud) in STD36) else None
        out.append((f"{rg}-{i}", pid, stapel or "2026-04", None,
                    str(cell(raw.iloc[i, 3]) or "") or None, str(cell(raw.iloc[i, 5]) or "") or None,
                    para, float(s),
                    float(bud) if bud is not None else None,
                    float(cell(raw.iloc[i, 21])) if cell(raw.iloc[i, 21]) is not None else None,
                    float(cell(raw.iloc[i, 22])) if cell(raw.iloc[i, 22]) is not None else None,
                    "bill.xls"))
    execute_values(cur, """insert into invoices
        (invoice_id,patient_id,service_month,rg_date,payer_type,kostentraeger,paragraph,
         amount_eur,mon_budget,mon_rest,verbrauch_pct,source_file) values %s
        on conflict (invoice_id) do nothing""", out)
    return len(out), miss


def main():
    c = conn(); cur = c.cursor()
    print("lk_codes :", load_prices(cur))
    print("patients :", load_patients(cur))
    nb, mb = load_budgets(cur); print(f"budgets  : {nb} rows ({mb} unmatched names)")
    ni, mi = load_invoices(cur); print(f"invoices : {ni} rows ({mi} unmatched names)")
    c.commit(); cur.close(); c.close()
    print("done.")


if __name__ == "__main__":
    main()
