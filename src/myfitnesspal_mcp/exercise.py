from datetime import date

from lxml import html as lh

from .entry_matching import find_entries, no_matching_entry, resolve_entry
from .mfp_web import (
    form_token,
    get_document,
    get_page,
    legacy_form_headers,
    post_legacy_delete,
    require_status,
    site_url,
)

ALL_MATCHES_ADVICE = (
    "Use a more specific query to pick one, or pass all_matches=True to remove "
    "every match (e.g. duplicate rows from a sync)."
)


def get_exercise(client, day: date) -> dict:
    sections = {}
    for section in client._get_exercises(day):
        sections[section.name.lower()] = section.get_as_list()
    return {"day": day.isoformat(), "exercise": sections}


def _exercise_diary_url(client, day: date | None = None) -> str:
    return site_url(client, f"exercise/diary/{client.effective_username}", day)


def exercise_page(client, day: date) -> tuple[lh.HtmlElement, str]:
    return get_page(client, _exercise_diary_url(client, day))


def _field_key(heading: str) -> str:
    words = "".join(c if c.isalnum() else " " for c in heading.lower()).split()
    return "_".join(words)


def _cell_number(cell) -> float | None:
    try:
        return float(cell.text_content().strip().replace(",", ""))
    except ValueError:
        return None


def _exercise_name(cell) -> str:
    for anchor in cell.xpath(".//a"):
        name = anchor.text_content().strip()
        if name:
            return name
    return cell.text_content().strip()


def exercise_entries(doc) -> list[dict]:
    entries = []
    for table in doc.xpath("//table[contains(@class, 'table0')]"):
        headings = table.xpath("./thead/tr[1]/td")
        if not headings:
            continue
        section = headings[0].text_content().strip().lower()
        field_keys = [_field_key(h.text_content()) for h in headings[1:]]
        for tr in table.xpath("./tbody/tr[not(@class)]"):
            delete_links = tr.xpath("./td[contains(@class, 'delete')]//a/@href")
            if not delete_links:
                continue
            cells = tr.xpath("./td")
            entry = {
                "entry_id": delete_links[0].split("?")[0].rstrip("/").split("/")[-1],
                "section": section,
                "name": _exercise_name(cells[0]),
            }
            for key, cell in zip(field_keys, cells[1:], strict=False):
                if key:
                    entry[key] = _cell_number(cell)
            entries.append(entry)
    return entries


def remove_exercise_entry(client, entry_id: str, token: str) -> None:
    post_legacy_delete(
        client,
        "exercise/remove",
        entry_id,
        token,
        referer=_exercise_diary_url(client),
    )


def delete_exercise(client, day: date, query: str, all_matches: bool = False) -> dict:
    doc, token = exercise_page(client, day)
    entries = exercise_entries(doc)
    if all_matches:
        targets = find_entries(entries, query)
        if not targets:
            raise no_matching_entry(entries, query, None, day)
    else:
        targets = [resolve_entry(entries, query, None, day, advice=ALL_MATCHES_ADVICE)]
    for entry in targets:
        remove_exercise_entry(client, entry["entry_id"], token)
    return {"day": day.isoformat(), "removed": targets, "count": len(targets)}


def _add_page_url(client, day: date) -> str:
    return site_url(
        client,
        "exercise/add_to_diary",
        params={"date": day.isoformat(), "type": "cardio"},
    )


def _hidden_value(doc, element_id: str) -> str | None:
    values = doc.xpath(f"//input[@id='{element_id}']/@value")
    return values[0] if values else None


