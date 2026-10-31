import datetime
import json
from pathlib import Path

import pytest

from myfitnesspal_mcp import my_foods
from myfitnesspal_mcp.mfp_client import is_auth_error

FIXTURES = Path(__file__).parent / "fixtures"

DAY = datetime.date(2026, 10, 19)


def _food_item(food_id="501", name="Soup + Chicken", sizes=None):
    return {
        "id": food_id,
        "version": food_id,
        "description": name,
        "brand_name": "",
        "deleted": False,
        "serving_sizes": sizes
        or [{"id": "7", "value": 1.0, "unit": "meal", "nutrition_multiplier": 1.0}],
        "nutritional_contents": {
            "energy": {"unit": "calories", "value": 430.0},
            "protein": 60.0,
            "carbohydrates": 20.0,
            "fat": 11.0,
        },
    }


def _saved_meal(meal_id=1111, name="Soup Dinner"):
    return {
        "description": name,
        "meal_id": meal_id,
        "foods": [
            {"description": "Canned Chicken, 12 oz", "calories": 280, "protein": 52},
            {"description": "Noodle Soup, 1.25 cup", "calories": 150, "carbs": 20},
        ],
    }


@pytest.fixture
def mine_client(client, make_response):
    client.session.route(
        "GET",
        "api/services/users/foods/mine",
        make_response(json_data=[_food_item()]),
    )
    client.session.route(
        "GET",
        "api/services/users/meals/mine",
        make_response(json_data=[_saved_meal(), _saved_meal(2222, "Taco Night")]),
    )
    client.session.route(
        "GET",
        "food/load_meals",
        make_response(text=(FIXTURES / "load_meals.html").read_text()),
    )
    client.session.route("POST", "v2/diary", make_response(status_code=201))
    return client


def _posts(client, fragment):
    return [c for c in client.session.calls if c[0] == "POST" and fragment in c[1]]


def test_create_food_posts_a_private_food(client, make_response):
    client.session.route(
        "GET",
        "v2/users/user-1",
        make_response(
            json_data={"item": {"location_preferences": {"country_code": "US"}}}
        ),
    )
    client.session.route(
        "POST", "v2/foods", make_response(json_data={"items": [_food_item()]})
    )
    created = my_foods.create_food(
        client,
        " Soup + Chicken ",
        430,
        nutrients={"protein": 60, "sodium": 3745},
        serving_unit="meal",
    )
    item = json.loads(_posts(client, "v2/foods")[0][2]["data"])["item"]
    assert item["public"] is False
    assert item["country_code"] == "US"
    assert item["description"] == "Soup + Chicken"
    assert item["nutritional_contents"] == {
        "energy": {"unit": "calories", "value": 430},
        "protein": 60,
        "sodium": 3745,
    }
    assert item["serving_sizes"] == [
        {"value": 1.0, "unit": "meal", "nutrition_multiplier": 1}
    ]
    assert created["food_id"] == "501"
    assert created["servings"][0]["label"] == "1 meal"


def test_create_food_adds_a_one_unit_serving_like_the_web_form(client, make_response):
    client.session.route("GET", "v2/users/user-1", make_response(json_data={}))
    client.session.route(
        "POST", "v2/foods", make_response(json_data={"items": [_food_item()]})
    )
    my_foods.create_food(
        client, "Rice", 200, nutrients={}, serving_value=150, serving_unit="g"
    )
    item = json.loads(_posts(client, "v2/foods")[0][2]["data"])["item"]
    assert item["serving_sizes"][1] == {
        "value": 1,
        "unit": "g",
        "nutrition_multiplier": 1 / 150,
    }


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"name": " "}, "name can't be empty"),
        ({"nutrients": {"steps": 1}}, "unknown nutrients: steps"),
        ({"calories": -1}, "can't be negative"),
        ({"serving_value": 0}, "greater than zero"),
    ],
)
def test_create_food_rejects_bad_input_before_any_request(client, kwargs, message):
    args = {"name": "Soup", "calories": 100, "nutrients": {}, **kwargs}
    with pytest.raises(ValueError, match=message):
        my_foods.create_food(client, args.pop("name"), args.pop("calories"), **args)
    assert client.session.calls == []


