"""Paper-trading engine and the lean decision step.

The model proposes orders; this module validates and fills them against live Yahoo prices with fees
and slippage. Nothing here touches a real exchange or wallet.
"""
import json
import math
import re
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

FEE_RATE = 0.001  # per side
SLIPPAGE = 0.001  # per side
MAX_POSITION_PCT = 0.40
MAX_TRADES_PER_CYCLE = 3
MIN_TRADE_USD = 2.0
STOP_LOSS_PCT = -0.10
FRESH_MINUTES = 30
BENCHMARKS = ["BTC-USD", "SPY"]
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-=^]{1,12}$")


def clean_watchlist(raw, limit=12):
    out = []
    for s in raw or []:
        s = str(s).strip().upper()
        if SYMBOL_RE.match(s) and s not in out:
            out.append(s)
    return out[:limit]


# ---------------------------------------------------------------- prices

def fetch_quote(sym, now):
    """Latest price plus indicators for one symbol; None if Yahoo has no usable data."""
    import yfinance as yf

    t = yf.Ticker(sym)
    close = t.history(period="1y", interval="1d")["Close"].dropna()
    if len(close) < 30:
        return None
    price, fresh = float(close.iloc[-1]), False
    try:
        intra = t.history(period="1d", interval="5m")
        c = intra["Close"].dropna()
        if len(c):
            price = float(c.iloc[-1])
            fresh = now - c.index[-1].to_pydatetime().astimezone(timezone.utc) <= timedelta(minutes=FRESH_MINUTES)
    except Exception:
        pass

    def pct(days):
        return round((price / float(close.iloc[-1 - days]) - 1) * 100, 2) if len(close) > days else None

    def vs_ma(days):
        return round((price / float(close.tail(days).mean()) - 1) * 100, 2) if len(close) >= days else None

    rets = close.pct_change().dropna().tail(20)
    return {
        "price": price,
        "fresh": fresh,
        "r1d": pct(1), "r5d": pct(5), "r1m": pct(21), "r3m": pct(63),
        "vs_ma50": vs_ma(50), "vs_ma200": vs_ma(200),
        "vol20": round(float(rets.std()) * math.sqrt(252) * 100, 1) if len(rets) > 5 else None,
        "off_high": round((price / float(close.max()) - 1) * 100, 1),
    }


def fetch_quotes(symbols, now):
    out = {}
    for s in symbols:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                q = fetch_quote(s, now)
            if q:
                out[s] = q
        except Exception as e:
            print(f"quote failed for {s}: {e}", flush=True)
    return out


# ---------------------------------------------------------------- stores

