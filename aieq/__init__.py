"""AI Equities Desk.

Importing a submodule (``aieq.config``) must not pull the model stack.
``analyze`` and ``scan_universe`` load on first use.
"""

__version__ = "1.0.0"


def __getattr__(name: str):
    if name in {"analyze", "scan_universe"}:
        from aieq.pipeline import analyze, scan_universe

        return analyze if name == "analyze" else scan_universe
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
