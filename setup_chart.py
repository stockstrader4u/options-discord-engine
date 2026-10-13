"""
setup_chart.py -- the nightly trade-idea chart (NEW_CHARTS=1): 1-hour candles, light theme, volume by price, EMA 34/50 cloud,
Fibonacci levels (next level above/below highlighted with % distance), call wall / put wall / max pain / expected-move band,
prior-day levels and gap, relative strength vs SPY, Wilder RSI, a "key numbers" header strip and a deterministic
"In plain English" notes block. Ported from bmt-live-dashboard/charts/chart_render.py (read-only reference) and extended with
what the idea owns: entry zone, stop, target 1, target 2 (right-edge tags + R:R in the header) and the flow behind the pick.

Layers (all pure, no network, unit-tested):
  compute_metrics(...)  -> every number the chart, the notes and the facts use (one source of truth)
  build_notes(m)        -> plain-English notes drawn under the chart
  build_facts(m)        -> exact display strings the LLM read may quote (setup_read.py validates against these)
  render(m, out_path)   -> the PNG (matplotlib, Agg)

Nothing here changes the idea: entry/stop/targets/strike are read from the setup dict and only drawn.
"""
from __future__ import annotations

import datetime as dt
import re
import textwrap

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

plt.rcParams["text.parse_math"] = False

BG, TXT, MUTE, BOX = "#ffffff", "#1f2328", "#6b7280", "#f3f5f8"
UP, DN, BLUE, AMB, VIO = "#16a085", "#d64545", "#2563eb", "#d97706", "#7c3aed"
RES, SUP, BETC = "#1b8a3a", "#c62828", "#6d28d9"       # fib levels above the price = green, below = red
CWC, PWC, MPC, EMC = "#0284c7", "#c026d3", "#0f172a", "#38bdf8"
PD = "#475569"
ENTRYC, STOPC, T1C, T2C = "#2563eb", "#b91c1c", "#15803d", "#b45309"
N = 100

pc = lambda a, b: (a / b - 1) * 100 if b else 0.0
money = lambda a: f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1000:.0f}K"
fmtk = lambda n: f"{n / 1000:.1f}K" if n < 1e6 else f"{n / 1e6:.1f}M"


class M(dict):
    """Metrics bag with attribute access."""
    __getattr__ = dict.__getitem__

    def get_(self, k, d=None):
        return dict.get(self, k, d)


# ------------------------------------------------------------------------------------------------ metrics (pure)

