import datetime
import json

import pytest

from myfitnesspal_mcp import goals

TODAY = datetime.date(2026, 9, 10)


def _goal(sat_fat=29.0):
    return {
        "energy": {"value": 2105.0, "unit": "calories"},
        "carbohydrates": 200.0,
        "protein": 180.0,
        "fat": 65.0,
        "saturated_fat": sat_fat,
        "sodium": 2300.0,
        "fiber": 38.0,
    }


def _goals_body(sat_fat=29.0):
    daily = [
        {"day_of_week": day, "group_id": 0, **_goal(sat_fat)}
        for day in ("monday", "tuesday")
    ]
    return {
        "items": [
            {
                "valid_from": "2026-09-01",
                "valid_to": "2100-01-01",
                "default_group_id": 0,
                "updated_at": "2026-09-01T00:00:00+0000",
                "default_goal": _goal(sat_fat),
                "daily_goals": daily,
            }
        ]
    }


@pytest.fixture
def goals_client(client, make_response):
    client.session.route(
        "GET", "v2/nutrient-goals", make_response(json_data=_goals_body())
    )
    client.session.route("POST", "v2/nutrient-goals", make_response(status_code=200))
    return client


def test_get_goals_summarizes_default_goal(goals_client):
    result = goals.get_goals(goals_client, TODAY)
    assert result["goals"]["calories"] == 2105.0
    assert result["goals"]["saturated_fat"] == 29.0


def test_set_goals_changes_only_given_fields_on_every_goal(goals_client):
    result = goals.set_goals(goals_client, TODAY, {"saturated_fat": 14})
    assert result["previous"] == {"saturated_fat": 29.0}
    posts = [c for c in goals_client.session.calls if c[0] == "POST"]
    item = json.loads(posts[0][2]["data"])["item"]
    assert item["valid_from"] == "2026-09-10"
    assert "valid_to" not in item and "updated_at" not in item
    for goal in [item["default_goal"], *item["daily_goals"]]:
        assert goal["saturated_fat"] == 14
        assert goal["protein"] == 180.0
        assert goal["meal_goals"] == []
        assert "group_id" not in goal


def test_set_goals_calories_go_into_energy(goals_client):
    goals.set_goals(goals_client, TODAY, {"calories": 2000})
    posts = [c for c in goals_client.session.calls if c[0] == "POST"]
    item = json.loads(posts[0][2]["data"])["item"]
    assert item["default_goal"]["energy"] == {"value": 2000, "unit": "calories"}


def test_set_goals_rejects_unknown_fields(goals_client):
    with pytest.raises(ValueError, match="unknown goal fields"):
        goals.set_goals(goals_client, TODAY, {"steps": 10000})
