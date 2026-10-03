import pytest

import web.account_policy as ap

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:00", (0, 0)),
        ("07:30", (7, 30)),
        ("23:59", (23, 59)),
        ("  07:30  ", (7, 30)),
    ],
)
def test_parse_hhmm_accepts(raw, expected):
    assert ap.parse_hhmm(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "9:30",
        "7:5",
        "24:00",
        "12:60",
        "abc",
        "07:30:00",
        12,
    ],
)
def test_parse_hhmm_rejects(raw):
    assert ap.parse_hhmm(raw) is None


def test_stop_types_constant():
    assert ap.STOP_TYPES == ("none", "stop", "stop_limit", "trailing_pct", "trailing_dollar", "trailing_staged")


def test_none_policy_constant():
    assert ap.NONE == ap.StopPolicy("none", None, None)


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        (None, ap.NONE),
        ({}, ap.NONE),
        ({"stop_type": "unknown"}, ap.NONE),
        ({"stop_type": "stop"}, ap.NONE),
        ({"stop_type": "stop_limit", "stop_value": "60"}, ap.NONE),
        (
            {"stop_type": "stop_limit", "stop_value": "60", "stop_limit_offset": "10"},
            ap.StopPolicy("stop_limit", 60.0, 10.0),
        ),
        (
            {"stop_type": "trailing_pct", "stop_value": "30"},
            ap.StopPolicy("trailing_pct", 30.0, None),
        ),
    ],
)
def test_stop_policy_from_account(account, expected):
    assert ap.StopPolicy.from_account(account) == expected


def test_validate_policy_none_discards_values():
    assert ap.validate_policy("none", 60, 10) == ap.NONE


def test_validate_policy_trailing_pct_drops_offset():
    policy = ap.validate_policy("trailing_pct", 30, 5)
    assert policy == ap.StopPolicy("trailing_pct", 30.0, None)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("unknown", 10, None), "stop_type must be one of"),
        (("stop", None, None), "stop_value is required"),
        (("stop", "", None), "stop_value is required"),
        (("stop", 0, None), "stop_value must be greater than 0"),
        (("stop", -5, None), "stop_value must be greater than 0"),
        (("stop", 100, None), "stop_value must be less than 100"),
        (("trailing_pct", "abc", None), "stop_value must be a positive number"),
        (("stop_limit", 60, None), "stop_limit_offset is required"),
        (("stop_limit", 60, ""), "stop_limit_offset is required"),
        (("stop_limit", 60, 0), "stop_limit_offset must be a positive number less than 100"),
        (("stop_limit", 60, 100), "stop_limit_offset must be a positive number less than 100"),
    ],
)
def test_validate_policy_raises(args, message):
    with pytest.raises(ValueError, match=message):
        ap.validate_policy(*args)


def test_evaluate_none_policy_holds():
    outcome = ap.evaluate(ap.NONE, entry=10, peak=10, mark=1)
    assert outcome == ap.StopOutcome("hold", 0.0, None, None, None, False)


def test_evaluate_non_positive_entry_holds():
    policy = ap.StopPolicy("stop", 60, None)
    assert ap.evaluate(policy, entry=0, peak=10, mark=3) == ap.StopOutcome(
        "hold", 0.0, None, None, None, False
    )
    assert ap.evaluate(policy, entry=-5, peak=10, mark=3) == ap.StopOutcome(
        "hold", 0.0, None, None, None, False
    )


