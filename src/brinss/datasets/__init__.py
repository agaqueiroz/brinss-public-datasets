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

Fonte dos dados
---------------

``source`` escolhe de onde os arquivos são baixados::

    df = load_beneficios_concedidos(periodo="2024-06")                    # "hf" (padrão)
    df = load_beneficios_concedidos(periodo="2024-06", source="inss")     # portal do INSS

``"hf"`` usa o espelho em Parquet publicado em
https://huggingface.co/datasets/agaqueiroz/brinss-public-datasets, gerado por
este mesmo repositório a partir dos arquivos do portal. É o padrão porque é a
mesma tabela por uma fração do custo: bem menos bytes baixados, leitura em
segundos em vez dos minutos que o ``openpyxl`` leva numa planilha grande, e
``columns=[...]`` aplicado dentro do próprio arquivo — as colunas não pedidas
nem chegam a ser descompactadas.

``"inss"`` vai direto a https://dadosabertos.inss.gov.br. É a fonte de
referência, e a que serve quando um mês acabou de ser publicado lá e ainda não
chegou ao espelho, ou quando se quer auditar o espelho contra a origem.

Os dois caminhos entregam o mesmo DataFrame: mesmos nomes de coluna, mesma
coluna ``periodo_referencia`` como ``pandas.Period``, mesmo ``dtype="str"`` por
padrão.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from . import _cache
from ._loader import list_datasets, list_periods, load_dataset
from ._period import PeriodoLike
from .enums import ColumnDtype, DataSource, XlsxEngine
from .exceptions import (
    BrinssError,
    CkanUnavailableError,
    ColumnNotFoundError,
    HuggingFaceUnavailableError,
    PeriodError,
    PeriodUnavailableError,
    UnsupportedArchiveError,
)

__all__ = [
    "BrinssError",
    "CkanUnavailableError",
    "ColumnDtype",
    "ColumnNotFoundError",
    "DataSource",
    "HuggingFaceUnavailableError",
    "PeriodError",
    "PeriodUnavailableError",
    "PeriodoLike",
    "UnsupportedArchiveError",
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
