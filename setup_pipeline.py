"""
setup_pipeline.py -- glue between main() in bmt_nightly_setups.py and the new modules. main() calls prepare() once, behind the env
flags NEW_CHARTS=1 (new hourly chart) and NEW_READ=1 (roles/risk in code, one validated read per setup, new embed).

prepare() mutates each setup dict with ONLY presentation fields (role, risk, read, _chart_path, ...). It never touches selection,
ranking, filters, strike/expiry, entry/stop/targets, scoring, the DB save or the idea log.

Every optional part degrades on its own: a failed options map, SPY series, flow print or earnings date removes just that part; bars that fail
the sanity check (or an unreachable Alpaca) mean no new chart for that ticker, and the card still posts.
"""
from __future__ import annotations

import datetime as dt
import os
import time

import requests

import setup_chart
import setup_market as mk
import setup_read
import setup_roles
from setup_embed import card_fallback_read

MAX_PNG_BYTES = 7 * 1024 * 1024          # Discord's webhook upload limit is 8 MB on the old tier; stay under it


def flags() -> tuple[bool, bool]:
    return os.environ.get("NEW_CHARTS") == "1", os.environ.get("NEW_READ") == "1"


# ------------------------------------------------------------------------------------------------ data sources

_UNSET = object()


class LiveData:
    """Alpaca (hourly bars, option chain) + what main() already fetched (flow print, earnings date)."""

    def __init__(self, session_day: dt.date, now: dt.datetime | None = None):
        self.session_day = session_day
        self.now = now or dt.datetime.now(mk.ET)
        self.headers = mk.alpaca_headers()
        self.sess = requests.Session()
        self._spy = _UNSET

    def hourly(self, c):
        if not self.headers:
            raise RuntimeError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set")
        df = mk.get_hourly(self.sess, self.headers, c["ticker"], self.now)
        why = mk.sanity(df, self.session_day)
        if why:
            raise ValueError(f"bars failed the sanity check: {why}")
        return df

    def spy(self):
        if self._spy is _UNSET:
            try:
                self._spy = mk.get_hourly(self.sess, self.headers, "SPY", self.now) if self.headers else None
            except Exception as e:
                print(f"  [CHART WARN] SPY series unavailable, relative strength panel skipped: {type(e).__name__}: {e}")
                self._spy = None
        return self._spy

    def options(self, c, spot):
        if not self.headers or not c.get("expiry_iso"):
            return None
        return mk.option_map(self.sess, self.headers, c["ticker"], spot, dt.date.fromisoformat(c["expiry_iso"]), self.session_day)

    def earnings(self, c):
        return c.get("er_date")

    def bet(self, c):
        return (c.get("flow") or {}).get("top_print")


# ------------------------------------------------------------------------------------------------ chart bundle

def build_bundle(c: dict, data, session_day: dt.date, out_dir: str) -> dict:
    """Render one chart. Raises when the bars are unusable (caller skips the chart); optional parts fail silently."""
    sym = c["ticker"]
    df = data.hourly(c)
    spy = None
    try:
        spy = data.spy()
    except Exception as e:
        print(f"  [CHART WARN] {sym}: SPY series unavailable: {type(e).__name__}")
    spot = float(df.Close.iloc[-1])
    opts = None
    try:
        opts = data.options(c, spot)
    except Exception as e:
        print(f"  [CHART WARN] {sym}: options map unavailable ({type(e).__name__}); wall/max-pain/expected-move omitted")
    earn = bet = None
    try:
        earn = data.earnings(c)
        bet = data.bet(c)
    except Exception as e:
        print(f"  [CHART WARN] {sym}: earnings/flow print unavailable ({type(e).__name__})")
    path = os.path.join(out_dir, f"chart_{sym}.png")
    out = setup_chart.build_chart(c, df, spy, opts, earn, bet, session_day, path)
    if os.path.getsize(path) > MAX_PNG_BYTES:
        setup_chart.build_chart(c, df, spy, opts, earn, bet, session_day, path, dpi=80)
    out["path"] = path
    out["has_options_map"] = bool(opts)
    return out


# ------------------------------------------------------------------------------------------------ prepare

def prepare(selected: list, market_context: dict, session_day: dt.date, *, tone_fn=None, data=None, out_dir: str = ".",
            want_chart: bool = True, want_read: bool = True, api_key: str | None = None, model: str | None = None) -> dict:
    """Returns {'market_backdrop': str|None, 'top_pick_ticker': str, 'top_pick_why': str|None, 'cost': float}.
    Sets on each setup: rr; (want_read) role, risk, read; (want_chart or want_read) _new_chart_path when a chart rendered."""
    started = time.time()
    model = model or os.environ.get("READ_MODEL", setup_read.DEFAULT_MODEL)
    for c in selected:
        c["rr"] = setup_roles.reward_risk(c)
    if want_read:
        roles = setup_roles.assign_roles(selected)
        for c in selected:
            c["role"] = roles[c["ticker"]]
            c["risk"] = setup_roles.risk_label(c)
            print(f"  {c['ticker']}: {c['role']} | {c['risk']}  (roles/risk computed in code)")

    data = data or LiveData(session_day)
    bundles: dict = {}
    for c in selected:
        t = c["ticker"]
        try:
            b = build_bundle(c, data, session_day, out_dir)
            bundles[t] = b
            c["_new_chart_path"] = b["path"]
            print(f"  [CHART] {t}: rendered {os.path.getsize(b['path']) // 1024} KB (bias {b['bias']}, options map {'yes' if b['has_options_map'] else 'no'})")
        except Exception as e:
            print(f"  [CHART SKIP] {t}: no new chart ({type(e).__name__}: {e}); the card still posts")

    cost = 0.0
    result = {"market_backdrop": None, "top_pick_ticker": selected[0]["ticker"], "top_pick_why": None, "cost": 0.0}
    if want_read:
        jobs = []
        for c in selected:
            b = bundles.get(c["ticker"])
            png = None
            if b:
                try:
                    with open(b["path"], "rb") as fh:
                        png = fh.read()
                except OSError:
                    png = None
            jobs.append({"ticker": c["ticker"], "facts": b["facts"] if b else {}, "png": png})
        reads = setup_read.run_reads(jobs, api_key, model)
        for c in selected:
            r = reads[c["ticker"]]
            cost += r["cost"]
            c["read"] = r["read"] or card_fallback_read(c)
            c["read_source"] = r["source"] if r["read"] else "card-fallback"
            print(f"  [READ] {c['ticker']}: source={c['read_source']} cost=${r['cost']:.4f}" + (f" fell back: {r['reason']}" if r.get("reason") else "") +
                  (f" rejected attempts: {r['rejections']}" if r.get("rejections") else ""))
        bd = setup_read.write_backdrop(api_key, model, market_context, tone_fn)
        cost += bd["cost"]
        result["market_backdrop"] = bd["text"]
        print(f"  [BACKDROP] source={bd['source']} cost=${bd['cost']:.4f}" + (f" ({bd['reason']})" if bd.get("reason") else ""))
        top = selected[0]                                            # the existing top rank, in code
        result["top_pick_ticker"] = top["ticker"]
        result["top_pick_why"] = top.get("read")
    result["cost"] = cost
    print(f"  [NEW PIPELINE] done in {time.time() - started:.1f}s; model cost ${cost:.4f} (logged only)")
    return result
