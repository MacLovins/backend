"""Signal temperature: the guide of every category and which guide a question shows to the model."""

from leadradar_ai import DEFAULT_TEMPERATURE, GENERIC_TEMPERATURE, TEMPERATURE_NAMES, temperature_guide
from leadradar_ai.presets import SIGNAL_CATEGORIES
from leadradar_ai.testing.factories import make_question


def test_every_category_has_a_complete_guide():
    assert list(DEFAULT_TEMPERATURE) == list(SIGNAL_CATEGORIES)  # same order as the UI labels
    for category, guide in [*DEFAULT_TEMPERATURE.items(), ("generic", GENERIC_TEMPERATURE)]:
        assert list(guide) == ["weak", "moderate", "strong"], category
        assert all(0 < len(text) <= 300 for text in guide.values()), category  # fits a question override
    assert TEMPERATURE_NAMES == {"weak": "cold", "moderate": "medium", "strong": "hot"}
    assert DEFAULT_TEMPERATURE["hiring"]["strong"] == (
        "10 or more relevant openings, a new team being built, or a hiring drive announced."
    )


def test_a_questions_own_guide_wins_when_complete():
    own = {"weak": "One RPA job ad.", "moderate": "A few RPA roles.", "strong": "An RPA team of 20."}
    assert temperature_guide(make_question(category="hiring", temperature=own)) == own
    assert temperature_guide(make_question(category="hiring")) == DEFAULT_TEMPERATURE["hiring"]
    partial = make_question(category="hiring", temperature={**own, "moderate": ""})
    assert temperature_guide(partial) == DEFAULT_TEMPERATURE["hiring"]
    assert temperature_guide(make_question(category="esg_reporting")) == GENERIC_TEMPERATURE

    guide = temperature_guide(make_question(category="hiring"))
    guide["weak"] = "changed"
    assert DEFAULT_TEMPERATURE["hiring"]["weak"] != "changed"  # callers get a copy
