"""AutoHedge runner: polls Supabase for config/commands, runs cycles, reports status.

Advisory only: AutoHedge 0.1.5 agents emit text recommendations and have no
trade-execution tool attached. Do not add wallet keys to this environment.
"""
import json
import os
import socket
import sys
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

MODEL_OVERRIDE = os.getenv("AUTOHEDGE_MODEL", "").strip()


def patch_agents():
    """AutoHedge hard-codes OpenAI models and gives the Quant agent no data tools; fix both."""
    from swarms import Agent

    from market_tools import market_snapshot

    original = Agent.__init__

    def patched(self, *args, **kwargs):
        if MODEL_OVERRIDE:
            kwargs["model_name"] = MODEL_OVERRIDE
        if kwargs.get("agent_name") == "Quant-Analyst":
            kwargs["tools"] = [market_snapshot]
            kwargs["system_prompt"] = (kwargs.get("system_prompt") or "") + (
                "\n\nYou have a market_snapshot tool (Yahoo Finance). Call it for each relevant ticker "
                "(e.g. CL=F WTI crude, BZ=F Brent, SPY, BTC-USD) and base every figure on its output. "
                "Never invent prices; say what you could not get."
            )
        original(self, *args, **kwargs)

    Agent.__init__ = patched


def synthesize(task, messages):
    """One final model call that turns the agents' raw hand-off output into a readable thesis."""
    import litellm

    text = json.dumps(messages, default=str)[:60000]
    resp = litellm.completion(
        model=MODEL_OVERRIDE or "gpt-4.1",
        max_tokens=1500,
        messages=[
            {"role": "system", "content": "You are a hedge-fund research editor. Write a concise markdown thesis "
             "(max ~350 words) from the agents' output: overall view, key drivers, quant levels with real numbers "
             "from the data, risks, and what would change the view. Use only facts present in the input; "
             "explicitly note any data that failed to load. This is analysis, not financial advice."},
            {"role": "user", "content": f"Task: {task}\n\nAgent output:\n{text}"},
        ],
    )
    return resp.choices[0].message.content


if sys.stdout is None or sys.stderr is None:
    # pythonw.exe (hidden Windows launch) has no console; swarms/loguru crash on a None stdout.
    _log = open(DATA_DIR / "runner.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

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
        patch_agents()  # must run before autohedge builds its agents
        from autohedge import AutoHedge  # imported lazily so the heartbeat works without LLM keys

        output = AutoHedge(name="autohedge", output_type="dict").run(task=task)
        raw = json.loads(json.dumps(output, default=str))
        try:
            thesis = synthesize(task, raw)
        except Exception as e:
            thesis = f"(thesis step failed: {str(e)[:200]})"
        payload = {"thesis": thesis, "raw": raw}
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
