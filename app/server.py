"""KB backend: serves the app + /ask (natural-language → SQL → read-only result).

The LLM only WRITES the SQL; numbers come from Postgres. Safety: single SELECT
only, executed in a READ ONLY transaction with a statement timeout + auto LIMIT.
Provider/key from env (LLM_PROVIDER=anthropic|openai, LLM_API_KEY=...); if unset,
/ask returns a clear 'not configured' message so the UI still runs.
"""
import os, re, json, time, uuid, pathlib, urllib.request
from flask import Flask, request, jsonify, send_from_directory
import psycopg2

DB = os.environ["DATABASE_URL"]
PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").lower()
KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "claude-3-5-sonnet-20241022" if PROVIDER == "anthropic" else "gpt-4o")
HERE = os.path.dirname(__file__)
BRIDGE = pathlib.Path(HERE) / "bridge"          # live in-session NL->SQL queue
ENGINE = BRIDGE / "ENGINE"                       # heartbeat touched by the Monitor loop
app = Flask(__name__, static_folder=HERE, static_url_path="")


def bridge_live():
    """True when the in-chat Claude loop is attached (heartbeat < 12s old)."""
    try:
        return (time.time() - ENGINE.stat().st_mtime) < 12
    except OSError:
        return False


def ask_via_bridge(question):
    """Hand the question to the in-session Claude via the file queue and wait
    for the answer it writes. No API key needed — full Claude quality, local."""
    qid = uuid.uuid4().hex[:12]
    (BRIDGE / "q").mkdir(parents=True, exist_ok=True)
    (BRIDGE / "a").mkdir(parents=True, exist_ok=True)
    (BRIDGE / "q" / f"{qid}.json").write_text(
        json.dumps({"id": qid, "question": question, "ts": time.time()}, ensure_ascii=False))
    ans = BRIDGE / "a" / f"{qid}.json"
    deadline = time.time() + 150
    while time.time() < deadline:
        if ans.exists():
            d = json.loads(ans.read_text())
            for p in (BRIDGE / "q" / f"{qid}.json", BRIDGE / "q" / f"{qid}.seen", ans):
                try:
                    p.unlink()
                except OSError:
                    pass
            if d.get("error"):
                return jsonify(error=d["error"], sql=d.get("sql", "")), 200
            return jsonify(sql=d.get("sql", ""), columns=d.get("columns", []), rows=d.get("rows", []))
        time.sleep(0.4)
    return jsonify(error="Zeitüberschreitung — die Live-Engine (Claude-Sitzung) hat nicht geantwortet."), 200

SCHEMA_CARD = """You write ONE PostgreSQL SELECT for a home-care (Pflegedienst) database.
Prefer these VIEWS:
- v_utilisation(tour_id,date,month,nurse,care_min,travel_min,shift_min,care_util,shift_util)
   care_util = care/(care+travel); month like '2026-04'. Lower care_util = more travel/idle.
- v_budget_utilisation(full_name,paragraph,budget_eur,used_eur,remaining_eur,util_pct)
   paragraph '36'|'39'|'45'; low util_pct / high remaining_eur = unused budget.
- v_budget_expiry(full_name,paragraph,as_of,balance_eur,expires_on)  (§45 expires 2026-06-30)
- v_customer_pnl(full_name,service_month,revenue,cost,profit)  profit<0 = loss-maker
- v_tour_efficiency(tour_id,date,nurse,care_min,travel_min,care_pct)
Base tables: patients(patient_id,full_name,pflegegrad,auftragstyp,lat,lng),
 nurses(name), tours(date,nurse_id,shift_start,shift_end),
 visits(patient_id,date,service_minutes,travel_minutes), visit_services(code), lk_codes(code,euro_per_unit,sgb).
Rules: SELECT only; use ILIKE '%name%' for people; always add LIMIT 100 unless aggregating.
Return ONLY the SQL, no markdown, no explanation."""

EXAMPLES = [
    ("welche Pflegekraft war im Mai am wenigsten ausgelastet",
     "select nurse, round(avg(care_util),1) avg_care_util, count(*) tours from v_utilisation where month='2026-05' group by nurse order by avg_care_util asc limit 10"),
    ("patients with over 2000 unused §45 budget",
     "select full_name, remaining_eur from v_budget_utilisation where paragraph='45' and remaining_eur>2000 order by remaining_eur desc limit 100"),
]


def ask_llm(question):
    sys = SCHEMA_CARD + "\nExamples:\n" + "\n".join(f"Q: {q}\nSQL: {s}" for q, s in EXAMPLES)
    if PROVIDER == "anthropic":
        body = {"model": MODEL, "max_tokens": 500, "system": sys,
                "messages": [{"role": "user", "content": question}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
              data=json.dumps(body).encode(),
              headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=40))
        return out["content"][0]["text"]
    else:
        body = {"model": MODEL, "max_tokens": 500,
                "messages": [{"role": "system", "content": sys}, {"role": "user", "content": question}]}
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
              data=json.dumps(body).encode(),
              headers={"Authorization": "Bearer " + KEY, "content-type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=40))
        return out["choices"][0]["message"]["content"]


def clean_sql(text):
    s = re.sub(r"```sql|```", "", text).strip().rstrip(";").strip()
    return s


def safe(sql):
    low = sql.lower()
    if not re.match(r"^\s*(select|with)\b", low):
        return False
    if ";" in sql:
        return False
    if re.search(r"\b(insert|update|delete|drop|alter|create|grant|truncate|copy)\b", low):
        return False
    return True


def run_ro(sql):
    if "limit" not in sql.lower():
        sql += " limit 100"
    c = psycopg2.connect(DB); cur = c.cursor()
    cur.execute("set statement_timeout='8s'")
    cur.execute("begin transaction read only")
    cur.execute(sql)
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute("rollback"); cur.close(); c.close()
    return cols, rows


@app.route("/")
def home():
    return send_from_directory(HERE, "index.html")


@app.route("/ask", methods=["POST"])
def ask():
    q = (request.json or {}).get("question", "").strip()
    if not q:
        return jsonify(error="Bitte Frage eingeben."), 400
    if not KEY:
        # No API key: use the live in-session engine if the Claude loop is attached.
        if bridge_live():
            return ask_via_bridge(q)
        return jsonify(error="Keine Engine verbunden. Entweder LLM_API_KEY setzen, oder die Claude-Sitzung-Schleife starten (Live-Engine)."), 503
    try:
        sql = clean_sql(ask_llm(q))
        if not safe(sql):
            return jsonify(error="Generierte Abfrage ist keine reine SELECT-Abfrage.", sql=sql), 400
        cols, rows = run_ro(sql)
        return jsonify(sql=sql, columns=cols, rows=rows[:100])
    except Exception as e:
        return jsonify(error=str(e)[:300]), 500


@app.route("/engine")
def engine():
    """Frontend polls this to show whether the live engine is attached."""
    return jsonify(live=bridge_live(), has_key=bool(KEY))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 4400)), threaded=True)
