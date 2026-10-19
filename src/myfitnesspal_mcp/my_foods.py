import json
from datetime import date
from urllib import parse

from . import diary
from .entry_matching import DiaryLookupError
from .food_ranking import normalize_query
from .goals import NUTRIENT_KEYS
from .mfp_web import (
    api_headers,
    form_token,
    get_document,
    get_json,
    legacy_form_headers,
    require_status,
    site_url,
)

FOOD = "food"
MEAL = "meal"


class NoMatchingFood(DiaryLookupError):
    pass


def _api_url(client, path: str) -> str:
    return parse.urljoin(client.BASE_API_URL, path)


def _post_api_json(client, path: str, body: dict) -> dict:
    resp = client.session.post(
        _api_url(client, path),
        data=json.dumps(body),
        headers=api_headers(
            client, {"Content-Type": "application/json", "Accept": "application/json"}
        ),
    )
    require_status(resp, f"/{path}", (200, 201))
    return resp.json()


def _country_code(client) -> str | None:
    profile = get_json(
        client,
        _api_url(
            client, f"v2/users/{client.user_id}?fields%5B%5D=location_preferences"
        ),
    )
    return ((profile or {}).get("item", {}).get("location_preferences") or {}).get(
        "country_code"
    )


def _serving_sizes(value: float, unit: str) -> list[dict]:
    """The web food form always adds a 1-unit serving next to a non-1 one."""
    sizes = [{"value": value, "unit": unit, "nutrition_multiplier": 1}]
    if value != 1:
        sizes.append({"value": 1, "unit": unit, "nutrition_multiplier": 1 / value})
    return sizes


def _food_view(item: dict) -> dict:
    contents = item.get("nutritional_contents") or {}
    return {
        "kind": FOOD,
        "food_id": str(item["id"]),
        "version": str(item.get("version") or item["id"]),
        "name": item.get("description"),
        "brand": item.get("brand_name") or None,
        "calories": (contents.get("energy") or {}).get("value"),
        "protein": contents.get("protein"),
        "carbs": contents.get("carbohydrates"),
        "fat": contents.get("fat"),
        "servings": [
            {
                "serving": index,
                "label": f"{size['value']:g} {size['unit']}",
                "value": size["value"],
                "unit": size["unit"],
                "nutrition_multiplier": size["nutrition_multiplier"],
            }
            for index, size in enumerate(item.get("serving_sizes") or [])
        ],
    }


def create_food(
    client,
    name: str,
    calories: float,
    *,
    nutrients: dict[str, float],
    serving_value: float = 1.0,
    serving_unit: str = "serving",
    brand: str = "",
) -> dict:
    """Replicates the web app's food/new form, always as a private food."""
    if not name.strip():
        raise ValueError("name can't be empty")
    if not serving_unit.strip():
        raise ValueError("serving_unit can't be empty")
    if serving_value <= 0:
        raise ValueError("serving_value must be greater than zero")
    unknown = set(nutrients) - set(NUTRIENT_KEYS)
    if unknown:
        raise ValueError(f"unknown nutrients: {', '.join(sorted(unknown))}")
    if calories < 0 or any(value < 0 for value in nutrients.values()):
        raise ValueError("calories and nutrients can't be negative")
    item = {
        "user_id": str(client.user_id),
        "brand_name": brand.strip(),
        "description": name.strip(),
        "nutritional_contents": {
            "energy": {"unit": "calories", "value": calories},
            **nutrients,
        },
        "serving_sizes": _serving_sizes(serving_value, serving_unit.strip()),
        "public": False,
        "country_code": _country_code(client),
    }
    body = _post_api_json(client, "v2/foods", {"item": item})
    return _food_view(body["items"][0])


def my_foods(client, search: str = "") -> list[dict]:
    items = get_json(
        client,
        site_url(client, "api/services/users/foods/mine", params={"search": search}),
    )
    return [_food_view(item) for item in items or [] if not item.get("deleted")]


def _meal_view(meal: dict) -> dict:
    foods = meal.get("foods") or []
    return {
        "kind": MEAL,
        "food_id": str(meal["meal_id"]),
        "name": meal.get("description"),
        "calories": sum(food.get("calories") or 0 for food in foods),
        "protein": sum(food.get("protein") or 0 for food in foods),
        "carbs": sum(food.get("carbs") or 0 for food in foods),
        "items": [food.get("description") for food in foods],
    }


def my_meals(client, search: str = "") -> list[dict]:
    meals = get_json(
        client,
        site_url(
            client,
            "api/services/users/meals/mine",
            params={"limit": 100, "search": search},
        ),
    )
    return [_meal_view(meal) for meal in meals or []]


def find_mine(client, search: str = "") -> list[dict]:
    return my_foods(client, search) + my_meals(client, search)


