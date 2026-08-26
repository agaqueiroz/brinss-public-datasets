from __future__ import annotations

import pandas as pd
import pytest

from brinss.datasets import DataSource, load_perfil_unidades


@pytest.mark.network
@pytest.mark.parametrize("source", [DataSource.HF, DataSource.INSS])
def test_load_perfil_unidades_real_download(tmp_path, source):
    df = load_perfil_unidades(cache_dir=tmp_path, source=source)
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    assert "periodo_referencia" in df.columns


@pytest.mark.network
def test_both_sources_agree_on_the_same_month(tmp_path):
    """The mirror is built from the portal's file, so it has to match it."""
    periodo = "2024-04"
    from_hub = load_perfil_unidades(periodo=periodo, cache_dir=tmp_path, source=DataSource.HF)
    from_portal = load_perfil_unidades(periodo=periodo, cache_dir=tmp_path, source=DataSource.INSS)

    assert list(from_hub.columns) == list(from_portal.columns)
    assert from_hub.shape == from_portal.shape
    assert from_hub.equals(from_portal)