class Store:
    """Supabase-backed state. MemoryStore below mirrors this API for tests."""

    def __init__(self, db):
        self.db = db

    def account(self):
        return self.db.table("paper_account").select("*").eq("id", 1).single().execute().data

    def save_account(self, **kw):
        self.db.table("paper_account").update(kw).eq("id", 1).execute()

    def positions(self):
        rows = self.db.table("paper_positions").select("*").execute().data
        return {r["symbol"]: {"qty": float(r["qty"]), "avg_cost": float(r["avg_cost"])} for r in rows}

    def set_position(self, sym, qty, avg_cost):
        if qty <= 1e-12:
            self.db.table("paper_positions").delete().eq("symbol", sym).execute()
        else:
            self.db.table("paper_positions").upsert(
                {"symbol": sym, "qty": qty, "avg_cost": avg_cost, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).execute()

    def add_trade(self, row):
        self.db.table("paper_trades").insert(row).execute()

    def add_equity(self, row):
        self.db.table("paper_equity").insert(row).execute()

    def recent_trades(self, n=6):
        return self.db.table("paper_trades").select("ts,symbol,side,usd,reason").order("id", desc=True).limit(n).execute().data

    def last_equity_ts(self):
        rows = self.db.table("paper_equity").select("ts").order("id", desc=True).limit(1).execute().data
        return datetime.fromisoformat(rows[0]["ts"]) if rows else None

    def reset(self, starting_cash=50):
        for table, col in (("paper_positions", "symbol"), ("paper_trades", "id"), ("paper_equity", "id")):
            self.db.table(table).delete().neq(col, "" if col == "symbol" else -1).execute()
        self.save_account(cash=starting_cash, starting_cash=starting_cash, benchmark_start=None,
                          started_at=datetime.now(timezone.utc).isoformat())


class MemoryStore:
    def __init__(self, cash=50.0):
        self._acct = {"starting_cash": cash, "cash": cash, "benchmark_start": None}
        self._pos, self.trades, self.equity = {}, [], []

    def account(self): return dict(self._acct)
    def save_account(self, **kw): self._acct.update(kw)
    def positions(self): return {k: dict(v) for k, v in self._pos.items()}

    def set_position(self, sym, qty, avg_cost):
        if qty <= 1e-12:
            self._pos.pop(sym, None)
        else:
            self._pos[sym] = {"qty": qty, "avg_cost": avg_cost}

    def add_trade(self, row): self.trades.append(row)
    def add_equity(self, row): self.equity.append(row)
    def recent_trades(self, n=6): return self.trades[-n:][::-1]
    def last_equity_ts(self): return None


# ---------------------------------------------------------------- engine

def tradable(sym, quotes):
    q = quotes.get(sym)
    return bool(q and (q["fresh"] or sym.endswith("-USD")))


def portfolio_value(store, quotes):
    acct, pos = store.account(), store.positions()
    value = sum(p["qty"] * quotes.get(s, {}).get("price", p["avg_cost"]) for s, p in pos.items())
    return float(acct["cash"]) + value


def _sell(store, quotes, sym, pct, reason, run_id):
    pos = store.positions().get(sym)
    if not pos:
        return None, "no position"
    qty = pos["qty"] * min(max(pct, 0), 100) / 100
    px = quotes[sym]["price"] * (1 - SLIPPAGE)
    proceeds = qty * px
    if proceeds < MIN_TRADE_USD and qty < pos["qty"] * 0.999:
        return None, f"order below ${MIN_TRADE_USD:.0f} minimum"
    fee = proceeds * FEE_RATE
    acct = store.account()
    store.save_account(cash=float(acct["cash"]) + proceeds - fee)
    store.set_position(sym, pos["qty"] - qty, pos["avg_cost"])
    row = {"symbol": sym, "side": "sell", "qty": qty, "price": px, "usd": proceeds, "fee": fee,
           "reason": reason[:300], "run_id": run_id}
    store.add_trade(row)
    return row, None


def _buy(store, quotes, sym, usd, reason, run_id):
    acct, pos = store.account(), store.positions()
    cash = float(acct["cash"])
    equity = portfolio_value(store, quotes)
    held = pos.get(sym, {"qty": 0.0, "avg_cost": 0.0})
    cur_val = held["qty"] * quotes[sym]["price"]
    usd = min(usd, cash, MAX_POSITION_PCT * equity - cur_val)
    if usd < MIN_TRADE_USD:
        return None, "below minimum after cash / 40% position cap"
    px = quotes[sym]["price"] * (1 + SLIPPAGE)
    fee = usd * FEE_RATE
    qty = (usd - fee) / px
    new_qty = held["qty"] + qty
    avg = (held["qty"] * held["avg_cost"] + qty * px) / new_qty
    store.save_account(cash=cash - usd)
    store.set_position(sym, new_qty, avg)
    row = {"symbol": sym, "side": "buy", "qty": qty, "price": px, "usd": usd, "fee": fee,
           "reason": reason[:300], "run_id": run_id}
    store.add_trade(row)
    return row, None


def apply_orders(store, quotes, orders, watchlist, run_id=None):
    executed, rejected = [], []
    for o in (orders or [])[:MAX_TRADES_PER_CYCLE]:
        try:
            sym = str(o.get("symbol", "")).upper()
            action = str(o.get("action", "")).lower()
            reason = str(o.get("reason", ""))
            if sym not in watchlist:
                raise ValueError("not on watchlist")
            if not tradable(sym, quotes):
                raise ValueError("market closed or no fresh price")
            if action == "buy":
                row, err = _buy(store, quotes, sym, float(o.get("usd", 0)), reason, run_id)
            elif action == "sell":
                row, err = _sell(store, quotes, sym, float(o.get("pct", 100)), reason, run_id)
            else:
                raise ValueError("action must be buy or sell")
            if err:
                raise ValueError(err)
            executed.append(row)
        except Exception as e:
            rejected.append({"order": o, "why": str(e)})
    return executed, rejected


def mark_to_market(store, quotes, run_id=None, now=None):
    """Apply the automatic stop-loss, then record an equity snapshot with benchmark prices."""
    stops = []
    for sym, p in list(store.positions().items()):
        q = quotes.get(sym)
        if q and tradable(sym, quotes) and q["price"] / p["avg_cost"] - 1 <= STOP_LOSS_PCT:
            row, _ = _sell(store, quotes, sym, 100, f"auto stop-loss at {STOP_LOSS_PCT * 100:.0f}%", run_id)
            if row:
                stops.append(row)
    acct = store.account()
    bench = {s: quotes[s]["price"] for s in BENCHMARKS if s in quotes}
    if acct.get("benchmark_start") is None and len(bench) == len(BENCHMARKS):
        store.save_account(benchmark_start=bench)
    equity = portfolio_value(store, quotes)
    store.add_equity({"equity": equity, "cash": float(store.account()["cash"]), "prices": bench, "run_id": run_id})
    return equity, stops


# ---------------------------------------------------------------- decision

SYSTEM_PROMPT = """You run a small paper-trading portfolio (simulated money, long-only, no leverage, no shorting).
Goal: grow the account while avoiding large drawdowns. Doing nothing is a valid and often correct choice.
Hard rules enforced by the engine: only symbols on the watchlist; max 40% of equity in one symbol;
max 3 orders per cycle; $2 minimum order; 0.1% fee and 0.1% slippage per side (so avoid churning);
positions are sold automatically at -10%. Non-crypto symbols can only trade while their market is open
(shown as tradable=no otherwise).
Use only the data given. If the data is thin or conflicting, hold cash.
Reply with ONLY a JSON object, no prose outside it:
{"summary": "2-3 sentence market read",
 "thesis": "why you are acting or holding, citing numbers from the data",
 "orders": [{"symbol": "BTC-USD", "action": "buy", "usd": 10, "reason": "short reason"},
            {"symbol": "SPY", "action": "sell", "pct": 100, "reason": "short reason"}],
 "risks": "what would change your mind"}"""


def _fmt(v, suffix="%"):
    return "n/a" if v is None else f"{v:+.1f}{suffix}"


def build_prompt(store, quotes, watchlist, news, deep_thesis):
    acct, pos = store.account(), store.positions()
    equity = portfolio_value(store, quotes)
    start = float(acct["starting_cash"])
    lines = [f"Account: equity ${equity:.2f} (started ${start:.2f}, {(equity / start - 1) * 100:+.1f}%), cash ${float(acct['cash']):.2f}."]
    for s, p in pos.items():
        px = quotes.get(s, {}).get("price", p["avg_cost"])
        lines.append(f"Holding {s}: {p['qty']:.6f} units, avg cost {p['avg_cost']:.4f}, now {px:.4f} ({(px / p['avg_cost'] - 1) * 100:+.1f}%), value ${p['qty'] * px:.2f}.")
    if not pos:
        lines.append("No open positions.")
    lines.append("\nWatchlist (price | 1d 5d 1m 3m | vs MA50 / MA200 | 20d vol | from 52w high | tradable now):")
    for s in watchlist:
        q = quotes.get(s)
        if not q:
            lines.append(f"{s}: no data")
            continue
        lines.append(f"{s} {q['price']:.4g} | {_fmt(q['r1d'])} {_fmt(q['r5d'])} {_fmt(q['r1m'])} {_fmt(q['r3m'])} | "
                     f"{_fmt(q['vs_ma50'])} / {_fmt(q['vs_ma200'])} | vol {q['vol20']}% | {q['off_high']}% | {'yes' if tradable(s, quotes) else 'no'}")
    recent = store.recent_trades()
    if recent:
        lines.append("\nRecent trades: " + "; ".join(f"{t['side']} {t['symbol']} ${float(t['usd']):.2f} ({str(t.get('reason') or '')[:60]})" for t in recent))
    if deep_thesis:
        lines.append("\nLatest deep research thesis:\n" + deep_thesis[:2500])
    lines.append("\nRecent news:\n" + (news or "(none available)"))
    return "\n".join(lines)


def parse_decision(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"summary": "", "thesis": "(model returned no JSON)", "orders": [], "risks": ""}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"summary": "", "thesis": "(model returned invalid JSON)", "orders": [], "risks": ""}
    d["orders"] = d.get("orders") if isinstance(d.get("orders"), list) else []
    return d


