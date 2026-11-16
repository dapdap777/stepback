"""The example agent and an offline mock API, so stepline can be tried without a key.

``stepline demo`` runs ``examples/weather_agent.py --mock``. The wheel ships a copy of that
file as ``stepline/demo/weather_agent.py``; in a source checkout the repository file is used.
"""

from __future__ import annotations

from pathlib import Path


def example_path() -> Path:
    packaged = Path(__file__).with_name("weather_agent.py")
    if packaged.is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[3] / "examples" / "weather_agent.py"
    if checkout.is_file():
        return checkout
    raise FileNotFoundError("the example agent is missing from this installation")
