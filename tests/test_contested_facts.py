"""The editorial fact sheet for contested topics (contested_facts.json)."""
import json
import os
from datetime import date

import pytest

import venice_knowledge as vk
from config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _topics():
    with open(os.path.join(ROOT, Config.CONTESTED_FACTS_FILE), encoding="utf-8") as f:
        return json.load(f)["topics"]


def test_every_position_is_sourced_and_dated():
    for topic in _topics():
        assert {"id", "title", "reviewed", "lead", "positions"} <= set(topic)
        date.fromisoformat(topic["reviewed"])
        assert topic["positions"]
        for p in topic["positions"]:
            assert p["who"] and p["position"] and p["source"].startswith("https://")
            date.fromisoformat(p["date"])


def test_covid_lead_never_says_confirmed():
    covid = next(t for t in _topics() if t["id"] == "covid-origin")
    assert "most likely" in covid["lead"].lower()
    assert "Do not call it confirmed" in covid["lead"]


@pytest.mark.parametrize(
    "text, hit",
    [
        ("was covid a lab leak? no fence sitting", True),
        ("is the lab leak theory true", True),
        ("where did the pandemic start?", True),
        ("did sars-cov-2 come from the wuhan institute?", True),
        ("covid vaccine side effects?", False),
        ("is the pandemic over?", False),
        ("what is DIEM?", False),
    ],
)
def test_matching(text, hit):
    assert bool(vk.relevant_positions(text)) is hit


def test_positions_become_gate_evidence():
    covid = next(t for t in _topics() if t["id"] == "covid-origin")
    lines = vk.position_evidence(covid)
    assert len(lines) == len(covid["positions"])
    assert all(line.startswith("[fact sheet] ") and "https://" in line for line in lines)


def test_stale_entries_are_reported():
    assert vk.stale_fact_topics(today=date(2026, 11, 13)) == []
    assert "covid-origin" in vk.stale_fact_topics(today=date(2027, 6, 1))
