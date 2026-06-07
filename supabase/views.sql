-- sozial-knowledgebase — seed "questions" as views.
-- (Best-effort; tune thresholds/joins after the first load. €40/h placeholder rate.)

-- Q1: "Which budgets are at risk of expiring?" — latest balance per pot.
--     §45 Entlastungsbetrag carryover expires 30 June; §39 Verhinderungspflege annual.
create or replace view v_budget_expiry as
with latest as (
  select distinct on (patient_id, paragraph)
         patient_id, paragraph, month as as_of, budget_eur
  from budgets
  order by patient_id, paragraph, month desc
)
select l.patient_id, p.full_name, l.paragraph, l.as_of, l.budget_eur as balance_eur,
       case l.paragraph when '45' then date '2026-06-30'
                        when '39' then date '2026-12-31' end as expires_on
from latest l join patients p using (patient_id)
where l.budget_eur > 0
order by l.budget_eur desc;

-- Q2: "Tour efficiency" — care vs travel per tour.
-- NB: travel_minutes is the tour TOTAL repeated on every visit row, so use
-- max() (one value per tour), not sum(); care is per-visit so sum() is right.
create or replace view v_tour_efficiency as
select t.tour_id, t.date, n.name as nurse,
       sum(v.service_minutes) as care_min,
       max(v.travel_minutes)  as travel_min,
       round(100.0 * sum(v.service_minutes)
             / nullif(sum(v.service_minutes) + max(v.travel_minutes), 0), 1) as care_pct
from tours t
join visits v using (tour_id)
left join nurses n using (nurse_id)
group by t.tour_id, t.date, n.name;

-- Q3: "Per-customer P&L / loss-makers" — billed revenue vs care+travel cost.
-- Travel is a tour total (repeated per row), so allocate each visit an equal
-- share = tour_travel / visits_in_tour, then sum per patient. (Marginal-detour
-- attribution is a later refinement.)
create or replace view v_customer_pnl as
with rev as (
  select patient_id, service_month, sum(amount_eur) as revenue
  from invoices group by patient_id, service_month
),
tour_travel as (
  select tour_id, max(travel_minutes) as t_travel, count(*) as n
  from visits group by tour_id
),
cost as (
  select v.patient_id, to_char(v.date, 'YYYY-MM') as month,
         round(sum(coalesce(v.service_minutes,0)
                   + coalesce(tt.t_travel,0) / nullif(tt.n,0)) / 60.0 * 40, 2) as cost
  from visits v join tour_travel tt using (tour_id)
  group by v.patient_id, to_char(v.date, 'YYYY-MM')
)
select p.full_name, r.service_month, r.revenue, coalesce(c.cost, 0) as cost,
       round(r.revenue - coalesce(c.cost, 0), 2) as profit
from rev r
join patients p using (patient_id)
left join cost c on c.patient_id = r.patient_id and c.month = r.service_month
order by profit asc;

-- Q4: "§36 budget headroom" — PG entitlement vs Grundpflege spend per month.
create or replace view v_budget_headroom as
select *,
       round(100.0 * spent / nullif(budget36, 0)) as spend_pct,
       (budget36 - spent) as unused
from (
  select p.patient_id, p.full_name, p.pflegegrad,
         case p.pflegegrad when 2 then 796 when 3 then 1497
                           when 4 then 1859 when 5 then 2299 else 0 end as budget36,
         sp.month,
         coalesce(sp.grundpflege, 0) as spent
  from patients p
  left join (
    select v.patient_id, to_char(v.date, 'YYYY-MM') as month,
           round(sum(vs.quantity * lk.euro_per_unit), 2) as grundpflege
    from visits v
    join visit_services vs using (visit_id)
    join lk_codes lk on lk.code = vs.code
    where lk.sgb = 'XI'
    group by v.patient_id, to_char(v.date, 'YYYY-MM')
  ) sp on sp.patient_id = p.patient_id
  where p.sachleistung_eligible
) x;

-- Q-a / Q-b: tour & nurse utilisation. care_util = care/(care+travel);
-- shift_util = (care+travel)/paid-shift. Per tour; roll up by nurse+month in the query.
create or replace view v_utilisation as
with base as (
  select t.tour_id, t.date, to_char(t.date,'YYYY-MM') as month, coalesce(n.name,'?') as nurse,
         sum(v.service_minutes) as care_min,
         max(v.travel_minutes)  as travel_min,
         (split_part(t.shift_end,':',1)::int*60 + split_part(t.shift_end,':',2)::int)
           - (split_part(t.shift_start,':',1)::int*60 + split_part(t.shift_start,':',2)::int) as shift_raw
  from tours t join visits v using (tour_id) left join nurses n using (nurse_id)
  where t.shift_start ~ '^[0-9]' and t.shift_end ~ '^[0-9]'
  group by t.tour_id, t.date, n.name, t.shift_start, t.shift_end
)
-- care_util is the meaningful metric (care vs travel/idle). shift_util is
-- ~100% by construction (the recorded shift = the visit span), so treat it as a
-- sanity column, not a KPI. Degenerate tours (no care / absurd span) excluded.
select * from (
  select tour_id, date, month, nurse, care_min, travel_min,
         case when shift_raw <= 0 then shift_raw + 1440 else shift_raw end as shift_min,
         round(100.0*care_min/nullif(care_min+travel_min,0),1) as care_util,
         round(100.0*(care_min+travel_min)
               /nullif(case when shift_raw<=0 then shift_raw+1440 else shift_raw end,0),1) as shift_util
  from base
) u
where care_min > 0 and shift_min between 30 and 960;

-- Q-c / Q-d: budget utilisation per patient per paragraph.
-- §36 = monthly Sachleistung (PG-standard budget vs Grundpflege spend, latest month);
-- §39/§45 = pot drawn over the loaded window (first balance → latest balance).
create or replace view v_budget_utilisation as
with pot as (
  select patient_id, paragraph,
    (array_agg(budget_eur order by month asc))[1]  as first_bal,
    (array_agg(budget_eur order by month desc))[1] as last_bal
  from budgets where paragraph in ('39','45') group by patient_id, paragraph
)
select p.full_name, b.paragraph,
       round(b.first_bal,0)                              as budget_eur,
       round(greatest(b.first_bal-b.last_bal,0),0)       as used_eur,
       round(b.last_bal,0)                               as remaining_eur,
       round(100.0*greatest(b.first_bal-b.last_bal,0)/nullif(b.first_bal,0),0) as util_pct
from pot b join patients p using (patient_id)
union all
select x.full_name, '36' as paragraph,
       x.budget36 as budget_eur, round(x.spent,0) as used_eur,
       round(x.budget36-x.spent,0) as remaining_eur,
       round(100.0*x.spent/nullif(x.budget36,0),0) as util_pct
from (
  select distinct on (p.patient_id) p.patient_id, p.full_name,
    case p.pflegegrad when 2 then 796 when 3 then 1497 when 4 then 1859 when 5 then 2299 else 0 end as budget36,
    coalesce(sp.grundpflege,0) as spent
  from patients p
  left join (
    select v.patient_id, to_char(v.date,'YYYY-MM') as month,
           sum(vs.quantity*lk.euro_per_unit) as grundpflege
    from visits v join visit_services vs using (visit_id) join lk_codes lk on lk.code=vs.code
    where lk.sgb='XI' group by v.patient_id, to_char(v.date,'YYYY-MM')
  ) sp on sp.patient_id = p.patient_id
  where p.sachleistung_eligible
  order by p.patient_id, sp.month desc nulls last
) x;
