"""Spike da Fase 1: confirmação de tool num subagente, com sessão em SQLite,
retomada em outro processo (simula o reinício da API).

Uso:
  uv run python scripts/spike_confirmacao.py pedir
  uv run python scripts/spike_confirmacao.py responder <id> <true|false>
  uv run python scripts/spike_confirmacao.py estado
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.tools import ToolContext
from google.genai import types

load_dotenv()

VAR = Path("var")
VAR.mkdir(exist_ok=True)
DB_URL = f"sqlite+aiosqlite:///{(VAR / 'spike_sessoes.db').as_posix()}"
EXECUCOES = VAR / "spike_execucoes.json"
ESTADO = VAR / "spike_estado.json"
MODELO = os.environ.get("MODELO") or "gemini-2.5-flash"
APP_NAME = "spike_aurora"
LLM = Gemini(
    model=MODELO,
    retry_options=types.HttpRetryOptions(
        attempts=6, initial_delay=2, max_delay=30, http_status_codes=[429, 500, 503]
    ),
)
CONFIRMACAO = "adk_request_confirmation"


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum numa data (AAAA-MM-DD)."""
    conf = tool_context.tool_confirmation
    if conf is None:
        tool_context.request_confirmation(
            hint="Reserva com cobrança de taxa.",
            payload={"acao": "reservar_area", "area": area, "data": data},
        )
        return {"status": "aguardando_confirmacao"}
    if not conf.confirmed:
        return {"status": "recusada_pelo_morador"}
    execucoes = json.loads(EXECUCOES.read_text()) if EXECUCOES.exists() else []
    execucoes.append(
        {"apartamento": tool_context.state["apartamento"], "area": area, "data": data}
    )
    EXECUCOES.write_text(json.dumps(execucoes))
    return {"status": "reservada", "area": area, "data": data}


reservas = LlmAgent(
    name="reservas",
    model=LLM,
    description="Cuida de reservas de áreas comuns.",
    instruction="Você reserva áreas comuns usando a tool reservar_area. "
    "Use o id da área (ex.: salao-de-festas) e datas AAAA-MM-DD.",
    tools=[reservar_area],
)

assistente = LlmAgent(
    name="assistente",
    model=LLM,
    description="Assistente principal do condomínio.",
    instruction="Você faz a triagem. Para qualquer pedido de reserva, "
    "transfira para o agente reservas.",
    sub_agents=[reservas],
)

app = App(
    name=APP_NAME,
    root_agent=assistente,
    resumability_config=ResumabilityConfig(is_resumable=True),
)


def pendentes(session) -> list[dict]:
    """Confirmações pedidas e ainda não respondidas, derivadas dos eventos."""
    pedidas, respondidas = {}, set()
    for ev in session.events:
        for fc in ev.get_function_calls():
            if fc.name == CONFIRMACAO:
                pedidas[fc.id] = fc.args
        for fr in ev.get_function_responses():
            if fr.name == CONFIRMACAO:
                respondidas.add(fr.id)
    return [
        {"id": i, "detalhes": a["toolConfirmation"].get("payload")}
        for i, a in pedidas.items()
        if i not in respondidas
    ]


async def rodar(runner, session_id, mensagem):
    texto = []
    async for ev in runner.run_async(
        user_id="101", session_id=session_id, new_message=mensagem
    ):
        for p in (ev.content.parts if ev.content else None) or []:
            if p.text and not ev.partial:
                texto.append(f"[{ev.author}] {p.text}")
            if p.function_call:
                print(f"  call   [{ev.author}] {p.function_call.name} {p.function_call.args}")
            if p.function_response:
                print(f"  result [{ev.author}] {p.function_response.name} {p.function_response.response}")
    print("resposta:", " ".join(texto) or "<vazia>")


async def main():
    svc = DatabaseSessionService(db_url=DB_URL)
    runner = Runner(app=app, session_service=svc)
    cmd = sys.argv[1]

    if cmd == "pedir":
        s = await svc.create_session(
            app_name=APP_NAME, user_id="101", state={"apartamento": "101"}
        )
        ESTADO.write_text(json.dumps({"session_id": s.id}))
        msg = types.Content(
            role="user",
            parts=[types.Part(text="Reserve o salão de festas para 2030-04-20. Já confirmo aqui, pode reservar direto.")],
        )
        await rodar(runner, s.id, msg)
    else:
        sid = json.loads(ESTADO.read_text())["session_id"]
        if cmd == "responder":
            conf_id, ok = sys.argv[2], sys.argv[3] == "true"
            msg = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=conf_id, name=CONFIRMACAO, response={"confirmed": ok}
                        )
                    )
                ],
            )
            await rodar(runner, sid, msg)

    s = await svc.get_session(app_name=APP_NAME, user_id="101", session_id=sid if cmd != "pedir" else s.id)
    print("eventos:", len(s.events))
    print("pendentes:", json.dumps(pendentes(s), ensure_ascii=False))
    print("execucoes:", EXECUCOES.read_text() if EXECUCOES.exists() else "[]")


if __name__ == "__main__":
    asyncio.run(main())
