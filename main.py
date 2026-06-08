from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from magicplanProjectFetcher.client import (
    MagicplanAPIError,
    MagicplanClient,
    MagicplanConfig,
    MagicplanConfigError,
)

EXPORT_DIR = Path("Exported Magicplan Projects")


def main() -> None:
    try:
        config = MagicplanConfig.from_env()
        client = MagicplanClient(config)

        print("Checking Magicplan workspace connection...")
        client.test_workspace_connection()
        print("Connection successful.\n")

        projects = client.list_projects()
        if not projects:
            print("No projects were returned for this Magicplan workspace.")
            return

        print("Available Magicplan projects:")
        for index, project in enumerate(projects, start=1):
            print(format_project_row(index, project))

        selected_project = prompt_for_project(projects)
        project_id = get_project_id(selected_project)

        if not project_id:
            print("The selected project does not include an id/project_id field.")
            print(json.dumps(selected_project, indent=2, ensure_ascii=False))
            return

        print(f"\nFetching full project bundle for project ID: {project_id}")
        project_bundle = client.get_project_bundle(project_id)

        # Helpful while still building the parser. Remove later if you no longer need it.
        save_debug_bundle(project_id, project_bundle)

        cleaned_export = build_clean_export(project_bundle)
        output_folder = save_clean_export(project_id, cleaned_export)

        print("\nCleaned project export saved successfully.")
        print(f"Output folder: {output_folder}")

        print("\nExported files:")
        for file_path in sorted(output_folder.glob("*.json")):
            print(f"- {file_path}")

    except (MagicplanConfigError, MagicplanAPIError, KeyboardInterrupt) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)


def prompt_for_project(projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    while True:
        raw_value = input("\nSelect a project number: ").strip()

        try:
            selected_index = int(raw_value)
        except ValueError:
            print("Please enter a valid number from the list.")
            continue

        if 1 <= selected_index <= len(projects):
            return projects[selected_index - 1]

        print(f"Please enter a number between 1 and {len(projects)}.")


def format_project_row(index: int, project: Dict[str, Any]) -> str:
    project_id = get_project_id(project) or "missing-id"
    name = (
        get_first_present(project, "name", "title", "project_name", "label")
        or get_nested(project, "data", "name")
        or "Untitled project"
    )
    updated = (
        get_first_present(project, "updated_at", "updatedAt", "modified_at", "modifiedAt")
        or get_nested(project, "data", "user_modified")
    )
    address = extract_address(project)

    parts = [f"{index}. {name}", f"id={project_id}"]

    if updated:
        parts.append(f"updated={updated}")

    if address:
        parts.append(f"address={address}")

    return " | ".join(parts)


def get_project_id(project: Dict[str, Any]) -> Optional[str]:
    value = get_first_present(project, "id", "project_id", "projectId", "uuid")

    if value is not None:
        return str(value)

    data = project.get("data")
    if isinstance(data, dict):
        value = get_first_present(data, "id", "project_id", "projectId", "uuid")
        if value is not None:
            return str(value)

    return None


def get_first_present(project: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        if isinstance(project, dict) and key in project and project[key] not in (None, "", [], {}):
            return project[key]
    return None


def extract_address(project: Dict[str, Any]) -> Optional[str]:
    address = project.get("address")

    if not address and isinstance(project.get("data"), dict):
        address = project["data"].get("address")

    if isinstance(address, str):
        return address

    if isinstance(address, dict):
        address_parts = [
            address.get("street"),
            address.get("city"),
            address.get("state"),
            address.get("postal_code"),
            address.get("zip"),
            address.get("country"),
        ]
        return ", ".join(str(part) for part in address_parts if part)

    return None


def build_clean_export(project_bundle: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]] | Dict[str, Any]]:
    project_payload = project_bundle.get("project", {})
    project_data = project_payload.get("data", project_payload)

    plans = project_bundle.get("plans", [])

    floors: List[Dict[str, Any]] = []
    rooms: List[Dict[str, Any]] = []
    walls: List[Dict[str, Any]] = []
    objects: List[Dict[str, Any]] = []
    wall_items: List[Dict[str, Any]] = []
    shyld_devices: List[Dict[str, Any]] = []
    doors: List[Dict[str, Any]] = []
    windows: List[Dict[str, Any]] = []

    for plan in plans:
        summary = plan.get("extracted_summary", {})

        raw_floors = summary.get("floors", [])
        raw_rooms = summary.get("rooms", [])
        raw_walls = summary.get("walls", [])
        raw_objects = summary.get("objects", [])
        raw_wall_items = summary.get("wall_items", [])
        raw_shyld_devices = summary.get("shyld_devices", [])
        raw_doors = summary.get("doors", [])
        raw_windows = summary.get("windows", [])

        # Derive important objects from wall_items too, because Magicplan stores
        # windows, doors, and wall-mounted custom objects there.
        derived_shyld_devices = filter_items(raw_wall_items, is_shyld_device)
        derived_doors = filter_items(raw_wall_items, is_door_item)
        derived_windows = filter_items(raw_wall_items, is_window_item)

        floors.extend(clean_items(raw_floors, clean_floor))
        rooms.extend(clean_items(raw_rooms, clean_room))
        walls.extend(clean_items(raw_walls, clean_wall))

        # objects.json should include regular objects AND wall_items,
        # because Magicplan may store placed objects under wall_items.
        objects.extend(clean_items(raw_objects, clean_object))
        objects.extend(clean_items(raw_wall_items, clean_object))

        wall_items.extend(clean_items(raw_wall_items, clean_wall_item))

        # Put derived wall_items first because they are more likely to include
        # Magicplan custom fields like "Serial Number*".
        shyld_devices.extend(clean_items(derived_shyld_devices, clean_shyld_device))
        shyld_devices.extend(clean_items(raw_shyld_devices, clean_shyld_device))

        doors.extend(clean_items(raw_doors, clean_door))
        doors.extend(clean_items(derived_doors, clean_door))

        windows.extend(clean_items(raw_windows, clean_window))
        windows.extend(clean_items(derived_windows, clean_window))

    return {
        "project": clean_project(project_data),
        "floors": dedupe_cleaned_items(floors),
        "rooms": dedupe_cleaned_items(rooms),
        "walls": dedupe_cleaned_items(walls),
        "objects": dedupe_cleaned_items(objects),
        "wall_items": dedupe_cleaned_items(wall_items),
        "shyld_devices": dedupe_cleaned_items(shyld_devices),
        "doors": dedupe_cleaned_items(doors),
        "windows": dedupe_cleaned_items(windows),
    }


