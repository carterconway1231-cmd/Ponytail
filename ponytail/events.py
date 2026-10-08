"""Macro event calendar (FOMC, CPI): implied volatility inflates into these
releases and collapses after, which punishes freshly bought premium. New
entries are blocked in the run-up; exits are unaffected."""
import json
import os
from datetime import timedelta


def load_events(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("events", [])


def blackout(events, today, days):
    """Events that fall within [today, today + days] (trading through them is avoided)."""
    end = (today + timedelta(days=days)).isoformat()
    return [e for e in events if today.isoformat() <= e["date"] <= end]


def calendar_warnings(events, today, horizon=45):
    """Flag a calendar that has gone stale, so blackouts don't silently stop working."""
    end = (today + timedelta(days=horizon)).isoformat()
    warnings = []
    for kind in ("FOMC", "CPI"):
        if not any(e["kind"] == kind and today.isoformat() <= e["date"] <= end for e in events):
            if kind == "CPI" or not any(e["kind"] == kind and e["date"] >= today.isoformat() for e in events):
                warnings.append(f"no {kind} date listed in the next {horizon} days; update events.json")
    return warnings
