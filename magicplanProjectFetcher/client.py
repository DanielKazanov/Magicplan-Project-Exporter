from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv


SHYLD_SYMBOL_IDS = {"co-f4f96516-dd74-4e38-9886-628bdea5a281"}


class MagicplanConfigError(RuntimeError):
    pass


class MagicplanAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class MagicplanConfig:
    customer_id: str
    api_key: str
    base_url: str = "https://cloud.magicplan.app/api/v2"
    accept_language: str = "en-US"

    @classmethod
    def from_env(cls) -> "MagicplanConfig":
        load_dotenv()
        customer_id = os.getenv("MAGICPLAN_CUSTOMER_ID", "").strip()
        api_key = os.getenv("MAGICPLAN_API_KEY", "").strip()
        base_url = os.getenv("MAGICPLAN_BASE_URL", cls.base_url).strip().rstrip("/")
        accept_language = os.getenv("MAGICPLAN_ACCEPT_LANGUAGE", cls.accept_language).strip()

        missing = []
        if not customer_id:
            missing.append("MAGICPLAN_CUSTOMER_ID")
        if not api_key:
            missing.append("MAGICPLAN_API_KEY")
        if missing:
            raise MagicplanConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )

        return cls(customer_id, api_key, base_url, accept_language)


class MagicplanClient:
    def __init__(self, config: MagicplanConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "customer": config.customer_id,
                "key": config.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Accept-Language": config.accept_language,
            }
        )

    def _request(self, method: str, path: str) -> Any:
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        try:
            response = self.session.request(method, url, timeout=30)
        except requests.RequestException as exc:
            raise MagicplanAPIError(f"Request failed for {url}: {exc}") from exc

        if response.status_code >= 400:
            raise MagicplanAPIError(
                f"Magicplan API error {response.status_code} for {url}: {response.text[:1000]}"
            )
        if not response.text.strip():
            return {}
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise MagicplanAPIError(
                f"Magicplan returned non-JSON response for {url}: {response.text[:500]}"
            ) from exc

    def _request_optional(self, method: str, path: str) -> Optional[Any]:
        try:
            return self._request(method, path)
        except MagicplanAPIError:
            return None

    def test_workspace_connection(self) -> Any:
        return self._request("GET", "/workspace")

    def list_projects(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/projects")
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("projects", "data", "items", "results"):
                if isinstance(payload.get(key), list):
                    return [x for x in payload[key] if isinstance(x, dict)]
        raise MagicplanAPIError("Could not find a project list in the API response.")

    def get_project(self, project_id: str) -> Dict[str, Any]:
        errors = []
        for path in (f"/projects/{project_id}", f"/projects/get/{project_id}"):
            try:
                payload = self._request("GET", path)
                return payload if isinstance(payload, dict) else {"payload": payload}
            except MagicplanAPIError as exc:
                errors.append(str(exc))
        raise MagicplanAPIError("Could not fetch project detail. " + " | ".join(errors))

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        for path in (f"/plans/get/{plan_id}", f"/plans/{plan_id}"):
            payload = self._request_optional("GET", path)
            if isinstance(payload, dict):
                return payload
        return None

    def get_plan_statistics(self, plan_id: str) -> Optional[Dict[str, Any]]:
        payload = self._request_optional("GET", f"/plans/statistics/{plan_id}")
        return payload if isinstance(payload, dict) else None

    def get_plan_forms(self, plan_id: str) -> Optional[Dict[str, Any]]:
        payload = self._request_optional("GET", f"/plans/forms/{plan_id}")
        return payload if isinstance(payload, dict) else None

    def get_project_bundle(self, project_id: str) -> Dict[str, Any]:
        project = self.get_project(project_id)
        bundle: Dict[str, Any] = {"project_id": project_id, "project": project, "plans": [], "warnings": []}
        plan_ids = extract_plan_ids(project)

        if not plan_ids:
            bundle["warnings"].append("No plan IDs were found in the project payload.")

        for plan_id in plan_ids:
            raw_plan = self.get_plan(plan_id)
            statistics = self.get_plan_statistics(plan_id)
            forms = self.get_plan_forms(plan_id)

            if raw_plan is None and statistics is None and forms is None:
                bundle["warnings"].append(f"Could not fetch data for plan ID: {plan_id}")
                continue

            summary = merge_summaries(summarize_payload(statistics or {}), summarize_payload(raw_plan or {}))
            summary = merge_summaries(summary, summarize_payload(forms or {}))

            bundle["plans"].append(
                {
                    "plan_id": plan_id,
                    "raw_plan": raw_plan,
                    "statistics": statistics,
                    "forms": forms,
                    "extracted_summary": summary,
                }
            )
        return bundle


def extract_plan_ids(project: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    containers = [project]
    if isinstance(project.get("data"), dict):
        containers.append(project["data"])

    for container in containers:
        for key in ("plan_id", "planId", "floorplan_id", "floorplanId"):
            if container.get(key):
                ids.append(str(container[key]))
        for key in ("plans", "floorplans", "floor_plans", "project_plans"):
            value = container.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        ids.append(item)
                    elif isinstance(item, dict):
                        item_id = first(item, "id", "uid", "uuid", "plan_id", "planId")
                        if item_id:
                            ids.append(str(item_id))
            elif isinstance(value, dict):
                item_id = first(value, "id", "uid", "uuid", "plan_id", "planId")
                if item_id:
                    ids.append(str(item_id))
    return list(dict.fromkeys(ids))


def summarize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    entities = extract_entities(payload)
    serial_records = extract_serial_records(payload)

    for key in ("objects", "items", "wall_items"):
        apply_serial_records(entities[key], serial_records)

    objects = dedupe(entities["objects"] + entities["items"])
    wall_items = dedupe(entities["wall_items"])
    placed = dedupe(objects + wall_items)

    shyld_devices = dedupe([x for x in placed if is_shyld(x)])
    apply_serial_records(shyld_devices, serial_records)

    summary = {
        "floors": dedupe(entities["floors"]),
        "rooms": dedupe(entities["rooms"]),
        "walls": dedupe(entities["walls"]),
        "wall_items": wall_items,
        "objects": objects,
        "doors": dedupe(entities["doors"] + [x for x in placed if is_door(x)]),
        "windows": dedupe(entities["windows"] + [x for x in placed if is_window(x)]),
        "outlets": dedupe(entities["outlets"] + [x for x in placed if is_outlet(x)]),
        "shyld_devices": shyld_devices,
        "serial_records": serial_records,
    }
    add_counts(summary)
    return summary


def extract_entities(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {
        "floors": [], "rooms": [], "walls": [], "wall_items": [], "objects": [], "items": [],
        "doors": [], "windows": [], "outlets": [],
    }
    key_map = {
        "floors": "floors", "floor": "floors", "levels": "floors", "level": "floors",
        "rooms": "rooms", "room": "rooms", "spaces": "rooms", "space": "rooms",
        "walls": "walls", "wall": "walls",
        "wall_items": "wall_items", "wallItems": "wall_items", "wallitems": "wall_items", "openings": "wall_items",
        "objects": "objects", "object": "objects", "items": "items", "item": "items", "assets": "items",
        "doors": "doors", "door": "doors", "windows": "windows", "window": "windows", "outlets": "outlets", "outlet": "outlets",
    }

    def walk(node: Any, ctx: Dict[str, str], entity: Optional[str] = None) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, ctx, entity)
            return
        if not isinstance(node, dict):
            return

        local = dict(ctx)
        if entity:
            item = add_context(node, entity, local)
            out[entity].append(item)
            update_context(item, entity, local)
        elif looks_placed(node):
            item = add_context(node, "objects", local)
            out["objects"].append(item)
            update_context(item, "objects", local)

        for key, value in node.items():
            walk(value, local, key_map.get(key))

    walk(payload, {})
    return out


def add_context(item: Dict[str, Any], entity: str, ctx: Dict[str, str]) -> Dict[str, Any]:
    item = dict(item)
    if entity in {"rooms", "walls", "wall_items", "objects", "items", "doors", "windows", "outlets"}:
        if ctx.get("floor_id") and not first(item, "floor_id", "floorId", "floor_uid", "floorUid"):
            item["floor_id"] = ctx["floor_id"]
    if entity in {"walls", "wall_items", "objects", "items", "doors", "windows", "outlets"}:
        if ctx.get("room_id") and not first(item, "room_id", "roomId", "room_uid", "roomUid"):
            item["room_id"] = ctx["room_id"]
    if entity in {"wall_items", "objects", "items", "doors", "windows", "outlets"}:
        if ctx.get("wall_uid") and not first(item, "wall_uid", "wall_id", "wallId", "wallUid"):
            item["wall_uid"] = ctx["wall_uid"]
    return item


def update_context(item: Dict[str, Any], entity: str, ctx: Dict[str, str]) -> None:
    item_id = item_id_of(item)
    if entity == "floors" and item_id:
        ctx["floor_id"] = item_id
    elif entity == "rooms" and item_id:
        ctx["room_id"] = item_id
        if first(item, "floor_id", "floorId", "floor_uid", "floorUid"):
            ctx["floor_id"] = str(first(item, "floor_id", "floorId", "floor_uid", "floorUid"))
    elif entity == "walls" and item_id:
        ctx["wall_uid"] = item_id
        if first(item, "room_id", "roomId", "room_uid", "roomUid"):
            ctx["room_id"] = str(first(item, "room_id", "roomId", "room_uid", "roomUid"))
    elif entity in {"wall_items", "objects", "items", "doors", "windows", "outlets"} and item_id:
        ctx["object_id"] = item_id


def extract_serial_records(payload: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []

    def walk(node: Any, ctx: Dict[str, str]) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, ctx)
            return
        if not isinstance(node, dict):
            return

        local = dict(ctx)
        ref = object_ref_id(node)
        if ref:
            local["object_id"] = ref
        room_id = first(node, "room_id", "roomId", "room_uid", "roomUid")
        if room_id:
            local["room_id"] = str(room_id)

        serial = serial_from_node(node)
        if serial:
            records.append(clean({"object_id": local.get("object_id"), "room_id": local.get("room_id"), "serial_number": str(serial)}))

        for value in node.values():
            walk(value, local)

    walk(payload, {})
    return dedupe(records)


def object_ref_id(item: Dict[str, Any]) -> Optional[str]:
    value = first(
        item, "object_uid", "objectUid", "object_id", "objectId", "placed_object_uid", "placedObjectUid",
        "placed_object_id", "placedObjectId", "item_uid", "itemUid", "item_id", "itemId",
        "entity_uid", "entityUid", "entity_id", "entityId", "target_uid", "targetUid", "target_id", "targetId",
    )
    if value:
        return str(value)
    if looks_placed(item):
        value = first(item, "uid", "uuid", "id")
        if value and not str(value).startswith("qcustomfield."):
            return str(value)
    return None


def serial_from_node(item: Dict[str, Any]) -> Optional[str]:
    label = normalize(str(first(item, "label", "name", "title") or ""))
    field_id = str(first(item, "id", "uid", "field_id", "fieldId") or "")
    if is_serial_label(label, field_id):
        value = magic_value(item.get("value") if "value" in item else item.get("values"))
        if value not in (None, "", [], {}):
            return str(value)

    values = values_map(item)
    serial = first(
        values, "Serial Number", "Serial Number*", "Shyld Device Serial Number",
        "shyld_device_serial_number", "serial_number", "serialNumber",
        "qcustomfield.bf63af5eq1", "qcustomfield.bf63af5e",
    )
    return str(serial) if serial not in (None, "", [], {}) else None


def apply_serial_records(items: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> None:
    by_id = {str(r["object_id"]): str(r["serial_number"]) for r in records if r.get("object_id") and r.get("serial_number")}
    for item in items:
        item_id = item_id_of(item)
        if item_id and item_id in by_id:
            item["serial_number"] = by_id[item_id]
            vals = item.get("values") if isinstance(item.get("values"), dict) else {}
            vals["Serial Number"] = by_id[item_id]
            vals["Shyld Device Serial Number"] = by_id[item_id]
            item["values"] = vals


def merge_summaries(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    keys = ["floors", "rooms", "walls", "wall_items", "objects", "doors", "windows", "outlets", "shyld_devices"]
    merged = {key: merge_lists(primary.get(key, []), secondary.get(key, [])) for key in keys}
    merged["serial_records"] = dedupe((primary.get("serial_records") or []) + (secondary.get("serial_records") or []))

    for key in ("objects", "wall_items", "shyld_devices"):
        apply_serial_records(merged[key], merged["serial_records"])

    placed = dedupe(merged["objects"] + merged["wall_items"] + merged["shyld_devices"])
    merged["shyld_devices"] = dedupe([x for x in placed if is_shyld(x)])
    merged["doors"] = dedupe(merged["doors"] + [x for x in placed if is_door(x)])
    merged["windows"] = dedupe(merged["windows"] + [x for x in placed if is_window(x)])
    merged["outlets"] = dedupe(merged["outlets"] + [x for x in placed if is_outlet(x)])
    add_counts(merged)
    return merged


def merge_lists(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    for item in list(a or []) + list(b or []):
        if not isinstance(item, dict):
            continue
        item_id = item_id_of(item)
        if not item_id:
            no_id.append(item)
        elif item_id not in by_id:
            by_id[item_id] = dict(item)
        else:
            by_id[item_id] = deep_merge(by_id[item_id], item)
    return list(by_id.values()) + no_id


def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return merge_lists(items, [])


def deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if out.get(key) in (None, "", [], {}):
            out[key] = value
        elif isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        elif isinstance(out.get(key), list) and isinstance(value, list):
            seen = {json.dumps(x, sort_keys=True, default=str) for x in out[key]}
            for x in value:
                marker = json.dumps(x, sort_keys=True, default=str)
                if marker not in seen:
                    seen.add(marker)
                    out[key].append(x)
    return out


def first(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, "", [], {}):
            return data[key]
    return None


def item_id_of(item: Dict[str, Any]) -> Optional[str]:
    value = first(item, "uid", "id", "uuid", "object_uid", "objectUid", "object_id", "objectId", "item_uid", "itemUid", "item_id", "itemId")
    return str(value) if value is not None else None


def symbol_id(item: Dict[str, Any]) -> str:
    symbol = item.get("symbol")
    if isinstance(symbol, dict):
        value = first(symbol, "id", "uid", "uuid", "symbol_id", "symbolId")
        if value:
            return str(value).strip().lower()
    if isinstance(symbol, str):
        return symbol.strip().lower()
    value = first(item, "symbol_id", "symbolId", "object_symbol_id", "objectSymbolId", "catalog_id", "catalogId", "custom_object_id", "customObjectId")
    return str(value).strip().lower() if value else ""


def symbol_name(item: Dict[str, Any]) -> str:
    symbol = item.get("symbol")
    if isinstance(symbol, dict):
        value = first(symbol, "name", "label", "title", "description")
        if value:
            return str(value).strip().lower()
    value = first(item, "name", "label", "title", "object_name", "objectName", "symbol_name", "symbolName")
    return str(value).strip().lower() if value else ""


def text_of(item: Dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, default=str).lower()


def looks_placed(item: Dict[str, Any]) -> bool:
    has_symbol = "symbol" in item or first(item, "symbol_id", "symbolId", "object_symbol_id", "objectSymbolId", "catalog_id", "catalogId", "custom_object_id", "customObjectId") is not None
    has_geometry = any(k in item for k in ("position", "center", "coordinates", "size", "formatted", "rotation", "width", "height", "depth"))
    has_id = first(item, "uid", "uuid", "object_uid", "objectUid", "object_id", "objectId", "item_uid", "itemUid", "item_id", "itemId") is not None
    if has_symbol and (has_geometry or has_id):
        return True
    return "shyld" in text_of(item) and has_id and not str(first(item, "id", "uid", "uuid") or "").startswith("qcustomfield.")


def is_shyld(item: Dict[str, Any]) -> bool:
    sid, name, text = symbol_id(item), symbol_name(item), text_of(item)
    return sid in SHYLD_SYMBOL_IDS or any(x in text for x in SHYLD_SYMBOL_IDS) or "shyld" in sid or "shyld device" in name or "shyld" in text


def is_window(item: Dict[str, Any]) -> bool:
    return "window" in symbol_id(item) or "window" in symbol_name(item) or "window" in text_of(item)


def is_door(item: Dict[str, Any]) -> bool:
    return "door" in symbol_id(item) or "door" in symbol_name(item) or "door" in text_of(item)


def is_outlet(item: Dict[str, Any]) -> bool:
    text = f"{symbol_id(item)} {symbol_name(item)} {text_of(item)}"
    return any(word in text for word in ("outlet", "socket", "receptacle"))


def values_map(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("values", "fields", "custom_fields", "customFields", "properties", "answers", "form_values", "formValues", "data"):
        part = extract_values(item.get(key))
        if part:
            out = deep_merge(out, part)
    return clean(out)


def extract_values(values: Any) -> Dict[str, Any]:
    if isinstance(values, dict):
        out = {}
        for key, value in values.items():
            actual = magic_value(value)
            if actual not in (None, "", [], {}):
                out[str(key)] = actual
                norm = normalize(str(key))
                if norm and norm != str(key):
                    out[norm] = actual
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


def magic_value(value: Any) -> Optional[Any]:
    if isinstance(value, dict):
        if value.get("has_value") and value.get("value") not in (None, "", [], {}):
            return value.get("value")
        for key in ("value", "text", "number", "name", "label", "display_value", "displayValue"):
            if value.get(key) not in (None, "", [], {}):
                return value.get(key)
        return None
    return value


def normalize(label: str) -> str:
    return label.strip().rstrip("*").strip()


def is_serial_label(label: str, field_id: str = "") -> bool:
    label = normalize(label).lower()
    field_id = field_id.lower()
    return label == "serial number" or label == "shyld device serial number" or "serial number" in label or field_id.startswith("qcustomfield.bf63af5e")


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [clean(x) for x in value if x not in (None, "", [], {})]
    return value


def add_counts(summary: Dict[str, Any]) -> None:
    for name in ("floor", "room", "wall", "wall_item", "object", "door", "window", "outlet", "shyld_device"):
        key = f"{name}s" if name != "shyld_device" else "shyld_devices"
        if name == "wall_item":
            key = "wall_items"
        summary[f"{name}_count"] = len(summary.get(key, []))