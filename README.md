# Residencial Aurora: assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora. Pelo chat, o morador reserva e cancela áreas comuns, autoriza visitantes e tira dúvidas sobre o regulamento. O modelo conduz a conversa, mas as regras críticas ficam no código: nenhuma mensagem consegue furá-las.

- Python 3.12, uv e `google-adk[db]==2.9.1` (versão exata fixada em `pyproject.toml`)
- Modelos Gemini via Google AI Studio (padrão: `gemini-3.5-flash`)
- API FastAPI em `http://localhost:8000`
- Armazenamento em SQLite, sem serviço externo: `var/condominio.db` (reservas e visitantes) e `var/sessoes.db` (sessões e eventos do ADK)

```
src/residencial_aurora/
├── config.py       caminhos, nomes e modelo
├── db.py           SQLite: schema, leitura, gravação e restauração
├── tools.py        tools dos especialistas, onde moram as regras
├── regulamento.py  divide o regulamento em capítulos
├── agents.py       agente principal, especialistas e App do ADK
├── api.py          rotas HTTP, Runner e sessões persistidas
└── restaurar.py    comando de restauração dos dados
scripts/
├── fluxo_avaliador.py   roda os passos 1 a 14 do fluxo do avaliador contra a API
└── spike_confirmacao.py teste de viabilidade da confirmação com sessão em SQLite
```

## Arquitetura

```
assistente (agente principal)
├── reservas     subagente, acionado por transferência
├── visitantes   subagente, acionado por transferência
└── regulamento  AgentTool, chamado como tool
```

| Agente | Responsabilidade | Como é acionado | Por quê |
| --- | --- | --- | --- |
| `assistente` | Recebe o morador e distribui o trabalho. Não tem tools de dados nem o regulamento nas instruções. | Raiz do `App`, chamado pelo `Runner` a cada mensagem | Um ponto de entrada único, com instruções curtas e sempre baratas |
| `reservas` | Lista áreas, confere disponibilidade, lista, reserva e cancela reservas do apartamento da sessão | Subagente (`sub_agents`): o principal transfere a conversa | Reservar área com taxa pede confirmação. Com o App resumível, o Runner devolve a resposta da confirmação ao agente que fez o pedido, por isso quem pede precisa ser um agente da árvore, não um AgentTool |
| `visitantes` | Lista e autoriza visitantes do apartamento da sessão | Subagente (`sub_agents`): o principal transfere a conversa | Mesmo motivo: toda autorização pede confirmação e precisa ser retomada no agente que pediu |
| `regulamento` | Responde dúvidas consultando um capítulo do regulamento por vez | `AgentTool` com `skip_summarization=True` | O AgentTool roda o especialista num Runner próprio, com sessão em memória. O texto do capítulo lido nunca vira evento da sessão principal: só a resposta final entra nela |

As definições estão em `src/residencial_aurora/agents.py`. Reservas e visitantes são lidos e gravados apenas pelas tools de `src/residencial_aurora/tools.py`, que acessam o SQLite por `src/residencial_aurora/db.py`.

### Como a confirmação funciona pela API

1. A tool chama `tool_context.request_confirmation(...)`. O ADK grava na sessão uma chamada `adk_request_confirmation` e a execução para.
2. `confirmacoes_pendentes()` em `api.py` lê os eventos persistidos: uma confirmação está pendente quando existe a chamada e ainda não existe a resposta com o mesmo `id`. Os detalhes saem do `payload` (área e data, ou nome e data).
3. `POST /sessoes/{id}/confirmacoes` confere que o `id` está pendente e envia ao `Runner` uma `FunctionResponse` chamada `adk_request_confirmation`, com o mesmo `id` e `{"confirmed": true|false}`.
4. Com `ResumabilityConfig(is_resumable=True)`, o Runner entrega a resposta ao subagente que pediu, que reexecuta a tool com `tool_context.tool_confirmation` preenchido. Isso também funciona depois de reiniciar a API.

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