def compute_metrics(c: dict, df, spy, opts, earn, bet, session_day: dt.date, bench: str = "SPY") -> M:
    """`df` / `spy`: complete hourly candles (DataFrame, Open/High/Low/Close/Volume, ET index); `spy` may be None.
    `opts`: options map for the idea's expiry or None; `earn`: ISO date or None; `bet`: largest aligned print or None."""
    sym = c["ticker"]
    close = df["Close"]
    e34, e50 = close.ewm(span=34, adjust=False).mean(), close.ewm(span=50, adjust=False).mean()
    d = close.diff()
    upm = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()          # Wilder smoothing
    dnm = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + upm / dnm.replace(0, np.nan))
    rsi = rsi.fillna(100.0).where(~((upm == 0) & (dnm == 0)), 50.0)
    v = df.iloc[-N:]
    x = np.arange(N)
    rs_win = None
    if spy is not None and len(spy):
        sp = spy["Close"].reindex(df.index).ffill().bfill()
        if sp.notna().all() and (sp > 0).all():
            rs = close / sp
            rs = rs / rs.iloc[-N] * 100
            rs_win = rs.iloc[-N:].values

    hi, lo = float(v.High.max()), float(v.Low.min())
    rng = hi - lo
    last = float(v.Close.iloc[-1])
    hi_i, lo_i = int(v.High.values.argmax()), int(v.Low.values.argmin())
    upswing = lo_i < hi_i
    pos = lambda r: (lo + rng * r) if upswing else (hi - rng * r)
    near_end = abs(last - pos(1)) < abs(last - pos(0))
    ratios = [0, .382, .5, .618, 1] + ([1.272, 1.618] if near_end else [-.272, -.618])
    # extensions only when close enough to matter (first within 8% of price, second within 6%): a far one just wastes plot height
    ratios = [r for r in ratios if (r in (1.272, -.272) and abs(pc(pos(r), last)) <= 8) or (r in (1.618, -.618) and abs(pc(pos(r), last)) <= 6) or r not in (1.272, -.272, 1.618, -.618)]
    levels = sorted([(pos(r), r) for r in ratios])
    prices = [p for p, _ in levels]

    trade = [float(c[k]) for k in ("entry_low", "entry_high", "stop", "target1", "target2") if c.get(k) is not None]
    t_lo, t_hi = min(prices + trade + [lo]), max(prices + trade + [hi])
    near = lambda p: p is not None and (t_lo - .1 * (t_hi - t_lo)) <= p <= (t_hi + .1 * (t_hi - t_lo))

    o = opts or {}
    cw, pw, mp = o.get("call_wall"), o.get("put_wall"), o.get("max_pain")
    em = (o.get("expected_move") or {}).get("straddle")
    em_lo, em_hi = (last - em, last + em) if em else (None, None)
    mp_far = bool(mp) and bool(em) and not (em_lo <= mp <= em_hi)
    exp_d = dt.date.fromisoformat(o["expiry"]) if o.get("expiry") else None
    if exp_d is None and c.get("expiry_iso"):
        try:
            exp_d = dt.date.fromisoformat(c["expiry_iso"])
        except ValueError:
            exp_d = None

    ext = [p for p in ([cw[0]] if cw and near(cw[0]) else []) + ([pw[0]] if pw and near(pw[0]) else []) + ([mp] if near(mp) else []) +
           ([bet["strike"]] if bet and near(bet["strike"]) else [])]
    # fib levels (extensions) and the expected-move band no longer stretch the axis: they clip at the edge, so no dead space above/below the candles
    ymin, ymax = min(trade + ext + [lo]), max(trade + ext + [hi])

    # volume by price
    bins = np.linspace(lo, hi, 31)
    mid = (bins[:-1] + bins[1:]) / 2
    vp = np.histogram(((v.High + v.Low + v.Close) / 3).values, bins=bins, weights=v.Volume.values)[0]
    order = np.argsort(vp)[::-1]
    va = set(order[: int(np.searchsorted(np.cumsum(vp[order]) / max(vp.sum(), 1), .7)) + 1])
    poc = float(mid[vp.argmax()])
    share_above = float(vp[mid > last].sum() / max(vp.sum(), 1) * 100)

    # prior-day levels and gap
    dates = sorted({t.date() for t in v.index})
    today, prev = dates[-1], dates[-2]
    pr = df[[t.date() == prev for t in df.index]]
    td = df[[t.date() == today for t in df.index]]
    t0 = next(k for k in range(N) if v.index[k].date() == today)
    pdh, pdl, pdc = float(pr.High.max()), float(pr.Low.min()), float(pr.Close.iloc[-1])
    gap = float(td.Open.iloc[0]) / pdc - 1
    gx = next(k for k in range(N) if v.index[k] == td.index[0])
    filled = float(td.Low.min()) <= pdc if gap > 0 else float(td.High.max()) >= pdc

    by_day = df.groupby([t.date() for t in df.index]).agg(H=("High", "max"), L=("Low", "min"))
    day_rng = float((by_day.H - by_day.L).iloc[-14:].mean())
    wk_rng = float(np.mean([float(by_day.H.iloc[i:i + 5].max() - by_day.L.iloc[i:i + 5].min()) for i in range(len(by_day) - 15, len(by_day) - 4)]))
    daily_close = df.groupby([t.date() for t in df.index])["Close"].last()
    usual = df.groupby((df.index.hour * 100 + df.index.minute).values)["Volume"].transform("mean")

    d34, d50 = float(e34.iloc[-1]), float(e50.iloc[-1])
    top, bot = max(d34, d50), min(d34, d50)
    state = ("uptrend" if (last > top and d34 >= d50) else "downtrend" if (last < bot and d34 < d50) else
             "inside the cloud" if bot <= last <= top else "pullback" if last < bot else "bounce")
    bias = "up" if state in ("uptrend", "bounce") else "down" if state in ("downtrend", "pullback") else "neutral"
    firming = abs(pc(d34, d50)) > abs(pc(float(e34.iloc[-21]), float(e50.iloc[-21])))
    rv = float(rsi.iloc[-1])

    direction = str(c.get("direction", "")).upper()
    is_call = direction == "CALL"
    counter_why, counter_kind = None, None
    if (is_call and bias == "down") or ((not is_call) and bias == "up"):
        counter_why = f"a {'call' if is_call else 'put'} while the chart shows a {state}"
        counter_kind = "against"
    elif (is_call and rv >= 70) or ((not is_call) and rv <= 30):
        counter_why = f"a {'call' if is_call else 'put'} after price has already stretched {'up' if is_call else 'down'}"
        counter_kind = "stretched"

    return M(sym=sym, c=c, df=df, v=v, x=x, e34=e34, e50=e50, rsi=rsi, rs_win=rs_win, bench=bench, hi=hi, lo=lo, rng=rng, last=last,
             hi_i=hi_i, lo_i=lo_i, levels=levels, prices=prices, trade=trade, near=near, opts=o, cw=cw, pw=pw, mp=mp, em=em, em_lo=em_lo,
             em_hi=em_hi, mp_far=mp_far, exp_d=exp_d, ymin=ymin, ymax=ymax, bins=bins, mid=mid, vp=vp, va=va, poc=poc,
             share_above=share_above, today=today, prev=prev, td=td, t0=t0, pdh=pdh, pdl=pdl, pdc=pdc, gap=gap, gx=gx, filled=filled,
             day_rng=day_rng, wk_rng=wk_rng, daily_close=daily_close, usual=usual, d34=d34, d50=d50, state=state, bias=bias,
             firming=firming, rv=rv, chg=last / float(v.Close.iloc[-2]) - 1, earn=earn, bet=bet, session_day=session_day,
             direction=direction, is_call=is_call, counter_why=counter_why, counter_kind=counter_kind)


# ------------------------------------------------------------------------------------------------ flow text (pure)

def flow_numbers(c: dict) -> dict | None:
    """Aligned / opposite bought premium from the existing flow dict (no new selection logic)."""
    f = c.get("flow") or {}
    if not f.get("premium") or f.get("call_pct") is None:
        return None
    tot = float(f["premium"])
    call_pct = float(f["call_pct"])
    is_call = str(c.get("direction", "")).upper() == "CALL"
    share = call_pct if is_call else 100 - call_pct
    return {"total": tot, "aligned": tot * share / 100, "share": round(share), "kind": "calls" if is_call else "puts", "bias": f.get("bias")}


def flow_line(c: dict, bet: dict | None) -> str | None:
    """Compact one-liner for the chart header (fits the figure width)."""
    fn = flow_numbers(c)
    if not fn:
        return None
    s = f"{money(fn['aligned'])} of {fn['kind']} bought ({fn['share']}% of the bought flow)"
    if bet:
        s += f" · biggest print {money(bet['amount'])} at the ${bet['strike']:g} strike{(', ' + bet['order']) if bet.get('order') else ''}, exp {dt.date.fromisoformat(bet['expiry']).strftime('%b %d')}"
    return s


def flow_summary(c: dict, bet: dict | None, asof_d: dt.date | None = None) -> str | None:
    fn = flow_numbers(c)
    if not fn:
        return None
    s = f"{money(fn['aligned'])} of {fn['kind']} bought in the last session ({fn['share']}% of the bought flow, {money(fn['total'])} total)"
    if bet:
        s += (f"; largest print {money(bet['amount'])} of {bet['kind']} bought at the ${bet['strike']:g} strike"
              f"{(', ' + bet['order']) if bet.get('order') else ''}, expires {dt.date.fromisoformat(bet['expiry']).strftime('%b %d')}")
    return s


