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


def _sum_hourly_precip(hourly: list) -> float:
    """Sum the hourly ``precipMM`` values into a daily precipitation total.

    Parameters
    ----------
    hourly: list
        List of hourly forecast dicts from the wttr.in ``j1`` payload.

    Returns
    -------
    float
        Total precipitation in millimetres for the day.
    """
    total = 0.0
    for entry in hourly or []:
        value = entry.get("precipMM")
        if value is None:
            continue
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return round(total, 1)


def _max_chance_of_rain(hourly: list) -> int:
    """Return the highest hourly chance of rain (percent) for a day."""
    best = 0
    for entry in hourly or []:
        try:
            best = max(best, int(entry.get("chanceofrain") or 0))
        except (TypeError, ValueError):
            continue
    return best


def _day_descriptions(day: dict) -> list:
    """Collect the distinct weather descriptions for a forecast day."""
    descriptions = []
    for entry in day.get("hourly", []):
        desc = (entry.get("weatherDesc") or [{}])[0].get("value")
        if desc and desc not in descriptions:
            descriptions.append(desc)
    return descriptions


def _forecast_for_date(data: dict, date: str) -> dict:
    """Extract the real per-day forecast from the wttr.in ``j1`` payload.

    Parameters
    ----------
    data: dict
        Decoded JSON payload from ``wttr.in/<city>?format=j1``.
    date: str
        ISO 8601 date (YYYY-MM-DD) to match against the ``weather`` array.

    Returns
    -------
    dict
        Forecast fields sourced from the actual ``weather`` array, or an empty
        dict if no matching day is available.
    """
    for day in data.get("weather", []):
        if day.get("date") == date:
            hourly = day.get("hourly", [])
            return {
                "temperature_2m_max": day.get("maxtempC"),
                "temperature_2m_min": day.get("mintempC"),
                "precipitation_sum": _sum_hourly_precip(hourly),
                "chance_of_rain": _max_chance_of_rain(hourly),
                "description": _day_descriptions(day),
                "uv_index": day.get("uvIndex"),
            }
    return {}


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
        today = datetime.now().strftime("%Y-%m-%d")
        data = _fetch_weather(city, date)

        current_condition = data.get("current_condition", [])
        live = current_condition[0] if current_condition else {}
        # Live conditions are only meaningful for the current day.
        if date == today and live:
            current = {
                "temperature": live.get("temp_C"),
                "windspeed": live.get("windspeedKmph"),
                "humidity": live.get("humidity"),
                "weather": (live.get("weatherDesc") or [{}])[0].get("value"),
            }
        else:
            current = None

        result = {
            "city": city,
            "date": date,
            "current": current,
            "forecast": _forecast_for_date(data, date),
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