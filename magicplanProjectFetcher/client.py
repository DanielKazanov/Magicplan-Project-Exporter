from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv


class MagicplanConfigError(RuntimeError):
    """Raised when required Magicplan configuration is missing."""


class MagicplanAPIError(RuntimeError):
    """Raised when the Magicplan API returns an unsuccessful response."""


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

        return cls(
            customer_id=customer_id,
            api_key=api_key,
            base_url=base_url,
            accept_language=accept_language,
        )


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

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.config.base_url}/{path.lstrip('/')}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=30,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise MagicplanAPIError(f"Request failed for {url}: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:1000]
            raise MagicplanAPIError(
                f"Magicplan API error {response.status_code} for {url}: {body}"
            )

        if not response.text.strip():
            return {}

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise MagicplanAPIError(
                f"Magicplan returned non-JSON response for {url}: {response.text[:500]}"
            ) from exc

    def _request_optional(self, method: str, path: str, **kwargs: Any) -> Optional[Any]:
        try:
            return self._request(method, path, **kwargs)
        except MagicplanAPIError:
            return None

    def test_workspace_connection(self) -> Any:
        return self._request("GET", "/workspace")

    def list_projects(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/projects")
        return _extract_list(payload, possible_keys=("projects", "data", "items", "results"))

    def get_project(self, project_id: str) -> Dict[str, Any]:
        errors: List[str] = []

        for path in (f"/projects/{project_id}", f"/projects/get/{project_id}"):
            try:
                payload = self._request("GET", path)
                if isinstance(payload, dict):
                    return payload
                return {"project_id": project_id, "payload": payload}
            except MagicplanAPIError as exc:
                errors.append(str(exc))

        raise MagicplanAPIError(
            "Could not fetch project detail using known endpoint patterns. "
            + " | ".join(errors)
        )

    def get_project_bundle(self, project_id: str) -> Dict[str, Any]:
        """
        Fetch a fuller export bundle for one project.

        Includes:
        - Project metadata
        - Any plans connected to the project
        - Full plan payloads, which should contain floors, rooms, walls,
          doors, windows, and room objects when available from the Magicplan API.
        """

        project = self.get_project(project_id)

        bundle: Dict[str, Any] = {
            "project_id": project_id,
            "project": project,
            "plans": [],
            "warnings": [],
        }

        plan_ids = extract_plan_ids(project)

        if not plan_ids:
            bundle["warnings"].append(
                "No plan IDs were found in the project payload. "
                "Check the raw project JSON to see what field Magicplan uses for plans."
            )

        for plan_id in plan_ids:
            plan_payload = self.get_plan(plan_id)

            if plan_payload is None:
                bundle["warnings"].append(f"Could not fetch plan data for plan ID: {plan_id}")
                continue

            bundle["plans"].append(
                {
                    "plan_id": plan_id,
                    "raw_plan": plan_payload,
                    "extracted_summary": summarize_plan(plan_payload),
                }
            )

        return bundle

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a full Magicplan plan.

        Magicplan docs reference /plans/get/{plan-id}.
        The script also tries /plans/{plan-id} as a fallback.
        """

        for path in (f"/plans/get/{plan_id}", f"/plans/{plan_id}"):
            payload = self._request_optional("GET", path)
            if isinstance(payload, dict):
                return payload

        return None


def _extract_list(payload: Any, possible_keys: tuple[str, ...]) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in possible_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    raise MagicplanAPIError(
        "Could not find a project list in the API response. Response shape was: "
        + json.dumps(payload, indent=2)[:1000]
    )


def extract_plan_ids(project_payload: Dict[str, Any]) -> List[str]:
    """
    Try common places where Magicplan may expose plan IDs.

    Your Magicplan response stores the plan_id inside:
    project_payload["data"]["plan_id"]
    """

    plan_ids: List[str] = []

    # Check top-level fields
    possible_direct_keys = [
        "plan_id",
        "planId",
        "floorplan_id",
        "floorplanId",
    ]

    for key in possible_direct_keys:
        value = project_payload.get(key)
        if value:
            plan_ids.append(str(value))

    # Check nested "data" object
    data = project_payload.get("data")
    if isinstance(data, dict):
        for key in possible_direct_keys:
            value = data.get(key)
            if value:
                plan_ids.append(str(value))

    # Check possible list fields at top level and inside data
    possible_list_keys = [
        "plans",
        "floorplans",
        "floor_plans",
        "project_plans",
    ]

    containers_to_check = [project_payload]
    if isinstance(data, dict):
        containers_to_check.append(data)

    for container in containers_to_check:
        for key in possible_list_keys:
            value = container.get(key)

            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        plan_ids.append(item)
                    elif isinstance(item, dict):
                        possible_id = (
                            item.get("id")
                            or item.get("plan_id")
                            or item.get("planId")
                            or item.get("uuid")
                        )
                        if possible_id:
                            plan_ids.append(str(possible_id))

            elif isinstance(value, dict):
                possible_id = (
                    value.get("id")
                    or value.get("plan_id")
                    or value.get("planId")
                    or value.get("uuid")
                )
                if possible_id:
                    plan_ids.append(str(possible_id))

    # Remove duplicates while preserving order
    return list(dict.fromkeys(plan_ids))


def summarize_plan(plan_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull out the most useful layout entities from the raw plan payload.

    Magicplan stores windows, doors, and other wall-mounted objects under
    wall_items, not always under separate top-level windows/doors lists.
    """

    floors = find_nested_lists_by_key(plan_payload, "floors")
    rooms = find_nested_lists_by_key(plan_payload, "rooms")
    walls = find_nested_lists_by_key(plan_payload, "walls")

    objects = find_nested_lists_by_key(plan_payload, "objects")
    wall_items = find_nested_lists_by_key(plan_payload, "wall_items")

    doors = find_nested_lists_by_key(plan_payload, "doors")
    windows = find_nested_lists_by_key(plan_payload, "windows")

    sliding_windows = [
        item for item in wall_items
        if is_sliding_window(item)
    ]

    shyld_devices_from_objects = [
        item for item in objects
        if is_shyld_device(item)
    ]

    shyld_devices_from_wall_items = [
        item for item in wall_items
        if is_shyld_device(item)
    ]

    shyld_devices = dedupe_items_by_uid(
        shyld_devices_from_objects + shyld_devices_from_wall_items
    )

    if not windows:
        windows = sliding_windows

    return {
        "floor_count": len(floors),
        "room_count": len(rooms),
        "wall_count": len(walls),
        "object_count": len(objects),
        "wall_item_count": len(wall_items),
        "shyld_device_count": len(shyld_devices),
        "window_count": len(windows),
        "door_count": len(doors),

        "floors": floors,
        "rooms": rooms,
        "walls": walls,
        "objects": objects,
        "wall_items": wall_items,
        "shyld_devices": shyld_devices,
        "doors": doors,
        "windows": windows,
    }


def find_nested_lists_by_key(payload: Any, target_key: str) -> List[Dict[str, Any]]:
    """
    Recursively search for lists under a target key.

    Example:
    - Any list found under "walls" gets flattened into one result list.
    - Any list found under "objects" gets flattened into one result list.
    """

    results: List[Dict[str, Any]] = []

    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == target_key and isinstance(value, list):
                results.extend([item for item in value if isinstance(item, dict)])
            else:
                results.extend(find_nested_lists_by_key(value, target_key))

    elif isinstance(payload, list):
        for item in payload:
            results.extend(find_nested_lists_by_key(item, target_key))

    return results

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


def is_sliding_window(item: Dict[str, Any]) -> bool:
    symbol_id = get_symbol_id(item)
    symbol_name = get_symbol_name(item)

    return (
        symbol_id in {"windowssliding", "windowsliding", "windows_sliding"}
        or symbol_name == "sliding window"
        or "sliding window" in symbol_name
    )


def is_shyld_device(item: Dict[str, Any]) -> bool:
    symbol_id = get_symbol_id(item)
    symbol_name = get_symbol_name(item)

    return (
        symbol_name == "shyld device"
        or "shyld device" in symbol_name
        or "shyld" in symbol_id
    )


def dedupe_items_by_uid(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []

    for item in items:
        item_id = (
            item.get("uid")
            or item.get("id")
            or item.get("uuid")
            or json.dumps(item, sort_keys=True)
        )

        if item_id in seen:
            continue

        seen.add(item_id)
        deduped.append(item)

    return deduped