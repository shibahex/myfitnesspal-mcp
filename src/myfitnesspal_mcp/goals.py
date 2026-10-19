import copy
import json
from datetime import date
from urllib import parse

from .mfp_web import api_headers, require_status

# Grams unless noted: cholesterol/sodium/potassium in mg, vitamins/minerals in %DV.
NUTRIENT_KEYS = (
    "carbohydrates",
    "protein",
    "fat",
    "saturated_fat",
    "polyunsaturated_fat",
    "monounsaturated_fat",
    "trans_fat",
    "cholesterol",
    "sodium",
    "potassium",
    "fiber",
    "sugar",
    "vitamin_a",
    "vitamin_c",
    "calcium",
    "iron",
)


def _goals_url(client, day: date | None = None) -> str:
    path = "v2/nutrient-goals"
    if day is not None:
        path += f"?date={day.isoformat()}"
    return parse.urljoin(client.BASE_API_URL, path)


def _current_goal_item(client, day: date) -> dict:
    resp = client.session.get(
        _goals_url(client, day),
        headers=api_headers(client, {"Accept": "application/json"}),
    )
    require_status(resp, "/v2/nutrient-goals", (200,))
    items = resp.json().get("items") or []
    if not items:
        raise RuntimeError("MyFitnessPal returned no nutrient goals")
    return items[0]


def _summary(goal: dict) -> dict:
    summary = {"calories": goal["energy"]["value"]}
    summary.update({key: goal.get(key) for key in NUTRIENT_KEYS})
    return summary


def get_goals(client, day: date) -> dict:
    item = _current_goal_item(client, day)
    return {
        "day": day.isoformat(),
        "valid_from": item.get("valid_from"),
        "goals": _summary(item["default_goal"]),
    }


def set_goals(client, today: date, changes: dict[str, float]) -> dict:
    """MFP's goals page POSTs the whole goal record back with valid_from set,
    so this copies the current record and overwrites only the given fields,
    on the default goal and every per-weekday goal alike."""
    unknown = set(changes) - {"calories", *NUTRIENT_KEYS}
    if unknown:
        raise ValueError(f"unknown goal fields: {', '.join(sorted(unknown))}")
    if not changes:
        raise ValueError("no goal changes given")
    if any(value < 0 for value in changes.values()):
        raise ValueError("goal values can't be negative")

    current = _current_goal_item(client, today)
    previous = _summary(current["default_goal"])
    item = copy.deepcopy(current)
    for key in ("valid_to", "default_group_id", "updated_at"):
        item.pop(key, None)
    item["valid_from"] = today.isoformat()
    for goal in [item["default_goal"], *item.get("daily_goals", [])]:
        goal["meal_goals"] = []
        goal.pop("group_id", None)
        for key, value in changes.items():
            if key == "calories":
                goal["energy"]["value"] = value
            else:
                goal[key] = value

    resp = client.session.post(
        _goals_url(client),
        data=json.dumps({"item": item}),
        headers=api_headers(client, {"Content-Type": "application/json"}),
    )
    require_status(resp, "/v2/nutrient-goals", (200, 201))
    return {
        "ok": True,
        "valid_from": today.isoformat(),
        "previous": {key: previous[key] for key in changes},
        "goals": get_goals(client, today)["goals"],
    }