def test_evaluate_stop_loss():
    policy = ap.StopPolicy("stop", 60, None)

    level = ap.evaluate(policy, entry=10, peak=10, mark=4.01)
    assert level == ap.StopOutcome("hold", 4.0, None, None, None, False)

    fill = ap.evaluate(policy, entry=10, peak=10, mark=4.0)
    assert fill == ap.StopOutcome("fill", 4.0, None, 4.0, "stop_loss", False)

    crossed_fill = ap.evaluate(policy, entry=10, peak=10, mark=3.0, prev_mark=5.0)
    assert crossed_fill == ap.StopOutcome("fill", 4.0, None, 4.0, "stop_loss", True)

    uncrossed_fill = ap.evaluate(policy, entry=10, peak=10, mark=3.0)
    assert uncrossed_fill == ap.StopOutcome("fill", 4.0, None, 3.0, "stop_loss", False)

    uncrossed_fill_2 = ap.evaluate(policy, entry=10, peak=10, mark=3.0, prev_mark=3.9)
    assert uncrossed_fill_2 == ap.StopOutcome("fill", 4.0, None, 3.0, "stop_loss", False)


def test_evaluate_trailing_pct():
    policy = ap.StopPolicy("trailing_pct", 30, None)

    normal = ap.evaluate(policy, entry=10, peak=20, mark=14.0)
    assert normal == ap.StopOutcome("fill", 14.0, None, 14.0, "trail_stop", False)

    peak_below_entry = ap.evaluate(policy, entry=10, peak=5, mark=7.0)
    assert peak_below_entry == ap.StopOutcome("fill", 7.0, None, 7.0, "trail_stop", False)


def test_evaluate_trailing_dollar():
    policy = ap.StopPolicy("trailing_dollar", 2.0, None)
    assert ap.evaluate(policy, entry=10, peak=20, mark=18.0) == ap.StopOutcome(
        "fill", 18.0, None, 18.0, "trail_stop", False
    )

    too_wide = ap.StopPolicy("trailing_dollar", 5.0, None)
    assert ap.evaluate(too_wide, entry=2, peak=2, mark=1).action == "hold"
    assert ap.evaluate(too_wide, entry=2, peak=2, mark=1).level == -3.0


def test_evaluate_stop_limit():
    policy = ap.StopPolicy("stop_limit", 60, 10)

    assert ap.evaluate(policy, entry=10, peak=10, mark=3.8) == ap.StopOutcome(
        "fill", 4.0, 3.6, 3.8, "stop_limit", False
    )

    armed = ap.evaluate(policy, entry=10, peak=10, mark=3.0)
    assert armed == ap.StopOutcome("arm", 4.0, 3.6, None, "stop_limit", False)

    armed_fill = ap.evaluate(policy, entry=10, peak=10, mark=3.7, armed=True)
    assert armed_fill == ap.StopOutcome("fill", 4.0, 3.6, 3.7, "stop_limit", False)

    armed_hold = ap.evaluate(policy, entry=10, peak=10, mark=3.5, armed=True)
    assert armed_hold == ap.StopOutcome("hold", 4.0, 3.6, None, None, False)


def test_evaluate_armed_ignored_for_non_stop_limit():
    policy = ap.StopPolicy("trailing_pct", 30, None)
    outcome = ap.evaluate(policy, entry=10, peak=20, mark=13.0, armed=True)
    assert outcome == ap.StopOutcome("fill", 14.0, None, 13.0, "trail_stop", False)


def test_describe_policy_all_types():
    sentences = {
        ap.describe_policy(ap.NONE),
        ap.describe_policy(ap.StopPolicy("stop", 60, None)),
        ap.describe_policy(ap.StopPolicy("stop_limit", 60, 10)),
        ap.describe_policy(ap.StopPolicy("trailing_pct", 30, None)),
        ap.describe_policy(ap.StopPolicy("trailing_dollar", 5.0, None)),
    }
    assert len(sentences) == 5
    assert all(s and isinstance(s, str) for s in sentences)
    assert "60" in ap.describe_policy(ap.StopPolicy("stop", 60, None))
    assert "60" in ap.describe_policy(ap.StopPolicy("stop_limit", 60, 10))
    assert "10" in ap.describe_policy(ap.StopPolicy("stop_limit", 60, 10))
    assert "30" in ap.describe_policy(ap.StopPolicy("trailing_pct", 30, None))
    assert "$5.00" in ap.describe_policy(ap.StopPolicy("trailing_dollar", 5.0, None))