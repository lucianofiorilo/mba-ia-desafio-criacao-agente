"""Caminhos, nomes e modelo usados pelo projeto."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]
load_dotenv(RAIZ / ".env")

DADOS = RAIZ / "dados"
VAR = RAIZ / "var"
VAR.mkdir(exist_ok=True)

# Dados do condomínio (reservas e visitantes) e sessões do ADK ficam em
# arquivos SQLite separados, ambos fora de dados/, que não pode ser alterado.
CONDOMINIO_DB = VAR / "condominio.db"
SESSOES_DB = VAR / "sessoes.db"
SESSOES_DB_URL = f"sqlite+aiosqlite:///{SESSOES_DB.as_posix()}"

APP_NAME = "residencial_aurora"
# O apartamento fica no state da sessão; o user_id do ADK é fixo porque a
# rota recebe só o session_id.
USER_ID = "morador"

# Modelo validado no fluxo do avaliador; MODELO no .env troca o padrão.
MODELO = os.environ.get("MODELO") or "gemini-3.5-flash"
