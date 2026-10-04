"""API HTTP do assistente (FastAPI + Runner do ADK)."""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, Session
from google.genai import types
from pydantic import BaseModel

from . import db
from .agents import app as adk_app
from .config import APP_NAME, CONDOMINIO_DB, SESSOES_DB_URL, USER_ID

CONFIRMACAO = "adk_request_confirmation"
logger = logging.getLogger("residencial_aurora")

# Garantia 3: sessões e eventos persistidos em SQLite.
session_service = DatabaseSessionService(db_url=SESSOES_DB_URL)
runner = Runner(app=adk_app, session_service=session_service)

# Uma execução por vez em cada sessão: evita que duas respostas para a mesma
# confirmação passem juntas pela checagem de pendência.
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def lifespan(_: FastAPI):
    primeira_vez = not CONDOMINIO_DB.exists()
    db.criar_schema()
    if primeira_vez:
        db.restaurar()
    yield
    await runner.close()


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


# --- Apoio -------------------------------------------------------------------


async def _sessao(session_id: str) -> Session:
    sessao = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(404, "sessão não encontrada")
    return sessao


def confirmacoes_pendentes(sessao: Session) -> list[dict]:
    """Garantia 1: pendências derivadas dos eventos persistidos da sessão.

    Uma confirmação está pendente quando existe a chamada
    adk_request_confirmation e ainda não existe a resposta com o mesmo id.
    """
    pedidas: dict[str, dict] = {}
    respondidas: set[str] = set()
    for evento in sessao.events:
        for chamada in evento.get_function_calls():
            if chamada.name == CONFIRMACAO:
                pedidas[chamada.id] = chamada.args or {}
        for resposta in evento.get_function_responses():
            if resposta.name == CONFIRMACAO:
                respondidas.add(resposta.id)

    pendentes = []
    for id_, args in pedidas.items():
        if id_ in respondidas:
            continue
        payload = dict((args.get("toolConfirmation") or {}).get("payload") or {})
        acao = payload.pop("acao", None) or args.get("originalFunctionCall", {}).get("name", "")
        pendentes.append({"id": id_, "acao": acao, "detalhes": payload})
    return pendentes


async def _executar(session_id: str, mensagem: types.Content) -> dict:
    textos = []
    # Tools com skip_summarization encerram o turno sem texto do modelo; a
    # mensagem final vem no próprio resultado da tool.
    mensagens_de_tool = []
    try:
        async for evento in runner.run_async(
            user_id=USER_ID, session_id=session_id, new_message=mensagem
        ):
            if evento.author == "user" or evento.partial or not evento.content:
                continue
            for parte in evento.content.parts or []:
                if parte.text and not parte.thought:
                    textos.append(parte.text)
                resposta = parte.function_response
                if resposta and evento.actions.skip_summarization and resposta.response:
                    final = resposta.response.get("mensagem") or resposta.response.get("result")
                    if isinstance(final, str):
                        mensagens_de_tool.append(final)
    except Exception:
        # Falha do modelo (cota, indisponibilidade) depois que as tools já
        # rodaram: o efeito gravado vale e as pendências continuam corretas,
        # então a resposta segue o contrato em vez de virar erro de servidor.
        logger.exception("falha ao executar o agente na sessão %s", session_id)
        textos.append(
            "\n\nNão consegui concluir a resposta agora. Tente novamente em instantes."
        )
    if not "".join(textos).strip():
        textos = mensagens_de_tool
    sessao = await _sessao(session_id)
    return {
        "resposta": "".join(textos).strip(),
        "confirmacoes_pendentes": confirmacoes_pendentes(sessao),
    }


# --- Conversa ----------------------------------------------------------------


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    if corpo.apartamento not in db.apartamentos():
        raise HTTPException(422, "apartamento inexistente")
    # Garantia 2: o apartamento é gravado uma única vez, pelo código, no state
    # da sessão. As tools leem dali; nenhuma tool recebe apartamento.
    sessao = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, state={"apartamento": corpo.apartamento}
    )
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem):
    async with _locks[session_id]:
        await _sessao(session_id)
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, mensagem)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    async with _locks[session_id]:
        sessao = await _sessao(session_id)
        # Garantia 1: só passa para o Runner um id pendente nesta sessão. Sem
        # esta checagem o ADK reexecuta a tool quando o mesmo id é reenviado.
        if corpo.id not in {p["id"] for p in confirmacoes_pendentes(sessao)}:
            raise HTTPException(409, "não existe confirmação pendente com esse id nesta sessão")
        mensagem = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id, name=CONFIRMACAO, response={"confirmed": corpo.confirmado}
                    )
                )
            ],
        )
        return await _executar(session_id, mensagem)


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    sessao = await _sessao(session_id)
    return [e.model_dump(mode="json", exclude_none=True) for e in sessao.events]


# --- Verificação (leitura direta, sem modelo) --------------------------------


@api.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return db.reservas_do_apartamento(numero)


@api.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return db.visitantes_do_apartamento(numero)


def main() -> None:
    uvicorn.run(api, host="127.0.0.1", port=8000)
