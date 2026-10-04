"""Acesso aos dados do condomínio (SQLite).

As regras que dependem do armazenamento moram aqui:
- exclusividade de área por data: índice único parcial `ux_reserva_ativa`,
  avaliado pelo SQLite no instante do INSERT (Garantia 5);
- código de reserva nunca repetido: chave primária em `codigo`, e reservas
  canceladas continuam na tabela com status 'cancelada'.

Toda função que lê ou altera reservas/visitantes de um morador recebe o
apartamento como parâmetro; quem chama (as tools) passa sempre o apartamento
da sessão.
"""

import json
import secrets
import sqlite3
from contextlib import closing
from functools import cache

from .config import CONDOMINIO_DB, DADOS

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL,
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa'
                CHECK (status IN ('ativa', 'cancelada'))
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data. Vale no momento
-- da gravação, mesmo com duas aprovações simultâneas.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa
    ON reservas (area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(CONDOMINIO_DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def criar_schema() -> None:
    with closing(conectar()) as con, con:
        con.executescript(SCHEMA)


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


@cache
def areas() -> dict[str, dict]:
    """Áreas comuns por id (dados estáticos, só leitura)."""
    return {a["id"]: a for a in _ler_json("areas.json")}


@cache
def apartamentos() -> set[str]:
    return {a["numero"] for a in _ler_json("apartamentos.json")}


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de dados/*.json."""
    criar_schema()
    with closing(conectar()) as con, con:
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM visitantes")
        con.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
            [(r["codigo"], r["apartamento"], r["area"], r["data"]) for r in _ler_json("reservas.json")],
        )
        con.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            [(v["apartamento"], v["nome"], v["data"]) for v in _ler_json("visitantes.json")],
        )


# --- Leitura -----------------------------------------------------------------


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with closing(conectar()) as con:
        rows = con.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with closing(conectar()) as con:
        rows = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def area_ocupada(area: str, data: str) -> bool:
    """Diz só se a data está ocupada, sem revelar de quem é a reserva."""
    with closing(conectar()) as con:
        row = con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area, data),
        ).fetchone()
    return row is not None


# --- Escrita -----------------------------------------------------------------


def criar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Grava a reserva e devolve o código, ou None se a data já está ocupada.

    Não há conferência prévia: quem decide é o índice único no INSERT, então
    duas gravações simultâneas nunca produzem duas reservas ativas.
    """
    for _ in range(5):
        codigo = f"RSV-{secrets.token_hex(4).upper()}"
        try:
            with closing(conectar()) as con, con:
                con.execute(
                    "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                    (codigo, apartamento, area, data),
                )
            return codigo
        except sqlite3.IntegrityError as e:
            if "reservas.codigo" in str(e):
                continue  # colisão de código (improvável): gera outro
            return None  # ux_reserva_ativa: área já reservada nessa data
    raise RuntimeError("não foi possível gerar um código de reserva único")


def cancelar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Cancela a reserva ativa do apartamento; devolve o código ou None."""
    with closing(conectar()) as con, con:
        row = con.execute(
            "UPDATE reservas SET status = 'cancelada'"
            " WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'"
            " RETURNING codigo",
            (apartamento, area, data),
        ).fetchone()
    return row["codigo"] if row else None


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with closing(conectar()) as con, con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