def _meal_weight_id(client, day: date, meal_id: str) -> str:
    """Saved meals log through /food/add like any food: the legacy My Meals
    tab lists each with its meal id as the food id and a "1 meal" weight."""
    doc = get_document(
        client, site_url(client, "food/load_meals", day, params={"meal": 0})
    )
    weight_ids = doc.xpath(
        f"//tr[.//input[contains(@name, '[food_id]') and @value='{meal_id}']]"
        "//select[contains(@name, '[weight_id]')]/option/@value"
    )
    if not weight_ids:
        raise NoMatchingFood(
            f"saved meal {meal_id} isn't in your MyFitnessPal meals; "
            "call fitness_list_my_foods to see what's saved"
        )
    return weight_ids[0]


def _pick(candidates: list[dict], query: str) -> dict | None:
    wanted = normalize_query(query)
    exact = [c for c in candidates if normalize_query(c["name"] or "") == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_mine(
    client, query: str | None, food_id: str | None
) -> tuple[dict | None, list[dict]]:
    if food_id is not None:
        candidates = find_mine(client, query or "")
        match = [c for c in candidates if c["food_id"] == str(food_id)]
        if not match:
            raise NoMatchingFood(
                f"food_id {food_id} isn't one of your custom foods or saved meals"
                + (f" matching {query!r}" if query else "")
                + "; call fitness_list_my_foods to see them"
            )
        return match[0], candidates
    if not query:
        raise ValueError("pass a query or a food_id")
    candidates = find_mine(client, query)
    if not candidates:
        names = [c["name"] for c in find_mine(client)]
        raise NoMatchingFood(
            f"none of your custom foods or saved meals match {query!r}. "
            "Yours: " + ("; ".join(repr(n) for n in names) or "none")
        )
    return _pick(candidates, query), candidates


def _log_custom_food(
    client, day: date, meal_position: int, food: dict, servings: float, serving: int
) -> None:
    size = food["servings"][serving]
    _post_api_json(
        client,
        "v2/diary",
        {
            "items": [
                {
                    "type": "food_entry",
                    "date": day.isoformat(),
                    "food": {"id": food["food_id"], "version": food["version"]},
                    "servings": servings,
                    "meal_position": meal_position,
                    "serving_size": {
                        "nutrition_multiplier": size["nutrition_multiplier"],
                        "unit": size["unit"],
                        "value": size["value"],
                    },
                }
            ]
        },
    )


def log_mine(
    client,
    day: date,
    meal: str,
    food: dict,
    *,
    quantity: float = 1.0,
    serving: int | None = None,
) -> dict:
    """Custom foods carry v2 ids, which /food/add rejects, so they log through
    the v2 diary API the web app itself uses for them."""
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")
    page = diary.diary_page(client, day)
    meal_position, meal_name = diary.resolve_meal(page[0], meal)
    if food["kind"] == MEAL:
        if serving not in (None, 0):
            raise ValueError("saved meals have one serving (1 meal); omit serving")
        diary.push_food(
            client,
            day,
            meal,
            food_id=food["food_id"],
            weight_id=_meal_weight_id(client, day, food["food_id"]),
            quantity=quantity,
            page=page,
        )
        label = "meal"
    else:
        index = 0 if serving is None else serving
        if not 0 <= index < len(food["servings"]):
            raise ValueError(
                f"serving must be between 0 and {len(food['servings']) - 1} "
                f"for {food['name']!r}"
            )
        _log_custom_food(client, day, int(meal_position), food, quantity, index)
        label = food["servings"][index]["label"]
    return {
        "logged": food["name"],
        "kind": food["kind"],
        "food_id": food["food_id"],
        "serving": label,
        "quantity": quantity,
        "meal": meal_name,
        "date": day.isoformat(),
    }


def save_meal(client, day: date, meal: str, name: str) -> dict:
    """Replicates the diary's "Remember Meal" form. MFP takes the foods from
    the diary meal the form page was opened for (meal + date in its URL), so
    the page is fetched first on the same session."""
    if not name.strip():
        raise ValueError("name can't be empty")
    doc, _ = diary.diary_page(client, day)
    meal_position, meal_name = diary.resolve_meal(doc, meal)
    form_url = site_url(client, "meal/new", day, params={"meal": meal_position})
    form_doc = get_document(client, form_url)
    if not form_doc.xpath("//table[@id='meal-table']/tbody/tr[td]"):
        raise NoMatchingFood(
            f"nothing is logged in {meal_name} on {day.isoformat()}, so there's "
            "no meal to save"
        )
    resp = client.session.post(
        site_url(client, "meal/create"),
        data={
            "authenticity_token": form_token(form_doc, "/meal/create"),
            "meal_id": meal_position,
            "meal[description]": name.strip(),
        },
        headers=legacy_form_headers(form_url),
    )
    require_status(resp, "/meal/create", (200, 302))
    wanted = normalize_query(name)
    saved = [m for m in my_meals(client, name) if normalize_query(m["name"]) == wanted]
    return {
        "saved": name.strip(),
        "from_meal": meal_name,
        "date": day.isoformat(),
        "meal": saved[-1] if saved else None,
    }
