"""Aidon: an autonomous agent that works from a plain-language goal.

The user gives a goal in chat. Aidon researches, plans, tests the
plan in a sandbox, verifies the result against evidence, asks the
human for approval before any live action, executes only what was
approved, and then records what it learned so the next goal can
reuse it.

This code was written during the Meta Global AI Developer
Hackathon, Oct 26 - Nov 4, 2026. The idea was validated with an
earlier prototype, and all code in this repository was written
during the hackathon. See README.md for what is new here.
"""

__version__ = "0.4.0"
