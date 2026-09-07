"""Predeclared P0-P3 premise membership; outcomes never alter admission."""
from track_c_multivenue import POLICIES


def membership(*, a2_trigger, c_trigger, external_eligible, a2_known_recent):
    values = {
        "P0": bool(a2_trigger),
        "P1": bool(a2_trigger and external_eligible),
        "P2": bool(c_trigger and external_eligible),
        "P3": bool(c_trigger and external_eligible and a2_known_recent),
    }
    if tuple(values) != POLICIES:
        raise AssertionError("policy registry changed")
    return values


def reasons(*, a2_trigger, c_trigger, external_eligible, a2_known_recent):
    selected = membership(
        a2_trigger=a2_trigger,
        c_trigger=c_trigger,
        external_eligible=external_eligible,
        a2_known_recent=a2_known_recent,
    )
    result = {}
    for name, accepted in selected.items():
        if accepted:
            result[name] = None
        elif name in ("P0", "P1") and not a2_trigger:
            result[name] = "no_new_a2_deceleration"
        elif name in ("P2", "P3") and not c_trigger:
            result[name] = "no_new_c_sell_episode"
        elif not external_eligible:
            result[name] = "external_discount_ineligible"
        else:
            result[name] = "no_causally_known_a2_deceleration"
    return result