def paper_cycle(store, watchlist, quotes, llm, news="", deep_thesis="", run_id=None):
    """One decision cycle. `llm(system, user) -> str` is injected so tests need no API."""
    if not quotes:
        raise RuntimeError("no price data (Yahoo Finance unreachable?)")
    prompt = build_prompt(store, quotes, watchlist, news, deep_thesis)
    decision = parse_decision(llm(SYSTEM_PROMPT, prompt))
    executed, rejected = apply_orders(store, quotes, decision["orders"], watchlist, run_id)
    equity, stops = mark_to_market(store, quotes, run_id)
    acct = store.account()
    acts = [f"- {r['side'].upper()} {r['symbol']} ${r['usd']:.2f} @ {r['price']:.4g}: {r['reason']}" for r in executed + stops]
    text = (f"**Equity ${equity:.2f}** ({(equity / float(acct['starting_cash']) - 1) * 100:+.1f}%), cash ${float(acct['cash']):.2f}\n\n"
            f"{decision.get('summary', '')}\n\n{decision.get('thesis', '')}\n\n"
            f"**Actions**\n{chr(10).join(acts) if acts else '- none (holding)'}\n\n"
            f"**Risks / what changes the view:** {decision.get('risks', '')}")
    if rejected:
        text += "\n\n**Rejected orders:** " + "; ".join(f"{r['order'].get('symbol')} ({r['why']})" for r in rejected)
    return {"thesis": text, "raw": {"decision": decision, "executed": executed, "stops": stops,
                                    "rejected": rejected, "prompt_chars": len(prompt)}}
