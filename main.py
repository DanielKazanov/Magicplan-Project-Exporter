from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from magicplanProjectFetcher import client
from magicplanProjectFetcher.client import (
    MagicplanAPIError,
    MagicplanClient,
    MagicplanConfig,
    MagicplanConfigError,
)

LOCAL_STORAGE_DIR = Path("Exported Magicplan Projects")


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
            print("Selected project payload:")
            print(json.dumps(selected_project, indent=2, ensure_ascii=False))
            return

        print(f"\nFetching full project bundle for project ID: {project_id}")
        project_bundle = client.get_project_bundle(project_id)

        print("\nFetched project bundle:")
        print(json.dumps(project_bundle, indent=2, ensure_ascii=False))

        output_path = save_project_json(project_id, project_bundle)
        print(f"\nSaved full project bundle JSON to: {output_path}")

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
    name = get_first_present(project, "name", "title", "project_name", "label") or "Untitled project"
    updated = get_first_present(project, "updated_at", "updatedAt", "modified_at", "modifiedAt")
    address = extract_address(project)

    parts = [f"{index}. {name}", f"id={project_id}"]
    if updated:
        parts.append(f"updated={updated}")
    if address:
        parts.append(f"address={address}")
    return " | ".join(parts)


def get_project_id(project: Dict[str, Any]) -> Optional[str]:
    value = get_first_present(project, "id", "project_id", "projectId", "uuid")
    return str(value) if value is not None else None


def get_first_present(project: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        if key in project and project[key] not in (None, ""):
            return project[key]
    return None


def extract_address(project: Dict[str, Any]) -> Optional[str]:
    address = project.get("address")
    if isinstance(address, str):
        return address
    if isinstance(address, dict):
        address_parts = [
            address.get("street"),
            address.get("city"),
            address.get("state"),
            address.get("zip"),
            address.get("country"),
        ]
        return ", ".join(str(part) for part in address_parts if part)
    return None


def save_project_json(project_id: str, project_info: Dict[str, Any]) -> Path:
    LOCAL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "_", project_id)
    output_path = LOCAL_STORAGE_DIR / f"magicplan_project_bundle_{safe_project_id}_{timestamp}.json"
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(project_info, file, indent=2, ensure_ascii=False)
        file.write("\n")

    return output_path


if __name__ == "__main__":
    main()