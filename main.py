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
SHYLD_SYMBOL_IDS = {"co-f4f96516-dd74-4e38-9886-628bdea5a281"}


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
    name = first(project, "name", "title", "project_name", "label") or nested(project, "data", "name") or "Untitled project"
    updated = first(project, "updated_at", "updatedAt", "modified_at", "modifiedAt") or nested(project, "data", "user_modified")
    address = extract_address(project)
    parts = [f"{index}. {name}", f"id={project_id}"]
    if updated:
        parts.append(f"updated={updated}")
    if address:
        parts.append(f"address={address}")
    return " | ".join(parts)


def get_project_id(project: Dict[str, Any]) -> Optional[str]:
    value = first(project, "id", "project_id", "projectId", "uuid")
    if value is not None:
        return str(value)
    data = project.get("data")
    if isinstance(data, dict):
        value = first(data, "id", "project_id", "projectId", "uuid")
        if value is not None:
            return str(value)
    return None


def extract_address(project: Dict[str, Any]) -> Optional[str]:
    address = project.get("address")
    if not address and isinstance(project.get("data"), dict):
        address = project["data"].get("address")
    if isinstance(address, str):
        return address
    if isinstance(address, dict):
        parts = [address.get("street"), address.get("city"), address.get("state"), address.get("postal_code"), address.get("zip"), address.get("country")]
        return ", ".join(str(part) for part in parts if part)
    return None


def build_clean_export(project_bundle: Dict[str, Any]) -> Dict[str, Any]:
    project_payload = project_bundle.get("project", {})
    project_data = project_payload.get("data", project_payload)
    all_floors: List[Dict[str, Any]] = []

    for plan in project_bundle.get("plans", []):
        plan_id = str(plan.get("plan_id") or "unknown_plan")
        summary = plan.get("extracted_summary", {})

        floors = dedupe(clean_items(summary.get("floors"), clean_floor))
        rooms = dedupe(clean_items(summary.get("rooms"), clean_room))
        walls = dedupe(clean_items(summary.get("walls"), clean_wall))

        raw_objects = (
            list_or_empty(summary.get("objects"))
            + list_or_empty(summary.get("wall_items"))
            + list_or_empty(summary.get("doors"))
            + list_or_empty(summary.get("windows"))
            + list_or_empty(summary.get("outlets"))
            + list_or_empty(summary.get("shyld_devices"))
        )
        objects = dedupe(clean_items(merge_raw_by_id(raw_objects), clean_room_object))

        if not floors and rooms:
            floors = [{"id": f"{plan_id}_floor", "uid": f"{plan_id}_floor", "name": "Floor"}]

        wall_to_room_id = build_wall_to_room_id(walls)
        assigned_room_ids: set[str] = set()
        nested_floors: List[Dict[str, Any]] = []

        for floor in floors:
            floor_export = dict(floor)
            floor_export["plan_id"] = plan_id
            floor_rooms = get_rooms_for_floor(floor, rooms, single_floor=len(floors) == 1)
            room_exports = []

            for room in floor_rooms:
                room_id = item_id(room)
                if room_id:
                    assigned_room_ids.add(room_id)
                room_exports.append(build_room_export(room, walls, objects, wall_to_room_id))

            floor_export["rooms"] = room_exports
            nested_floors.append(clean(floor_export))

        unassigned_rooms = [room for room in rooms if (item_id(room) or "") not in assigned_room_ids]
        if unassigned_rooms:
            if not nested_floors:
                nested_floors.append({"id": f"{plan_id}_unassigned_floor", "uid": f"{plan_id}_unassigned_floor", "name": "Unassigned Floor", "plan_id": plan_id, "rooms": []})
            for room in unassigned_rooms:
                nested_floors[0].setdefault("rooms", []).append(build_room_export(room, walls, objects, wall_to_room_id))

        all_floors.extend(nested_floors)

    return {"project": clean_project(project_data), "floors": dedupe_floors(all_floors)}