# ------------------------------------------------------------------------------------------------ notes (pure)

def counter_trend_explainer(m: M, ups: list, dns: list) -> str:
    """Plain words for a counter-trend idea: what the chart is doing, what the options buyers are betting on, and the one thing that
    has to happen first. No jargon."""
    sym = m.sym
    if m.counter_kind == "stretched":
        way = "down" if not m.is_call else "up"
        return (f"This is a counter-trend idea. {sym} has already moved a long way {way}, so a {'call' if m.is_call else 'put'} now is chasing that move. "
                f"Moves this stretched often pause or reverse first, so the idea can look wrong for a while before it works.")
    fn = flow_numbers(m.c)
    big = f"big traders bought {money(fn['aligned'])} of {fn['kind']}" if fn else "big options traders are positioned the other way"
    if m.is_call:
        need = (f" It needs price to turn up first: a move back above {ups[0]:,.2f} would be the first sign buyers are back." if ups else " It needs price to turn up first.")
        return f"This is a counter-trend idea. {sym} is still falling, but {big}, betting on a bounce. Until the stock turns, the trend is working against this call.{need}"
    need = (f" It needs price to turn down first: a slip under {dns[0]:,.2f} would be the first sign sellers are taking over." if dns else " It needs price to turn down first.")
    return f"This is a counter-trend idea. {sym} is still rising, but {big}, betting on a drop. Until the stock turns, the trend is working against this put.{need}"


def build_notes(m: M) -> list:
    sym, last, c = m.sym, m.last, m.c
    prices, cw, pw, mp, em, exp_d, earn, bet = m.prices, m.cw, m.pw, m.mp, m.em, m.exp_d, m.earn, m.bet
    notes = []
    top, bot, d34, d50 = max(m.d34, m.d50), min(m.d34, m.d50), m.d34, m.d50
    firm = "strengthening" if m.firming else "slowing"
    if m.state == "uptrend":
        t = f"Uptrend. Price ({last:,.2f}) is above both averages, {pc(last, top):.1f}% over the cloud. The trend is {'strengthening' if m.firming else 'still up but slowing'}."
    elif m.state == "downtrend":
        t = f"Downtrend. Price ({last:,.2f}) is below both averages, {abs(pc(last, bot)):.1f}% under the cloud. The trend is {'strengthening' if m.firming else 'still down but slowing'}."
    elif m.state == "inside the cloud":
        t = f"No clear trend. Price is sitting inside the cloud, between the two averages ({bot:,.2f} and {top:,.2f})."
    elif m.state == "pullback":
        t = f"Pullback. Price ({last:,.2f}) dropped {abs(pc(last, bot)):.1f}% under the cloud, but the 34 is still above the 50, so the longer uptrend has not turned yet."
    else:
        t = f"Bounce. Price ({last:,.2f}) is {pc(last, top):.1f}% above the cloud, but the 34 is still under the 50, so the longer downtrend has not turned yet."
    notes.append(("Trend", t))

    rv, rr = m.rv, m.rsi.iloc[-N:]
    rsi_peak = float(rr.max())
    t = (f"RSI {rv:.0f}: stretched to the upside. Rallies this extended often pause." if rv >= 70 else
         f"RSI {rv:.0f}: stretched to the downside. Selloffs this extended often pause." if rv <= 30 else f"RSI {rv:.0f}: a normal reading, not stretched either way.")
    if m.last >= m.hi * .995 and rv < rsi_peak - 5:
        t += f" Price is at its high, but momentum peaked higher ({rsi_peak:.0f} on {rr.idxmax().strftime('%b %d')}): the buying is less forceful this time."
    elif m.last <= m.lo * 1.005 and rv > float(rr.min()) + 5:
        t += f" Price is at its low, but momentum bottomed lower ({float(rr.min()):.0f} on {rr.idxmin().strftime('%b %d')}): the selling is less forceful this time."
    notes.append(("Momentum", t))

    if m.rs_win is not None:
        rsw = m.rs_win
        ahead = rsw[-1] >= 100
        nm = "the market" if m.bench == "SPY" else m.bench
        notes.append(("Versus " + nm, f"{'Beating' if ahead else 'Trailing'} {nm}: {sym} is {abs(rsw[-1] - 100):.1f}% {'ahead of' if ahead else 'behind'} {m.bench} "
                      f"since {m.v.index[0].strftime('%b %d')}, and {'still pulling ahead' if rsw[-1] > rsw[-20] else 'losing ground'} lately."))

    ups = sorted([p for p in prices if p > last])[:2]
    dns = sorted([p for p in prices if p <= last], reverse=True)[:1]
    t = ""
    if ups:
        t += f"Above: {' then '.join(f'{p:,.2f}' for p in ups)} ({pc(ups[0], last):+.1f}% to the first). "
    if dns:
        t += f"Below: {dns[0]:,.2f} ({pc(dns[0], last):+.1f}%) is the first level price has to hold."
    notes.append(("Key levels", t.strip()))

    # the idea itself (drawn on the chart: entry zone, stop, targets)
    if c.get("strike") and c.get("entry_low") is not None:
        k = c["strike"]
        dist = pc(k, last)
        days = abs(k - last) / m.day_rng if m.day_rng else 0
        t = (f"{m.direction} ${k:g}{(' expiring ' + c['next_expiry']) if c.get('next_expiry') else ''}"
             f"{(' (' + str(c['dte']) + ' days)') if c.get('dte') is not None else ''}. The strike is {abs(dist):.1f}% {'above' if k > last else 'below'} the price, "
             f"about {days:.1f} days of normal movement away. Plan on the chart: entry {c['entry_low']:,.2f}-{c['entry_high']:,.2f}, "
             f"stop {c['stop']:,.2f}, target 1 {c['target1']:,.2f}, target 2 {c['target2']:,.2f}.")
        notes.append(("The idea", t))
    if m.counter_why:
        notes.append(("Why this goes against the chart", counter_trend_explainer(m, ups, dns)))

    fn = flow_numbers(c)
    if fn:
        t = f"{money(fn['aligned'])} of {fn['kind']} were bought in the last session, {fn['share']}% of all the bought flow ({money(fn['total'])})."
        if bet:
            bd = dt.date.fromisoformat(bet["expiry"])
            t += (f" The largest single print was {money(bet['amount'])} of {bet['kind']} at the ${bet['strike']:g} strike"
                  f"{(' (' + bet['order'] + ')') if bet.get('order') else ''}, expiring {bd.strftime('%b %d')}, "
                  f"{abs(pc(bet['strike'], last)):.1f}% {'above' if bet['strike'] > last else 'below'} the price.")
        notes.append(("Options flow behind the pick", t))

    if cw or pw or em:
        t = ""
        if cw:
            t += f"The call wall is {cw[0]:,.2f} ({pc(cw[0], last):+.1f}%): the strike with the most open call contracts, which traders watch as a possible ceiling"
        if pw:
            t += f"{'; the' if t else 'The'} put wall is {pw[0]:,.2f} ({pc(pw[0], last):+.1f}%): the most open put contracts, watched as a possible floor"
        t += ". " if t else ""
        if em and exp_d:
            t += f"The options market prices a move of about ±{em:,.2f} ({em / last * 100:.1f}%) by {exp_d.strftime('%b %d')}: {m.em_lo:,.2f} to {m.em_hi:,.2f}."
        if earn and exp_d:
            ed = dt.date.fromisoformat(earn[:10])
            t += f" Earnings ({ed.strftime('%b %d')}) fall {'after' if ed > exp_d else 'before'} this expiry."
        notes.append(("Options map", t.strip()))
    if mp:
        t = (f"{mp:,.2f} ({pc(mp, last):+.1f}%) is where option buyers as a group would lose the most at expiry, so sellers keep the most premium. "
             "Price sometimes drifts toward it into expiry. ")
        t += ("It is outside the options-implied range, so a weak signal." if m.mp_far else "It is inside the options-implied range, so worth watching into expiry.")
        notes.append(("Max pain", t))

    t = f"Most shares traded near {m.poc:,.2f}, {abs(pc(m.poc, last)):.1f}% {'below' if m.poc < last else 'above'} the current price."
    if m.share_above < 15:
        t += f" Little has traded above {last:,.2f} in this window, so there is less history up there."
    elif m.share_above > 85:
        t += f" Little has traded below {last:,.2f} in this window, so there is less history down there."
    notes.append(("Where trading happened", t))

    reach = ""
    if ups:
        reach = f" The first level above ({ups[0]:,.2f}) is about {abs(ups[0] - last) / m.day_rng:.1f} days of normal movement away"
        reach += (f", the next ({ups[1]:,.2f}) about {abs(ups[1] - last) / m.day_rng:.1f}." if len(ups) > 1 else ".")
    if dns:
        reach += f" The first level below ({dns[0]:,.2f}) is about {abs(last - dns[0]) / m.day_rng:.1f} days away."
    notes.append(("How far it normally moves", f"{sym} typically swings {m.day_rng:,.2f} ({m.day_rng / last * 100:.1f}%) a day and {m.wk_rng:,.2f} ({m.wk_rng / last * 100:.1f}%) over five days." + reach))

    where = (f"above yesterday's high ({m.pdh:,.2f})" if last > m.pdh else f"below yesterday's low ({m.pdl:,.2f})" if last < m.pdl
             else f"inside yesterday's range ({m.pdl:,.2f}-{m.pdh:,.2f})")
    notes.append(("Yesterday and the gap", f"Price is {where}. It opened {abs(m.gap) * 100:.2f}% {'above' if m.gap > 0 else 'below'} yesterday's close ({m.pdc:,.2f}) "
                  f"and that gap is {'closed' if m.filled else 'still open'}."))
    return notes


