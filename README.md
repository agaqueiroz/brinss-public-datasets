# brinss-public-datasets

Carregamento (com download e cache automáticos) dos datasets abertos do INSS
publicados em [dadosabertos.inss.gov.br](https://dadosabertos.inss.gov.br),
no estilo `load_iris()` do scikit-learn.

Os dados podem vir do portal do INSS ou do espelho em Parquet no Hugging Face
gerado por este mesmo repositório — que é o padrão, por ser muito mais rápido e
leve. Veja [Fonte dos dados](#fonte-dos-dados).

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
    source="hf",            # "hf": espelho Parquet (padrão) | "inss": portal dadosabertos.inss.gov.br
    force_download=False,   # ignora o cache local e baixa de novo
    force_refresh=False,    # ignora o cache (24h) do catálogo de períodos disponíveis
    cache_dir=None,         # sobrescreve o diretório de cache para esta chamada
)
```

## Fonte dos dados

O parâmetro `source` escolhe de onde os arquivos são baixados:

```python
df = load_beneficios_concedidos(periodo="2024-06")                 # espelho no Hugging Face (padrão)
df = load_beneficios_concedidos(periodo="2024-06", source="inss")  # portal do INSS
```

| | `source="hf"` (padrão) | `source="inss"` |
| --- | --- | --- |
| Origem | [espelho em Parquet no Hugging Face](https://huggingface.co/datasets/agaqueiroz/brinss-public-datasets) | [dadosabertos.inss.gov.br](https://dadosabertos.inss.gov.br) |
| Formato | Parquet zstd, um arquivo por mês | XLSX, ou ZIP com CSV dentro |
| Download | 7,7 MB | 68,6 MB |
| Leitura | 3,3 s | 101,4 s |
| `columns=[...]` | lido só o que foi pedido, direto do arquivo | arquivo inteiro lido, colunas descartadas depois |
| Atualidade | pode ficar um ciclo de publicação atrás do portal | sempre o mais recente |
| Disponibilidade | não depende da CKAN estar de pé | depende |

Os números de download e leitura são de `beneficios_concedidos` em junho/2024
(628.457 linhas × 24 colunas), medidos numa mesma máquina — cerca de **9× menos
bytes e 30× menos tempo**. A diferença cresce nas famílias pesadas, onde a
alternativa é descompactar gigabytes de CSV.

**As duas fontes entregam o mesmo DataFrame.** Mesmos nomes de coluna (incluindo
os sufixos de nomes repetidos, `APS` e `APS.1`), mesma coluna
`periodo_referencia` como `pandas.Period`, mesmo `dtype="str"` por padrão. Não é
coincidência: o Parquet do espelho é gerado por esta mesma biblioteca lendo o
arquivo do portal (veja
[Publicação em Parquet no Hugging Face](#publicação-em-parquet-no-hugging-face)),
então a linha de título das planilhas, a escolha do membro do ZIP e a detecção
de encoding já vêm resolvidas de lá — resolvidas uma vez, e não a cada leitura.
Há um teste de rede que carrega o mesmo mês pelas duas fontes e compara os
DataFrames.

Vale usar `source="inss"` quando:

- o mês acabou de ser publicado no portal e ainda não subiu para o espelho —
  como o espelho é reconstruído a partir do portal, ele fica para trás por até
  um ciclo de publicação. Nesse caso o período aparece como indisponível na
  fonte `hf`, e a mensagem de erro lembra de tentar a outra;
- você quer auditar o espelho contra a origem oficial.

O espelho cobre hoje as 8 famílias, de junho/2023 a julho/2026. Para ver o que
cada fonte tem, `list_periods` também aceita `source`:

```python
from brinss.datasets import list_periods

list_periods("beneficios_concedidos")                  # meses no espelho
list_periods("beneficios_concedidos", source="inss")   # meses no portal
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

As duas fontes dividem o mesmo diretório, sem colidir: o Parquet do espelho e o
XLSX do portal são arquivos distintos, cada um com sua linha no registro de
hashes. Carregar o mesmo mês pelas duas fontes deixa as duas cópias em disco.

O portal não publica checksum dos arquivos; no primeiro download o SHA256 é
calculado e guardado localmente, e passa a ser conferido nas chamadas
seguintes (detectando automaticamente se o governo trocar o conteúdo de um
arquivo sem trocar o nome).

O catálogo de períodos disponíveis também fica em cache por 24h, nas duas
fontes: a resposta da CKAN na fonte `inss`, o `manifest.json` na fonte `hf`.
`force_refresh=True` ignora esse cache. Se a fonte estiver fora do ar e houver
cache local, ele é usado com um aviso, em vez de a chamada falhar.

### Formato dos arquivos

Esta seção é sobre a fonte `inss`. Na fonte `hf` nada disso se aplica: é sempre
Parquet, com nomes de coluna e encoding gravados dentro do próprio arquivo. A
biblioteca reconhece o formato pelos primeiros bytes do arquivo baixado, então
nem precisa saber de qual fonte ele veio.

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

Na fonte `hf` as colunas já estão gravadas como texto no Parquet, então
`dtype="infer"` é aplicado depois da leitura, coluna a coluna: a que converte
inteira para número vira número, a que não converte fica texto. O resultado bate
com o da fonte `inss` nestes datasets — cujas colunas são número ou texto livre
— e perde os zeros à esquerda do mesmo jeito, que é justamente o que
`dtype="infer"` significa.

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

Alguns meses descompactam para vários gigabytes. Na fonte `inss`, tamanhos por
mês medidos em maio/2026: `beneficios_mantidos_ativos` ~891 MB,
`beneficios_mantidos_cessados` ~786 MB e `beneficios_emitidos` ~763 MB
compactados — um único mês de `beneficios_emitidos` chega a ~10 GB
descompactado. `beneficios_mantidos_suspensos`, em contraste, é leve
(~5 MB/mês).

Na fonte padrão o download encolhe bastante (`beneficios_mantidos_ativos` fica
em ~385 MB/mês em Parquet) e a leitura deixa de passar por descompactar CSV de
gigabytes. O DataFrame resultante ocupa a mesma RAM, então o cuidado abaixo
continua valendo; o que muda é o custo de chegar até ele. `columns=[...]` ajuda
mais aqui do que na fonte `inss`: no Parquet as colunas descartadas não chegam a
ser lidas.

`periodo="all"` ou intervalos grandes nas famílias pesadas podem exigir bastante
RAM e espaço em disco. Nenhum limite é aplicado
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
[huggingface.co/datasets/agaqueiroz/brinss-public-datasets](https://huggingface.co/datasets/agaqueiroz/brinss-public-datasets)
— o espelho que a biblioteca lê por padrão. Ele vive em `scripts/`, fora de
`src/brinss`, e portanto **não faz parte do pacote distribuído** — usa a própria
biblioteca para ler os arquivos, herdando de graça o tratamento de banner,
membros de ZIP e encoding. Ele sempre lê da fonte `inss`, claro: converter o
espelho de volta para ele mesmo publicaria cópia de cópia.

O layout do repositório (`data/<família>/<AAAA-MM>.parquet`, `manifest.json`)
está em `src/brinss/datasets/_hf.py`, importado tanto pelo script que escreve
quanto pelo código que lê. Quem escreve e quem lê não podem divergir sobre onde
um arquivo mora.

```bash
uv run --group publish python scripts/publish_to_hf.py            # mostra o plano
uv run --group publish python scripts/publish_to_hf.py --push     # publica
```

**Dry run é o padrão.** Sem `--push` o script apenas lista o que subiria e o que
seria pulado. Publicar exige um token de escrita, seja em `HF_TOKEN` seja
guardado em disco por `hf auth login`.

### Cache local dos Parquet

Converter é a metade cara de uma execução: ler um XLSX de centenas de MB com o
openpyxl leva minutos. Os arquivos convertidos ficam em `tmp/` (ignorada pelo
git), no mesmo layout do Hub, ao lado de um `tmp/build-index.json` que registra
de qual origem cada um saiu:

```
tmp/data/<família>/<AAAA-MM>.parquet
tmp/build-index.json
```

Uma execução interrompida **retoma** daí em vez de recomeçar. Um Parquet só é
reaproveitado quando o índice confere em três pontos: SHA256 da origem, receita
de conversão e tamanho em bytes. O tamanho é o que descarta um arquivo truncado
por queda de energia, cujo registro no índice está íntegro mas cujo conteúdo não.

O índice **não** é o manifesto. O manifesto responde "isto está publicado no
Hub"; o índice responde "isto já foi convertido aqui". Confundir os dois foi
justamente o que estragou uma carga anterior.

⚠️ **O cache cresce.** São 291 arquivos numa carga completa, e as famílias
pesadas têm meses de vários GB. O resumo de cada execução mostra quanto `tmp/`
está ocupando e avisa acima de 5 GB. Para liberar espaço sem perder trabalho
útil, `--prune-parquet` apaga só os arquivos que o manifesto confirma publicados:

```bash
uv run --group publish python scripts/publish_to_hf.py --prune-parquet
```

### Gerar amostras locais

`--sample` converte **só o que já está no cache de downloads**, gravando no
mesmo `tmp/`:

```bash
uv run --group publish python scripts/publish_to_hf.py --sample
```

Ele nunca baixa nada e nunca fala com o Hub — meses ausentes do cache são
reportados e pulados. É a forma barata de conferir o resultado real antes de
encarar uma publicação completa, que são **291 arquivos** e dezenas de GB vindos
do portal. Como grava no mesmo cache, também serve para **aquecer** um `--push`
posterior: o que o `--sample` já converteu não é convertido de novo.

Use `--parquet-dir` para gravar em outro lugar (`--sample-dir` continua valendo
como apelido) e `--force` para reconverter o que já está em cache.

### Log

Cada execução grava um log em `logs/publish-AAAAMMDD-HHMMSS.log` (também
ignorada pelo git), em nível DEBUG, com os downloads e leituras da biblioteca
misturados aos passos do script. O console fica em INFO; `-v` mostra o DEBUG
nele também, `--no-log-file` desliga o arquivo e `--log-dir`/`--log-file`
mudam o destino.

O log abre com o cabeçalho da execução (modo, repositório, famílias, períodos,
receita de conversão, tamanho do manifesto) e fecha com um bloco de resumo:
status explícito — `CONCLUIDO COM SUCESSO`, `CONCLUIDO COM FALHAS` ou
`INTERROMPIDO` —, contagens, bytes convertidos e reaproveitados, commits,
duração, **em que mês parou** e o erro de cada falha.

A linha `iniciando <família>/<período>` é gravada *antes* do trabalho, não
depois, e com `fsync`. É o que permite descobrir em que mês a máquina desligou:
o último mês concluído não interessa, o que estava em andamento sim.

Um mês que estoura (XLSX corrompido, por exemplo) é registrado e a execução
**segue para o próximo**, terminando com código de saída 1. Códigos: `0` sucesso,
`1` falhas parciais, `2` erro de uso.

### Flags úteis

`--familia` e `--periodo` (repetíveis) para restringir o escopo, `--limite N`
para uma primeira carga parcial, `--force` para reenviar mesmo sem mudança,
`--create-repo` para criar o repositório no Hub na primeira vez e
`--commit-size` para ajustar quantos arquivos entram em cada commit.

### Layout publicado

```
data/<família>/<AAAA-MM>.parquet
manifest.json
README.md
```

Um arquivo por mês, por família — é o que torna o reenvio incremental possível:
se só um mês mudou na origem, só ele sobe. O `README.md` é gerado com um config
do viewer por família, mas **só é escrito quando ainda não existe** no Hub, para
não sobrescrever uma edição feita pela interface web. Para regerá-lo de
propósito, use `--update-card`.

### Dados e manifesto no mesmo commit

Os arquivos sobem agrupados: um commit a cada `--commit-size` arquivos (padrão
25) ou a cada 2 GB, o que vier primeiro, e sempre fechando no fim de cada
família. Um commit por arquivo faria a primeira carga render umas 300 revisões
no Hub, arriscando o rate limit no meio do caminho.

**O `manifest.json` viaja no mesmo commit dos arquivos que ele descreve.** Ele
era enviado só no fim da execução, o que abria uma janela — de minutos, numa
carga completa — em que o Hub tinha arquivos que o manifesto desconhecia. Não é
hipotético: um desligamento abrupto da máquina dentro dessa janela deixou 25
meses publicados e não registrados, e toda execução seguinte os reconvertia e
reenviava como `novo`. Com os dois no mesmo commit esse estado é inalcançável.

Um manifesto ilegível é **erro fatal**, nunca um manifesto vazio: tratá-lo como
vazio reclassificaria os 291 meses como `novo` e reenviaria o dataset inteiro.

### Reconciliar arquivos órfãos

Sempre que fala com o Hub, o script compara a listagem do repositório com o
manifesto e avisa sobre **órfãos** — Parquet publicados sem entrada no manifesto,
que seriam reenviados como `novo` para sempre. Com o commit atômico isso não
deve mais acontecer; quando acontecer, o log conta.

`--reconciliar` adota esses arquivos no manifesto sem reconverter nem reenviar:

```bash
uv run --group publish python scripts/publish_to_hf.py --reconciliar          # lista
uv run --group publish python scripts/publish_to_hf.py --reconciliar --push   # grava
```

O SHA da origem vem, nessa ordem, do `registry.json` do cache de downloads (que
guarda o hash mesmo depois do arquivo de origem ser apagado), do arquivo em
cache, ou de um download. A entrada nasce com `rows: null` e `adopted: true` — a
contagem de linhas exigiria reler a origem, que é exatamente o custo que a
reconciliação existe para evitar.

**A ressalva:** reconciliar assume que o Parquet publicado foi gerado da origem
que está no portal *agora*. Se o portal trocou o arquivo entre o envio e a
reconciliação, a entrada nasce errada e aquele mês nunca mais se atualiza
sozinho. O `adopted: true` marca esses casos para uma auditoria futura com
`--force`.

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

Ao carregar pela fonte `hf`, a biblioteca descarta essa coluna de texto e insere
de volta o `pandas.Period` — as duas fontes devolvem a mesma coluna, do mesmo
tipo, na mesma posição. Quem lê o Parquet direto (DuckDB, polars, o viewer do
Hub) continua vendo a string legível.

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
- [ ] Rodar os testes de rede (`pytest -m network`) periodicamente no CI, num
      workflow agendado que abra issue ao falhar. Eles ficam fora do CI comum de
      propósito — dependem do portal e do espelho estarem de pé, e um deles baixa
      dados de verdade —, mas isso significa que hoje **nada avisa** quando o INSS
      muda o layout de um arquivo ou quando as duas fontes deixam de concordar.
- [ ] Automatizar a atualização do espelho no Hugging Face. Hoje
      `scripts/publish_to_hf.py` é rodado à mão, então um mês novo no portal só
      chega à fonte `hf` quando alguém lembra de rodá-lo — é a causa do atraso
      descrito em [Fonte dos dados](#fonte-dos-dados).
- [ ] Expor leitura em streaming na API pública. `open_resource_chunks` já existe
      e é o que permite ao script de publicação converter arquivos de dezenas de
      GB, mas quem chama `load_*` ainda recebe o mês inteiro de uma vez —
      `periodo="all"` nas famílias pesadas continua limitado pela RAM.
- [ ] Medir cobertura de testes: `pytest-cov` está no grupo `dev`, mas nenhum
      comando o usa e não há mínimo configurado.
- [ ] Refinar `dtype="infer"` na fonte `hf`. A conversão pós-leitura só tenta
      número, então uma coluna que o `read_csv` inferiria como booleano ou data
      fica como texto. Não afeta os datasets publicados hoje, mas é uma diferença
      real entre as duas fontes.
- [ ] Manter um CHANGELOG. As notas de release saem dos commits, o que serve para
      acompanhar o desenvolvimento, mas não conta o que muda para quem apenas usa
      a biblioteca.

## Desenvolvimento

```bash
uv sync
uv run pytest              # suíte completa (sem os testes que precisam de rede)
uv run pytest -m network   # inclui os testes que baixam dados de verdade
uv run ruff check .
```

Os testes marcados com `network` são desmarcados por padrão (`addopts` no
`pyproject.toml`). São três: um download real de cada fonte e uma comparação do
mesmo mês pelas duas, conferindo que entregam DataFrames idênticos.
