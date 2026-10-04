"""Leitura do regulamento por capítulo (Garantia 4).

O texto nunca é entregue inteiro: o especialista recebe só os títulos e busca
um capítulo por vez.
"""

import re
from functools import cache

from .config import DADOS


@cache
def capitulos() -> dict[str, dict]:
    """Capítulos por numeral romano: {"IV": {"titulo": ..., "texto": ...}}."""
    texto = (DADOS / "regulamento.md").read_text(encoding="utf-8")
    resultado = {}
    for bloco in re.split(r"^(?=## )", texto, flags=re.MULTILINE):
        m = re.match(r"## Capítulo ([IVXLC]+): (.+)", bloco)
        if m:
            resultado[m.group(1)] = {"titulo": m.group(2).strip(), "texto": bloco.strip()}
    return resultado


def indice() -> str:
    """Só os títulos, para a instrução do especialista em regulamento."""
    return "\n".join(f"- {num}: {c['titulo']}" for num, c in capitulos().items())
