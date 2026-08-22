from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetFamily:
    """One logical dataset family, backed by one or more CKAN package slugs.

    ``slugs`` is ordered oldest-coverage first, current/rolling package last.
    Today every family has a single slug (v1 covers only the current
    "Plano de Dados Abertos jun-2023 em diante" packages, all monthly XLSX).
    Older sibling packages exist on the portal but use yearly ZIP archives
    instead of monthly XLSX and are intentionally out of scope for now; the
    tuple shape is kept so they can be appended later without changing
    ``_catalog.py``'s merge logic.
    """

    key: str
    title: str
    slugs: tuple[str, ...]


FAMILIES: dict[str, DatasetFamily] = {
    "beneficios_concedidos": DatasetFamily(
        key="beneficios_concedidos",
        title="Benefícios concedidos",
        slugs=("beneficios-concedidos-plano-de-dados-abertos-jun-2023-a-jun-2025",),
    ),
    "beneficios_emitidos": DatasetFamily(
        key="beneficios_emitidos",
        title="Benefícios emitidos",
        slugs=("beneficios-emitidos-plano-de-dados-abertos-jun-2023-a-jun-2025",),
    ),
    "beneficios_mantidos": DatasetFamily(
        key="beneficios_mantidos",
        title="Benefícios mantidos",
        slugs=("beneficios-mantidos-plano-de-dados-abertos-jun-2023-a-jun-2025",),
    ),
    "beneficios_indeferidos": DatasetFamily(
        key="beneficios_indeferidos",
        title="Benefícios indeferidos",
        slugs=("beneficios-indeferidos-plano-de-dados-abertos-jun-2023-a-jun-2025",),
    ),
    "comunicacoes_acidente_trabalho": DatasetFamily(
        key="comunicacoes_acidente_trabalho",
        title="Comunicações de Acidente de Trabalho (CAT)",
        slugs=("comunicacoes-de-acidente-de-trabalho-cat-plano-de-dados-abertos-jun-2023-a-jun-2025",),
    ),
    "perfil_unidades": DatasetFamily(
        key="perfil_unidades",
        title="Perfil das unidades do INSS",
        slugs=("perfil-das-unidades-plano-de-dados-abertos-jun-2023-a-jun-2025",),
    ),
}
