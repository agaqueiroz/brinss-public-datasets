from __future__ import annotations

import brinss.datasets as ds

EXPECTED_FAMILY_KEYS = {
    "beneficios_concedidos",
    "beneficios_emitidos",
    "beneficios_mantidos_ativos",
    "beneficios_mantidos_cessados",
    "beneficios_mantidos_suspensos",
    "beneficios_indeferidos",
    "comunicacoes_acidente_trabalho",
    "perfil_unidades",
    "pessoal_ativo_consolidado",
    "ocupantes_funcoes_cargos",
    "pessoal_sem_identificacao",
    "requerimentos_solicitados",
    "requerimentos_pendentes",
}


def test_list_datasets_returns_all_family_keys():
    assert set(ds.list_datasets()) == EXPECTED_FAMILY_KEYS


def test_load_wrappers_delegate_to_load_dataset_with_the_right_family_key(monkeypatch):
    calls = []

    def fake_load_dataset(name, periodo=None, **kwargs):
        calls.append(name)
        return "ok"

    monkeypatch.setattr(ds, "load_dataset", fake_load_dataset)

    assert ds.load_beneficios_concedidos() == "ok"
    assert ds.load_beneficios_emitidos() == "ok"
    assert ds.load_beneficios_mantidos_ativos() == "ok"
    assert ds.load_beneficios_mantidos_cessados() == "ok"
    assert ds.load_beneficios_mantidos_suspensos() == "ok"
    assert ds.load_beneficios_indeferidos() == "ok"
    assert ds.load_comunicacoes_acidente_trabalho() == "ok"
    assert ds.load_perfil_unidades() == "ok"
    assert ds.load_pessoal_ativo_consolidado() == "ok"
    assert ds.load_ocupantes_funcoes_cargos() == "ok"
    assert ds.load_pessoal_sem_identificacao() == "ok"
    assert ds.load_requerimentos_solicitados() == "ok"
    assert ds.load_requerimentos_pendentes() == "ok"

    assert set(calls) == EXPECTED_FAMILY_KEYS


def test_load_wrappers_forward_dtype_to_load_dataset(monkeypatch):
    # The wrappers take **kwargs, so nothing in them names dtype -- this pins
    # that it still reaches load_dataset.
    captured = {}

    def fake_load_dataset(name, periodo=None, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(ds, "load_dataset", fake_load_dataset)
    ds.load_beneficios_concedidos(dtype=ds.ColumnDtype.INFER)

    assert captured["dtype"] is ds.ColumnDtype.INFER


def test_load_wrappers_forward_source_to_load_dataset(monkeypatch):
    # Same contract as dtype above: source rides in on **kwargs.
    captured = {}

    def fake_load_dataset(name, periodo=None, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(ds, "load_dataset", fake_load_dataset)
    ds.load_beneficios_concedidos(source=ds.DataSource.INSS)

    assert captured["source"] is ds.DataSource.INSS


def test_load_wrappers_forward_chunksize_to_load_dataset(monkeypatch):
    # Same contract as dtype and source: chunksize rides in on **kwargs.
    captured = {}

    def fake_load_dataset(name, periodo=None, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(ds, "load_dataset", fake_load_dataset)
    ds.load_beneficios_emitidos(periodo="all", chunksize=500_000)

    assert captured["chunksize"] == 500_000


def test_streaming_types_are_public():
    assert issubclass(ds.StreamEncodingError, ds.BrinssError)
    assert {"DatasetChunks", "StreamEncodingError"} <= set(ds.__all__)


def test_get_cache_dir_returns_a_path(tmp_path):
    assert ds.get_cache_dir(tmp_path) == tmp_path
