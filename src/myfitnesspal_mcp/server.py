import asyncio
import datetime
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import (
    diary,
    exercise,
    food_logging,
    food_search,
    goals,
    measurements,
    mfp_client,
    my_foods,
    notes,
    refresh,
    sync,
    water,
)
from .food_ranking import MacroTargets
from .store import Store, trend_column

mcp = FastMCP("myfitnesspal")

_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def parse_day(value: str | None) -> datetime.date:
    if value is None:
        return datetime.date.today()
    return datetime.date.fromisoformat(value)


def parse_range(
    start: str | None, end: str | None, span_days: int = 30
) -> tuple[datetime.date, datetime.date]:
    end_day = parse_day(end)
    if start is None:
        start_day = end_day - datetime.timedelta(days=span_days)
    else:
        start_day = parse_day(start)
    if start_day > end_day:
        raise ValueError("start must be on or before end")
    return start_day, end_day


async def run_with_refresh(ctx: Context, op: Callable[[], Any]) -> Any:
    try:
        return await asyncio.to_thread(op)
    except Exception as exc:
        if not mfp_client.is_auth_error(exc):
            raise
        await ctx.info(
            "MyFitnessPal rejected the session — refreshing credentials and retrying."
        )
        try:
            await asyncio.to_thread(refresh.refresh_session)
            result = await asyncio.to_thread(op)
        except Exception as retry_exc:
            await ctx.info("Session refresh failed.")
            raise RuntimeError(
                f"{mfp_client.RECONNECT_HINT} (retry after refresh failed: {retry_exc})"
            ) from retry_exc
        await ctx.info("Session refreshed; the retried call succeeded.")
        return result


async def with_session(ctx: Context, op: Callable[[Store, Any], Any]) -> Any:
    """Resolves the client inside the retried call so a refreshed session is
    picked up."""
    return await run_with_refresh(ctx, lambda: op(get_store(), mfp_client.get_client()))


async def refresh_after_write(ctx: Context, day: datetime.date) -> dict:
    def refresh_op(store, client):
        sync.refresh_day(store, client, day)

    try:
        await with_session(ctx, refresh_op)
    except Exception as exc:
        return {
            "refresh_warning": (
                f"the diary change was saved, but refreshing the local copy of "
                f"{day.isoformat()} failed ({exc}); don't repeat the change"
            )
        }
    return {}


