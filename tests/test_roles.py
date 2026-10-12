import setup_roles as ro


def S(t, d="CALL", px=100.0, strike=104.0, dte=10, prem=1_000_000, call_pct=80, ivrv=1.0):
    return {"ticker": t, "direction": d, "current_price": px, "strike": strike, "dte": dte, "iv_rv_ratio": ivrv,
            "flow": {"premium": prem, "call_pct": call_pct, "bias": "Bullish"},
            "entry_low": 99.5, "entry_high": 100.5, "stop": 97.0, "target1": 106.0, "target2": 112.0}


def five():
    return [S("A", strike=104, prem=1e6),          # rank 0, 4% move
            S("B", strike=101, prem=9e5),          # smallest move (1%)
            S("C", strike=115, prem=8e5),          # largest move (15%)
            S("D", strike=106, prem=9e6),          # biggest aligned flow
            S("E", strike=105, prem=2e6)]          # the leftover


def test_roles_in_documented_order_one_each():
    r = ro.assign_roles(five())
    assert r == {"A": "Best Overall", "B": "Lowest-Move Setup", "C": "Speculative / High-Risk Breakout Setup",
                 "D": "Flow-Backed Momentum Setup", "E": "Best Balanced Setup"}
    assert len(set(r.values())) == 5


def test_best_overall_is_always_the_existing_top_rank_even_if_another_has_smaller_move():
    sel = five()
    sel[0]["strike"] = 130                       # rank-0 now has the largest move; it is still Best Overall
    assert ro.assign_roles(sel)["A"] == "Best Overall"


def test_speculative_tie_break_is_lowest_dte():
    sel = [S("A"), S("B", strike=110, dte=12), S("C", strike=110, dte=5), S("D", strike=101), S("E", strike=103)]
    assert ro.assign_roles(sel)["C"] == "Speculative / High-Risk Breakout Setup"


def test_flow_role_uses_aligned_side_only():
    big_but_wrong_side = S("X", d="PUT", strike=96, prem=9e6, call_pct=90)      # a PUT idea with 90% call flow: aligned = 10%
    sel = [S("A"), big_but_wrong_side, S("C", strike=115), S("D", strike=101), S("E", strike=106, prem=3e6)]
    r = ro.assign_roles(sel)
    assert ro.aligned_flow_premium(big_but_wrong_side) == 9e5
    assert r["E"] == "Flow-Backed Momentum Setup"


def test_fewer_than_five_setups_never_duplicates():
    for n in range(1, 5):
        r = ro.assign_roles(five()[:n])
        assert len(r) == n and len(set(r.values())) == n


def test_required_move_is_signed_and_roles_use_absolute():
    put = S("P", d="PUT", strike=95)
    assert ro.required_move_pct(put) == -5.0
    sel = [S("A"), put, S("C", strike=130), S("D", strike=112), S("E", strike=108)]
    assert ro.assign_roles(sel)["P"] == "Lowest-Move Setup"


def test_risk_label_scales_with_required_move_dte_and_iv():
    assert ro.risk_label(S("a", strike=101, dte=10)) == "Low"
    assert ro.risk_label(S("a", strike=104, dte=10)) == "Moderate"       # 4% / sqrt(10) = 1.26
    assert ro.risk_label(S("a", strike=107, dte=10)) == "Elevated"       # 7% / sqrt(10) = 2.21
    assert ro.risk_label(S("a", strike=108, dte=10)) == "High"           # 2.53
    assert ro.risk_label(S("a", strike=108, dte=3)) == "Speculative"     # same move, less time = riskier
    assert ro.risk_label(S("a", strike=104, dte=10, ivrv=1.8)) == "Elevated"      # rich options bump one notch
    assert ro.risk_label(S("a", strike=125, dte=5, ivrv=2.0)) == "Speculative"
    assert ro.risk_label({"strike": None}) == "Moderate"
    assert {ro.risk_label(S("a", strike=100 + k, dte=7)) for k in range(0, 30)} <= {"Low", "Moderate", "Elevated", "High", "Speculative"}


def test_reward_risk_matches_the_existing_formula():
    # same arithmetic as compute_reward_risk(): entry mid 100, risk 3, reward 6 and 12
    assert ro.reward_risk(S("a")) == (2.0, 4.0)
    put = {"direction": "PUT", "entry_low": 99.5, "entry_high": 100.5, "stop": 103.0, "target1": 94.0, "target2": 88.0}
    assert ro.reward_risk(put) == (2.0, 4.0)
    assert ro.reward_risk({"direction": "CALL", "entry_low": 1, "entry_high": 1, "stop": 2, "target1": 3, "target2": 4}) == (None, None)
