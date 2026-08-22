from __future__ import annotations

import pytest
import requests
import responses

from brinss.datasets import _ckan
from brinss.datasets.exceptions import CkanUnavailableError


@responses.activate
def test_package_list_success():
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_list",
        json={"success": True, "result": ["a", "b"]},
        status=200,
    )
    assert _ckan.package_list() == ["a", "b"]


@responses.activate
def test_package_show_success(load_fixture):
    fixture = load_fixture("package_show_beneficios_concedidos.json")
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        json=fixture,
        status=200,
    )
    result = _ckan.package_show("beneficios-concedidos-plano-de-dados-abertos-jun-2023-a-jun-2025")
    assert result["name"] == "beneficios-concedidos-plano-de-dados-abertos-jun-2023-a-jun-2025"
    assert len(result["resources"]) == 4


@responses.activate
def test_package_show_http_error_raises():
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        status=500,
    )
    with pytest.raises(CkanUnavailableError):
        _ckan.package_show("qualquer-slug")


@responses.activate
def test_package_show_unsuccessful_payload_raises():
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        json={"success": False, "error": {"message": "Not found"}},
        status=200,
    )
    with pytest.raises(CkanUnavailableError):
        _ckan.package_show("slug-inexistente")


@responses.activate
def test_package_show_connection_error_raises():
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        body=requests.exceptions.ConnectionError("boom"),
    )
    with pytest.raises(CkanUnavailableError):
        _ckan.package_show("slug")
