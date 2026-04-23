from django.db import models


class MarkupType(models.TextChoices):
    LATEX = "latex", "LaTeX"
    TYPST = "typst", "Typst"


def source_filename_for_markup(markup_type: str) -> str:
    if markup_type == MarkupType.TYPST:
        return "main.typ"
    return "main.tex"
