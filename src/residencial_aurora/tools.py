"""Tools dos especialistas. As regras do condomínio são aplicadas aqui.

- Garantia 2: nenhuma tool recebe apartamento. O apartamento vem sempre de
  `tool_context.state["apartamento"]`, gravado pela API na criação da sessão.
  Respostas sobre a agenda dizem só livre/ocupada, nunca de quem é a reserva.
- Garantia 1: reserva com taxa > 0 e autorização de visitante chamam
  `tool_context.request_confirmation` e só gravam quando a confirmação chega
  com `confirmed=True`, vinda da rota de confirmações.
"""

from datetime import date

from google.adk.tools import ToolContext

from . import db
from .regulamento import capitulos

INDISPONIVEL = {
    "status": "indisponivel",
    "mensagem": "Essa área não está disponível nessa data. Escolha outra data.",
}


def _apartamento(tool_context: ToolContext) -> str:
    # Gravado pela API em POST /sessoes; nenhuma tool altera esse valor.
    return tool_context.state["apartamento"]


def _validar_data(data: str) -> dict | None:
    try:
        date.fromisoformat(data)
    except (TypeError, ValueError):
        return {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}
    if len(data) != 10:
        return {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}
    return None


def _validar_area(area: str) -> dict | None:
    if area not in db.areas():
        return {
            "status": "erro",
            "mensagem": f"Área desconhecida. Use um destes ids: {', '.join(db.areas())}.",
        }
    return None


# --- Reservas ----------------------------------------------------------------


def listar_areas() -> list[dict]:
    """Lista as áreas comuns que podem ser reservadas, com id, nome e taxa em reais.

    Taxa 0 significa área sem cobrança.
    """
    return [
        {"id": a["id"], "nome": a["nome"], "taxa": a["taxa"]} for a in db.areas().values()
    ]


def listar_minhas_reservas(tool_context: ToolContext) -> list[dict]:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    return db.reservas_do_apartamento(_apartamento(tool_context))


def verificar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre numa data.

    Args:
        area: id da área (ex.: salao-de-festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.
    """
    erro = _validar_area(area) or _validar_data(data)
    if erro:
        return erro
    return {"area": area, "data": data, "disponivel": not db.area_ocupada(area, data)}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa geram cobrança e ficam pendentes até o morador aprovar pelo
    aplicativo; a confirmação não pode ser dada pela conversa.

    Args:
        area: id da área (ex.: salao-de-festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.
    """
    erro = _validar_area(area) or _validar_data(data)
    if erro:
        return erro
    apartamento = _apartamento(tool_context)
    taxa = db.areas()[area]["taxa"]

    if taxa > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            if db.area_ocupada(area, data):
                return INDISPONIVEL
            tool_context.request_confirmation(
                hint=f"Reserva de {db.areas()[area]['nome']} em {data} gera cobrança de R$ {taxa:.2f}.",
                payload={"acao": "reservar_area", "area": area, "data": data, "taxa": taxa},
            )
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "A reserva gera cobrança e só será feita depois que o "
                "morador aprovar pelo aplicativo.",
            }
        # Retomada pela rota de confirmações: o resultado já traz a mensagem
        # final, então não há outra chamada ao modelo só para redigi-la.
        tool_context.actions.skip_summarization = True
        if not confirmacao.confirmed:
            return {"status": "nao_realizada", "mensagem": "Cobrança recusada. Nenhuma reserva foi feita."}

    # Sem conferência prévia aqui: a exclusividade é decidida no INSERT.
    codigo = db.criar_reserva(apartamento, area, data)
    if codigo is None:
        return INDISPONIVEL
    nome = db.areas()[area]["nome"]
    return {
        "status": "reservada",
        "codigo": codigo,
        "area": area,
        "data": data,
        "taxa": taxa,
        "mensagem": f"Reserva confirmada: {nome} em {data}, código {codigo}.",
    }


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva do apartamento do morador desta conversa.

    Args:
        area: id da área (ex.: salao-de-festas, churrasqueira, quadra).
        data: data da reserva no formato AAAA-MM-DD.
    """
    erro = _validar_area(area) or _validar_data(data)
    if erro:
        return erro
    codigo = db.cancelar_reserva(_apartamento(tool_context), area, data)
    if codigo is None:
        # Mesma resposta exista ou não reserva de outro apartamento nessa data.
        return {
            "status": "nao_encontrada",
            "mensagem": "Não há reserva do apartamento do morador para essa área nessa data.",
        }
    return {"status": "cancelada", "codigo": codigo, "area": area, "data": data}


# --- Visitantes --------------------------------------------------------------


def listar_meus_visitantes(tool_context: ToolContext) -> list[dict]:
    """Lista os visitantes autorizados pelo apartamento do morador desta conversa."""
    return db.visitantes_do_apartamento(_apartamento(tool_context))


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio numa data.

    Sempre fica pendente até o morador aprovar pelo aplicativo; a confirmação
    não pode ser dada pela conversa.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    erro = _validar_data(data)
    if erro:
        return erro
    nome = " ".join(nome.split())
    if not nome:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} em {data}.",
            payload={"acao": "autorizar_visitante", "nome": nome, "data": data},
        )
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A autorização libera acesso ao prédio e só será feita depois "
            "que o morador aprovar pelo aplicativo.",
        }
    tool_context.actions.skip_summarization = True
    if not confirmacao.confirmed:
        return {"status": "nao_realizada", "mensagem": "Autorização recusada. Nenhum acesso foi liberado."}

    db.autorizar_visitante(_apartamento(tool_context), nome, data)
    return {
        "status": "autorizado",
        "nome": nome,
        "data": data,
        "mensagem": f"Entrada de {nome} autorizada para {data}.",
    }


# --- Regulamento -------------------------------------------------------------


def consultar_capitulo(numero: str) -> dict:
    """Devolve o texto de um único capítulo do regulamento.

    Args:
        numero: numeral romano do capítulo (ex.: IV).
    """
    capitulo = capitulos().get(numero.strip().upper())
    if capitulo is None:
        return {"status": "erro", "mensagem": f"Capítulo inexistente. Use um de: {', '.join(capitulos())}."}
    return {"capitulo": numero, "titulo": capitulo["titulo"], "texto": capitulo["texto"]}
