"""Fuel Supply Intelligence & Resilience Platform — backend application package.

The platform is a **simulation-only** decision-support tool (brief section 24):
it recommends, it never executes, and every predictive response is labelled
``simulated: true``. It talks to the published BUP Fuel Supply Simulator and
never to real fuel infrastructure.

This package is assembled by :func:`app.main.create_app`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
