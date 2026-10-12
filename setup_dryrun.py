"""
setup_dryrun.py -- render the five charts, five reads, roles and cards locally. POSTS NOTHING (no Discord import, no webhook).

  python setup_dryrun.py                       # synthetic fixture data, deterministic reads only (no network at all)
  python setup_dryrun.py --source yahoo        # REAL hourly bars from Yahoo for AXTI/NVDA/UPST/SMCI/ORCL-style tickers (visual check only;
                                               # production uses Alpaca bars), options map / flow print still from the fixture
  python setup_dryrun.py --live-llm            # also call Grok (needs OPENROUTER_API_KEY in the environment); reads are validated as in production
  python setup_dryrun.py --out dryrun_out      # where the PNGs, reads.json and cards.md go

The same code path as production is used (setup_pipeline.prepare), only the data source differs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import setup_fixtures as fx
import setup_pipeline as sp
from setup_embed import build_setup_embed_v2

RISK_COLORS = {"Low": 0x3BA55D, "Moderate": 0x5865F2, "Elevated": 0xE5A012, "High": 0xED4245, "Speculative": 0xED4245}


class FixtureData:
    def __init__(self, bars, spy, opts, bets, earn):
        self.bars, self._spy, self.opts, self.bets, self.earn_ = bars, spy, opts, bets, earn

    def hourly(self, c):
        return self.bars[c["ticker"]]

    def spy(self):
        return self._spy

    def options(self, c, spot):
        return self.opts.get(c["ticker"])

    def earnings(self, c):
        return self.earn_.get(c["ticker"])

    def bet(self, c):
        return self.bets.get(c["ticker"])


def yahoo_hourly(sym: str):
    import yfinance as yf
    h = yf.Ticker(sym).history(period="60d", interval="1h")
    h = h[["Open", "High", "Low", "Close", "Volume"]].dropna()
    h.index = h.index.tz_convert("America/New_York")
    return h[h.Volume > 0]


def tone(m):
    return "Firm close near session high" if (m.get("pct") or 0) > 0 else "Weak close near session low"


def post_test(selected, res, ctx):
    """Header + best choice, then one message per setup (card embed + chart attached), to the TEST webhook. Refuses the production one."""
    import json as _json
    import requests
    url = os.environ.get("TEST_WEBHOOK_URL", "")
    if not url:
        sys.exit("--post needs TEST_WEBHOOK_URL")
    if url == os.environ.get("NIGHTLY_SETUPS_DISCORD_WEBHOOK"):
        sys.exit("refusing to post to the production webhook")
    top = res["top_pick_ticker"]
    head = [{"title": "TRADE IDEAS — TEST RENDER (new charts + reads)", "description": res["market_backdrop"], "color": 0x2B2D31},
            {"title": "Best Choice Tonight", "color": 0xFBBF24, "description": f"**${top}**\n{res['top_pick_why']}"}]
    r = requests.post(url, json={"username": "BMT TEST", "embeds": head}, timeout=30)
    print("header:", r.status_code)
    for i, c in enumerate(selected):
        emb = build_setup_embed_v2(c, i + 1, c["ticker"] == top, RISK_COLORS.get(c["risk"], 0x2B2D31))
        path = c.get("_new_chart_path")
        if path:
            fn = os.path.basename(path)
            payload = {"username": "BMT TEST", "embeds": [emb, {"title": f"${c['ticker']} — Chart", "color": emb["color"], "image": {"url": f"attachment://{fn}"}}]}
            with open(path, "rb") as fh:
                r = requests.post(url, data={"payload_json": _json.dumps(payload)}, files={"file": (fn, fh, "image/png")}, timeout=60)
        else:
            r = requests.post(url, json={"username": "BMT TEST", "embeds": [emb]}, timeout=30)
        print(f"{c['ticker']}: {r.status_code}")

    # the two closing posts production makes after the setups: the contract list and the summary card with all five ideas.
    # Built with main()'s own functions (dummy env so importing it needs no secrets; its production webhook is never used here).
    for k in ("JARVIS_API_KEY", "OPENROUTER_API_KEY", "FINNHUB_API_KEY", "NIGHTLY_SETUPS_DISCORD_WEBHOOK"):
        os.environ.setdefault(k, "unused")
    import bmt_nightly_setups as m
    r = requests.post(url, json={"username": "BMT TEST", "embeds": [m.build_contract_list_embed(selected)]}, timeout=30)
    print("contract list:", r.status_code)
    mk_ = lambda t: {"price": ctx[t]["price"], "pct": ctx[t]["pct"], "note": m.get_tone_phrase({"price": ctx[t]["price"], "open": ctx[t]["price"] * .995, "high": ctx[t]["price"] * 1.004,
                                                                                         "low": ctx[t]["price"] * .993, "pct": ctx[t]["pct"]})}
    market = {k.lower(): mk_(k) for k in ("SPY", "QQQ", "IWM")}
    n_calls = sum(1 for c in selected if c["direction"].upper() == "CALL")
    n_puts = len(selected) - n_calls
    dir_summary = "All calls" if n_puts == 0 else "All puts" if n_calls == 0 else f"{n_calls} calls, {n_puts} puts"
    expiries = {c["next_expiry"] for c in selected}
    d = selected[0]["expiry_iso"]
    close_str = "last session"
    out = os.path.join(os.path.dirname(selected[0].get("_new_chart_path") or "."), "summary_card.png")
    m.render_watchlist_card([m.build_setup_dict_from_v3(c) for c in selected], market,
                            f"$SPY closed at ${ctx['SPY']['price']} ({ctx['SPY']['pct']}%) and $QQQ at ${ctx['QQQ']['price']} ({ctx['QQQ']['pct']}%).",
                            title_date="Test render", subtitle=f"{len(selected)} setups · {dir_summary} · {'All ' + list(expiries)[0] + ' expiry' if len(expiries) == 1 else 'Mixed expiries'} · Based on {close_str}",
                            footer="TEST RENDER · Not financial advice", out_path=out)
    with open(out, "rb") as fh:
        r = requests.post(url, data={"username": "BMT TEST", "content": ""}, files={"file": ("summary_card.png", fh, "image/png")}, timeout=60)
    print("summary card:", r.status_code, out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["fixture", "yahoo", "alpaca"], default="fixture")
    ap.add_argument("--live-llm", action="store_true")
    ap.add_argument("--post", action="store_true", help="post the cards to the TEST webhook in env TEST_WEBHOOK_URL (never the production one)")
    ap.add_argument("--out", default="dryrun_out")
    ap.add_argument("--tickers", default="", help="yahoo source: comma list replacing the five fixture tickers, e.g. NVDA,AMD,TSLA,AAPL,MSFT")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    selected, bars, spy, opts, bets, earn, session_day = fx.fixture_setups()
    if a.source in ("yahoo", "alpaca"):
        import datetime as dt
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()] or [c["ticker"] for c in selected]
        if a.source == "alpaca":
            import setup_market as mk
            import datetime as _dt
            hdrs, _now, _sess = mk.alpaca_headers(), _dt.datetime.now(mk.ET), __import__("requests").Session()
            if not hdrs:
                sys.exit("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set")
            hourly_src = lambda sym: mk.get_hourly(_sess, hdrs, sym, _now)
        else:
            hourly_src = yahoo_hourly
        spy = hourly_src("SPY")
        session_day = spy.index[-1].date()
        new_sel, new_bars = [], {}
        for i, t in enumerate(tickers[:5]):
            try:
                df = hourly_src(t)
            except Exception as e:
                print(f"  yahoo bars failed for {t}: {e}")
                continue
            px = round(float(df.Close.iloc[-1]), 2)
            base = selected[i % len(selected)]
            direction = base["direction"]
            step = 1.0 if px < 50 else 2.5 if px < 200 else 5.0
            strike = round((px * (1.04 if direction == "CALL" else .96)) / step) * step
            exp = session_day + dt.timedelta(days=14)
            c = fx._setup(t, direction, base["pattern"], px, strike, exp, 14, base["flow"], base["iv_rv_ratio"], base["analyst_target"], .03, i)
            new_sel.append(c)
            new_bars[t] = df
            opts[t] = fx.option_map_fixture(px, exp, step) if i != 3 else None
            bets[t] = {"strike": strike, "kind": "calls" if direction == "CALL" else "puts", "side": "bought", "amount": 610_000, "order": "sweep",
                       "expiry": exp.isoformat(), "date": session_day.isoformat()}
            earn[t] = (exp + dt.timedelta(days=12)).isoformat() if i % 2 == 0 else None
        selected, bars = new_sel, new_bars

    ctx = {"SPY": {"price": 673.42, "pct": 0.41}, "QQQ": {"price": 611.08, "pct": 0.66}, "IWM": {"price": 247.9, "pct": -0.18}}
    data = FixtureData(bars, spy, opts, bets, earn)
    if a.source == "alpaca":
        import setup_pipeline as _sp
        live = _sp.LiveData(session_day)
        live._spy = spy
        live.hourly = lambda c, _b=bars: _b[c["ticker"]]
        data = live                                     # real options map from Alpaca; flow print and earnings come from the setup dicts
        for c in selected:
            c["flow"] = dict(c["flow"], top_print=bets.get(c["ticker"]))
            c["er_date"] = earn.get(c["ticker"])
    api_key = os.environ.get("OPENROUTER_API_KEY") if a.live_llm else None
    if a.live_llm and not api_key:
        print("--live-llm given but OPENROUTER_API_KEY is not set: using the deterministic fallback reads")
    res = sp.prepare(selected, ctx, session_day, tone_fn=tone, data=data, out_dir=a.out, want_chart=True, want_read=True, api_key=api_key)

    cards = [f"# Dry run -- {session_day}  (source={a.source}, llm={'live' if api_key else 'fallback only'})\n", f"**Header backdrop:** {res['market_backdrop']}\n"]
    out = {"market_backdrop": res["market_backdrop"], "setups": []}
    for i, c in enumerate(selected):
        emb = build_setup_embed_v2(c, i + 1, c["ticker"] == res["top_pick_ticker"], RISK_COLORS.get(c["risk"], 0x2B2D31))
        cards.append(f"## {emb['title']}\n- " + "\n- ".join(f"**{f['name']}:** {f['value']}" for f in emb["fields"]) +
                     f"\n\n> {emb.get('description', '(no read)')}\n\nchart: `{c.get('_new_chart_path', '(none)')}`  read source: {c.get('read_source')}\n")
        out["setups"].append({"ticker": c["ticker"], "role": c["role"], "risk": c["risk"], "read": c.get("read"), "read_source": c.get("read_source"),
                              "embed": emb, "chart": c.get("_new_chart_path")})
    with open(os.path.join(a.out, "cards.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cards))
    with open(os.path.join(a.out, "reads.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("\n".join(cards))
    if a.post:
        post_test(selected, res, ctx)
        print(f"\nWrote PNGs, cards.md and reads.json to {a.out}/ -- posted to the TEST webhook only.")
    else:
        print(f"\nWrote PNGs, cards.md and reads.json to {a.out}/ -- nothing was posted.")


if __name__ == "__main__":
    sys.exit(main())
