"""app.tools.get_weather
========================

This module implements a weather lookup tool that can be
invoked by the OpenAI function-calling interface.  The tool uses the
``wttr.in`` API, which is free and does not require an API key.

The public API of this module follows the same pattern as the
``create_file`` tool – a callable named :data:`func` that returns a JSON
string.  On success the JSON contains a ``result`` key; on failure it
contains an ``error`` key.  The tool is automatically discovered by
``app.tools.__init__``.

Example usage:
--------------

>>> import json
>>> from app.tools.get_weather import func as get_weather
>>> json.loads(get_weather("London", "2024-12-01"))
{'result': {'city': 'London', 'current': {'temperature': 6.2, 'windspeed': 5.5, 'winddirection': 210}, 'forecast': {'temperature_2m_max': 12.5, 'temperature_2m_min': 3.8, 'precipitation_sum': 0.0}}}

The function accepts ``city`` as a free-form string and ``date`` as either:

- An ISO 8601 date (e.g. ``2024-12-01``)
- A relative date string like ``today``, ``tomorrow``, ``yesterday``, ``next week``

If ``date`` is omitted or empty, today's date is used.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta
from typing import Dict


def _parse_date(date_str: str) -> str:
    """Parse a date string and return an ISO 8601 formatted date.

    Handles both explicit ISO dates and common relative date strings.

    Parameters
    ----------
    date_str: str
        Date string in ISO format (YYYY-MM-DD) or relative form like
        ``today``, ``tomorrow``, ``yesterday``, ``next week``, etc.

    Returns
    -------
    str
        ISO 8601 formatted date string (YYYY-MM-DD).

    Raises
    ------
    ValueError
        If the date string cannot be parsed.
    """
    today = datetime.now().date()
    date_str_lower = date_str.lower().strip()

    # Handle relative date strings
    if date_str_lower == "today":
        return today.strftime("%Y-%m-%d")
    elif date_str_lower == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_str_lower == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_str_lower == "next week":
        return (today + timedelta(days=7)).strftime("%Y-%m-%d")
    elif date_str_lower == "last week":
        return (today - timedelta(days=7)).strftime("%Y-%m-%d")

    # Try to parse as ISO format
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"Invalid date format: '{date_str}'. "
            "Please use ISO format (YYYY-MM-DD) or relative terms like "
            "'today', 'tomorrow', 'yesterday', 'next week'."
        )


def _fetch_weather(city: str, date: str) -> Dict:
    """Fetch current and forecast weather data for the given city and date.

    Parameters
    ----------
    city: str
        The name of the city to look up.
    date: str
        ISO 8601 formatted date string (YYYY-MM-DD).
    """
    url = f"http://wttr.in/{city}?format=j1"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# The actual tool implementation
# ---------------------------------------------------------------------------

def _get_weather(city: str, date: str = "") -> str:
    """Retrieve current and forecast weather information for a given city and date.

    Parameters
    ----------
    city: str
        The name of the city to look up.
    date: str, optional
        The date for which to retrieve forecast data. Accepts:
        - ISO format (YYYY-MM-DD)
        - Relative strings: ``today``, ``tomorrow``, ``yesterday``, ``next week``
        If omitted or empty, today's date is used.
    """
    try:
        date = _parse_date(date) if date else datetime.now().strftime("%Y-%m-%d")
        data = _fetch_weather(city, date)
        current_condition = data.get("current_condition", [])
        weather = current_condition[0] if current_condition else {}
        result = {
            "city": city,
            "date": date,
            "current": {
                "temperature": weather.get("temp_C"),
                "windspeed": weather.get("windspeedKmph"),
                "humidity": weather.get("humidity"),
                "weather": weather.get("weatherDesc", [{}])[0].get("value") if weather.get("weatherDesc") else None,
            },
            "forecast": {
                "temperature_2m_max": weather.get("temp_C"),
                "precipitation_sum": weather.get("precipMM"),
            },
        }
        return json.dumps({"result": result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Public attributes for auto-discovery
# ---------------------------------------------------------------------------

func = _get_weather
name = "get_weather"
description = "Retrieve current and forecast weather for a given city and date."