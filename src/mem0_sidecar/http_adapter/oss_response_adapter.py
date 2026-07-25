from typing import Any


def as_oss_add_response(sidecar_result: dict[str, Any]) -> dict[str, Any]:
    return sidecar_result["memory"]


def as_oss_search_response(sidecar_result: dict[str, Any]) -> dict[str, Any]:
    return sidecar_result


def as_oss_mutation_response(sidecar_result: dict[str, Any]) -> dict[str, Any]:
    return sidecar_result["memory"]