def test_find_mine_lists_foods_and_meals_with_meal_totals(mine_client):
    results = my_foods.find_mine(mine_client, "soup")
    assert [(r["kind"], r["food_id"]) for r in results] == [
        ("food", "501"),
        ("meal", "1111"),
        ("meal", "2222"),
    ]
    assert results[1]["calories"] == 430
    assert results[1]["protein"] == 52


def test_resolve_mine_prefers_the_exact_name(mine_client):
    food, _ = my_foods.resolve_mine(mine_client, "taco night", None)
    assert food["food_id"] == "2222"


def test_resolve_mine_returns_candidates_when_ambiguous(mine_client):
    food, candidates = my_foods.resolve_mine(mine_client, "soup", None)
    assert food is None
    assert len(candidates) == 3


def test_resolve_mine_without_matches_lists_what_exists(client, make_response):
    client.session.route("GET", "search=pizza", make_response(json_data=[]))
    client.session.route(
        "GET", "foods/mine", make_response(json_data=[_food_item(name="Oats")])
    )
    client.session.route("GET", "meals/mine", make_response(json_data=[]))
    with pytest.raises(my_foods.NoMatchingFood, match="'Oats'") as raised:
        my_foods.resolve_mine(client, "pizza session", None)
    assert not is_auth_error(raised.value)


def test_log_custom_food_uses_the_v2_diary(mine_client):
    food, _ = my_foods.resolve_mine(mine_client, "Soup + Chicken", None)
    result = my_foods.log_mine(mine_client, DAY, "dinner", food, quantity=2)
    body = json.loads(_posts(mine_client, "v2/diary")[0][2]["data"])
    assert body["items"][0] == {
        "type": "food_entry",
        "date": "2026-10-19",
        "food": {"id": "501", "version": "501"},
        "servings": 2,
        "meal_position": 2,
        "serving_size": {"nutrition_multiplier": 1.0, "unit": "meal", "value": 1.0},
    }
    assert result["meal"] == "Dinner"
    assert _posts(mine_client, "food/add") == []


def test_log_custom_food_rejects_an_unknown_serving(mine_client):
    food, _ = my_foods.resolve_mine(mine_client, "Soup + Chicken", None)
    with pytest.raises(ValueError, match="between 0 and 0"):
        my_foods.log_mine(mine_client, DAY, "dinner", food, serving=3)


def test_log_saved_meal_adds_it_through_food_add(mine_client):
    meal, _ = my_foods.resolve_mine(mine_client, None, "2222")
    my_foods.log_mine(mine_client, DAY, "snacks", meal)
    data = _posts(mine_client, "food/add")[0][2]["data"]
    assert data["food_entry[food_id]"] == "2222"
    assert data["food_entry[weight_id]"] == "9002"
    assert data["food_entry[meal_id]"] == "3"
    assert _posts(mine_client, "v2/diary") == []


def test_resolve_mine_rejects_a_foreign_food_id(mine_client):
    with pytest.raises(my_foods.NoMatchingFood, match="food_id 999"):
        my_foods.resolve_mine(mine_client, None, "999")


def test_save_meal_posts_the_remember_meal_form(mine_client, make_response):
    mine_client.session.route(
        "GET", "meal/new", make_response(text=(FIXTURES / "meal_new.html").read_text())
    )
    mine_client.session.route("POST", "meal/create", make_response(status_code=200))
    result = my_foods.save_meal(mine_client, DAY, "dinner", "Soup Dinner")
    form_get = [c for c in mine_client.session.calls if "meal/new" in c[1]][0]
    assert "meal=2" in form_get[1] and "date=2026-10-19" in form_get[1]
    data = _posts(mine_client, "meal/create")[0][2]["data"]
    assert data == {
        "authenticity_token": "meal-form-token",
        "meal_id": "2",
        "date": "2026-10-19",
        "meal[description]": "Soup Dinner",
    }
    assert result["meal"]["food_id"] == "1111"


def test_save_meal_refuses_an_empty_diary_meal(mine_client, make_response):
    mine_client.session.route(
        "GET",
        "meal/new",
        make_response(text=(FIXTURES / "meal_new_empty.html").read_text()),
    )
    with pytest.raises(my_foods.NoMatchingFood, match="nothing is logged in Dinner"):
        my_foods.save_meal(mine_client, DAY, "dinner", "Soup Dinner")
    assert _posts(mine_client, "meal/create") == []


