"""Helpers shared by the stop-loss experiments (the same stop_series as run_3_stops_exits.py)."""
from __future__ import annotations

from rules import *  # noqa: F403

NET = {s: DF[f"{SHORT[s]}_net_rs"] for s in STRATEGIES}


def stop_series(s: str, level: float, extreme: bool = False):
    """Daily net when a stop at -`level` rupees (close-marked gross) fills at the next bar's open; also the days it triggered and the bar."""
    have = set(DATA["strategy_days"][s])
    out, trig = {}, {}
    for d in DAYS:
        if d not in have:
            out[d] = np.nan
            continue
        ds = SIMS[d][s]
        j = first_breach(ds, UNITS, level, extreme)
        if j is None:
            out[d] = net_close(d, s, UNITS)
        else:
            out[d] = net_stop(d, s, UNITS, j)
            trig[d] = j
    return pd.Series(out), trig