# ------------------------------------------------------------------------------------------------ facts for the read (pure)

def _analyst_fact(c: dict, last: float) -> str | None:
    """From the existing analyst_target string ("Analyst target $182.50 (+3.2% from current), consensus: Buy").
    Reworded to "price goal": the read may not use the word target."""
    s = c.get("analyst_target") or ""
    mm = re.search(r"\$([\d,]+\.?\d*)\s*\(([+-]?[\d.]+)% from current\)(?:, consensus: (.+))?", s)
    if not mm:
        return None
    px = float(mm.group(1).replace(",", ""))
    pct = pc(px, last)
    side = "below" if px < last else "above"
    cons = f", consensus {mm.group(3).strip().lower()}" if mm.group(3) and mm.group(3).strip().lower() != "not provided" else ""
    return f"analysts' average price goal is {px:,.2f} ({pct:+.1f}% from here, {side} the price){cons}"


def build_facts(m: M) -> dict:
    """Everything the one-short-read may mention, as exact display strings (the model copies them; the checker rejects any
    number that is not here)."""
    sym, last, c = m.sym, m.last, m.c
    prices, cw, pw, mp, em, exp_d, earn, bet = m.prices, m.cw, m.pw, m.mp, m.em, m.exp_d, m.earn, m.bet
    top, bot = max(m.d34, m.d50), min(m.d34, m.d50)
    f = {"symbol": sym, "price": f"{last:,.2f}", "data_as_of": m.session_day.strftime("%b %d"),
         "trend": m.state, "bias": m.bias, "ema34": f"{m.d34:,.2f}", "ema50": f"{m.d50:,.2f}"}
    if not (bot <= last <= top):
        f["price_vs_cloud"] = f"{pc(last, top if last > top else bot):+.1f}%"
    f["trend_momentum"] = "strengthening" if m.firming else "slowing"
    rr = m.rsi.iloc[-N:]
    f["rsi"] = f"{m.rv:.0f}"
    f["rsi_zone"] = "stretched up (70+)" if m.rv >= 70 else "stretched down (30 or less)" if m.rv <= 30 else "normal (30-70)"
    if m.last >= m.hi * .995 and m.rv < float(rr.max()) - 5:
        f["rsi_note"] = f"price is at its high but momentum peaked higher ({float(rr.max()):.0f} on {rr.idxmax().strftime('%b %d')})"
    elif m.last <= m.lo * 1.005 and m.rv > float(rr.min()) + 5:
        f["rsi_note"] = f"price is at its low but momentum bottomed lower ({float(rr.min()):.0f} on {rr.idxmin().strftime('%b %d')})"
    if m.rs_win is not None:
        rsw = m.rs_win
        f[f"vs_{m.bench}"] = f"{rsw[-1] - 100:+.1f}% since {m.v.index[0].strftime('%b %d')}, {'gaining' if rsw[-1] > rsw[-20] else 'losing'} ground lately"
    ups = sorted([p for p in prices if p > last])[:2]
    dns = sorted([p for p in prices if p <= last], reverse=True)[:1]
    if ups:
        f["next_level_above"] = f"{ups[0]:,.2f} ({pc(ups[0], last):+.1f}%)"
    if len(ups) > 1:
        f["second_level_above"] = f"{ups[1]:,.2f} ({pc(ups[1], last):+.1f}%)"
    if dns:
        f["first_level_below"] = f"{dns[0]:,.2f} ({pc(dns[0], last):+.1f}%)"
    f["typical_daily_move"] = f"{m.day_rng / last * 100:.1f}%"
    f["typical_weekly_move"] = f"{m.wk_rng / last * 100:.1f}%"
    f["gap_today"] = f"{m.gap * 100:+.2f}% vs the prior close, {'closed' if m.filled else 'still open'}"
    f["vs_yesterday_range"] = "above yesterday's high" if last > m.pdh else "below yesterday's low" if last < m.pdl else "inside yesterday's range"
    if len(m.daily_close) >= 6:
        f["five_day_change"] = f"{pc(last, float(m.daily_close.iloc[-6])):+.1f}%"
    f["window_high"] = f"{m.hi:,.2f} on {m.v.index[m.hi_i].strftime('%b %d')} ({pc(last, m.hi):+.1f}% from here)"
    f["window_low"] = f"{m.lo:,.2f} on {m.v.index[m.lo_i].strftime('%b %d')} ({pc(last, m.lo):+.1f}% from here)"
    f["volume_vs_normal"] = f"{float((m.df['Volume'] / m.usual).iloc[-7:].mean()):.1f}x the usual for these hours"
    f["busiest_price"] = f"{m.poc:,.2f} ({pc(m.poc, last):+.1f}%)"
    if exp_d:
        f["options_expiry"] = exp_d.strftime("%b %d")
    if cw:
        f["call_wall"] = f"{cw[0]:,.2f} ({pc(cw[0], last):+.1f}%)"
    if pw:
        f["put_wall"] = f"{pw[0]:,.2f} ({pc(pw[0], last):+.1f}%)"
    if em and exp_d:
        f["expected_move"] = f"±{em:,.2f} (±{em / last * 100:.1f}%) by {exp_d.strftime('%b %d')}, i.e. {m.em_lo:,.2f} to {m.em_hi:,.2f}"
    if mp:
        f["max_pain"] = f"{mp:,.2f} ({pc(mp, last):+.1f}%), " + ("outside the expected range so a weak signal" if m.mp_far else "inside the expected range")
    if earn and exp_d:
        ed = dt.date.fromisoformat(earn[:10])
        f["earnings"] = f"{ed.strftime('%b %d')}, {'after' if ed > exp_d else 'before'} the {exp_d.strftime('%b %d')} expiry"
    # the idea itself
    k = c.get("strike")
    if k:
        days = abs(k - last) / m.day_rng if m.day_rng else 0
        f["strike_price"] = f"{k:g}"
        f["idea"] = f"{m.direction.lower()} at the {k:g} strike{(', expires ' + exp_d.strftime('%b %d')) if exp_d else ''}"
        if c.get("dte") is not None:
            f["days_to_expiry"] = f"{c['dte']}"
        f["strike_distance"] = f"{pc(k, last):+.1f}% from the price, about {days:.1f} days of normal movement"
    f["idea_vs_chart"] = (f"counter-trend: {m.counter_why}" if m.counter_why else "agrees with the chart")
    f["counter_trend"] = bool(m.counter_why)
    if m.counter_why:
        f["counter_kind"] = m.counter_kind
        f["counter_trend_story"] = (
            f"the chart is {'falling' if m.bias == 'down' else 'rising'}, but the idea is a {'call betting on a bounce' if m.is_call else 'put betting on a drop'}; "
            "it only works if price turns first" if m.counter_kind == "against" else
            f"price has already moved a long way {'up' if m.is_call else 'down'}, so a {'call' if m.is_call else 'put'} now is chasing the move; a pause or reversal can come first")
    fs = flow_summary(c, bet)
    if fs:
        f["options_flow"] = fs
    af = _analyst_fact(c, last)
    if af:
        f["analyst_view"] = af
    return f


