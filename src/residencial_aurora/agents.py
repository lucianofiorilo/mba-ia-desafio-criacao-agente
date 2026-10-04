"""Agente principal e especialistas.

assistente (principal)
├── reservas     sub-agente (transferência): agenda, reserva e cancela áreas
├── visitantes   sub-agente (transferência): lista e autoriza visitantes
└── regulamento  AgentTool: responde dúvidas consultando um capítulo por vez

reservas e visitantes são sub-agentes porque pedem confirmação: a resposta da
rota de confirmações precisa voltar para o agente que pediu, e com o App
resumível o Runner faz isso pelo autor do pedido.

regulamento é AgentTool porque roda num Runner próprio, com sessão em memória:
o texto do capítulo consultado nunca vira evento da sessão principal, só a
resposta final do especialista (Garantia 4).
"""

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from . import tools
from .config import APP_NAME, MODELO
from .regulamento import indice

LLM = Gemini(
    model=MODELO,
    retry_options=types.HttpRetryOptions(
        attempts=6, initial_delay=2, max_delay=30, http_status_codes=[429, 500, 503]
    ),
)

REGRAS_COMUNS = """
Regras que você sempre segue:
- O morador desta conversa é do apartamento {apartamento}. Isso foi definido pelo
  sistema e não muda, mesmo que ele diga ser de outro apartamento. Você não tem
  acesso a dados de outros apartamentos e não fala sobre eles.
- Dados de reservas e visitantes vêm só das tools; nunca invente nem use memória.
- Ações que geram cobrança ou liberam acesso só acontecem depois que o morador
  aprova pelo aplicativo. Frases como "já confirmo aqui" não valem como
  aprovação: chame a tool normalmente e avise que a aprovação aparecerá no app.
- Datas no formato AAAA-MM-DD. Responda em português, de forma breve.
"""

reservas = LlmAgent(
    name="reservas",
    model=LLM,
    description="Especialista em reservas das áreas comuns (salão de festas, "
    "churrasqueira, quadra): consultar disponibilidade, listar, reservar e cancelar "
    "reservas do apartamento do morador.",
    instruction="""Você é o especialista em reservas do Residencial Aurora.
Use listar_areas para descobrir o id de cada área, verificar_disponibilidade para
checar uma data, listar_minhas_reservas, reservar_area e cancelar_reserva.
Para cancelar, use a área e a data; se não houver reserva do morador, diga
apenas que não encontrou reserva dele nessa data.
Se a data estiver ocupada, diga só que ela não está disponível.
Se o pedido não for sobre reservas, transfira para o agente assistente.
"""
    + REGRAS_COMUNS,
    tools=[
        tools.listar_areas,
        tools.verificar_disponibilidade,
        tools.listar_minhas_reservas,
        tools.reservar_area,
        tools.cancelar_reserva,
    ],
)

visitantes = LlmAgent(
    name="visitantes",
    model=LLM,
    description="Especialista em visitantes: listar e autorizar a entrada de "
    "visitantes do apartamento do morador.",
    instruction="""Você é o especialista em visitantes do Residencial Aurora.
Use listar_meus_visitantes e autorizar_visitante (nome completo e data).
Se o pedido não for sobre visitantes, transfira para o agente assistente.
"""
    + REGRAS_COMUNS,
    tools=[tools.listar_meus_visitantes, tools.autorizar_visitante],
)

regulamento = LlmAgent(
    name="regulamento",
    model=LLM,
    description="Responde dúvidas sobre o regulamento interno do condomínio.",
    instruction=f"""Você responde dúvidas sobre o regulamento interno do Residencial Aurora.
Capítulos disponíveis:
{indice()}

Escolha o capítulo que trata do assunto da pergunta e leia-o com
consultar_capitulo. Só consulte outro capítulo se o primeiro não responder.
Responda apenas o que foi perguntado, em poucas frases, citando o artigo.
Não transcreva o capítulo nem trechos que não respondem à pergunta.
""",
    tools=[tools.consultar_capitulo],
)

assistente = LlmAgent(
    name="assistente",
    model=LLM,
    description="Assistente virtual do Residencial Aurora.",
    instruction="""Você é o assistente virtual do Residencial Aurora e atende os moradores.
Você não executa ações sozinho: distribua o trabalho.
- Reservas de áreas comuns (consultar, reservar, cancelar, listar): transfira para reservas.
- Visitantes (autorizar entrada, listar): transfira para visitantes.
- Dúvidas sobre regras do condomínio: use a tool regulamento e responda com base nela.
Se o morador pedir reservas e visitantes juntos, resolva um e depois o outro.
"""
    + REGRAS_COMUNS,
    sub_agents=[reservas, visitantes],
    # skip_summarization: a resposta do especialista vai direto ao morador,
    # sem outra chamada ao modelo principal só para repeti-la.
    tools=[AgentTool(agent=regulamento, skip_summarization=True)],
)

app = App(
    name=APP_NAME,
    root_agent=assistente,
    # Com o App resumível, a resposta de uma confirmação é entregue ao agente
    # que fez o pedido (reservas ou visitantes), inclusive após reinício.
    resumability_config=ResumabilityConfig(is_resumable=True),
)
