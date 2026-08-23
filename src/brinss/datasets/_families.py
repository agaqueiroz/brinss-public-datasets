from __future__ import annotations

from dataclasses import dataclass

_MANTIDOS_SLUG = "beneficios-mantidos-plano-de-dados-abertos-jun-2023-a-jun-2025"


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

    ``resource_filter`` narrows a package down when it holds more than one
    dataset side by side, which is how "benefícios mantidos" is published:
    ativos, cessados and suspensos, three resources for every month.
    """

    key: str
    title: str
    slugs: tuple[str, ...]
    resource_filter: str | None = None

    def matches_resource(self, resource_name: str) -> bool:
        """Whether a CKAN resource of one of ``slugs`` belongs to this family."""
        if self.resource_filter is None:
            return True
        return self.resource_filter in resource_name.casefold()


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
    "beneficios_mantidos_ativos": DatasetFamily(
        key="beneficios_mantidos_ativos",
        title="Benefícios mantidos ativos",
        slugs=(_MANTIDOS_SLUG,),
        resource_filter="ativos",
    ),
    "beneficios_mantidos_cessados": DatasetFamily(
        key="beneficios_mantidos_cessados",
        title="Benefícios mantidos cessados",
        slugs=(_MANTIDOS_SLUG,),
        resource_filter="cessados",
    ),
    "beneficios_mantidos_suspensos": DatasetFamily(
        key="beneficios_mantidos_suspensos",
        title="Benefícios mantidos suspensos",
        slugs=(_MANTIDOS_SLUG,),
        resource_filter="suspensos",
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