def clean_items(
    items: Any,
    cleaner_function: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []

    cleaned = []

    for item in items:
        if isinstance(item, dict):
            cleaned_item = remove_empty_values(cleaner_function(item))
            if cleaned_item:
                cleaned.append(cleaned_item)

    return cleaned


def filter_items(
    items: Any,
    predicate: Callable[[Dict[str, Any]], bool],
) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []

    return [item for item in items if isinstance(item, dict) and predicate(item)]


def clean_project(project: Dict[str, Any]) -> Dict[str, Any]:
    return remove_empty_values(
        {
            "id": get_first_present(project, "id", "project_id", "projectId", "uuid"),
            "plan_id": get_first_present(project, "plan_id", "planId"),
            "name": get_first_present(project, "name", "title", "project_name"),
            "description": project.get("description"),
            "cloud_url": project.get("cloud_url"),
            "created_at": get_first_present(project, "user_created", "created_at", "createdAt"),
            "modified_at": get_first_present(project, "user_modified", "modified_at", "modifiedAt"),
            "address": project.get("address"),
        }
    )


def clean_floor(floor: Dict[str, Any]) -> Dict[str, Any]:
    return remove_empty_values(
        {
            "id": get_first_present(floor, "id", "uid", "uuid"),
            "uid": get_first_present(floor, "uid", "id", "uuid"),
            "name": get_first_present(floor, "name", "label", "title"),
            "level": get_first_present(floor, "level", "floor_number", "floorNumber"),
            "rooms": get_first_present(floor, "rooms", "room_ids", "roomIds"),
        }
    )


def clean_room(room: Dict[str, Any]) -> Dict[str, Any]:
    return remove_empty_values(
        {
            "id": get_first_present(room, "id", "uid", "uuid"),
            "uid": get_first_present(room, "uid", "id", "uuid"),
            "floor_id": get_first_present(room, "floor_id", "floorId", "floor_uid"),
            "name": get_first_present(room, "name", "label", "title"),
            "type": get_first_present(room, "type", "room_type", "roomType"),
            "area": get_first_present(room, "area", "surface", "square_feet", "squareFeet"),
            "perimeter": room.get("perimeter"),
            "height": room.get("height"),
            "walls": get_first_present(room, "walls", "wall_ids", "wallIds"),
            "objects": get_first_present(room, "objects", "object_ids", "objectIds"),
        }
    )


def clean_wall(wall: Dict[str, Any]) -> Dict[str, Any]:
    return remove_empty_values(
        {
            "id": get_first_present(wall, "id", "uid", "uuid"),
            "uid": get_first_present(wall, "uid", "id", "uuid"),
            "room_id": get_first_present(wall, "room_id", "roomId", "room_uid"),
            "start": get_first_present(wall, "start", "start_point", "startPoint"),
            "end": get_first_present(wall, "end", "end_point", "endPoint"),
            "length": wall.get("length"),
            "height": wall.get("height"),
            "thickness": wall.get("thickness"),
            "openings": get_first_present(wall, "openings", "opening_ids", "openingIds"),
        }
    )


def clean_object(obj: Dict[str, Any]) -> Dict[str, Any]:
    values = extract_values_map(obj.get("values"))

    return remove_empty_values(
        {
            "id": get_first_present(obj, "uid", "id", "uuid"),
            "uid": get_first_present(obj, "uid", "id", "uuid"),
            "room_id": get_first_present(obj, "room_id", "roomId", "room_uid"),
            "wall_uid": get_first_present(obj, "wall_uid", "wall_id", "wallId"),
            "symbol": get_symbol_info(obj),
            "name": get_first_present(obj, "name", "label", "title"),
            "type": get_first_present(obj, "type", "object_type", "objectType", "category"),
            "formatted": obj.get("formatted"),
            "position": get_first_present(obj, "position", "center", "coordinates"),
            "rotation": obj.get("rotation"),
            "size": obj.get("size"),
            "width": get_nested(obj, "size", "x") or obj.get("width"),
            "depth": get_nested(obj, "size", "y") or get_first_present(obj, "depth", "length"),
            "height": get_nested(obj, "size", "z") or obj.get("height"),
            "values": values,
        }
    )


def clean_wall_item(item: Dict[str, Any]) -> Dict[str, Any]:
    values = extract_values_map(item.get("values"))

    return remove_empty_values(
        {
            "id": get_first_present(item, "uid", "id", "uuid"),
            "uid": get_first_present(item, "uid", "id", "uuid"),
            "wall_uid": get_first_present(item, "wall_uid", "wall_id", "wallId"),
            "room_id": get_first_present(item, "room_id", "roomId", "room_uid"),
            "symbol": get_symbol_info(item),
            "formatted": item.get("formatted"),
            "position": item.get("position"),
            "rotation": item.get("rotation"),
            "size": item.get("size"),
            "width": get_nested(item, "size", "x") or item.get("width"),
            "depth": get_nested(item, "size", "y") or item.get("depth"),
            "height": get_nested(item, "size", "z") or item.get("height"),
            "values": values,
        }
    )


def clean_shyld_device(item: Dict[str, Any]) -> Dict[str, Any]:
    values = extract_values_map(item.get("values"))

    return remove_empty_values(
        {
            "id": get_first_present(item, "uid", "id", "uuid"),
            "uid": get_first_present(item, "uid", "id", "uuid"),
            "room_id": get_first_present(item, "room_id", "roomId", "room_uid"),
            "wall_uid": get_first_present(item, "wall_uid", "wall_id", "wallId"),
            "symbol": get_symbol_info(item),
            "formatted": item.get("formatted"),
            "position": item.get("position"),
            "rotation": item.get("rotation"),
            "size": item.get("size"),
            "width": get_nested(item, "size", "x") or item.get("width"),
            "depth": get_nested(item, "size", "y") or item.get("depth"),
            "height": get_nested(item, "size", "z") or item.get("height"),

            "serial_number": get_first_present(
            values,
            "Serial Number",
            "Serial Number*",
            "Shyld Device Serial Number",
            "shyld_device_serial_number",
            "serial_number",
            "serialNumber",
            "qcustomfield.bf63af5eq1",
        ) or find_serial_number(item),

            "values": values,
        }
    )

def find_serial_number(payload: Any) -> Optional[str]:
    """
    Recursively search a Magicplan object for the Shyld serial number.

    Handles raw Magicplan custom field objects like:
    {
      "id": "qcustomfield.bf63af5eq1",
      "label": "Serial Number*",
      "value": {
        "has_value": true,
        "value": "9889"
      }
    }
    """

    if isinstance(payload, dict):
        label = payload.get("label")
        field_id = payload.get("id")
        value_payload = payload.get("value")

        normalized_label = normalize_magicplan_label(str(label)) if label else ""

        is_serial_field = (
            normalized_label.lower() == "serial number"
            or str(field_id).startswith("qcustomfield.bf63af5e")
        )

        if is_serial_field:
            extracted_value = extract_magicplan_value(value_payload)
            if extracted_value not in (None, "", [], {}):
                return str(extracted_value)

        for value in payload.values():
            result = find_serial_number(value)
            if result:
                return result

    elif isinstance(payload, list):
        for item in payload:
            result = find_serial_number(item)
            if result:
                return result

    return None


def extract_magicplan_value(value_payload: Any) -> Optional[Any]:
    if isinstance(value_payload, dict):
        if value_payload.get("has_value"):
            return value_payload.get("value")
        return None

    return value_payload


def normalize_magicplan_label(label: str) -> str:
    return label.strip().rstrip("*").strip()


def clean_door(item: Dict[str, Any]) -> Dict[str, Any]:
    values = extract_values_map(item.get("values"))

    return remove_empty_values(
        {
            "id": get_first_present(item, "uid", "id", "uuid"),
            "uid": get_first_present(item, "uid", "id", "uuid"),
            "wall_uid": get_first_present(item, "wall_uid", "wall_id", "wallId"),
            "room_id": get_first_present(item, "room_id", "roomId", "room_uid"),
            "symbol": get_symbol_info(item),
            "formatted": item.get("formatted"),
            "position": item.get("position"),
            "rotation": item.get("rotation"),
            "size": item.get("size"),
            "width": get_nested(item, "size", "x") or item.get("width"),
            "depth": get_nested(item, "size", "y") or item.get("depth"),
            "height": get_nested(item, "size", "z") or item.get("height"),
            "swing": get_first_present(item, "swing", "door_swing", "doorSwing"),
            "values": values,
        }
    )


def clean_window(item: Dict[str, Any]) -> Dict[str, Any]:
    values = extract_values_map(item.get("values"))

    return remove_empty_values(
        {
            "id": get_first_present(item, "uid", "id", "uuid"),
            "uid": get_first_present(item, "uid", "id", "uuid"),
            "wall_uid": get_first_present(item, "wall_uid", "wall_id", "wallId"),
            "room_id": get_first_present(item, "room_id", "roomId", "room_uid"),
            "symbol": get_symbol_info(item),
            "formatted": item.get("formatted"),
            "position": item.get("position"),
            "rotation": item.get("rotation"),
            "size": item.get("size"),
            "width": get_nested(item, "size", "x") or item.get("width"),
            "depth": get_nested(item, "size", "y") or item.get("depth"),
            "height": get_nested(item, "size", "z") or item.get("height"),
            "values": values,
        }
    )


def get_symbol_info(item: Dict[str, Any]) -> Dict[str, Any]:
    symbol = item.get("symbol")

    if not isinstance(symbol, dict):
        return {}

    return remove_empty_values(
        {
            "id": symbol.get("id"),
            "name": symbol.get("name"),
            "description": symbol.get("description"),
        }
    )


def get_symbol_id(item: Dict[str, Any]) -> str:
    symbol = item.get("symbol")

    if isinstance(symbol, dict):
        value = symbol.get("id")
        return str(value).strip().lower() if value else ""

    return ""


def get_symbol_name(item: Dict[str, Any]) -> str:
    symbol = item.get("symbol")

    if isinstance(symbol, dict):
        value = symbol.get("name")
        return str(value).strip().lower() if value else ""

    name = item.get("name") or item.get("label") or item.get("title")
    return str(name).strip().lower() if name else ""


def is_shyld_device(item: Dict[str, Any]) -> bool:
    symbol_id = get_symbol_id(item)
    symbol_name = get_symbol_name(item)

    return (
        "shyld" in symbol_id
        or "shyld device" in symbol_name
        or symbol_name == "shyld device"
    )


def is_window_item(item: Dict[str, Any]) -> bool:
    symbol_id = get_symbol_id(item)
    symbol_name = get_symbol_name(item)

    return "window" in symbol_id or "window" in symbol_name


def is_door_item(item: Dict[str, Any]) -> bool:
    symbol_id = get_symbol_id(item)
    symbol_name = get_symbol_name(item)

    return "door" in symbol_id or "door" in symbol_name


def extract_values_map(values: Any) -> Dict[str, Any]:
    """
    Converts Magicplan's custom field list into a simpler dictionary.

    Handles labels like:
    - "Serial Number"
    - "Serial Number*"
    - "Shyld Device Serial Number"

    The asterisk means the field is required in Magicplan, so we strip it
    and store both the raw label and cleaned label when useful.
    """

    if isinstance(values, dict):
        return remove_empty_values(values)

    if not isinstance(values, list):
        return {}

    cleaned = {}

    for field in values:
        if not isinstance(field, dict):
            continue

        field_id = field.get("id")
        label = field.get("label")
        value_payload = field.get("value")

        actual_value = None

        if isinstance(value_payload, dict):
            if value_payload.get("has_value"):
                actual_value = value_payload.get("value")
        else:
            actual_value = value_payload

        if actual_value in (None, "", [], {}):
            continue

        keys_to_store = []

        if field_id:
            keys_to_store.append(str(field_id))

        if label:
            raw_label = str(label).strip()
            cleaned_label = normalize_magicplan_label(raw_label)

            keys_to_store.append(raw_label)

            if cleaned_label and cleaned_label != raw_label:
                keys_to_store.append(cleaned_label)

        for key in keys_to_store:
            cleaned[key] = actual_value

    return cleaned

def normalize_magicplan_label(label: str) -> str:
    """
    Magicplan required custom fields may end with '*'.
    Example: 'Serial Number*' should become 'Serial Number'.
    """

    return label.strip().rstrip("*").strip()


def get_nested(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    current: Any = data

    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)

    return current


def remove_empty_values(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if value not in (None, "", [], {})
    }


def dedupe_cleaned_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate by id/uid, but merge fields instead of dropping duplicates.

    This matters because the same Magicplan object may appear once without
    custom fields and once with custom fields. We want to keep the richer one.
    """

    merged_by_id: Dict[str, Dict[str, Any]] = {}
    fallback_items: List[Dict[str, Any]] = []

    for item in items:
        item_id = item.get("id") or item.get("uid")

        if not item_id:
            fallback_items.append(item)
            continue

        item_id = str(item_id)

        if item_id not in merged_by_id:
            merged_by_id[item_id] = item
        else:
            merged_by_id[item_id] = merge_dicts(merged_by_id[item_id], item)

    return list(merged_by_id.values()) + fallback_items

def merge_dicts(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge two dictionaries.

    Existing non-empty values are kept unless the incoming value is richer.
    Nested dictionaries are merged recursively.
    """

    merged = dict(base)

    for key, incoming_value in incoming.items():
        existing_value = merged.get(key)

        if incoming_value in (None, "", [], {}):
            continue

        if existing_value in (None, "", [], {}):
            merged[key] = incoming_value
            continue

        if isinstance(existing_value, dict) and isinstance(incoming_value, dict):
            merged[key] = merge_dicts(existing_value, incoming_value)

    return merged


def save_clean_export(
    project_id: str,
    cleaned_export: Dict[str, List[Dict[str, Any]] | Dict[str, Any]],
) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "_", project_id)

    output_folder = EXPORT_DIR / f"{safe_project_id}_{timestamp}"
    output_folder.mkdir(parents=True, exist_ok=True)

    for object_type, data in cleaned_export.items():
        output_path = output_folder / f"{object_type}.json"

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")

    return output_folder


def save_debug_bundle(project_id: str, project_bundle: Dict[str, Any]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "_", project_id)

    debug_folder = EXPORT_DIR / f"{safe_project_id}_{timestamp}_debug"
    debug_folder.mkdir(parents=True, exist_ok=True)

    output_path = debug_folder / "raw_project_bundle_debug.json"

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(project_bundle, file, indent=2, ensure_ascii=False)
        file.write("\n")

    return output_path


if __name__ == "__main__":
    main()