@mcp.tool()
async def fitness_get_day(date: str | None = None, ctx: Context = None) -> dict:
    """Nutrition summary, diary entries, the MyFitnessPal daily note, and the
    local feel note for a day.

    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        sync.poll(store, client)
        sync.refresh_day(store, client, day)
        return store.day_record(day.isoformat())

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_search_food(
    query: str, limit: int = 5, with_macros: bool = True, ctx: Context = None
) -> dict:
    """Search MyFitnessPal's food database and return candidate matches.

    Each candidate has name, brand, calories, macros, serving, and the
    food_id + weight_id to pass to fitness_log_food to log exactly that item.
    """

    def op(store, client):
        return {
            "query": query,
            "results": food_search.search_food(client, query, limit, with_macros),
        }

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_draft_food(
    query: str,
    quantity: float = 1.0,
    meal: str = food_logging.DEFAULT_MEAL,
    date: str | None = None,
    min_calories: float | None = None,
    max_calories: float | None = None,
    min_protein: float | None = None,
    max_protein: float | None = None,
    min_carbs: float | None = None,
    max_carbs: float | None = None,
    min_fat: float | None = None,
    max_fat: float | None = None,
    limit: int = 10,
    ctx: Context = None,
) -> dict:
    """Draft a food entry: numbered options to choose from before logging.

    Each option lists every serving size with its calories/protein/carbs/fat
    for the whole entry (serving × quantity) and a suggested_serving. The
    min_/max_ targets (grams; calories in kcal) also apply to the whole
    entry: options that fit come first, near misses follow, flagged
    fits_targets=false. A food you've confirmed before for this query is
    pinned to the top. The order is fixed once drafted.

    Log a choice with fitness_log_food(draft_id=..., option=N, serving=M);
    call this again with different targets to refine. Drafts last 24 hours.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)
    targets = MacroTargets(
        min_calories=min_calories,
        max_calories=max_calories,
        min_protein=min_protein,
        max_protein=max_protein,
        min_carbs=min_carbs,
        max_carbs=max_carbs,
        min_fat=min_fat,
        max_fat=max_fat,
    )

    def op(store, client):
        return food_logging.draft_food(
            client,
            store,
            query,
            day,
            meal=meal,
            quantity=quantity,
            targets=targets,
            limit=limit,
        )

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_log_food(
    query: str | None = None,
    meal: str | None = None,
    quantity: float | None = None,
    date: str | None = None,
    draft_id: str | None = None,
    option: int | None = None,
    serving: int | None = None,
    pin: bool = True,
    food_id: str | None = None,
    weight_id: str | None = None,
    ctx: Context = None,
) -> dict:
    """Log a food to the real MyFitnessPal diary.

    Preferred: pick from a fitness_draft_food draft with draft_id + option
    (+ serving, default the option's suggested_serving). meal, quantity, and
    date default to the draft's. pin=True remembers the choice so logging
    the same query later reuses this food and serving.

    With only `query`: logs a pinned food, or the single exact-name match;
    otherwise nothing is logged and a draft is returned (needs_choice=true)
    to pick from. With food_id + weight_id (from fitness_search_food): logs
    exactly that item, `query` being its display name.

    meal: breakfast|lunch|dinner|snacks (default breakfast) or any meal name
    on the account. quantity: number of servings (default 1).
    date: YYYY-MM-DD (default today).
    """
    explicit_day = parse_day(date) if date else None
    day = explicit_day or parse_day(None)
    chosen_meal = meal or food_logging.DEFAULT_MEAL
    chosen_quantity = 1.0 if quantity is None else quantity

    def op(store, client):
        if draft_id is not None:
            if option is None:
                raise ValueError("option is required with draft_id")
            return food_logging.log_from_draft(
                client,
                store,
                draft_id,
                option,
                serving=serving,
                quantity=quantity,
                meal=meal,
                day=explicit_day,
                pin=pin,
            )
        if food_id is not None and weight_id is not None:
            food = {
                "food_id": food_id,
                "weight_id": weight_id,
                "name": query or food_id,
            }
            logged = food_logging.log_exact(
                client, food, day, chosen_meal, quantity=chosen_quantity
            )
            return {**logged, "source": "ids"}
        if query:
            return food_logging.log_by_query(
                client, store, query, day, meal=chosen_meal, quantity=chosen_quantity
            )
        raise ValueError("pass draft_id + option, food_id + weight_id, or query")

    result = await with_session(ctx, op)
    if not result.get("logged"):
        return {"ok": True, **result}
    refresh_warning = await refresh_after_write(
        ctx, datetime.date.fromisoformat(result["date"])
    )
    return {
        "ok": True,
        **result,
        **refresh_warning,
        "day": get_store().day_record(result["date"]),
    }


@mcp.tool()
def fitness_list_food_pins() -> dict:
    """List remembered food choices (query → food and serving) that
    fitness_log_food reuses. Stored locally only."""
    return {"pins": get_store().pins()}


@mcp.tool()
def fitness_clear_food_pin(query: str | None = None, clear_all: bool = False) -> dict:
    """Forget a remembered food choice for `query`, or every choice with
    clear_all=True, so the next log of that query drafts options again."""
    store = get_store()
    if clear_all:
        return {"cleared": store.clear_pins()}
    if not query:
        raise ValueError("pass query, or clear_all=True")
    return {"cleared": int(store.clear_pin(query))}