def search_cardio(client, day: date, query: str) -> list[dict]:
    """The legacy add page's search form; results link to the per-exercise
    entry form, whose path carries the exercise id."""
    add_url = _add_page_url(client, day)
    doc, _ = get_page(client, add_url)
    resp = client.session.post(
        site_url(client, "exercise/search"),
        data={
            "authenticity_token": form_token(doc, "/exercise/search"),
            "type": "cardio",
            "date": day.isoformat(),
            "search": query,
        },
        headers=legacy_form_headers(add_url),
    )
    require_status(resp, "/exercise/search", (200,))
    results = []
    for anchor in lh.fromstring(resp.text).xpath("//ul[@id='matching']//li/a"):
        href = anchor.get("href", "")
        if "/exercise/update_exercise/" not in href:
            continue
        results.append(
            {
                "exercise_id": href.split("?")[0].rstrip("/").split("/")[-1],
                "name": anchor.text_content().strip(),
            }
        )
    return results


def _pick_exercise(results: list[dict], query: str) -> dict | None:
    wanted = query.strip().lower()
    exact = [r for r in results if r["name"].lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(results) == 1:
        return results[0]
    return None


def log_cardio(
    client,
    day: date,
    minutes: float,
    query: str | None = None,
    exercise_id: str | None = None,
    calories: int | None = None,
) -> dict:
    """Replicates the legacy add-cardio form. Without `calories`, burns are
    computed the way the form's own JavaScript does: METs x hours x the
    account's current weight in kg x the page's calorie multiplier."""
    if minutes <= 0:
        raise ValueError("minutes must be greater than zero")
    if exercise_id is None:
        if not query:
            raise ValueError("pass a query or an exercise_id")
        results = search_cardio(client, day, query)
        if not results:
            raise ValueError(f"no MyFitnessPal cardio exercise matches {query!r}")
        picked = _pick_exercise(results, query)
        if picked is None:
            return {
                "ok": False,
                "needs_choice": True,
                "query": query,
                "candidates": results,
                "next_step": (
                    "Call again with exercise_id set to one candidate's id, "
                    "or with query set to its exact name."
                ),
            }
        exercise_id = picked["exercise_id"]

    add_doc, _ = get_page(client, _add_page_url(client, day))
    weight_kg = float(_hidden_value(add_doc, "current_weight") or 0)
    multiplier = float(_hidden_value(add_doc, "calorie_multiplier") or 1.0)

    entry_url = site_url(client, f"exercise/update_exercise/{exercise_id}", day)
    form = get_document(client, entry_url)
    token = form.xpath("//input[@name='authenticity_token']/@value")
    if not token:
        raise RuntimeError("couldn't read the MyFitnessPal exercise entry form")
    names = form.xpath("//p[@class='exercise-description']/text()")
    name = names[0].strip() if names else str(exercise_id)
    exercise_type = _hidden_value(form, "exercise_entry_exercise_type") or "0"
    if calories is None:
        mets = float(_hidden_value(form, "mets") or 0)
        if not mets or not weight_kg:
            raise RuntimeError(
                "MyFitnessPal didn't supply METs or weight; pass calories explicitly"
            )
        calories = round(mets * (minutes / 60) * weight_kg * multiplier)

    resp = client.session.post(
        site_url(client, "exercise/add"),
        data={
            "authenticity_token": token[0],
            "calorie_multiplier": multiplier,
            "exercise_entry[exercise_id]": exercise_id,
            "exercise_entry[date]": day.isoformat(),
            "exercise_entry[exercise_type]": exercise_type,
            "exercise_entry[quantity]": f"{minutes:g}",
            "exercise_entry[start_time(1i)]": day.year,
            "exercise_entry[start_time(2i)]": day.month,
            "exercise_entry[start_time(3i)]": day.day,
            "exercise_entry[start_time(4i)]": "12",
            "exercise_entry[start_time(5i)]": "00",
            "exercise_entry[calories]": calories,
        },
        headers=legacy_form_headers(_add_page_url(client, day)),
    )
    require_status(resp, "/exercise/add", (200, 302))
    return {
        "ok": True,
        "day": day.isoformat(),
        "exercise_id": str(exercise_id),
        "name": name,
        "minutes": minutes,
        "calories": calories,
    }
