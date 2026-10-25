"""AutoHedge runner: polls Supabase for config/commands, runs cycles, reports status.

Advisory only: AutoHedge 0.1.5 agents emit text recommendations and have no
trade-execution tool attached. Do not add wallet keys to this environment.
"""
import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent / ".env")

POLL_SECONDS = 30
DATA_DIR = Path(os.getenv("AUTOHEDGE_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(DATA_DIR)  # AutoHedge writes ./outputs relative to cwd

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
HOST = socket.gethostname()


def now():
    return datetime.now(timezone.utc)


def heartbeat(state, message=None):
    db.table("bot_heartbeat").upsert(
        {"id": 1, "last_seen": now().isoformat(), "state": state, "host": HOST, "message": message}
    ).execute()


def config():
    return db.table("bot_config").select("*").eq("id", 1).single().execute().data


def runs_today():
    start = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = db.table("bot_runs").select("id", count="exact").gte("started_at", start).execute()
    return res.count or 0


def last_run_start():
    rows = db.table("bot_runs").select("started_at").order("started_at", desc=True).limit(1).execute().data
    return datetime.fromisoformat(rows[0]["started_at"]) if rows else None


def run_cycle(task, trigger):
    run = db.table("bot_runs").insert({"task": task, "trigger": trigger}).execute().data[0]
    heartbeat("running", f"run {run['id']}")
    try:
        from autohedge import AutoHedge  # imported lazily so the heartbeat works without LLM keys

        output = AutoHedge(name="autohedge", output_type="dict").run(task=task)
        payload = json.loads(json.dumps(output, default=str))
        db.table("bot_runs").update(
            {"status": "ok", "finished_at": now().isoformat(), "output": payload}
        ).eq("id", run["id"]).execute()
    except Exception as e:  # keep the loop alive; surface the error on the dashboard
        db.table("bot_runs").update(
            {"status": "error", "finished_at": now().isoformat(), "error": str(e)[:2000]}
        ).eq("id", run["id"]).execute()


def main():
    heartbeat("starting")
    while True:
        try:
            cfg = config()
            cmds = db.table("bot_commands").select("*").is_("handled_at", "null").order("id").execute().data
            for cmd in cmds:
                run_cycle(cfg["task"], "manual")
                db.table("bot_commands").update(
                    {"handled_at": now().isoformat(), "result": "ran"}
                ).eq("id", cmd["id"]).execute()
            if cmds:
                continue
            if cfg["paused"]:
                heartbeat("paused")
            else:
                last = last_run_start()
                due = last is None or now() - last >= timedelta(minutes=cfg["interval_minutes"])
                if due and runs_today() < cfg["max_runs_per_day"]:
                    run_cycle(cfg["task"], "schedule")
                else:
                    heartbeat("idle")
        except Exception as e:
            print(f"loop error: {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