| Onde | O quê |
| --- | --- |
| `tools.py`, `reservar_area` | Se `taxa > 0` e não há `tool_context.tool_confirmation`, chama `request_confirmation` com `payload={"acao", "area", "data", "taxa"}` e retorna sem gravar. Só chama `db.criar_reserva` quando a confirmação chega com `confirmed=True`. Área com taxa 0 grava direto, sem confirmação. |
| `tools.py`, `autorizar_visitante` | Sempre pede confirmação com `payload={"acao", "nome", "data"}`. Só chama `db.autorizar_visitante` com `confirmed=True`. |
| `api.py`, `confirmacoes_pendentes` | As pendências são calculadas pelos eventos gravados na sessão, não pelo que o modelo diz. |
| `api.py`, `responder_confirmacao` | Recusa com `409` qualquer `id` que não esteja pendente nesta sessão, antes de chamar o Runner. Sem essa checagem, o ADK reexecutaria a tool ao receber de novo um `id` já respondido. |
| `api.py`, `_locks` | Uma execução por vez em cada sessão: duas respostas à mesma confirmação não passam juntas pela checagem. |

Por que não depende do modelo: a única forma de produzir `tool_confirmation.confirmed=True` é a `FunctionResponse` montada por `responder_confirmacao`, e ela só é enviada para um `id` pendente. Se o morador escrever "já confirmo aqui", isso chega como texto e não preenche `tool_confirmation`, então a tool pede a confirmação de novo.

### Garantia 2: cada sessão pertence a um apartamento

| Onde | O quê |
| --- | --- |
| `api.py`, `criar_sessao` | O apartamento é gravado uma única vez, pelo código, em `state={"apartamento": ...}` na criação da sessão. |
| `tools.py`, `_apartamento` | Todas as tools que leem ou gravam dados de morador usam `tool_context.state["apartamento"]`. Nenhuma tool tem parâmetro de apartamento. |
| `tools.py`, `verificar_disponibilidade` e `INDISPONIVEL` | A agenda responde só `disponivel: true/false`, sem código nem apartamento. |
| `tools.py`, `cancelar_reserva` e `db.py`, `cancelar_reserva` | O `UPDATE` filtra por `apartamento = ?` da sessão. Exista ou não reserva de outro apartamento na data, a resposta é a mesma (`nao_encontrada`). |
| `db.py`, `area_ocupada` | Faz `SELECT 1` e não traz dono nem código. |

Por que não depende do modelo: o modelo só escolhe área, data e nome. O apartamento nunca é um argumento que ele preenche, e nenhuma tool devolve dados de outro apartamento. Mesmo que o morador diga ser do 302, não existe chamada de tool capaz de ler ou alterar os dados do 302.

### Garantia 3: nada se perde no reinício

| Onde | O quê |
| --- | --- |
| `api.py`, `session_service = DatabaseSessionService(...)` | Sessões, state e eventos ficam em `var/sessoes.db` (SQLite, endereço em `config.py`, `SESSOES_DB_URL`). |
| `agents.py`, `App(..., resumability_config=ResumabilityConfig(is_resumable=True))` | Uma confirmação pedida antes do reinício pode ser respondida depois dele. |
| `db.py`, `conectar` e `CONDOMINIO_DB` | Reservas e visitantes ficam em `var/condominio.db`. |
| `api.py`, `lifespan` | Na subida, só cria o schema. Os dados iniciais só são carregados se o banco ainda não existe, então reiniciar não apaga nada. |

### Garantia 4: o regulamento é consultado, não carregado