def build_room_export(room: Dict[str, Any], walls: List[Dict[str, Any]], objects: List[Dict[str, Any]], wall_to_room_id: Dict[str, str]) -> Dict[str, Any]:
    room_export = dict(room)
    room_id = item_id(room)
    if room_id:
        room_export["id"] = room_id
        room_export["uid"] = room_export.get("uid") or room_id
    room_export["walls"] = get_walls_for_room(room, walls)
    room_export["objects"] = get_objects_for_room(room, objects, wall_to_room_id)
    return clean(room_export)


def get_rooms_for_floor(floor: Dict[str, Any], rooms: List[Dict[str, Any]], single_floor: bool) -> List[Dict[str, Any]]:
    floor_id = item_id(floor)
    explicit_room_ids = relationship_ids(floor.get("rooms"))
    matches = []
    for room in rooms:
        room_id = item_id(room)
        room_floor_id = first(room, "floor_id", "floorId", "floor_uid", "floorUid")
        if room_id and room_id in explicit_room_ids:
            matches.append(room)
        elif floor_id and room_floor_id and str(room_floor_id) == floor_id:
            matches.append(room)
    return rooms if not matches and single_floor else matches


def get_walls_for_room(room: Dict[str, Any], walls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    room_id = item_id(room)
    explicit_wall_ids = relationship_ids(room.get("walls"))
    matches = []
    for wall in walls:
        wall_id = item_id(wall)
        wall_room_id = first(wall, "room_id", "roomId", "room_uid", "roomUid")
        if wall_id and wall_id in explicit_wall_ids:
            matches.append(wall)
        elif room_id and wall_room_id and str(wall_room_id) == room_id:
            matches.append(wall)
    return dedupe(matches)


def get_objects_for_room(room: Dict[str, Any], objects: List[Dict[str, Any]], wall_to_room_id: Dict[str, str]) -> List[Dict[str, Any]]:
    room_id = item_id(room)
    explicit_object_ids = relationship_ids(room.get("objects"))
    matches = []
    for obj in objects:
        obj_id = item_id(obj)
        obj_room_id = object_room_id(obj, wall_to_room_id)
        if obj_id and obj_id in explicit_object_ids:
            matches.append(obj)
        elif room_id and obj_room_id and obj_room_id == room_id:
            matches.append(obj)
    return dedupe(matches)


def object_room_id(obj: Dict[str, Any], wall_to_room_id: Dict[str, str]) -> Optional[str]:
    direct = first(obj, "room_id", "roomId", "room_uid", "roomUid")
    if direct:
        return str(direct)
    wall_uid = first(obj, "wall_uid", "wall_id", "wallId", "wallUid")
    return wall_to_room_id.get(str(wall_uid)) if wall_uid else None


def build_wall_to_room_id(walls: List[Dict[str, Any]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for wall in walls:
        room_id = first(wall, "room_id", "roomId", "room_uid", "roomUid")
        if not room_id:
            continue
        for key in ("id", "uid", "uuid"):
            if wall.get(key):
                mapping[str(wall[key])] = str(room_id)
    return mapping


def clean_room_object(obj: Dict[str, Any]) -> Dict[str, Any]:
    if is_shyld(obj):
        cleaned = clean_shyld_device(obj)
        cleaned["object_category"] = "shyld_device"
        return clean(cleaned)
    if is_door(obj):
        cleaned = clean_door_or_window(obj)
        cleaned["object_category"] = "door"
        return clean(cleaned)
    if is_window(obj):
        cleaned = clean_door_or_window(obj)
        cleaned["object_category"] = "window"
        return clean(cleaned)
    if is_outlet(obj):
        cleaned = clean_generic_object(obj)
        cleaned["object_category"] = "outlet"
        return clean(cleaned)
    cleaned = clean_generic_object(obj)
    cleaned["object_category"] = "object"
    return clean(cleaned)


def clean_project(project: Dict[str, Any]) -> Dict[str, Any]:
    return clean({
        "id": first(project, "id", "project_id", "projectId", "uuid"),
        "plan_id": first(project, "plan_id", "planId"),
        "name": first(project, "name", "title", "project_name"),
        "description": project.get("description"),
        "cloud_url": project.get("cloud_url"),
        "created_at": first(project, "user_created", "created_at", "createdAt"),
        "modified_at": first(project, "user_modified", "modified_at", "modifiedAt"),
        "address": project.get("address"),
    })


def clean_floor(floor: Dict[str, Any]) -> Dict[str, Any]:
    return clean({
        "id": first(floor, "id", "uid", "uuid"),
        "uid": first(floor, "uid", "id", "uuid"),
        "name": first(floor, "name", "label", "title"),
        "level": first(floor, "level", "floor_number", "floorNumber"),
        "rooms": first(floor, "rooms", "room_ids", "roomIds"),
    })


def clean_room(room: Dict[str, Any]) -> Dict[str, Any]:
    return clean({
        "id": first(room, "id", "uid", "uuid"),
        "uid": first(room, "uid", "id", "uuid"),
        "floor_id": first(room, "floor_id", "floorId", "floor_uid", "floorUid"),
        "name": first(room, "name", "label", "title"),
        "type": first(room, "type", "room_type", "roomType"),
        "area": first(room, "area", "surface", "square_feet", "squareFeet"),
        "perimeter": room.get("perimeter"),
        "height": room.get("height"),
        "walls": first(room, "walls", "wall_ids", "wallIds"),
        "objects": first(room, "objects", "object_ids", "objectIds"),
    })


def clean_wall(wall: Dict[str, Any]) -> Dict[str, Any]:
    return clean({
        "id": first(wall, "id", "uid", "uuid"),
        "uid": first(wall, "uid", "id", "uuid"),
        "room_id": first(wall, "room_id", "roomId", "room_uid", "roomUid"),
        "start": first(wall, "start", "start_point", "startPoint"),
        "end": first(wall, "end", "end_point", "endPoint"),
        "length": wall.get("length"),
        "height": wall.get("height"),
        "thickness": wall.get("thickness"),
        "openings": first(wall, "openings", "opening_ids", "openingIds"),
    })


def clean_generic_object(obj: Dict[str, Any]) -> Dict[str, Any]:
    values = values_map(obj)
    return clean({
        "id": first(obj, "uid", "id", "uuid", "object_uid", "objectUid", "object_id", "objectId", "item_uid", "itemUid", "item_id", "itemId"),
        "uid": first(obj, "uid", "id", "uuid", "object_uid", "objectUid", "object_id", "objectId", "item_uid", "itemUid", "item_id", "itemId"),
        "room_id": first(obj, "room_id", "roomId", "room_uid", "roomUid"),
        "wall_uid": first(obj, "wall_uid", "wall_id", "wallId", "wallUid"),
        "symbol": symbol_info(obj),
        "name": first(obj, "name", "label", "title"),
        "type": first(obj, "type", "object_type", "objectType", "category"),
        "formatted": obj.get("formatted"),
        "position": first(obj, "position", "center", "coordinates"),
        "rotation": obj.get("rotation"),
        "size": obj.get("size"),
        "width": nested(obj, "size", "x") or obj.get("width"),
        "depth": nested(obj, "size", "y") or first(obj, "depth", "length"),
        "height": nested(obj, "size", "z") or obj.get("height"),
        "values": values,
    })


def clean_shyld_device(obj: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = clean_generic_object(obj)
    serial = obj.get("serial_number") or first(
        cleaned.get("values", {}),
        "Serial Number", "Serial Number*", "Shyld Device Serial Number", "serial_number", "serialNumber", "qcustomfield.bf63af5eq1", "qcustomfield.bf63af5e",
    ) or find_serial(obj)
    if serial not in (None, "", [], {}):
        cleaned["serial_number"] = str(serial)
    return clean(cleaned)


def clean_door_or_window(obj: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = clean_generic_object(obj)
    swing = first(obj, "swing", "door_swing", "doorSwing")
    if swing:
        cleaned["swing"] = swing
    return clean(cleaned)


def values_map(obj: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("values", "fields", "custom_fields", "customFields", "properties", "answers", "form_values", "formValues", "data"):
        part = extract_values(obj.get(key))
        if part:
            out = merge_dicts(out, part)
    return clean(out)


def extract_values(values: Any) -> Dict[str, Any]:
    if isinstance(values, dict):
        out = {}
        for key, value in values.items():
            actual = magic_value(value)
            if actual not in (None, "", [], {}):
                out[str(key)] = actual
                normalized = normalize(str(key))
                if normalized and normalized != str(key):
                    out[normalized] = actual
        return clean(out)

    if isinstance(values, list):
        out = {}
        for field in values:
            if not isinstance(field, dict):
                continue
            actual = magic_value(field.get("value") if "value" in field else field.get("values"))
            if actual in (None, "", [], {}):
                continue
            for key in (field.get("id"), field.get("uid"), field.get("label"), field.get("name"), field.get("title")):
                if key:
                    out[str(key).strip()] = actual
                    out[normalize(str(key))] = actual
        return clean(out)

    return {}


def find_serial(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        label = normalize(str(first(payload, "label", "name", "title") or ""))
        field_id = str(first(payload, "id", "uid", "field_id", "fieldId") or "")
        if is_serial_label(label, field_id):
            value = magic_value(payload.get("value") if "value" in payload else payload.get("values"))
            if value not in (None, "", [], {}):
                return str(value)
        for value in payload.values():
            result = find_serial(value)
            if result:
                return result
    elif isinstance(payload, list):
        for item in payload:
            result = find_serial(item)
            if result:
                return result
    return None


def magic_value(value: Any) -> Optional[Any]:
    if isinstance(value, dict):
        if value.get("has_value") and value.get("value") not in (None, "", [], {}):
            return value.get("value")
        for key in ("value", "text", "number", "name", "label", "display_value", "displayValue"):
            if value.get(key) not in (None, "", [], {}):
                return value.get(key)
        return None
    return value


def symbol_info(obj: Dict[str, Any]) -> Dict[str, Any]:
    symbol = obj.get("symbol")
    if isinstance(symbol, dict):
        return clean({"id": first(symbol, "id", "uid", "uuid", "symbol_id", "symbolId"), "name": first(symbol, "name", "label", "title")})
    if isinstance(symbol, str):
        return {"id": symbol}
    return clean({"id": first(obj, "symbol_id", "symbolId", "object_symbol_id", "objectSymbolId", "catalog_id", "catalogId", "custom_object_id", "customObjectId")})


def is_shyld(obj: Dict[str, Any]) -> bool:
    sid, name, text = symbol_id(obj), symbol_name(obj), json.dumps(obj, ensure_ascii=False, default=str).lower()
    return sid in SHYLD_SYMBOL_IDS or any(x in text for x in SHYLD_SYMBOL_IDS) or "shyld" in sid or "shyld device" in name or "shyld" in text


def is_door(obj: Dict[str, Any]) -> bool:
    text = f"{symbol_id(obj)} {symbol_name(obj)} {json.dumps(obj, default=str).lower()}"
    return "door" in text


def is_window(obj: Dict[str, Any]) -> bool:
    text = f"{symbol_id(obj)} {symbol_name(obj)} {json.dumps(obj, default=str).lower()}"
    return "window" in text


def is_outlet(obj: Dict[str, Any]) -> bool:
    text = f"{symbol_id(obj)} {symbol_name(obj)} {json.dumps(obj, default=str).lower()}"
    return any(word in text for word in ("outlet", "socket", "receptacle"))


def symbol_id(obj: Dict[str, Any]) -> str:
    symbol = obj.get("symbol")
    if isinstance(symbol, dict):
        value = first(symbol, "id", "uid", "uuid", "symbol_id", "symbolId")
        if value:
            return str(value).strip().lower()
    if isinstance(symbol, str):
        return symbol.strip().lower()
    value = first(obj, "symbol_id", "symbolId", "object_symbol_id", "objectSymbolId", "catalog_id", "catalogId", "custom_object_id", "customObjectId")
    return str(value).strip().lower() if value else ""


def symbol_name(obj: Dict[str, Any]) -> str:
    symbol = obj.get("symbol")
    if isinstance(symbol, dict):
        value = first(symbol, "name", "label", "title", "description")
        if value:
            return str(value).strip().lower()
    value = first(obj, "name", "label", "title", "object_name", "objectName", "symbol_name", "symbolName")
    return str(value).strip().lower() if value else ""


def clean_items(items: Any, cleaner: Callable[[Dict[str, Any]], Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [clean(cleaner(item)) for item in items if isinstance(item, dict) and clean(cleaner(item))]


def list_or_empty(value: Any) -> List[Dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def merge_raw_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    for item in items:
        obj_id = item_id(item)
        if not obj_id:
            no_id.append(item)
        elif obj_id not in by_id:
            by_id[obj_id] = dict(item)
        else:
            by_id[obj_id] = merge_dicts(by_id[obj_id], item)
    return list(by_id.values()) + no_id


def merge_dicts(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if out.get(key) in (None, "", [], {}):
            out[key] = value
        elif isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = merge_dicts(out[key], value)
        elif isinstance(out.get(key), list) and isinstance(value, list):
            seen = {json.dumps(x, sort_keys=True, default=str) for x in out[key]}
            for x in value:
                marker = json.dumps(x, sort_keys=True, default=str)
                if marker not in seen:
                    seen.add(marker)
                    out[key].append(x)
    return out


def relationship_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, list):
        for item in value:
            ids.update(relationship_ids(item))
    elif isinstance(value, dict):
        possible_id = item_id(value)
        if possible_id:
            ids.add(possible_id)
    elif value not in (None, "", [], {}):
        ids.add(str(value))
    return ids


def item_id(item: Dict[str, Any]) -> Optional[str]:
    value = first(item, "id", "uid", "uuid", "object_uid", "objectUid", "object_id", "objectId", "item_uid", "itemUid", "item_id", "itemId")
    return str(value) if value is not None else None


def first(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, "", [], {}):
            return data[key]
    return None


def nested(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if current not in (None, "", [], {}) else None


def normalize(label: str) -> str:
    return label.strip().rstrip("*").strip()


def is_serial_label(label: str, field_id: str = "") -> bool:
    label = normalize(label).lower()
    field_id = field_id.lower()
    return label == "serial number" or label == "shyld device serial number" or "serial number" in label or field_id.startswith("qcustomfield.bf63af5e")


def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return merge_raw_by_id(items)


def dedupe_floors(floors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    for floor in floors:
        floor_id = item_id(floor)
        if not floor_id:
            no_id.append(floor)
            continue
        if floor_id not in by_id:
            by_id[floor_id] = dict(floor)
        else:
            existing_rooms = by_id[floor_id].get("rooms", [])
            incoming_rooms = floor.get("rooms", [])
            by_id[floor_id] = merge_dicts(by_id[floor_id], floor)
            by_id[floor_id]["rooms"] = dedupe(list_or_empty(existing_rooms) + list_or_empty(incoming_rooms))
    return list(by_id.values()) + no_id


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean(inner) for key, inner in value.items() if inner not in (None, "", [], {})}
    if isinstance(value, list):
        return [clean(item) for item in value if item not in (None, "", [], {})]
    return value


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")
    return cleaned or "magicplan_project"


def save_clean_export(project_id: str, cleaned_export: Dict[str, Any]) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_folder = EXPORT_DIR / f"{safe_filename(project_id)}_{timestamp}"
    output_folder.mkdir(parents=True, exist_ok=True)

    files = {
        "project.json": cleaned_export.get("project", {}),
        "floors.json": cleaned_export.get("floors", []),
        "full_export.json": cleaned_export,
    }

    for filename, payload in files.items():
        with (output_folder / filename).open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)

    return output_folder


if __name__ == "__main__":
    main()