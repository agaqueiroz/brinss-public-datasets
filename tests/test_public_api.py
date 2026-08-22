from __future__ import annotations

import brinss.datasets as ds

EXPECTED_FAMILY_KEYS = {
    "beneficios_concedidos",
    "beneficios_emitidos",
    "beneficios_mantidos",
    "beneficios_indeferidos",
    "comunicacoes_acidente_trabalho",
    "perfil_unidades",
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
    assert ds.load_beneficios_mantidos() == "ok"
    assert ds.load_beneficios_indeferidos() == "ok"
    assert ds.load_comunicacoes_acidente_trabalho() == "ok"
    assert ds.load_perfil_unidades() == "ok"

    assert set(calls) == EXPECTED_FAMILY_KEYS


def test_get_cache_dir_returns_a_path(tmp_path):
    assert ds.get_cache_dir(tmp_path) == tmp_path
