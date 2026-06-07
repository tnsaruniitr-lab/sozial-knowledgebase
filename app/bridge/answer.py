"""Bridge answer-writer. The in-session Claude composes SQL and calls this to
run it read-only and drop the result where server.py is polling.

  python3 answer.py <id> '<sql>'        # sql as arg
  echo '<sql>' | python3 answer.py <id> -   # or on stdin

Safety mirrors server.py: single SELECT/WITH only, READ ONLY txn, statement
timeout, auto-LIMIT. DB from $DATABASE_URL or ../etl/.env. Writes a/<id>.json
atomically (tmp+rename) so the server never reads a half-written file.
"""
import os, sys, re, json, pathlib, datetime
from decimal import Decimal
import psycopg2

HERE = pathlib.Path(__file__).resolve().parent            # app/bridge


def db_url():
    u = os.environ.get("DATABASE_URL")
    if u:
        return u
    envf = HERE.parent.parent / "etl" / ".env"            # ~/sozial-knowledgebase/etl/.env
    if envf.exists():
        for ln in envf.read_text().splitlines():
            if ln.startswith("DATABASE_URL="):
                return ln.split("=", 1)[1].strip()
    raise SystemExit("no DATABASE_URL")


def safe(sql):
    low = sql.lower()
    if not re.match(r"^\s*(select|with)\b", low):
        return False
    if ";" in sql:
        return False
    if re.search(r"\b(insert|update|delete|drop|alter|create|grant|truncate|copy)\b", low):
        return False
    return True


def run_ro(sql, DB):
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


def jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def main():
    qid = sys.argv[1]
    sql = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else sys.stdin.read()
    sql = re.sub(r"```sql|```", "", sql).strip().rstrip(";").strip()
    out_path = HERE / "a" / f"{qid}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not safe(sql):
            out = {"id": qid, "error": "Nur reine SELECT-Abfragen erlaubt.", "sql": sql}
        else:
            cols, rows = run_ro(sql, db_url())
            rows = [{k: jsonable(v) for k, v in r.items()} for r in rows]
            out = {"id": qid, "sql": sql, "columns": cols, "rows": rows[:100]}
    except Exception as e:
        out = {"id": qid, "error": str(e)[:300], "sql": sql}
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.rename(out_path)
    print("wrote", out_path, "rows=", len(out.get("rows", [])) if "rows" in out else "err")


if __name__ == "__main__":
    main()