# ------------------------------------------------------------------------------------------------ drawing

NOTE_FS, NOTE_TITLE_FS, WRAP = 7.5, 8.0, 72


def _layout_notes(notes: list):
    lh = NOTE_FS * 1.32 / 72
    est = [(1 + len(textwrap.wrap(b, WRAP))) * lh + .075 for _, b in notes]
    half = min(range(1, len(notes)), key=lambda i: max(sum(est[:i]), sum(est[i:]))) if len(notes) > 1 else 1
    return half, max(sum(est[:half]), sum(est[half:])), lh


def _spread(tags: list, gap: float, lo: float, hi: float):
    """Push tag y-positions apart (min `gap`), keeping them inside [lo, hi]: up pass, then down pass if the top overflows."""
    tags.sort(key=lambda t: t[0])
    for k in range(1, len(tags)):
        if tags[k][1] - tags[k - 1][1] < gap:
            tags[k][1] = tags[k - 1][1] + gap
    if tags and tags[-1][1] > hi:
        tags[-1][1] = hi
        for k in range(len(tags) - 2, -1, -1):
            if tags[k + 1][1] - tags[k][1] < gap:
                tags[k][1] = tags[k + 1][1] - gap
    if tags and tags[0][1] < lo:
        tags[0][1] = lo
        for k in range(1, len(tags)):
            if tags[k][1] - tags[k - 1][1] < gap:
                tags[k][1] = tags[k - 1][1] + gap