@mcp.tool()
async def fitness_delete_food(
    query: str, meal: str | None = None, date: str | None = None, ctx: Context = None
) -> dict:
    """Remove a food from the MyFitnessPal diary by name match.

    query: text matched against logged entry names (e.g. "banana"). If this
    matches nothing, the error lists what's actually logged that day/meal — use
    that to retry with a better query. If it matches more than one entry (and
    none is an exact name match), the error lists the candidates; narrow `query`
    to pick one. Identical duplicates (same name, including its quantity, in
    the same meal) are interchangeable, so one of them is removed; call again
    to remove another.
    meal: optional breakfast|lunch|dinner|snacks or any meal name on the
    account, to disambiguate duplicates. date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return diary.delete_food(client, day, query, meal)

    result = await with_session(ctx, op)
    return {"ok": True, **result, **await refresh_after_write(ctx, day)}


@mcp.tool()
async def fitness_modify_food(
    query: str,
    new_query: str | None = None,
    meal: str = food_logging.DEFAULT_MEAL,
    quantity: float | None = None,
    date: str | None = None,
    draft_id: str | None = None,
    option: int | None = None,
    serving: int | None = None,
    ctx: Context = None,
) -> dict:
    """Replace a MyFitnessPal diary entry: deletes the match, then adds a food.

    query: the existing entry to replace (name match) within `meal`. If this
    matches nothing, the error lists what's actually logged that day/meal — use
    that to retry with a better query. If it matches more than one entry (and
    none is an exact name match), the error lists the candidates; narrow `query`
    to pick one. Of several identical duplicates, one is replaced.
    new_query: the food to add instead; omit to re-add `query` (e.g. to change
    quantity). The replacement is chosen like fitness_log_food's: a pinned
    food or a single exact-name match is used directly; otherwise nothing is
    changed and a draft comes back (needs_choice=true) — call again with the
    same query plus draft_id + option (and optionally serving).
    meal: breakfast|lunch|dinner|snacks or any meal name on the account,
    used for both the delete and the add. quantity: servings (default 1, or
    the draft's). date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return food_logging.modify_food(
            client,
            store,
            day,
            meal,
            query=query,
            new_query=new_query,
            quantity=quantity,
            draft_id=draft_id,
            option=option,
            serving=serving,
        )

    result = await with_session(ctx, op)
    if result.get("needs_choice"):
        return {"ok": True, **result}
    return {"ok": True, **result, **await refresh_after_write(ctx, day)}


@mcp.tool()
async def fitness_create_food(
    name: str,
    calories: float,
    protein: float | None = None,
    carbohydrates: float | None = None,
    fat: float | None = None,
    serving_value: float = 1.0,
    serving_unit: str = "serving",
    brand: str | None = None,
    saturated_fat: float | None = None,
    sodium: float | None = None,
    fiber: float | None = None,
    sugar: float | None = None,
    cholesterol: float | None = None,
    potassium: float | None = None,
    ctx: Context = None,
) -> dict:
    """Create a private custom food in MyFitnessPal (it shows under My Foods).

    Use it for a combination eaten often (e.g. "soup + canned chicken" with the
    summed totals) or a food the database lacks. Nutrition is per one serving
    of serving_value x serving_unit (e.g. 1 x "meal", 150 x "g"). Units:
    calories in kcal; protein/carbohydrates/fat/saturated_fat/fiber/sugar in
    g; sodium/cholesterol/potassium in mg. Every call creates a new food, so
    check fitness_list_my_foods first. Log it with fitness_log_my_food, not
    fitness_log_food (custom foods aren't in the public search).
    """
    nutrients = {
        key: value
        for key, value in {
            "protein": protein,
            "carbohydrates": carbohydrates,
            "fat": fat,
            "saturated_fat": saturated_fat,
            "sodium": sodium,
            "fiber": fiber,
            "sugar": sugar,
            "cholesterol": cholesterol,
            "potassium": potassium,
        }.items()
        if value is not None
    }

    def op(store, client):
        return my_foods.create_food(
            client,
            name,
            calories,
            nutrients=nutrients,
            serving_value=serving_value,
            serving_unit=serving_unit,
            brand=brand or "",
        )

    return {"ok": True, "created": await with_session(ctx, op)}


