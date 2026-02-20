from __future__ import annotations

import os
import re
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Tuple

import yaml
import pytz
import requests
from icalendar import Calendar, Event


@dataclass(frozen=True)
class Classified:
    shift_type: str
    shift_layer: str


def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def hhmm(dt) -> str:
    if hasattr(dt, "hour"):
        return f"{dt.hour:02d}:{dt.minute:02d}"
    return "00:00"


def stable_uid(original_uid: str, suffix: str) -> str:
    base = (original_uid or "").strip()
    if not base:
        base = hashlib.sha1(suffix.encode("utf-8")).hexdigest()[:16]
    return f"{base}-{suffix}"


def make_calendar(name: str) -> Calendar:
    cal = Calendar()
    cal.add("PRODID", "-//rldatix-ical-splitter//")
    cal.add("VERSION", "2.0")
    cal.add("X-WR-CALNAME", name)
    return cal


def classify_event(
    ev: Event,
    tz,
    type_rules: Dict[str, List[str]],
    time_rules: Dict[str, Tuple[str, str]],
) -> Classified:
    summary = normalize(str(ev.get("SUMMARY", "")))
    description = normalize(str(ev.get("DESCRIPTION", "")))
    haystack = f"{summary} {description}"

    chosen_type = "andet"
    for t, keywords in type_rules.items():
        if t == "andet":
            continue
        for kw in keywords:
            if normalize(kw) in haystack:
                chosen_type = t
                break
        if chosen_type == t:
            break

    dtstart = ev.decoded("DTSTART")
    dtend = ev.decoded("DTEND")

    if hasattr(dtstart, "tzinfo") and dtstart.tzinfo is None:
        dtstart = tz.localize(dtstart)
    if hasattr(dtend, "tzinfo") and dtend.tzinfo is None:
        dtend = tz.localize(dtend)

    start_s = hhmm(dtstart)
    end_s = hhmm(dtend)

    chosen_layer = "andet"
    for layer, (s, e) in time_rules.items():
        if start_s == s and end_s == e:
            chosen_layer = layer
            break

    return Classified(shift_type=chosen_type, shift_layer=chosen_layer)


def main():
    # Load config (local: config.yaml, CI: config.example.yaml)
    config_path = "config.yaml" if os.path.exists("config.yaml") else "config.example.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    url = os.environ.get("RLDATIX_ICS_URL")
    if not url:
        raise RuntimeError("Missing env var: RLDATIX_ICS_URL")

    tz = pytz.timezone(cfg.get("timezone", "Europe/Copenhagen"))
    out_dir = cfg.get("output_dir", "site")

    type_rules = cfg.get("shift_type_rules", {})
    time_rules_raw = cfg.get("shift_time_rules", {})
    time_rules = {k: (v[0], v[1]) for k, v in time_rules_raw.items()}

    r = requests.get(url, timeout=30)
    r.raise_for_status()
    source = Calendar.from_ical(r.content)

    events = [c for c in source.walk() if c.name == "VEVENT"]
    print(f"Found {len(events)} events")

    buckets: Dict[Tuple[str, str], Calendar] = {}

    for ev in events:
        cls = classify_event(ev, tz, type_rules, time_rules)
        key = (cls.shift_type, cls.shift_layer)

        if key not in buckets:
            buckets[key] = make_calendar(f"RLDatix - {cls.shift_type} - {cls.shift_layer}")

        new_ev = Event()
        for k in ev.keys():
            new_ev.add(k, ev.get(k))

        new_ev["UID"] = stable_uid(str(ev.get("UID", "")), f"{cls.shift_type}-{cls.shift_layer}")
        buckets[key].add_component(new_ev)

    os.makedirs(out_dir, exist_ok=True)

    written = 0
    for (t, layer), cal in buckets.items():
        safe_t = re.sub(r"[^a-z0-9\-]+", "-", normalize(t))
        safe_l = re.sub(r"[^a-z0-9\-]+", "-", normalize(layer))
        filename = f"{safe_t}__{safe_l}.ics"
        path = os.path.join(out_dir, filename)
        with open(path, "wb") as f:
            f.write(cal.to_ical())
        written += 1

    print(f"Wrote {written} calendars to {out_dir}/")


if __name__ == "__main__":
    main()
