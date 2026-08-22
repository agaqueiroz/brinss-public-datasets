from __future__ import annotations

import requests

from .exceptions import CkanUnavailableError

BASE_URL = "https://dadosabertos.inss.gov.br"
DEFAULT_TIMEOUT = 30.0


def _get_action(action: str, *, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    url = f"{BASE_URL}/api/3/action/{action}"
    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CkanUnavailableError(f"falha ao acessar {url}: {exc}") from exc

    if not payload.get("success", False):
        raise CkanUnavailableError(f"resposta da CKAN sem sucesso para {url}: {payload}")
    return payload["result"]


def package_list(*, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Return every dataset slug published on the portal."""
    return _get_action("package_list", timeout=timeout)


def package_show(slug: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return the full CKAN package metadata (including resources) for one slug."""
    return _get_action("package_show", params={"id": slug}, timeout=timeout)
