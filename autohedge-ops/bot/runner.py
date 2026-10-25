"""AutoHedge runner: polls Supabase for config/commands, runs paper-trading cycles, reports status.

Paper trading only: simulated $50 account, no exchange or wallet access. Do not add wallet keys
to this environment.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).parent / ".env")

POLL_SECONDS = 30
DATA_DIR = Path(os.getenv("AUTOHEDGE_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(DATA_DIR)  # AutoHedge writes ./outputs relative to cwd

MODEL_OVERRIDE = os.getenv("AUTOHEDGE_MODEL", "").strip()


def patch_agents():
    """AutoHedge hard-codes OpenAI models and gives the Quant agent no data tools; fix both."""
    from swarms import Agent

    from market_tools import market_snapshot, news_search

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
        if kwargs.get("agent_name") == "Sentiment-Agent":
            # AutoHedge's bundled Exa tool returns 2 results truncated to 20 characters.
            kwargs["tools"] = [news_search]
        original(self, *args, **kwargs)

    Agent.__init__ = patched


def synthesize(task, messages):
    """One final model call that turns the agents' raw hand-off output into a readable thesis."""
    import litellm

    text = json.dumps(messages, default=str)[:60000]
    resp = litellm.completion(
        model=MODEL_OVERRIDE or "gpt-4.1",
        max_tokens=3000,
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
    res = db.table("bot_runs").select("id", count="exact").gte("started_at", start).neq("trigger", "deep").execute()
    return res.count or 0


def last_run_start(trigger_filter=None):
    q = db.table("bot_runs").select("started_at").order("started_at", desc=True).limit(1)
    q = q.eq("trigger", "deep") if trigger_filter == "deep" else q.neq("trigger", "deep")
    rows = q.execute().data
    return datetime.fromisoformat(rows[0]["started_at"]) if rows else None


def finish(run_id, **fields):
    db.table("bot_runs").update({"finished_at": now().isoformat(), **fields}).eq("id", run_id).execute()


NEWS_QUERIES = [
    "US stock market and economy news today",
    "crypto market news Bitcoin Ethereum Solana today",
    "oil gold commodities market news today",
]


def llm(system, user):
    import litellm

    litellm.drop_params = True  # newer models reject some sampling params (e.g. temperature)
    resp = litellm.completion(
        model=MODEL_OVERRIDE or "gpt-4.1-mini", max_tokens=1200,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content


def run_deep(task):
    """Optional: the full AutoHedge agent pipeline (several model calls, so at most once a day)."""
    run = db.table("bot_runs").insert({"task": task, "trigger": "deep"}).execute().data[0]
    heartbeat("running", f"deep research {run['id']}")
    try:
        patch_agents()  # must run before autohedge builds its agents
        from autohedge import AutoHedge  # imported lazily so the heartbeat works without LLM keys

        raw = json.loads(json.dumps(AutoHedge(name="autohedge", output_type="dict").run(task=task), default=str))
        try:
            thesis = synthesize(task, raw)
        except Exception as e:
            thesis = f"(thesis step failed: {str(e)[:200]})"
        finish(run["id"], status="ok", output={"thesis": thesis, "raw": raw})
        return thesis
    except Exception as e:
        finish(run["id"], status="error", error=str(e)[:2000])
        return ""


def latest_deep_thesis():
    rows = db.table("bot_runs").select("started_at,output").eq("trigger", "deep").eq("status", "ok") \
        .order("started_at", desc=True).limit(1).execute().data
    if rows and now() - datetime.fromisoformat(rows[0]["started_at"]) < timedelta(hours=36):
        return (rows[0]["output"] or {}).get("thesis", "")
    return ""


def run_paper(cfg, trigger):
    """One lean decision cycle: prices (free) + one news search + one model call."""
    import paper
    from market_tools import fetch_news

    if cfg.get("deep_research"):
        last = last_run_start("deep")
        if last is None or now() - last >= timedelta(hours=24):
            run_deep(cfg["task"])
    run = db.table("bot_runs").insert({"task": "paper trading decision", "trigger": trigger}).execute().data[0]
    heartbeat("running", f"run {run['id']}")
    try:
        store = paper.Store(db)
        watchlist = paper.clean_watchlist(cfg.get("watchlist"))
        quotes = paper.fetch_quotes(sorted(set(watchlist) | set(paper.BENCHMARKS) | set(store.positions())), now())
        news = ""
        try:
            query = NEWS_QUERIES[run["id"] % len(NEWS_QUERIES)]
            news = "\n".join(f"- {a['title']} ({(a['publishedDate'] or '')[:10]}): {(a['text'] or '')[:450]}"
                             for a in fetch_news(query, n=5, chars=500, days=3))
        except Exception as e:  # news is optional; the cycle still works on prices
            news = f"(news unavailable: {str(e)[:100]})"
        out = paper.paper_cycle(store, watchlist, quotes, llm, news=news, deep_thesis=latest_deep_thesis(), run_id=run["id"])
        out["raw"] = json.loads(json.dumps(out["raw"], default=str))
        finish(run["id"], status="ok", output=out)
    except Exception as e:  # keep the loop alive; surface the error on the dashboard
        finish(run["id"], status="error", error=str(e)[:2000])


def mark_only():
    """Cheap hourly valuation (no model call): applies the automatic stop-loss and records equity."""
    import paper

    store = paper.Store(db)
    quotes = paper.fetch_quotes(sorted(set(paper.BENCHMARKS) | set(store.positions())), now())
    if quotes:
        paper.mark_to_market(store, quotes)


def main():
    import paper

    heartbeat("starting")
    last_mark = paper.Store(db).last_equity_ts() or now()
    while True:
        try:
            cfg = config()
            cmds = db.table("bot_commands").select("*").is_("handled_at", "null").order("id").execute().data
            for cmd in cmds:
                if cmd["command"] == "reset_paper":
                    paper.Store(db).reset()
                    result = "reset"
                else:
                    run_paper(cfg, "manual")
                    result = "ran"
                    last_mark = now()
                db.table("bot_commands").update({"handled_at": now().isoformat(), "result": result}).eq("id", cmd["id"]).execute()
            if cmds:
                continue
            if cfg["paused"]:
                heartbeat("paused")
            else:
                last = last_run_start()
                due = last is None or now() - last >= timedelta(minutes=cfg["interval_minutes"])
                if due and runs_today() < cfg["max_runs_per_day"]:
                    run_paper(cfg, "schedule")
                    last_mark = now()
                elif now() - last_mark >= timedelta(minutes=60):
                    heartbeat("marking")
                    mark_only()
                    last_mark = now()
                else:
                    heartbeat("idle")
        except Exception as e:
            print(f"loop error: {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
