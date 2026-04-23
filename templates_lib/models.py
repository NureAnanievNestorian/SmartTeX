from django.db import models

from SmartTeX.markup import MarkupType


class Template(models.Model):
    class Category(models.TextChoices):
        LAB = "lab", "Лабораторна"
        COURSE = "course", "Курсова"
        PRACTICE = "practice", "Практика"
        OTHER = "other", "Інше"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.OTHER)
    markup_type = models.CharField(max_length=10, choices=MarkupType.choices, default=MarkupType.LATEX)
    content = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title
