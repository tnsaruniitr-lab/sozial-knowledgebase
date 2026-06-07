-- sozial-knowledgebase — schema (Postgres / Supabase, region eu-central-1)
-- patients = master anchor (from pg.xls); every source joins via patient_id,
-- with patient_aliases resolving name variants so monthly re-loads stay clean.

create table if not exists patients (
  patient_id   text primary key,            -- slug e.g. 'damdounis-georgios'
  nachname     text not null,
  vorname      text,
  full_name    text not null,               -- "Nachname, Vorname"
  geburtsdatum date,
  geschlecht   text,
  pflegegrad   smallint,                    -- 0..5, null = none
  auftragstyp  text,                        -- 'SGB V' | 'SGB XI' | 'SGB V+XI' | 'Sonstige'
  sachleistung_eligible boolean,            -- PG>=2 AND has XI
  strasse text, plz text, ort text,
  lat double precision, lng double precision,
  kostentraeger text,
  aufnahme date,
  active boolean default true,
  created_at timestamptz default now()
);

create table if not exists patient_aliases (
  alias_norm text primary key,              -- normalized name variant
  patient_id text references patients(patient_id),
  source text,
  created_at timestamptz default now()
);

create table if not exists nurses (
  nurse_id text primary key,
  name text not null,
  role text
);

-- LK price catalogue (real €/unit derived from tour1 billed ÷ qty)
create table if not exists lk_codes (
  code text primary key,                    -- 'P02','K26a',...
  label text,
  sgb text,                                 -- 'XI' (Grundpflege) | 'V' (Behandlungspflege)
  kind text,                                -- 'points' | 'euro'
  value numeric,
  euro_per_unit numeric,
  source text
);

create table if not exists tours (
  tour_id text primary key,
  date date not null,
  nurse_id text references nurses(nurse_id),
  period text,                              -- 'morning' | 'evening'
  shift_start text, shift_end text,
  source_file text,
  created_at timestamptz default now()
);
create index if not exists idx_tours_date on tours(date);

create table if not exists visits (
  visit_id text primary key,
  tour_id text references tours(tour_id),
  patient_id text references patients(patient_id),
  date date not null,
  sequence smallint,
  arrival_time text,
  service_minutes int,
  travel_minutes int,
  source_file text
);
create index if not exists idx_visits_patient on visits(patient_id);
create index if not exists idx_visits_date on visits(date);

create table if not exists visit_services (
  visit_id text references visits(visit_id),
  code text references lk_codes(code),
  quantity int default 1,
  primary key (visit_id, code)
);

-- Billing (bill.xls) — one row per invoice line
create table if not exists invoices (
  invoice_id text primary key,
  patient_id text references patients(patient_id),
  service_month text not null,              -- 'YYYY-MM'
  rg_date date,
  payer_type text,
  kostentraeger text,
  paragraph text,                           -- '36' | '39' | '45' | 'V' | ...
  amount_eur numeric,
  mon_budget numeric,
  mon_rest numeric,
  verbrauch_pct numeric,
  source_file text
);
create index if not exists idx_invoices_patient on invoices(patient_id);
create index if not exists idx_invoices_month on invoices(service_month);

-- Budget pots as a MONTHLY time-series (from 39-june.xls / 45-june.xls).
-- "Expiring 30 June" is derived in views (§45 carryover) from the latest month.
create table if not exists budgets (
  patient_id text references patients(patient_id),
  paragraph text not null,                  -- '36' | '39' | '45'
  month text not null,                      -- 'YYYY-MM' snapshot
  budget_eur numeric,                       -- available balance that month
  minderung_eur numeric,
  valid_from date,
  source_file text,
  primary key (patient_id, paragraph, month)
);
create index if not exists idx_budgets_para_month on budgets(paragraph, month);

-- Service-level authorizations (Verordnung / Muster 12) — populated later from
-- the prescription PDFs; unlocks "authorised but not serviced".
create table if not exists authorizations (
  auth_id text primary key,
  patient_id text references patients(patient_id),
  code text,
  paragraph text,                           -- 'V' (Muster 12) | 'XI'
  valid_from date,
  valid_to date,
  qty_prescribed numeric,
  status text,
  source_doc text
);
create index if not exists idx_auth_patient on authorizations(patient_id);
