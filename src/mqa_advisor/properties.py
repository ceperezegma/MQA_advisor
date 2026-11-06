"""Load the reconciled Property catalog from MQA_methodology/MQA-properties-for-app.xlsx.

Parses the workbook's three sheets (Datasets, Distributions, Dataservices) into an
immutable, type-checked `PropertyCatalog` — this project's own vocabulary (`Property`,
`Importance`, `Weight`), never the MQA App's JSON field names (e.g. `metric`). Two known
cosmetic defects in the source xlsx are normalized on load, and each row's Importance/
Weight pair is validated against the standard mapping, with a narrow named exception for
two confirmed non-standard rows.

Usage:

    catalog = load_property_catalog()
    for prop in catalog.dataset:
        ...
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import openpyxl

DEFAULT_PROPERTY_CATALOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "MQA_methodology"
    / "MQA-properties-for-app.xlsx"
)

_EXPECTED_HEADER = (
    "Property name",
    "Property checked",
    "Relevance",
    "Layer",
    "Weight",
    "Metric definition",
)

_SHEET_TO_COLLECTION = {
    "Datasets": "dataset",
    "Distributions": "distribution",
    "Dataservices": "data_service",
}

# Correction for a known typo in the committed xlsx (Dataservices sheet, issue #3) —
# becomes harmless dead code if a future re-sync fixes the typo upstream. The source cell
# was re-edited (single "s") after the original correction was written; the dict key
# tracks whatever the current source spelling is.
_PROPERTY_NAME_CORRECTIONS = {
    "Landing page accesibility test": "Landing page accessibility test",
}

# Correction for a stray trailing backtick on the "Serves dataset" row's Relevance
# cell (issue #3) — stripping any trailing backtick is a no-op once the source is fixed.
_RELEVANCE_TRAILING_BACKTICK = "`"


class PropertyCatalogError(Exception):
    """Raised when the property catalog xlsx is missing, malformed, or internally inconsistent."""


class Importance(str, Enum):
    MANDATORY = "Mandatory"
    RECOMMENDED = "Recommended"
    OPTIONAL = "Optional"

    def __repr__(self) -> str:
        return repr(self.value)


_STANDARD_WEIGHT = {
    Importance.MANDATORY: 1.0,
    Importance.RECOMMENDED: 0.5,
    Importance.OPTIONAL: 0.25,
}

# Two confirmed Mandatory/0.25 accessibility-test rows where Weight intentionally
# doesn't follow the standard Importance->Weight mapping (issue #3, Correction 2).
_ACCESSIBILITY_CHECK_EXCEPTION_WEIGHT = 0.25
_MANDATORY_ACCESSIBILITY_CHECK_WEIGHT_EXCEPTIONS = frozenset(
    {
        ("Distributions", "Access URL accessibility test", "dcat:accessURL"),
        ("Dataservices", "Endpoint URL accessibility test", "dcat:endpointURL"),
    }
)


@dataclass(frozen=True)
class Property:
    name: str
    property_uri: str
    importance: Importance
    weight: float
    metric_description: str


@dataclass(frozen=True)
class PropertyCatalog:
    dataset: tuple[Property, ...]
    distribution: tuple[Property, ...]
    data_service: tuple[Property, ...]


def load_property_catalog(path: Path | None = None) -> PropertyCatalog:
    """Parse the reconciled property catalog xlsx into a `PropertyCatalog`."""
    resolved_path = path or DEFAULT_PROPERTY_CATALOG_PATH
    workbook = openpyxl.load_workbook(resolved_path, data_only=True)

    collections = {
        attr: _parse_sheet(workbook, sheet_name)
        for sheet_name, attr in _SHEET_TO_COLLECTION.items()
    }
    return PropertyCatalog(**collections)


def _parse_sheet(workbook: openpyxl.Workbook, sheet_name: str) -> tuple[Property, ...]:
    if sheet_name not in workbook.sheetnames:
        raise PropertyCatalogError(
            f"Property catalog is missing expected sheet {sheet_name!r} "
            f"(found: {workbook.sheetnames!r})"
        )
    worksheet = workbook[sheet_name]

    try:
        header_row = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True))
    except StopIteration as exc:
        raise PropertyCatalogError(
            f"Sheet {sheet_name!r} is empty (no header row)"
        ) from exc
    header = tuple(_strip(cell) for cell in header_row)
    if header != _EXPECTED_HEADER:
        raise PropertyCatalogError(
            f"Sheet {sheet_name!r} has unexpected header {header!r}, "
            f"expected {_EXPECTED_HEADER!r}"
        )

    properties = []
    for row_number, row in enumerate(
        worksheet.iter_rows(min_row=2, values_only=True), start=2
    ):
        if all(value is None for value in row):
            continue
        properties.append(_parse_row(sheet_name, row_number, row))
    return tuple(properties)


def _parse_row(sheet_name: str, row_number: int, row: tuple) -> Property:
    (
        name_raw,
        property_uri_raw,
        relevance_raw,
        _layer,
        weight_raw,
        metric_description_raw,
    ) = row

    name_raw = _require_cell(name_raw, sheet_name, row_number, "Property name")
    property_uri_raw = _require_cell(
        property_uri_raw, sheet_name, row_number, "Property checked"
    )
    relevance_raw = _require_cell(relevance_raw, sheet_name, row_number, "Relevance")
    weight_raw = _require_cell(weight_raw, sheet_name, row_number, "Weight")
    metric_description_raw = _require_cell(
        metric_description_raw, sheet_name, row_number, "Metric definition"
    )

    name = _PROPERTY_NAME_CORRECTIONS.get(_strip(name_raw), _strip(name_raw))
    property_uri = _strip(property_uri_raw)
    metric_description = _strip(metric_description_raw)

    relevance = _strip(relevance_raw)
    if relevance.endswith(_RELEVANCE_TRAILING_BACKTICK):
        relevance = relevance[: -len(_RELEVANCE_TRAILING_BACKTICK)].strip()

    try:
        importance = Importance(relevance)
    except ValueError as exc:
        raise PropertyCatalogError(
            f"{sheet_name!r} row {row_number} ({name!r}) has unrecognized Relevance "
            f"{relevance!r}"
        ) from exc

    try:
        weight = float(weight_raw)
    except (TypeError, ValueError) as exc:
        raise PropertyCatalogError(
            f"{sheet_name!r} row {row_number} ({name!r}) has non-numeric Weight "
            f"{weight_raw!r}"
        ) from exc
    is_named_exception = (sheet_name, name, property_uri) in (
        _MANDATORY_ACCESSIBILITY_CHECK_WEIGHT_EXCEPTIONS
    )
    if is_named_exception:
        expected_weight = _ACCESSIBILITY_CHECK_EXCEPTION_WEIGHT
    else:
        expected_weight = _STANDARD_WEIGHT[importance]
    if weight != expected_weight:
        raise PropertyCatalogError(
            f"{sheet_name!r} row {row_number} ({name!r}, {property_uri!r}) has "
            f"Importance {importance.value} but Weight {weight} (expected {expected_weight})"
        )

    return Property(
        name=name,
        property_uri=property_uri,
        importance=importance,
        weight=weight,
        metric_description=metric_description,
    )


def _strip(value: Any) -> str:
    return value.strip() if isinstance(value, str) else value


def _require_cell(
    value: Any, sheet_name: str, row_number: int, column_name: str
) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PropertyCatalogError(
            f"{sheet_name!r} row {row_number} has a blank {column_name!r} cell"
        )
    return value
