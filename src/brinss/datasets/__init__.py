"""Datasets abertos do INSS, no estilo ``load_iris()`` do scikit-learn.

Cada ``load_*`` baixa (com cache local) e retorna um ``pandas.DataFrame``
com os dados do período solicitado. Sem período informado, retorna apenas
o mês mais recente disponível.

Exemplo::

    from brinss.datasets import load_beneficios_concedidos

    df = load_beneficios_concedidos()                          # mês mais recente
    df = load_beneficios_concedidos(periodo="2024-06")          # um mês
    df = load_beneficios_concedidos(periodo=("2024-01", "2024-06"))  # intervalo
    df = load_beneficios_concedidos(periodo="all")               # todo o histórico disponível

Por padrão todas as colunas vêm como texto (``str``), preservando zeros à
esquerda em códigos como CID, CBO, CNAE e código IBGE. Para deixar o pandas
inferir os tipos, passe ``dtype="infer"``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from . import _cache
from ._loader import list_datasets, list_periods, load_dataset
from ._period import PeriodoLike
from .enums import ColumnDtype, XlsxEngine
from .exceptions import (
    BrinssError,
    CkanUnavailableError,
    ColumnNotFoundError,
    PeriodError,
    PeriodUnavailableError,
)

__all__ = [
    "BrinssError",
    "CkanUnavailableError",
    "ColumnDtype",
    "ColumnNotFoundError",
    "PeriodError",
    "PeriodUnavailableError",
    "PeriodoLike",
    "XlsxEngine",
    "get_cache_dir",
    "list_datasets",
    "list_periods",
    "load_beneficios_concedidos",
    "load_beneficios_emitidos",
    "load_beneficios_indeferidos",
    "load_beneficios_mantidos_ativos",
    "load_beneficios_mantidos_cessados",
    "load_beneficios_mantidos_suspensos",
    "load_comunicacoes_acidente_trabalho",
    "load_dataset",
    "load_perfil_unidades",
]


def get_cache_dir(cache_dir: str | os.PathLike | None = None) -> Path:
    """Return the resolved local cache directory (without creating it)."""
    return _cache.get_cache_root(cache_dir)


def load_beneficios_concedidos(periodo: PeriodoLike = None, **kwargs) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("beneficios_concedidos", periodo, **kwargs)


def load_beneficios_emitidos(periodo: PeriodoLike = None, **kwargs) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("beneficios_emitidos", periodo, **kwargs)


def load_beneficios_mantidos_ativos(periodo: PeriodoLike = None, **kwargs) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("beneficios_mantidos_ativos", periodo, **kwargs)


def load_beneficios_mantidos_cessados(
    periodo: PeriodoLike = None, **kwargs
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("beneficios_mantidos_cessados", periodo, **kwargs)


def load_beneficios_mantidos_suspensos(
    periodo: PeriodoLike = None, **kwargs
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("beneficios_mantidos_suspensos", periodo, **kwargs)


def load_beneficios_indeferidos(periodo: PeriodoLike = None, **kwargs) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("beneficios_indeferidos", periodo, **kwargs)


def load_comunicacoes_acidente_trabalho(
    periodo: PeriodoLike = None, **kwargs
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("comunicacoes_acidente_trabalho", periodo, **kwargs)


def load_perfil_unidades(periodo: PeriodoLike = None, **kwargs) -> pd.DataFrame | dict[str, pd.DataFrame]:
    return load_dataset("perfil_unidades", periodo, **kwargs)
