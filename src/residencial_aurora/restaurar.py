"""Volta reservas e visitantes ao estado de dados/*.json.

Uso: uv run restaurar            (mantém as sessões)
     uv run restaurar --sessoes  (também apaga as sessões; rode com a API parada)
"""

import sys

from . import db
from .config import SESSOES_DB


def main() -> None:
    db.restaurar()
    print("Reservas e visitantes restaurados a partir de dados/.")
    if "--sessoes" in sys.argv[1:]:
        SESSOES_DB.unlink(missing_ok=True)
        print("Sessões apagadas.")


if __name__ == "__main__":
    main()