@mcp.tool()
async def fitness_save_meal(
    name: str,
    meal: str = food_logging.DEFAULT_MEAL,
    date: str | None = None,
    ctx: Context = None,
) -> dict:
    """Save what's logged in one diary meal as a named MyFitnessPal saved meal
    (My Meals), like the diary's "Remember Meal" button.

    Log the foods first (fitness_log_food), then save, e.g. meal="dinner",
    name="Soup + canned chicken". The saved meal keeps each food and its
    quantity; log it again later with fitness_log_my_food. Every call saves a
    new meal, so check fitness_list_my_foods first.
    meal: breakfast|lunch|dinner|snacks or any meal name on the account.
    date: YYYY-MM-DD of the diary day to copy from (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return my_foods.save_meal(client, day, meal, name)

    return {"ok": True, **await with_session(ctx, op)}


@mcp.tool()
async def fitness_create_meal(
    name: str, items: list[dict], ctx: Context = None
) -> dict:
    """Create a named MyFitnessPal saved meal (My Meals) from foods, without
    logging them to any of your days.

    items: each food in the meal, one of
      {"food_id": ..., "weight_id": ..., "quantity": 2} from fitness_search_food, or
      {"food_id": ..., "serving": 0, "quantity": 1} for one of your custom foods
      (fitness_list_my_foods; serving is the index of its serving size).
    quantity is the number of servings. The meal is built in Snacks on
    2001-01-01 and those entries are removed again, so that day must be empty.
    To save foods already logged on a day, use fitness_save_meal. Every call
    creates a new meal, so check fitness_list_my_foods first. Log it later with
    fitness_log_my_food.
    """

    def op(store, client):
        return my_foods.create_meal(client, name, items)

    return {"ok": True, **await with_session(ctx, op)}


@mcp.tool()
async def fitness_delete_meal(food_id: str, ctx: Context = None) -> dict:
    """Delete one of your MyFitnessPal saved meals (My Meals). It can't be
    undone, so only when the user asks to delete that meal.

    food_id: the meal's food_id from fitness_list_my_foods (kind "meal").
    Foods already logged from it stay in the diary.
    """

    def op(store, client):
        return my_foods.delete_meal(client, food_id)

    return {"ok": True, **await with_session(ctx, op)}


@mcp.tool()
async def fitness_list_my_foods(search: str = "", ctx: Context = None) -> dict:
    """List your MyFitnessPal custom foods (kind "food") and saved meals
    (kind "meal"), optionally filtered by name.

    Each has a food_id to pass to fitness_log_my_food, its calories and
    macros (per serving for foods, the total for meals), and for foods the
    serving sizes by index.
    """

    def op(store, client):
        return my_foods.find_mine(client, search)

    return {"ok": True, "search": search, "results": await with_session(ctx, op)}


@mcp.tool()
async def fitness_log_my_food(
    query: str | None = None,
    food_id: str | None = None,
    meal: str | None = None,
    quantity: float | None = None,
    serving: int | None = None,
    date: str | None = None,
    ctx: Context = None,
) -> dict:
    """Log one of your own custom foods or saved meals to the diary.

    query: matched against your custom foods and saved meals by name. It logs
    when one name matches exactly or there's only one match; otherwise nothing
    is logged and the candidates come back (needs_choice=true) — call again
    with food_id. food_id: from fitness_list_my_foods or those candidates.
    Logging a saved meal adds each of its foods as separate diary entries.
    meal: breakfast|lunch|dinner|snacks (default breakfast) or any meal name
    on the account. quantity: number of servings (default 1). serving: a
    custom food's serving-size index (default 0); saved meals have only one.
    date: YYYY-MM-DD (default today).
    """
    day = parse_day(date)
    chosen_meal = meal or food_logging.DEFAULT_MEAL

    def op(store, client):
        food, candidates = my_foods.resolve_mine(client, query, food_id)
        if food is None:
            return {
                "logged": None,
                "needs_choice": True,
                "query": query,
                "candidates": candidates,
            }
        return my_foods.log_mine(
            client,
            day,
            chosen_meal,
            food,
            quantity=1.0 if quantity is None else quantity,
            serving=serving,
        )

    result = await with_session(ctx, op)
    if not result.get("logged"):
        return {"ok": True, **result}
    return {
        "ok": True,
        **result,
        **await refresh_after_write(ctx, day),
        "day": get_store().day_record(day.isoformat()),
    }


@mcp.tool()
async def fitness_log_weight(
    weight: float, date: str | None = None, ctx: Context = None
) -> dict:
    """Log a weight measurement to MyFitnessPal.

    weight: in your MyFitnessPal account's display unit (kg or lbs).
    Logging twice for the same date updates that day's measurement.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        result = measurements.set_weight(client, day, weight)
        store.upsert_nutrition(day.isoformat(), weight=result["weight"])
        return {"ok": True, **result}

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_log_water(
    amount: float,
    unit: str = "cup",
    replace: bool = False,
    date: str | None = None,
    ctx: Context = None,
) -> dict:
    """Log water to the real MyFitnessPal water tracker.

    amount: quantity to add to the day's total. unit: cup | fl_oz | ml | l
    (common spellings like "cups", "oz", "fl oz", "milliliters", "litres" are
    accepted). replace: set the day's total to `amount` instead of adding —
    use it to correct a mislogged total (amount=0 clears the day).
    Returns previous_ml; to undo a mistake, call again with
    amount=previous_ml, unit="ml", replace=True.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        result = water.log_water(client, day, amount, unit, replace=replace)
        store.upsert_nutrition(day.isoformat(), water_ml=result["water_ml"])
        return {"ok": True, **result}

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_get_goals(date: str | None = None, ctx: Context = None) -> dict:
    """Read the MyFitnessPal nutrition goals in effect on a day.

    Returns calories plus every nutrient goal (grams; cholesterol, sodium and
    potassium in mg; vitamins and minerals in %DV).
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return goals.get_goals(client, day)

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_set_goals(
    calories: float | None = None,
    protein: float | None = None,
    carbohydrates: float | None = None,
    fat: float | None = None,
    saturated_fat: float | None = None,
    cholesterol: float | None = None,
    sodium: float | None = None,
    fiber: float | None = None,
    sugar: float | None = None,
    potassium: float | None = None,
    ctx: Context = None,
) -> dict:
    """Change MyFitnessPal nutrition goals from today onward.

    Pass only the goals to change; everything else keeps its current value.
    Units: calories in kcal; protein/carbohydrates/fat/saturated_fat/fiber/
    sugar in g; cholesterol/sodium/potassium in mg. Applies to every weekday.
    Returns the previous values of what changed, to undo a mistake.
    """
    changes = {
        key: value
        for key, value in {
            "calories": calories,
            "protein": protein,
            "carbohydrates": carbohydrates,
            "fat": fat,
            "saturated_fat": saturated_fat,
            "cholesterol": cholesterol,
            "sodium": sodium,
            "fiber": fiber,
            "sugar": sugar,
            "potassium": potassium,
        }.items()
        if value is not None
    }
    today = datetime.date.today()

    def op(store, client):
        return goals.set_goals(client, today, changes)

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_get_exercise(date: str | None = None, ctx: Context = None) -> dict:
    """Read the MyFitnessPal exercise diary (cardio + strength) for a day.

    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return exercise.get_exercise(client, day)

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_get_exercise_entries(
    date: str | None = None, ctx: Context = None
) -> dict:
    """List a day's cardio and strength exercise-diary entries.

    Each entry has its section, name, and that section's columns (cardio:
    minutes, calories_burned; strength: sets, reps_set, weight_set). entry_id
    is informational only — fitness_delete_exercise deletes by name match.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        doc, _ = exercise.exercise_page(client, day)
        return {"day": day.isoformat(), "entries": exercise.exercise_entries(doc)}

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_log_exercise(
    minutes: float,
    query: str | None = None,
    exercise_id: str | None = None,
    calories: int | None = None,
    date: str | None = None,
    ctx: Context = None,
) -> dict:
    """Log a cardio exercise (e.g. walking) to the MyFitnessPal exercise diary.

    query: searched in MFP's cardio database, e.g. "walking, 12.5 mins per km".
    It logs when one result's name matches exactly or there is only one
    result; otherwise nothing is logged and the candidates come back
    (needs_choice=true) — call again with exercise_id. minutes: duration.
    calories: burned; omit to let MFP's formula (METs x hours x your current
    weight) compute it, as the web form does. Steps aren't a web-diary
    concept: log them as walking minutes (~100-110 steps per minute at a
    moderate pace). date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return exercise.log_cardio(
            client,
            day,
            minutes,
            query=query,
            exercise_id=exercise_id,
            calories=calories,
        )

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_delete_exercise(
    query: str,
    date: str | None = None,
    all_matches: bool = False,
    ctx: Context = None,
) -> dict:
    """Remove exercise-diary entries by name match (case-insensitive).

    query: text matched against logged exercise names. By default deletes
    exactly one entry, like fitness_delete_food: an exact name match wins;
    otherwise several matches raise an error listing the candidates, and no
    match raises an error listing what's actually logged. Of several identical
    rows (same name and columns), one is removed.
    all_matches: delete every entry whose name contains `query` — for
    cleaning up syncs that log one workout as several same-named rows
    (e.g. Garmin's "Aerobics, general"). Check fitness_get_exercise_entries
    first; a short query can match many entries.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return exercise.delete_exercise(client, day, query, all_matches=all_matches)

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_get_note(date: str | None = None, ctx: Context = None) -> dict:
    """Read the MyFitnessPal daily diary note (the free-text 'Notes' box at the
    bottom of the day) straight from your account.

    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        body = notes.get_note(client, day)
        store.set_note(day.isoformat(), body)
        return {"day": day.isoformat(), "note": body}

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_log_note(
    text: str, date: str | None = None, append: bool = False, ctx: Context = None
) -> dict:
    """Write the MyFitnessPal daily diary note (the free-text 'Notes' box at the
    bottom of the day). This is your real MFP note, synced to your account —
    distinct from the local-only fitness_log_feel.

    text: the note body. append: add to the existing note on a new line instead
    of replacing it. date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        result = notes.push_note(client, day, text, append=append)
        store.set_note(day.isoformat(), result["note"])
        return {"ok": True, **result}

    return await with_session(ctx, op)


@mcp.tool()
def fitness_log_feel(
    note: str | None = None, rating: int | None = None, date: str | None = None
) -> dict:
    """Save a 'how I feel today' note. Stored locally only — never sent to
    MyFitnessPal.

    rating: optional 1-5. date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)
    return get_store().set_feel(day.isoformat(), note, rating)


@mcp.tool()
async def fitness_get_trends(
    metric: str, start: str | None = None, end: str | None = None, ctx: Context = None
) -> dict:
    """A single metric over a date range, for charts/analysis.

    metric: weight | calories_in | protein | carbs | fat.
    start/end: YYYY-MM-DD (default: last 30 days).
    Returns {metric, points: [{day, value}, ...]} with nulls omitted.
    """
    trend_column(metric)
    start_day, end_day = parse_range(start, end)

    def op(store, client):
        sync.poll(store, client)
        return {
            "metric": metric,
            "points": store.trend(metric, start_day.isoformat(), end_day.isoformat()),
        }

    return await with_session(ctx, op)


@mcp.tool()
async def fitness_bulk_export(
    start: str | None = None,
    end: str | None = None,
    sync_first: bool = False,
    ctx: Context = None,
) -> dict:
    """Export a whole date range at once for analysis: per-day nutrition
    totals, food entries with macros, the MyFitnessPal daily note, and local
    feel notes. Read-only.

    start/end: YYYY-MM-DD (default: last 30 days ending today).
    sync_first: gap-fill from MyFitnessPal before exporting. Off by default so
    large historical exports stay fast on cached data.
    """
    start_day, end_day = parse_range(start, end)

    def op():
        store = get_store()
        if sync_first:
            span = (end_day - start_day).days + 1
            sync.poll(store, mfp_client.get_client(), days=span, force=True)
        days = store.export_range(start_day.isoformat(), end_day.isoformat())
        return {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "count": len(days),
            "days": days,
        }

    return await run_with_refresh(ctx, op)
