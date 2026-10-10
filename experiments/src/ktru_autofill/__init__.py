"""Автозаполнение характеристик КТРУ по тексту позиции (путь А: фрагменты + эмбеддинги)."""

from .autofill import Autofiller, Result
from .schema import Characteristic, KtruCandidate

__all__ = ["Autofiller", "Result", "Characteristic", "KtruCandidate", "main"]


def main() -> None:
    from .cli import main as _main

    _main()