class _Pages:
    """A response that gives each page in turn, then keeps giving the last."""

    status_code = 200

    def __init__(self, *pages):
        self.pages = list(pages)

    @property
    def text(self):
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

    def raise_for_status(self):
        pass


def _with_snacks(diary_html, *entries):
    rows = "".join(
        f'<tr><td><a data-food-entry-id="{e}">Food {e}</a></td></tr>' for e in entries
    )
    return diary_html.replace(
        '<tr class="meal_header"><td>Snacks</td></tr>',
        '<tr class="meal_header"><td>Snacks</td></tr>' + rows,
    )


@pytest.fixture
def scratch_client(mine_client, make_response, diary_html):
    mine_client.session.route(
        "GET",
        "food/diary?date=",
        _Pages(diary_html, _with_snacks(diary_html, "s1", "s2")),
    )
    mine_client.session.route(
        "GET", "meal/new", make_response(text=(FIXTURES / "meal_new.html").read_text())
    )
    mine_client.session.route("POST", "meal/create", make_response(status_code=200))
    return mine_client


def test_create_meal_builds_it_on_the_scratch_day_and_cleans_up(scratch_client):
    result = my_foods.create_meal(
        scratch_client,
        "Soup Dinner",
        [
            {"food_id": "123", "weight_id": "456", "quantity": 2},
            {"food_id": "501", "serving": 0, "quantity": 1},
        ],
    )
    added = _posts(scratch_client, "food/add")[0][2]["data"]
    assert (
        added["food_entry[date]"] == "2001-01-01"
        and added["food_entry[meal_id]"] == "3"
    )
    assert (
        added["food_entry[food_id]"] == "123" and added["food_entry[quantity]"] == "2.0"
    )
    custom = json.loads(_posts(scratch_client, "v2/diary")[0][2]["data"])
    assert (
        custom["items"][0]["date"] == "2001-01-01"
        and custom["items"][0]["meal_position"] == 3
    )
    saved = _posts(scratch_client, "meal/create")[0][2]["data"]
    assert saved["date"] == "2001-01-01" and saved["meal_id"] == "3"
    removed = [c[1].rsplit("/", 1)[1] for c in _posts(scratch_client, "food/remove")]
    assert removed == ["s1", "s2"]
    assert result["saved"] == "Soup Dinner"


def test_create_meal_leaves_a_scratch_meal_that_isnt_empty_alone(
    mine_client, diary_html
):
    mine_client.session.route(
        "GET", "food/diary?date=", _Pages(_with_snacks(diary_html, "mine"))
    )
    with pytest.raises(my_foods.DiaryLookupError, match="isn't empty"):
        my_foods.create_meal(mine_client, "X", [{"food_id": "1", "weight_id": "2"}])
    assert (
        _posts(mine_client, "food/add") == []
        and _posts(mine_client, "food/remove") == []
    )


def test_create_meal_cleans_up_when_saving_fails(scratch_client, make_response):
    scratch_client.session.route("POST", "meal/create", make_response(status_code=500))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        my_foods.create_meal(scratch_client, "X", [{"food_id": "1", "weight_id": "2"}])
    assert len(_posts(scratch_client, "food/remove")) == 2


@pytest.mark.parametrize(
    "items, message",
    [
        ([], "at least one food"),
        ([{"weight_id": "2"}], "needs a food_id"),
        ([{"food_id": "1111"}], "is a saved meal"),
    ],
)
def test_create_meal_rejects_bad_items_before_logging(mine_client, items, message):
    with pytest.raises(ValueError, match=message):
        my_foods.create_meal(mine_client, "X", items)
    assert _posts(mine_client, "food/add") == []


def test_delete_meal_deletes_a_saved_meal(mine_client, make_response):
    mine_client.session.route(
        "DELETE", "api/services/users/meals/delete/", make_response(status_code=204)
    )
    assert my_foods.delete_meal(mine_client, "2222") == {
        "deleted": "Taco Night",
        "food_id": "2222",
    }
    deletes = [c for c in mine_client.session.calls if c[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][1].endswith("api/services/users/meals/delete/2222")


def test_delete_meal_refuses_what_isnt_a_saved_meal(mine_client):
    with pytest.raises(my_foods.NoMatchingFood, match="isn't one of your saved meals"):
        my_foods.delete_meal(mine_client, "501")
    assert not [c for c in mine_client.session.calls if c[0] == "DELETE"]
