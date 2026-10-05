"""Per-browser output fingerprints; never cache or throttle native execution."""

import hashlib
import json
from functools import wraps

from dash import no_update


def changed_outputs(values, previous):
    previous = previous or []
    fingerprints, outputs = [], []
    for index, value in enumerate(values):
        old = previous[index] if index < len(previous) else None
        if value is no_update:
            fingerprints.append(old)
            outputs.append(no_update)
            continue
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        fingerprints.append(fingerprint)
        outputs.append(no_update if fingerprint == old else value)
    return (*outputs, fingerprints if fingerprints != previous else no_update)


def quiet_outputs(function):
    """The callback's final State is its own browser's output fingerprint list."""

    @wraps(function)
    def wrapped(*args):
        return changed_outputs(function(*args[:-1]), args[-1])

    return wrapped
