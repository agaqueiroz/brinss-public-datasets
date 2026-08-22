from __future__ import annotations

import pandas as pd
import pytest

from brinss.datasets import load_perfil_unidades


@pytest.mark.network
def test_load_perfil_unidades_real_download(tmp_path):
    df = load_perfil_unidades(cache_dir=tmp_path)
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    assert "periodo_referencia" in df.columns
