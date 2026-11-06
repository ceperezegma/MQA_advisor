import dataclasses

import openpyxl
import pytest

from mqa_advisor.properties import (
    _EXPECTED_HEADER,
    Importance,
    Property,
    PropertyCatalogError,
    load_property_catalog,
)


@pytest.fixture(scope="module")
def catalog():
    return load_property_catalog()


def test_loading_produces_exactly_three_collections(catalog):
    assert len(catalog.dataset) == 18
    assert len(catalog.distribution) == 20
    assert len(catalog.data_service) == 18


def test_known_dataset_row_parses_correctly(catalog):
    title = next(p for p in catalog.dataset if p.property_uri == "dct:title")
    assert title.name == "Title"
    assert title.importance == Importance.MANDATORY
    assert title.weight == 1.0


def test_importance_repr_is_a_plain_quoted_string():
    assert repr(Importance.OPTIONAL) == "'Optional'"


def test_property_repr_shows_importance_as_plain_string(catalog):
    title = next(p for p in catalog.dataset if p.property_uri == "dct:title")
    assert "importance='Mandatory'" in repr(title)
    assert "Importance.MANDATORY" not in repr(title)


def test_two_identifier_rows_are_both_kept(catalog):
    identifiers = [p for p in catalog.dataset if p.name == "Identifier"]
    assert {p.property_uri for p in identifiers} == {
        "dct:identifier",
        "adms:identifier",
    }
    assert len(identifiers) == 2


def test_serves_dataset_row_is_normalized(catalog):
    serves_dataset = next(
        p for p in catalog.data_service if p.property_uri == "dcat:servesDataset"
    )
    assert serves_dataset.name == "Serves dataset"
    assert serves_dataset.importance == Importance.RECOMMENDED


def test_landing_page_accessibility_typo_is_corrected(catalog):
    names = [p.name for p in catalog.data_service]
    assert "Landing page accessibility test" in names
    assert not any("accesiblity" in name for name in names)


def test_real_xlsx_including_both_exceptions_loads_without_raising(catalog):
    access_url_check = next(
        p for p in catalog.distribution if p.name == "Access URL accessibility test"
    )
    assert access_url_check.property_uri == "dcat:accessURL"
    assert access_url_check.importance == Importance.MANDATORY
    assert access_url_check.weight == 0.25

    endpoint_check = next(
        p for p in catalog.data_service if p.name == "Endpoint URL accessibility test"
    )
    assert endpoint_check.property_uri == "dcat:endpointURL"
    assert endpoint_check.importance == Importance.MANDATORY
    assert endpoint_check.weight == 0.25


def test_no_dimension_field_exists_on_property():
    field_names = {f.name for f in dataclasses.fields(Property)}
    assert "dimension" not in {name.lower() for name in field_names}
    assert field_names == {
        "name",
        "property_uri",
        "importance",
        "weight",
        "metric_description",
    }


def test_repeated_loads_are_equal(catalog):
    assert load_property_catalog() == catalog


def _write_workbook(path, *, importance, weight, name="Some Property"):
    workbook = openpyxl.Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    for sheet_name in ("Datasets", "Distributions", "Dataservices"):
        worksheet = workbook.create_sheet(sheet_name)
        worksheet.append(_EXPECTED_HEADER)
        worksheet.append(
            [
                name,
                "ex:someProperty",
                importance,
                "L1",
                weight,
                "The property is set",
            ]
        )

    workbook.save(path)
    return path


def test_mismatched_row_raises_clear_error(tmp_path):
    bad_xlsx = _write_workbook(
        tmp_path / "bad.xlsx", importance="Mandatory", weight=0.5
    )

    with pytest.raises(PropertyCatalogError) as exc_info:
        load_property_catalog(bad_xlsx)

    message = str(exc_info.value)
    assert "Datasets" in message
    assert "Some Property" in message
    assert "Mandatory" in message
    assert "0.5" in message


def test_named_exception_row_with_wrong_weight_still_raises(tmp_path):
    workbook = openpyxl.Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    for sheet_name in ("Datasets", "Distributions", "Dataservices"):
        worksheet = workbook.create_sheet(sheet_name)
        worksheet.append(_EXPECTED_HEADER)
    distributions = workbook["Distributions"]
    distributions.append(
        [
            "Access URL accessibility test",
            "dcat:accessURL",
            "Mandatory",
            "L1",
            0.5,  # not the confirmed exception weight of 0.25
            "The access URL is accessible",
        ]
    )
    bad_xlsx = tmp_path / "bad.xlsx"
    workbook.save(bad_xlsx)

    with pytest.raises(PropertyCatalogError, match="Access URL accessibility test"):
        load_property_catalog(bad_xlsx)


def test_blank_property_name_raises_clear_error(tmp_path):
    bad_xlsx = _write_workbook(
        tmp_path / "bad.xlsx", importance="Mandatory", weight=1.0, name=None
    )

    with pytest.raises(PropertyCatalogError, match="blank 'Property name'"):
        load_property_catalog(bad_xlsx)


def test_blank_relevance_raises_clear_error(tmp_path):
    bad_xlsx = _write_workbook(tmp_path / "bad.xlsx", importance=None, weight=1.0)

    with pytest.raises(PropertyCatalogError, match="blank 'Relevance'"):
        load_property_catalog(bad_xlsx)


def test_blank_weight_raises_clear_error(tmp_path):
    bad_xlsx = _write_workbook(
        tmp_path / "bad.xlsx", importance="Mandatory", weight=None
    )

    with pytest.raises(PropertyCatalogError, match="blank 'Weight'"):
        load_property_catalog(bad_xlsx)


def test_non_numeric_weight_raises_clear_error(tmp_path):
    bad_xlsx = _write_workbook(
        tmp_path / "bad.xlsx", importance="Mandatory", weight="not-a-number"
    )

    with pytest.raises(PropertyCatalogError, match="non-numeric Weight"):
        load_property_catalog(bad_xlsx)


def test_sheet_with_no_data_raises_clear_error(tmp_path):
    workbook = openpyxl.Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    for sheet_name in ("Datasets", "Distributions", "Dataservices"):
        workbook.create_sheet(sheet_name)
    empty_xlsx = tmp_path / "empty.xlsx"
    workbook.save(empty_xlsx)

    with pytest.raises(PropertyCatalogError, match="unexpected header"):
        load_property_catalog(empty_xlsx)
