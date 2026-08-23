from __future__ import annotations

import pandas as pd
import pytest
import responses
from responses import matchers

from brinss.datasets import _catalog, _ckan
from brinss.datasets._families import DatasetFamily
from brinss.datasets.exceptions import PeriodUnavailableError

SLUG = "beneficios-concedidos-plano-de-dados-abertos-jun-2023-a-jun-2025"


def _mock_package_show(slug: str, fixture: dict) -> None:
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        json=fixture,
        status=200,
        match=[matchers.query_param_matcher({"id": slug})],
    )


def _entry(period: str, *, resource_id: str = "r", package_slug: str = "s") -> _catalog.ResourceEntry:
    return _catalog.ResourceEntry(
        period=pd.Period(period, freq="M"),
        url=f"https://fixtures.test/{resource_id}.xlsx",
        resource_id=resource_id,
        resource_name=f"Recurso {period}",
        package_slug=package_slug,
        format="XLSX",
    )


@responses.activate
def test_build_catalog_parses_resources_and_skips_unparseable(load_fixture, cache_dir):
    fixture = load_fixture("package_show_beneficios_concedidos.json")
    _mock_package_show(SLUG, fixture)
    family = DatasetFamily(key="beneficios_concedidos", title="Benefícios concedidos", slugs=(SLUG,))

    with pytest.warns(UserWarning, match="nao tem periodo reconhecivel"):
        catalog = _catalog.build_catalog(family, cache_dir=cache_dir)

    assert [str(entry.period) for entry in catalog.entries] == ["2024-04", "2024-05", "2024-06"]
    assert catalog.max_period == pd.Period("2024-06", freq="M")
    assert catalog.min_period == pd.Period("2024-04", freq="M")


@responses.activate
def test_build_catalog_uses_disk_cache_within_ttl(load_fixture, cache_dir):
    fixture = load_fixture("package_show_minimal.json")
    _mock_package_show(SLUG, fixture)
    family = DatasetFamily(key="beneficios_concedidos", title="x", slugs=(SLUG,))

    _catalog.build_catalog(family, cache_dir=cache_dir)
    responses.reset()  # no responses registered anymore: a second live call would now fail

    catalog = _catalog.build_catalog(family, cache_dir=cache_dir)
    assert len(catalog.entries) == 3


@responses.activate
def test_build_catalog_force_refresh_falls_back_to_stale_cache_with_warning(load_fixture, cache_dir):
    fixture = load_fixture("package_show_minimal.json")
    _mock_package_show(SLUG, fixture)
    family = DatasetFamily(key="beneficios_concedidos", title="x", slugs=(SLUG,))
    _catalog.build_catalog(family, cache_dir=cache_dir)

    responses.reset()  # API now "unreachable"; a stale cache from the call above still exists on disk
    with pytest.warns(UserWarning, match="CKAN indisponivel"):
        catalog = _catalog.build_catalog(family, cache_dir=cache_dir, force_refresh=True)

    assert len(catalog.entries) == 3


@responses.activate
def test_build_catalog_raises_when_api_unreachable_and_no_cache(cache_dir):
    from brinss.datasets.exceptions import CkanUnavailableError

    family = DatasetFamily(key="beneficios_concedidos", title="x", slugs=(SLUG,))
    with pytest.raises(CkanUnavailableError):
        _catalog.build_catalog(family, cache_dir=cache_dir)


@responses.activate
def test_build_catalog_sibling_overlap_last_slug_wins(cache_dir):
    old_slug, new_slug = "familia-historica", "familia-atual"
    old_fixture = {
        "success": True,
        "result": {
            "resources": [
                {
                    "id": "old-1",
                    "name": "Recurso junho 2024",
                    "format": "XLSX",
                    "url": "https://fixtures.test/old.xlsx",
                }
            ]
        },
    }
    new_fixture = {
        "success": True,
        "result": {
            "resources": [
                {
                    "id": "new-1",
                    "name": "Recurso junho 2024",
                    "format": "XLSX",
                    "url": "https://fixtures.test/new.xlsx",
                }
            ]
        },
    }
    _mock_package_show(old_slug, old_fixture)
    _mock_package_show(new_slug, new_fixture)
    family = DatasetFamily(key="familia", title="x", slugs=(old_slug, new_slug))

    with pytest.warns(UserWarning, match="presente em mais de um pacote"):
        catalog = _catalog.build_catalog(family, cache_dir=cache_dir)

    assert len(catalog.entries) == 1
    assert catalog.entries[0].package_slug == new_slug


def test_resolve_periods_none_returns_latest():
    family = DatasetFamily(key="x", title="x", slugs=("x",))
    entries = tuple(_entry(p) for p in ("2024-01", "2024-02", "2024-03"))
    catalog = _catalog.DatasetCatalog(family=family, entries=entries)
    assert _catalog.resolve_periods(catalog, None) == [pd.Period("2024-03", freq="M")]


def test_resolve_periods_all_returns_every_entry():
    family = DatasetFamily(key="x", title="x", slugs=("x",))
    entries = tuple(_entry(p) for p in ("2024-01", "2024-02", "2024-03"))
    catalog = _catalog.DatasetCatalog(family=family, entries=entries)
    assert _catalog.resolve_periods(catalog, "all") == [e.period for e in entries]


def test_resolve_periods_range_with_partial_gap_warns():
    family = DatasetFamily(key="x", title="x", slugs=("x",))
    entries = tuple(_entry(p) for p in ("2024-01", "2024-03"))  # 2024-02 missing
    catalog = _catalog.DatasetCatalog(family=family, entries=entries)

    with pytest.warns(UserWarning, match="periodos indisponiveis"):
        result = _catalog.resolve_periods(catalog, ("2024-01", "2024-03"))

    assert result == [pd.Period("2024-01", freq="M"), pd.Period("2024-03", freq="M")]


def test_resolve_periods_single_unavailable_raises():
    family = DatasetFamily(key="x", title="x", slugs=("x",))
    catalog = _catalog.DatasetCatalog(family=family, entries=(_entry("2024-01"),))
    with pytest.raises(PeriodUnavailableError):
        _catalog.resolve_periods(catalog, "2020-01")


def test_resolve_periods_empty_catalog_raises():
    family = DatasetFamily(key="x", title="x", slugs=("x",))
    catalog = _catalog.DatasetCatalog(family=family, entries=())
    with pytest.raises(PeriodUnavailableError):
        _catalog.resolve_periods(catalog, None)
