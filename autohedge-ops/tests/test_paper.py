"""Offline tests: run with `python tests/test_paper.py` from autohedge-ops (no keys or network needed)."""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="x", AUTOHEDGE_DATA_DIR=tempfile.mkdtemp())

import paper  # noqa: E402
import runner  # noqa: E402


class Res:
    def __init__(self, data, count=None): self.data, self.count = data, count


class Q:
    PK = {"paper_positions": "symbol", "bot_heartbeat": "id", "paper_account": "id", "bot_config": "id"}

    def __init__(self, db, name):
        self.db, self.name, self.rows = db, name, db.tables.setdefault(name, [])
        self.filters, self.op, self.payload, self._order, self._limit, self._single, self._count = [], "select", None, None, None, False, False

    def select(self, cols="*", count=None): self._count = bool(count); return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, row): self.op, self.payload = "update", row; return self
    def upsert(self, row): self.op, self.payload = "upsert", row; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, k, v): self.filters.append(lambda r: r.get(k) == v); return self
    def neq(self, k, v): self.filters.append(lambda r: r.get(k) != v); return self
    def is_(self, k, v): self.filters.append(lambda r: r.get(k) is None); return self
    def gte(self, k, v): self.filters.append(lambda r: str(r.get(k)) >= str(v)); return self
    def order(self, k, desc=False): self._order = (k, desc); return self
    def limit(self, n): self._limit = n; return self
    def single(self): self._single = True; return self

    def execute(self):
        match = lambda r: all(f(r) for f in self.filters)
        if self.op == "insert":
            row = dict(self.payload, id=len(self.rows) + 1, started_at=datetime.now(timezone.utc).isoformat(),
                       ts=datetime.now(timezone.utc).isoformat(), status=self.payload.get("status", "running"))
            self.rows.append(row); return Res([row])
        if self.op == "upsert":
            pk = self.PK[self.name]
            for r in self.rows:
                if r.get(pk) == self.payload[pk]: r.update(self.payload); return Res([r])
            self.rows.append(dict(self.payload)); return Res([self.payload])
        if self.op == "update":
            for r in self.rows:
                if match(r): r.update(self.payload)
            return Res([])
        if self.op == "delete":
            self.rows[:] = [r for r in self.rows if not match(r)]; return Res([])
        out = [r for r in self.rows if match(r)]
        if self._order: out.sort(key=lambda r: r[self._order[0]], reverse=self._order[1])
        if self._limit: out = out[: self._limit]
        return Res(out[0] if self._single else out, len(out) if self._count else None)


class FakeDB:
    def __init__(self): self.tables = {}
    def table(self, name): return Q(self, name)


def quote(price, fresh=True):
    return {"price": price, "fresh": fresh, "r1d": 1, "r5d": 2, "r1m": 3, "r3m": 4, "vs_ma50": 1, "vs_ma200": 2, "vol20": 40, "off_high": -5}


def test_engine():
    quotes = {"BTC-USD": quote(100.0), "SPY": quote(500.0, fresh=False), "ETH-USD": quote(10.0)}
    wl = ["BTC-USD", "SPY", "ETH-USD"]
    st = paper.MemoryStore(50.0)
    ex, rej = paper.apply_orders(st, quotes, [
        {"symbol": "BTC-USD", "action": "buy", "usd": 100, "reason": "big"},
        {"symbol": "SPY", "action": "buy", "usd": 10, "reason": "closed"},
        {"symbol": "DOGE-USD", "action": "buy", "usd": 10, "reason": "x"},
    ], wl)
    assert [round(r["usd"], 2) for r in ex] == [20.0] and len(rej) == 2   # 40% cap; closed market; off watchlist
    assert abs(st.account()["cash"] - 30.0) < 1e-9
    _, stops = paper.mark_to_market(st, dict(quotes, **{"BTC-USD": quote(85.0)}))
    assert stops and not st.positions()                                    # -10% auto stop-loss


def test_runner_flow():
    fake = FakeDB()
    fake.tables["bot_config"] = [{"id": 1, "paused": False, "task": "t", "watchlist": ["BTC-USD", "ETH-USD", "BAD SYMBOL!"], "deep_research": False}]
    fake.tables["paper_account"] = [{"id": 1, "starting_cash": 50, "cash": 50, "benchmark_start": None}]
    runner.db = fake
    paper.fetch_quotes = lambda syms, now: {s: quote(100.0) for s in syms}
    runner.llm = lambda s, u: '{"summary":"s","thesis":"t","orders":[{"symbol":"ETH-USD","action":"buy","usd":15,"reason":"r"}],"risks":"k"}'
    import market_tools
    market_tools.fetch_news = lambda *a, **k: [{"title": "T", "publishedDate": "2026-10-25", "text": "body"}]
    runner.run_paper(fake.tables["bot_config"][0], "manual")
    run = fake.tables["bot_runs"][0]
    assert run["status"] == "ok", run
    assert "BUY ETH-USD" in run["output"]["thesis"]
    assert len(fake.tables["paper_trades"]) == 1 and fake.tables["paper_equity"]
    assert fake.tables["paper_account"][0]["benchmark_start"]
    assert runner.runs_today() == 1
    paper.Store(fake).reset()
    assert not fake.tables["paper_positions"] and not fake.tables["paper_trades"] and fake.tables["paper_account"][0]["cash"] == 50


if __name__ == "__main__":
    test_engine(); test_runner_flow(); print("all tests passed")
