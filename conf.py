#!/usr/bin/env python3
"""Single source of config truth for the whole pipeline.

Every user-specific knob (show name, audience, host persona, delivery target,
owner identity) lives in config.json so nobody has to fork the code to make the
podcast their own. The shipped default is config.example.json; copy it:

    cp config.example.json config.json

Two ways to read it, so bash and python share one config:
  - Python:  `from conf import load; cfg = load(); cfg["audience"]`
  - Bash:    `python3 conf.py audience`   (prints the value, nothing else)

Missing config.json falls back to config.example.json so a fresh clone still
runs the selftests before the user has copied anything.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
EXAMPLE = os.path.join(HERE, "config.example.json")

# Hard defaults: the pipeline must run even with an empty config.json.
DEFAULTS = {
    "show_name": "AI News Daily",
    "host_persona": "a sharp curator of new AI tools and developments",
    "audience": "AI builders, developers, and startup founders",
    "forbidden_terms": [],
    "curate_count": "4 to 5",
    "window_hours": 48,
    "episode_length": "short",
    "episode_format": "brief",
    "owner_name": "",
    "owner_email": "",
    "show_link": "",
    "delivery": "none",
    "output_dir": "episodes",
}


def load():
    """Return DEFAULTS overlaid with config.json (or config.example.json)."""
    cfg = dict(DEFAULTS)
    path = CONFIG if os.path.exists(CONFIG) else EXAMPLE
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if v is not None})
    return cfg


def get(key):
    """One value, as a plain string for bash. Lists join on a space."""
    val = load().get(key, "")
    if isinstance(val, (list, tuple)):
        return " ".join(str(x) for x in val)
    return str(val)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: conf.py <key>", file=sys.stderr)
        raise SystemExit(64)
    sys.stdout.write(get(sys.argv[1]))