def render(m: M, notes: list, out_path, dpi: int = 110) -> None:
    sym, last, c, v, x = m.sym, m.last, m.c, m.v, m.x
    prices, cw, pw, mp, em, exp_d, earn, bet = m.prices, m.cw, m.pw, m.mp, m.em, m.exp_d, m.earn, m.bet
    asof_d = m.session_day
    levels = m.levels
    rr1, rr2 = c.get("rr", (None, None)) if isinstance(c.get("rr"), (tuple, list)) else (None, None)

    # ---------- header content ----------
    row1 = []
    if c.get("entry_low") is not None:
        row1 += [("ENTRY ZONE", f"{c['entry_low']:,.2f}-{c['entry_high']:,.2f}", "", ENTRYC),
                 ("STOP", f"{c['stop']:,.2f}", f"{pc(c['stop'], last):+.1f}%", STOPC),
                 ("TARGET 1", f"{c['target1']:,.2f}", f"{pc(c['target1'], last):+.1f}%", T1C),
                 ("TARGET 2", f"{c['target2']:,.2f}", f"{pc(c['target2'], last):+.1f}%", T2C)]
        if rr1 is not None:
            row1.append(("R:R", f"{rr1:.1f} / {rr2:.1f}", "T1 / T2", TXT))
        if c.get("strike"):
            row1.append(("STRIKE", f"{c['strike']:g}", f"{pc(c['strike'], last):+.1f}% away", TXT))
    cells = []
    if cw:
        cells.append(("CALL WALL", f"{cw[0]:,.2f}", f"{pc(cw[0], last):+.1f}% · {fmtk(cw[1])} open", CWC))
    if pw:
        cells.append(("PUT WALL", f"{pw[0]:,.2f}", f"{pc(pw[0], last):+.1f}% · {fmtk(pw[1])} open", PWC))
    if mp:
        cells.append(("MAX PAIN", f"{mp:,.2f}", f"{pc(mp, last):+.1f}%" + (" · weak" if m.mp_far else ""), MPC))
    if em and exp_d:
        cells.append(("EXPECTED MOVE", f"±{em:,.2f}", f"±{em / last * 100:.1f}% by {exp_d.strftime('%b %d')}", EMC))
    if earn:
        ed = dt.date.fromisoformat(earn[:10])
        cells.append(("EARNINGS", ed.strftime("%b %d"), f"in {(ed - asof_d).days} days", TXT))
    fl = flow_line(c, bet)

    half, notes_h, lh = _layout_notes(notes)
    W = 10.4
    HEAD = 0.95 + (0.50 if row1 else 0) + (0.82 if cells else 0) + (0.34 if fl else 0)
    MAIN, PAN, GAP, XLAB = 7.3, 1.3, 0.1, 0.32
    NOTES_TOP_PAD, FOOT = 0.42, 0.42
    H = HEAD + MAIN + GAP + PAN + GAP + PAN + XLAB + NOTES_TOP_PAD + notes_h + FOOT
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=BG)
    fy = lambda t: 1 - t / H                       # inches from the top -> figure fraction
    fx = lambda i: i / W
    LEFT, AXW = 0.5, 7.9

    def axes_at(top, h):
        a = fig.add_axes([fx(LEFT), fy(top + h), AXW / W, h / H], facecolor=BG)
        a.grid(False)
        [s.set_visible(False) for s in a.spines.values()]
        a.tick_params(colors=MUTE, labelsize=8, length=0)
        a.set_xlim(-1, N + 21)
        a.set_xticklabels([])
        return a

    ax = axes_at(HEAD, MAIN)
    ar = axes_at(HEAD + MAIN + GAP, PAN)
    av = axes_at(HEAD + MAIN + GAP + PAN + GAP, PAN)

    rngv = m.ymax - m.ymin
    padv = rngv * .06
    vol_h = rngv * .12
    ax.set_ylim(m.ymin - padv - vol_h, m.ymax + padv)
    ylo, yhi = ax.get_ylim()
    yr = yhi - ylo

    # expected-move band
    if em and exp_d:
        ax.axhspan(m.em_lo, m.em_hi, color=EMC, alpha=.07, lw=0, zorder=0)
        for p in (m.em_lo, m.em_hi):
            ax.axhline(p, color=EMC, lw=.9, ls=(0, (1, 2)), alpha=.9, zorder=1)
        ax.text(.8, max(m.em_lo, ylo + vol_h * 1.25), f"options-implied range to {exp_d.strftime('%b %d')}: {m.em_lo:,.2f} to {m.em_hi:,.2f}", color=EMC, fontsize=7, va="top" if m.em_lo >= ylo + vol_h * 1.25 else "bottom", ha="left",
                fontweight="bold", zorder=4, bbox=dict(fc=BG, ec="none", pad=.5, alpha=.8))

    # volume by price
    w = m.vp / max(m.vp.max(), 1) * 13
    ax.barh(m.mid, w, height=(m.bins[1] - m.bins[0]) * .9, left=N + 20 - w,
            color=["#3b5bdb" if k in m.va else "#a5b4e8" for k in range(len(m.vp))], alpha=.75, lw=0, zorder=1)
    ax.axhline(m.poc, color="#3b5bdb", lw=.8, alpha=.5, zorder=1)
    ax.text(N + 5.5, m.poc, "POC", color="#3b5bdb", fontsize=7.5, va="center", ha="right", fontweight="bold", bbox=dict(fc=BG, ec="none", pad=.8))

    # EMA cloud
    a_, b_ = m.e34.iloc[-N:].values, m.e50.iloc[-N:].values
    ax.fill_between(x, a_, b_, where=a_ >= b_, color=BLUE, alpha=.14, lw=0, interpolate=True)
    ax.fill_between(x, a_, b_, where=a_ < b_, color=AMB, alpha=.16, lw=0, interpolate=True)
    ax.plot(x, a_, color=BLUE, lw=1.2)
    ax.plot(x, b_, color=AMB, lw=1.2)

    # volume bars
    vmax = v.Volume.max()
    for k, (op, cl, vol) in enumerate(zip(v.Open, v.Close, v.Volume)):
        ax.add_patch(plt.Rectangle((k - .36, ylo), .72, vol / vmax * vol_h * .95, color=UP if cl >= op else DN, alpha=.5, lw=0, zorder=1))

    # previous-day levels and the gap
    for val, nm in ((m.pdh, "PDH"), (m.pdl, "PDL"), (m.pdc, "PDC")):
        ax.plot([m.t0 - .5, N - .5], [val, val], color=PD, lw=.9, ls=(0, (1, 2)), zorder=2)
        ax.text(m.t0 - .8, val, f"{nm} {val:,.2f}", color=PD, fontsize=7, va="center", ha="right", zorder=4, bbox=dict(fc=BG, ec="none", pad=.6, alpha=.85))
    ax.scatter([m.gx], [float(m.td.Open.iloc[0])], marker=">", s=22, color=PD, zorder=6)

    # candles
    for k, (op, h, l, cl) in enumerate(zip(v.Open, v.High, v.Low, v.Close)):
        col = UP if cl >= op else DN
        ax.plot([k, k], [l, h], color=col, lw=.9, zorder=3)
        ax.add_patch(plt.Rectangle((k - .33, min(op, cl)), .66, max(abs(cl - op), m.rng * .0015), color=col, zorder=3, lw=0))

    # idea: entry zone, stop, targets
    tags = []
    if c.get("entry_low") is not None:
        ax.axhspan(c["entry_low"], c["entry_high"], color=ENTRYC, alpha=.16, lw=0, zorder=1)
        for key, nm, col, ls in (("stop", "STOP", STOPC, "-"), ("target1", "T1", T1C, "-"), ("target2", "T2", T2C, (0, (6, 3)))):
            ax.axhline(c[key], color=col, lw=1.6, ls=ls, alpha=.9, zorder=2)
            tags.append([c[key], c[key], f"{nm}  {c[key]:,.2f}  ({pc(c[key], last):+.1f}%)", col, "idea"])

    # fibonacci levels
    above = [p for p in prices if p > last]
    below = [p for p in prices if p <= last]
    nxt_up, nxt_dn = (min(above) if above else None), (max(below) if below else None)
    if nxt_up:
        ax.axhspan(last, nxt_up, color=RES, alpha=.06, lw=0, zorder=0)
    if nxt_dn:
        ax.axhspan(nxt_dn, last, color=SUP, alpha=.06, lw=0, zorder=0)
    for p, r in levels:
        col = RES if p > last else SUP
        key = p in (nxt_up, nxt_dn)
        ax.axhline(p, color=col, lw=1.5 if key else .8, ls="-" if key else (0, (5, 4)), alpha=1 if key else .6, zorder=2)
    # Right-hand axis tags are kept to the five that matter: STOP, T1, T2 (above), call wall and put wall (below).
    # Fibonacci levels, max pain and the flow strike stay as plain lines (their numbers are in the header and the notes).
    for val, nm, col, ls in ((cw[0] if cw else None, "Call wall", CWC, (0, (6, 2, 1, 2))), (pw[0] if pw else None, "Put wall", PWC, (0, (6, 2, 1, 2))),
                             (mp, "Max pain", MPC, (0, (1, 2)))):
        if val is not None and m.near(val):
            ax.axhline(val, color=col, lw=1.2, ls=ls, alpha=.95, zorder=2)
            if nm != "Max pain":
                tags.append([val, val, f"{nm}  {val:,.2f}", col, "key"])
    if bet and m.near(bet["strike"]):
        ax.axhline(bet["strike"], color=BETC, lw=1.1, ls=(0, (2, 2)), alpha=.75, zorder=4)
    tags.append([last, last, f"{last:,.2f}", TXT, "last"])
    _spread(tags, yr * .029, ylo + yr * .012, yhi - yr * .012)
    for p, ty, txt, col, kind in tags:
        if kind == "last":
            bb = dict(boxstyle="round,pad=0.25", fc=TXT, ec="none")
            tc, fw = "white", "bold"
        elif kind == "idea":
            bb = dict(boxstyle="round,pad=0.25", fc=BG, ec=col, lw=1.4)
            tc, fw = col, "bold"
        else:
            bb = dict(boxstyle="round,pad=0.25", fc=col, ec="none", alpha=1 if kind == "key" else .8)
            tc, fw = "white", "bold" if kind == "key" else "normal"
        ax.text(N + 20.4, ty, txt, color=tc, fontsize=7.8, va="center", ha="left", fontweight=fw, bbox=bb, clip_on=False, zorder=7)
    ax.axhline(last, color=TXT, lw=.7, ls=":", alpha=.8, zorder=2)

    # ---------- header ----------
    dir_col = UP if m.is_call else DN
    fig.text(fx(LEFT), fy(.30), sym, color=TXT, fontsize=17, fontweight="bold", va="center")
    fig.text(fx(LEFT + .30 + .17 * len(sym)), fy(.30), f"{last:,.2f}   {m.chg * 100:+.2f}% last hour", color=UP if m.chg >= 0 else DN, fontsize=11, va="center")
    if c.get("strike"):
        tag = f"{m.direction} ${c['strike']:g}" + (f"  ·  {c['next_expiry']}" if c.get("next_expiry") else "")
        fig.text(fx(W - LEFT), fy(.30), tag, color="white", fontsize=11, fontweight="bold", va="center", ha="right",
                 bbox=dict(boxstyle="round,pad=0.35", fc=dir_col, ec="none"))
    fig.text(fx(LEFT), fy(.60), f"Data through the {asof_d.strftime('%b %d')} close · 1h candles from the 9:30 open, regular hours only · EMA 34/50 cloud · "
             "blue bars = volume by price (IEX volume)", color=MUTE, fontsize=7.8, va="center")

    def strip(top, title, items):
        fig.text(fx(LEFT), fy(top), title, color=MUTE, fontsize=7.2, fontweight="bold", va="center")
        cw_ = (W - 2 * LEFT) / len(items)
        for i, (lab, val, sub, col) in enumerate(items):
            x0 = LEFT + i * cw_
            fig.patches.append(FancyBboxPatch((fx(x0 + .03), fy(top + .08 + .44)), (cw_ - .06) / W, .44 / H, boxstyle="round,pad=0,rounding_size=.006",
                                              transform=fig.transFigure, fc=BOX, ec="none", zorder=0))
            fig.text(fx(x0 + .12), fy(top + .08 + .12), lab, color=MUTE, fontsize=6.4, fontweight="bold", va="center")
            fig.text(fx(x0 + .12), fy(top + .08 + .30), val, color=col, fontsize=11, fontweight="bold", va="center")
            if sub:
                fig.text(fx(x0 + cw_ - .12), fy(top + .08 + .30), sub, color=MUTE, fontsize=6.4, va="center", ha="right")

    cur = .88
    if row1:
        strip(cur, "THE IDEA · entry, stop and targets are on the chart", row1)
        cur += .62
    if cells:
        title = (f"OPTIONS MAP · {exp_d.strftime('%b %d')} expiry ({(exp_d - asof_d).days} days) · open interest as of "
                 f"{dt.date.fromisoformat(m.opts['oi_date']).strftime('%b %d')}") if exp_d and m.opts.get("oi_date") else "KEY NUMBERS"
        cur += .20
        strip(cur, title, cells)
        cur += .62
    if fl:
        txt = "◆ Flow behind the pick: " + fl
        fig.text(fx(LEFT), fy(cur + .08), txt, color=BETC, fontsize=8, fontweight="bold", va="center")

    # ---------- indicator panels ----------
    ar.plot(x, m.rsi.iloc[-N:].values, color=VIO, lw=1.4)
    ar.set_ylim(10, 90)
    ar.set_yticks([30, 70])
    ar.axhspan(70, 90, color=DN, alpha=.07, lw=0)
    ar.axhspan(10, 30, color=UP, alpha=.07, lw=0)
    for lv in (30, 50, 70):
        ar.axhline(lv, color=MUTE, lw=.5, ls=(0, (3, 3)), alpha=.6)
    ar.text(.005, .93, f"RSI 14   {m.rsi.iloc[-1]:.0f}", transform=ar.transAxes, color=TXT, fontsize=8.5, va="top", bbox=dict(fc=BG, ec="none", pad=1.5, alpha=.9), zorder=6)
    if m.rs_win is not None:
        r = m.rs_win
        av.plot(x, r, color=BLUE, lw=1.4)
        av.fill_between(x, r, 100, where=r >= 100, color=UP, alpha=.2, lw=0, interpolate=True)
        av.fill_between(x, r, 100, where=r < 100, color=DN, alpha=.2, lw=0, interpolate=True)
        av.axhline(100, color=MUTE, lw=.6, ls=(0, (3, 3)))
        av.set_yticks([100])
        lo_r, hi_r = min(r.min(), 100), max(r.max(), 100)
        pad_r = max(hi_r - lo_r, 0.5)
        av.set_ylim(lo_r - pad_r * .1, hi_r + pad_r * .55)
        av.text(.005, .93, f"Relative strength vs {m.bench}   {r[-1] - 100:+.1f}% over the window ({'outperforming' if r[-1] > r[-20] else 'lagging'} the last 20 bars)",
                transform=av.transAxes, color=TXT, fontsize=8.5, va="top", bbox=dict(fc=BG, ec="none", pad=1.5, alpha=.9), zorder=6)
    else:
        av.text(.5, .5, f"Relative strength vs {m.bench} unavailable", transform=av.transAxes, color=MUTE, fontsize=8.5, ha="center", va="center")
    ticks = [k for k in range(N) if k == 0 or v.index[k].date() != v.index[k - 1].date()][::2]
    av.set_xticks(ticks)
    av.set_xticklabels([v.index[k].strftime("%b %d") for k in ticks])

    # ---------- plain-English notes ----------
    ny = HEAD + MAIN + GAP + PAN + GAP + PAN + XLAB + NOTES_TOP_PAD
    fig.text(fx(LEFT), fy(ny - .12), "IN PLAIN ENGLISH · written fresh from this chart's numbers", color=TXT, fontsize=8.2, fontweight="bold", va="top")

    def block(x0, items):
        y = ny + .08
        for name, body in items:
            fig.text(fx(x0), fy(y), name, color=TXT, fontsize=NOTE_TITLE_FS, fontweight="bold", va="top")
            y += lh
            lines = textwrap.wrap(body, WRAP)
            fig.text(fx(x0), fy(y), "\n".join(lines), color=MUTE, fontsize=NOTE_FS, va="top", linespacing=1.25)
            y += lh * len(lines) + .075

    block(LEFT, notes[:half])
    block(LEFT + 4.85, notes[half:])
    fig.text(fx(LEFT), fy(H - .12), "Reference levels and readings from price history and options data, not a forecast or advice. Entry, stop and targets come from the nightly idea. "
             "Volume is IEX exchange only; open interest is the prior day's.", color=MUTE, fontsize=6.6, va="bottom")
    fig.savefig(out_path, facecolor=BG, dpi=dpi)
    plt.close(fig)


def build_chart(c: dict, df, spy, opts, earn, bet, session_day: dt.date, out_path, dpi: int = 110) -> dict:
    """Render one chart and return {'facts':..., 'notes':..., 'bias':..., 'levels':...}. Raises on any failure (caller degrades)."""
    m = compute_metrics(c, df, spy, opts, earn, bet, session_day)
    notes = build_notes(m)
    facts = build_facts(m)
    render(m, notes, out_path, dpi=dpi)
    return {"facts": facts, "notes": notes, "bias": m.bias, "last": m.last, "levels": [(round(p, 2), r) for p, r in m.levels]}
