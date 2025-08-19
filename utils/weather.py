# weather.py — lightweight live weather handler (Open-Meteo, no API key)

from __future__ import annotations
import re, json
from urllib.parse import urlencode, quote_plus
from urllib.request import urlopen, Request

UA = "mini-ai-chat/1.0 (+https://example.local)"

_WEATHER_PATTERNS = [
    re.compile(r"^(?:what(?:'s| is)\s+)?weather\s+(?:in|for)\s+(?P<place>.+)$", re.I),
    re.compile(r"^(?:forecast|temperature)\s+(?:in|for)\s+(?P<place>.+)$", re.I),
    re.compile(r"^(?:weather|forecast)\s*:\s*(?P<place>.+)$", re.I),
]

WMO_CODES = {
    0:"Clear", 1:"Mainly clear", 2:"Partly cloudy", 3:"Overcast",
    45:"Fog", 48:"Depositing rime fog", 51:"Light drizzle", 53:"Drizzle",
    55:"Dense drizzle", 56:"Freezing drizzle", 57:"Freezing drizzle",
    61:"Light rain", 63:"Rain", 65:"Heavy rain",
    66:"Freezing rain", 67:"Heavy freezing rain",
    71:"Light snow", 73:"Snow", 75:"Heavy snow",
    77:"Snow grains", 80:"Rain showers", 81:"Rain showers", 82:"Violent rain",
    85:"Snow showers", 86:"Snow showers", 95:"Thunderstorm",
    96:"Thunderstorm w/ hail", 99:"Thunderstorm w/ heavy hail"
}
EMOJI = {
    0:"☀️", 1:"🌤️", 2:"⛅", 3:"☁️", 45:"🌫️", 48:"🌫️",
    51:"🌦️", 53:"🌦️", 55:"🌧️", 61:"🌧️", 63:"🌧️", 65:"🌧️",
    66:"🌧️", 67:"🌧️", 71:"🌨️", 73:"🌨️", 75:"❄️",
    80:"🌧️", 81:"🌧️", 82:"⛈️", 85:"🌨️", 86:"❄️",
    95:"⛈️", 96:"⛈️", 99:"⛈️"
}

def _http_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _geocode(place: str) -> tuple[float,float,str] | None:
    qs = urlencode({"name": place, "count": 1, "language": "en", "format": "json"})
    data = _http_json(f"https://geocoding-api.open-meteo.com/v1/search?{qs}")
    results = (data or {}).get("results") or []
    if not results: return None
    r = results[0]
    name = ", ".join([p for p in [r.get("name"), r.get("admin1"), r.get("country")] if p])
    return float(r["latitude"]), float(r["longitude"]), name

def _forecast(lat: float, lon: float) -> dict:
    qs = urlencode({
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"
    })
    return _http_json(f"https://api.open-meteo.com/v1/forecast?{qs}")

def _brief(resp: dict) -> tuple[str,str]:
    cur = (resp.get("current") or {})
    daily = (resp.get("daily") or {})
    wcode = int(cur.get("weather_code", -1))
    desc = WMO_CODES.get(wcode, "Conditions")
    icon = EMOJI.get(wcode, "🌡️")
    t = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    wind = cur.get("wind_speed_10m")
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    pmax = (daily.get("precipitation_probability_max") or [None])[0]
    now = f"{icon} {desc}. {t}°C (feels {feels}°C), wind {wind} km/h."
    today = f"Today: {tmin}–{tmax}°C, precip {pmax}%."
    return now, today

def parse_weather_query(message: str) -> str | None:
    text = (message or "").strip()
    for pat in _WEATHER_PATTERNS:
        m = pat.match(text)
        if m:
            place = (m.group("place") or "").strip().strip('"\'')
            return place or None
    return None

def handle(message: str) -> tuple[bool, str]:
    place = parse_weather_query(message)
    if not place:
        return False, ""
    geo = _geocode(place)
    if not geo:
        return True, f"Couldn’t find “{place}”. Try a city + country/region."
    lat, lon, label = geo
    data = _forecast(lat, lon)
    now, today = _brief(data)
    maps = f"https://www.google.com/maps/search/{quote_plus(label)}"
    return True, f"Weather in {label}\n{now}\n{today}\nMaps: {maps}"
