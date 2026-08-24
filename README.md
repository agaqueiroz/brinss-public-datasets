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

As colunas vêm como texto por padrão, para não perder os zeros à esquerda dos
códigos do INSS. Para deixar o pandas converter os tipos automaticamente, passe
`dtype="infer"` — veja [Tipos das colunas](#tipos-das-colunas).

Datasets disponíveis (`brinss.datasets.list_datasets()`):

| Função | Descrição |
| --- | --- |
| `load_beneficios_concedidos` | Benefícios concedidos |
| `load_beneficios_emitidos` | Benefícios emitidos |
| `load_beneficios_mantidos_ativos` | Benefícios mantidos ativos |
| `load_beneficios_mantidos_cessados` | Benefícios mantidos cessados |
| `load_beneficios_mantidos_suspensos` | Benefícios mantidos suspensos |
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
    dtype="str",            # "str": tudo como texto (padrão) | "infer": pandas infere os tipos
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

### Tipos das colunas

Por padrão **todas as colunas são carregadas como texto** (`dtype="str"`). Não é
um detalhe de conveniência: os arquivos publicados são texto, e a inferência de
tipos do pandas destrói dados reais desses datasets.

- **Zeros à esquerda somem.** CID, CBO, CNAE e código IBGE de município vêm como
  `"01234"` na origem e viram `1234` na inferência — o join com qualquer tabela
  de referência passa a não casar.
- **O tipo muda de mês para mês.** Um mês em que a coluna vem toda preenchida é
  inferido como `int64`; outro com células vazias vira `float64`. Ao pedir um
  intervalo de meses, o DataFrame concatenado sai com o tipo dependendo de quais
  meses foram pedidos.

Para deixar o pandas converter os tipos, como faria um `read_csv` cru:

```python
df = load_beneficios_concedidos(periodo="2024-06", dtype="infer")
```

O parâmetro também aceita o enum exportado, se preferir explicitar:

```python
from brinss.datasets import ColumnDtype

df = load_beneficios_concedidos(periodo="2024-06", dtype=ColumnDtype.INFER)
```

Duas observações sobre o modo texto:

- Células vazias continuam saindo como `NaN`, e não como `""` — `.isna()` segue
  funcionando normalmente para filtrar linhas incompletas.
- `periodo_referencia` **não** é afetada: é metadado inserido pela biblioteca, e
  continua como `pandas.Period` nos dois modos, para permitir filtros por
  período direto no DataFrame.

### Benefícios mantidos: três datasets, não um

O portal publica **três recursos por mês** para benefícios mantidos — ativos,
cessados e suspensos — dentro do mesmo pacote, e eles **não compartilham as
mesmas colunas**: cessados não traz `Clientela` e repete `Sexo.`, e ativos usa
`Motivo Cessação/Suspensão` onde os outros dois usam `Motivo
Cessação/Suspensão Novo`. Por isso são três funções separadas, e não uma só
com tudo empilhado.

O mês de referência é lido do nome do recurso, mas quando dois recursos
reivindicam o mesmo mês o nome do arquivo na URL desempata — é o que recupera
suspensos de maio/2025, que o portal publicou rotulado como "abril 2025".

### Arquivos grandes

Alguns meses descompactam para vários gigabytes. Tamanhos por mês, medidos em
maio/2026: `beneficios_mantidos_ativos` ~891 MB, `beneficios_mantidos_cessados`
~786 MB e `beneficios_emitidos` ~763 MB compactados — um único mês de
`beneficios_emitidos` chega a ~10 GB descompactado. `beneficios_mantidos_suspensos`,
em contraste, é leve (~5 MB/mês). `periodo="all"` ou intervalos grandes nas
famílias pesadas podem exigir bastante RAM e espaço em disco. Nenhum limite é aplicado
automaticamente nesta versão — prefira pedir um `periodo` específico e, se
precisar, usar `columns=[...]` para reduzir o volume carregado em memória.
Nessas famílias pesadas vale lembrar que o padrão `dtype="str"` costuma ocupar
mais RAM que colunas numéricas; se os códigos com zero à esquerda não importarem
para a sua análise, `dtype="infer"` reduz o consumo.

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

## Publicação em Parquet no Hugging Face

O repositório traz um script de manutenção que converte os datasets para Parquet
e publica em
[huggingface.co/datasets/agaqueiroz/brinss-public-datasets](https://huggingface.co/datasets/agaqueiroz/brinss-public-datasets).
Ele vive em `scripts/`, fora de `src/brinss`, e portanto **não faz parte do
pacote distribuído** — usa a própria biblioteca para ler os arquivos, herdando
de graça o tratamento de banner, membros de ZIP e encoding.

```bash
uv run --group publish python scripts/publish_to_hf.py            # mostra o plano
uv run --group publish python scripts/publish_to_hf.py --push     # publica
```

### Gerar amostras locais

Antes de publicar qualquer coisa, `--sample` converte para Parquet **só o que já
está no cache de downloads**, gravando em `tmp/` (ignorada pelo git):

```bash
uv run --group publish python scripts/publish_to_hf.py --sample
```

Ele nunca baixa nada e nunca fala com o Hub — meses ausentes do cache são
reportados e pulados. É a forma barata de conferir o resultado real antes de
encarar uma publicação completa, que são **291 arquivos** e dezenas de GB
vindos do portal.

O layout em `tmp/` espelha o do Hub (`tmp/data/<família>/<AAAA-MM>.parquet`),
então o que você inspecta é exatamente o que seria publicado. Arquivos já
gerados são pulados; use `--force` para refazer, ou `--sample-dir` para gravar
em outro lugar.

**Dry run é o padrão.** Sem `--push` o script apenas lista o que subiria e o que
seria pulado. Publicar exige um token de escrita, seja em `HF_TOKEN` seja
guardado em disco por `hf auth login`.

Flags úteis: `--familia` e `--periodo` (repetíveis) para restringir o escopo,
`--limite N` para uma primeira carga parcial, `--force` para reenviar mesmo sem
mudança, `--create-repo` para criar o repositório no Hub na primeira vez e
`--commit-size` para ajustar quantos arquivos entram em cada commit.

Os arquivos sobem agrupados: um commit a cada `--commit-size` arquivos (padrão
25) ou a cada 2 GB, o que vier primeiro, e sempre fechando no fim de cada
família. Um commit por arquivo faria a primeira carga render umas 300 revisões
no Hub, arriscando o rate limit no meio do caminho.

O `README.md` do dataset é gerado, mas **só é escrito quando ainda não existe**
no Hub — assim uma edição feita pela interface web não é sobrescrita a cada
execução. Para regerá-lo de propósito, use `--update-card`.

### Layout publicado

```
data/<família>/<AAAA-MM>.parquet
manifest.json
README.md
```

Um arquivo por mês, por família — é o que torna o reenvio incremental possível:
se só um mês mudou na origem, só ele sobe. O `README.md` é gerado com um config
do viewer por família.

### Como o script evita reenviar o que não mudou

O `manifest.json` guarda, para cada arquivo publicado, o **SHA256 do arquivo de
origem** de que ele foi gerado — o mesmo hash que a biblioteca já calcula para o
cache local. Um mês só é reconvertido quando esse hash muda (o portal troca o
conteúdo sem trocar o nome), quando a receita de conversão muda, ou com
`--force`.

O hash é o do arquivo **de origem**, e não o do Parquet, de propósito: Parquet
não é byte-reproduzível entre versões do pyarrow, então comparar o resultado
faria todo mês parecer alterado a cada execução.

Como o portal não publica checksum, saber se um mês mudou exige ter o arquivo
de origem em mãos. O manifesto poupa a parte cara — leitura, conversão e upload
—, não o download. Para não transformar um simples `--push`-less em dezenas de
GB de tráfego, o dry run **não baixa nada**: meses ainda ausentes do cache
aparecem como `fonte ainda nao baixada`.

### Tipos no Parquet

As colunas saem como texto, pelo mesmo motivo descrito em
[Tipos das colunas](#tipos-das-colunas). A exceção é `periodo_referencia`, que
sai como string `AAAA-MM` em vez de `pandas.Period`: o Parquet guardaria o
`Period` como um tipo de extensão do pandas sobre o ordinal do mês (`2024-06`
vira `653`), e todo leitor que não fosse pandas — o viewer do Hugging Face,
DuckDB, polars — mostraria um inteiro sem sentido.

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