| Onde | O quê |
| --- | --- |
| `regulamento.py`, `capitulos` e `indice` | Divide `dados/regulamento.md` em 14 capítulos. O especialista recebe nas instruções só os títulos (`indice()`). |
| `tools.py`, `consultar_capitulo` | Devolve o texto de um único capítulo por chamada. |
| `agents.py`, `regulamento` | A instrução manda escolher o capítulo do assunto e responder sem transcrever trechos. |
| `agents.py`, `AgentTool(agent=regulamento, skip_summarization=True)` | O especialista roda em sessão própria, em memória: a chamada a `consultar_capitulo` e o texto do capítulo não entram nos eventos da sessão do morador. Só a resposta final do especialista é gravada. |
| `agents.py`, `assistente` | A instrução do agente principal não contém o regulamento nem o índice. |

### Garantia 5: dois moradores, uma reserva

| Onde | O quê |
| --- | --- |
| `db.py`, `SCHEMA`, `ux_reserva_ativa` | `CREATE UNIQUE INDEX ux_reserva_ativa ON reservas (area, data) WHERE status = 'ativa'`: o SQLite rejeita uma segunda reserva ativa na mesma área e data no instante do `INSERT`. |
| `db.py`, `criar_reserva` | Faz o `INSERT` sem conferência prévia. O `IntegrityError` do índice vira `None`, uma resposta normal. |
| `tools.py`, `reservar_area` | `None` vira `INDISPONIVEL` ("Essa área não está disponível nessa data"), devolvido com `200`, sem erro de servidor. |
| `db.py`, `reservas.codigo TEXT PRIMARY KEY` | Reservas canceladas continuam na tabela com `status = 'cancelada'`, então um código novo (`RSV-` + 8 hexadecimais aleatórios) nunca repete o de outra reserva, nem de uma cancelada. |

Por que não depende do modelo nem do tempo: a conferência em `reservar_area`, antes de pedir a confirmação, existe só para não pedir cobrança de uma data já ocupada. Quem decide de fato é o índice único, avaliado pelo SQLite na gravação. Em teste com 30 gravações simultâneas na mesma área e data, exatamente uma foi gravada.

## Como rodar

### Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (ele instala o Python 3.12 se for preciso)
- Uma chave de API do [Google AI Studio](https://aistudio.google.com/apikey)

Não há serviço externo para subir: os bancos são arquivos SQLite criados na pasta `var/`.

### Variáveis do `.env`

Copie o exemplo e preencha a chave:

```
cp .env.example .env
```

| Variável | Valor |
| --- | --- |
| `GOOGLE_API_KEY` | Sua chave do Google AI Studio |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` (usa o AI Studio, não o Vertex AI) |
| `MODELO` | Opcional. Modelo Gemini de todos os agentes. Vazio usa `gemini-3.5-flash` |

O fluxo completo do avaliador faz algumas dezenas de chamadas ao modelo. O plano gratuito limita as chamadas diárias por modelo, então pode faltar cota.

### Comandos

```
uv sync          # instala as dependências
uv run restaurar # volta reservas e visitantes ao estado de dados/
uv run api       # sobe a API em http://localhost:8000
```

`uv run restaurar` mantém as sessões. Para apagá-las também, rode `uv run restaurar --sessoes` com a API parada. Na primeira subida, a API cria `var/condominio.db` com os dados iniciais mesmo sem o comando de restauração.

### Fluxo do avaliador automatizado

Com a API parada:

```
uv run python scripts/fluxo_avaliador.py
```

O script restaura os dados, sobe a API, executa os passos 1 a 14 (incluindo o reinício no passo 13 e as duas aprovações simultâneas no passo 14) e termina com `TUDO OK` ou com a lista de falhas. O log da API fica em `var/api_fluxo.log`. Para dividir a cota gratuita entre dois modelos, use `--modelo <antes do reinício> --modelo-reinicio <depois do reinício>`.

`scripts/spike_confirmacao.py` é o teste de viabilidade da primeira fase: pede, aprova ou nega e retoma uma confirmação em processos separados, com sessão em SQLite. Ficou no repositório como registro de como a retomada foi validada.
