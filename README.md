# brinss-public-datasets

Carregamento (com download e cache automáticos) dos datasets abertos do INSS
publicados em [dadosabertos.inss.gov.br](https://dadosabertos.inss.gov.br),
no estilo `load_iris()` do scikit-learn.

## Instalação

```bash
uv add brinss-public-datasets
```

## Uso

```python
from brinss.datasets import load_beneficios_concedidos

df = load_beneficios_concedidos()                                  # mês mais recente disponível
df = load_beneficios_concedidos(periodo="2024-06")                 # um mês específico
df = load_beneficios_concedidos(periodo=("2024-01", "2024-06"))    # intervalo de meses
df = load_beneficios_concedidos(periodo="all")                     # todo o histórico disponível
```

Datasets disponíveis (`brinss.datasets.list_datasets()`):

| Função | Descrição |
| --- | --- |
| `load_beneficios_concedidos` | Benefícios concedidos |
| `load_beneficios_emitidos` | Benefícios emitidos |
| `load_beneficios_mantidos` | Benefícios mantidos |
| `load_beneficios_indeferidos` | Benefícios indeferidos |
| `load_comunicacoes_acidente_trabalho` | Comunicações de Acidente de Trabalho (CAT) |
| `load_perfil_unidades` | Perfil das unidades do INSS |

Todas aceitam os mesmos parâmetros de `load_dataset`:

```python
from brinss.datasets import load_dataset

df = load_dataset(
    "beneficios_concedidos",
    periodo="2024-06",
    as_dict=False,          # True: dict[str, DataFrame] por período, em vez de concatenar
    columns=None,           # lista de colunas para carregar só um subconjunto
    force_download=False,   # ignora o cache local e baixa de novo
    force_refresh=False,    # ignora o cache (24h) do catálogo de períodos disponíveis
    cache_dir=None,         # sobrescreve o diretório de cache para esta chamada
)
```

Outras funções úteis:

```python
from brinss.datasets import list_datasets, list_periods, get_cache_dir

list_datasets()                        # chaves de todos os datasets disponíveis
list_periods("beneficios_concedidos")  # todos os períodos (meses) disponíveis para um dataset
get_cache_dir()                        # onde os arquivos baixados estão sendo guardados
```

## Cache local

Os arquivos baixados ficam em cache em disco, na pasta padrão de cache do
sistema operacional (via [`platformdirs`](https://pypi.org/project/platformdirs/)):

- Windows: `%LOCALAPPDATA%\brinss\Cache`
- Linux: `~/.cache/brinss`
- macOS: `~/Library/Caches/brinss`

Para usar outro diretório, defina a variável de ambiente `BRINSS_DATA_HOME`
ou passe `cache_dir=...` em qualquer chamada de `load_*`/`load_dataset`.

O portal não publica checksum dos arquivos; no primeiro download o SHA256 é
calculado e guardado localmente, e passa a ser conferido nas chamadas
seguintes (detectando automaticamente se o governo trocar o conteúdo de um
arquivo sem trocar o nome).

### Formato dos arquivos

O campo `format` que a CKAN retorna para cada recurso não é confiável: já foi
visto marcado como `CSV` para um arquivo que na prática é um **ZIP contendo
um único CSV** (`;` como delimitador, encoding Latin-1/cp1252), e marcado
como `XLS`/`XLSX` para arquivos que às vezes são XLSX genuíno, às vezes
`.xltx`, etc. Por isso a biblioteca **inspeciona o conteúdo real do arquivo
baixado** (em vez de confiar no `format`) para decidir como ler: ZIP com
planilha OOXML dentro é lido como Excel; ZIP com um único CSV dentro é
descompactado e o CSV é lido com detecção automática de delimitador/encoding;
Excel binário legado (`.xls` no formato antigo OLE2) é lido via `xlrd`.

Os recursos da CAT (`comunicacoes_acidente_trabalho`) vão além: trazem **o mesmo
dado três vezes** dentro do ZIP, em `.csv`, `.json` e `.xml`. Nesse caso (membros
que só diferem na extensão) a biblioteca lê a versão tabular — o CSV — e registra
no log qual membro foi lido e quais foram ignorados; os outros formatos ficam
intactos dentro do arquivo em cache, sem serem descompactados. Um ZIP com
datasets **diferentes** dentro continua sendo erro (`UnsupportedArchiveError`),
já que não haveria como saber qual deles carregar.

### Cabeçalho das planilhas

As planilhas de `beneficios_concedidos` e `beneficios_indeferidos` abrem com
uma **linha de título** que preenche uma única célula (ex: `DADOS ABERTOS -
BENEFÍCIOS CONCEDIDOS - ANO JULHO DE 2026`), e os nomes reais das colunas só
vêm na linha seguinte. Lidas ingenuamente, essas planilhas produzem colunas
chamadas `Unnamed: 1`, `Unnamed: 2`, etc.

O texto dessa linha muda de mês para mês, então a biblioteca a identifica pela
forma, e não pelo conteúdo: o cabeçalho é a primeira linha que preenche mais de
uma célula. Planilhas que já começam com um cabeçalho de verdade, como as de
`perfil_unidades`, não são afetadas.

Como o cabeçalho passa a ser o correto, `columns=[...]` funciona com os nomes
reais dessas famílias (`"APS"`, `"Competência concessão"`, ...) — antes não
havia nome válido para pedir.

Algumas famílias repetem nomes de coluna na origem, em pares de código e
descrição (`APS`, `APS`, `Espécie`, `Espécie`, ... em `beneficios_concedidos`;
`CBO`, `CBO`, ... na CAT). Esses casos saem com o sufixo padrão do pandas:
`APS` e `APS.1`.

### Arquivos grandes

Alguns meses de `beneficios_emitidos` e `beneficios_mantidos` descompactam
para vários gigabytes (um único mês de `beneficios_emitidos` chega a ~10 GB
descompactado). `periodo="all"` ou intervalos grandes nessas famílias podem
exigir bastante RAM e espaço em disco. Nenhum limite é aplicado
automaticamente nesta versão — prefira pedir um `periodo` específico e, se
precisar, usar `columns=[...]` para reduzir o volume carregado em memória.

### Mensagens de progresso

Cada chamada de `load_*` escreve no `stderr` o andamento de cada período:
conclusão do download (ou aviso de que o arquivo veio do cache), início e fim
da leitura para o DataFrame, e — quando há mais de um período — a
concatenação final:

```
Downloading file 'res-06__Benefícios concedidos junho 2024.xlsx' from '...' to '...'.
Download complete: 'res-06__Benefícios concedidos junho 2024.xlsx' (12.4 MB) in 8.3 s.
Reading 'res-06__Benefícios concedidos junho 2024.xlsx' (12.4 MB) into a DataFrame...
DataFrame loaded: 148,203 rows x 17 columns from 'res-06__Benefícios concedidos junho 2024.xlsx' in 4.1 s.
```

As mensagens saem pelo logger `brinss` e podem ser silenciadas (ou
redirecionadas) com o `logging` padrão:

```python
import logging

logging.getLogger("brinss").setLevel(logging.WARNING)
```

A primeira linha (`Downloading file ...`) é do
[`pooch`](https://www.fatiando.org/pooch/), e se silencia à parte:

```python
import pooch

pooch.get_logger().setLevel("WARNING")
```

## To-do

- [x] Adicionar tópicos ao repositório no GitHub, para facilitar descoberta.
- [x] Adicionar mensagem quando o download do dataset for concluído
- [x] Adicionar mensagem de início e conclusão do carregamento no dataframe
- [ ] Suportar as séries históricas mais antigas (2012–2023), publicadas em
      arquivos ZIP com granularidade anual (ex: `beneficios-concedidos-dez-2012-a-nov-2018-...`).
- [ ] Suportar a segunda camada de pacotes legados (`inss-beneficios-*`, até
      mai/2023), com mistura de formatos ZIP/CSV/JSON/XML e múltiplas
      categorias por mês (ex: ativos/suspensos/cessados em "mantidos").
- [ ] Implementar `brinss.ops`: funções de transformação/análise sobre os
      datasets carregados (hoje é só um namespace reservado, vazio).

## Desenvolvimento

```bash
uv sync
uv run pytest              # suíte completa (sem os testes que precisam de rede)
uv run pytest -m network   # inclui um teste real contra o portal
uv run ruff check .
```
