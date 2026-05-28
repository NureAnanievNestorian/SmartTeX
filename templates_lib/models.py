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
    content = models.TextField(blank=True, default="")
    zip_file = models.FileField(upload_to="template_zips/", blank=True, null=True)
    main_file = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title


class TemplateLongDocDefaults(models.Model):
    template = models.OneToOneField(Template, on_delete=models.CASCADE, related_name="longdoc_defaults")
    enabled = models.BooleanField(default=True)
    context_enabled = models.BooleanField(default=True)
    outline_enabled = models.BooleanField(default=True)
    tasks_enabled = models.BooleanField(default=True)
    notes_enabled = models.BooleanField(default=True)
    summaries_enabled = models.BooleanField(default=True)
    requirements_enabled = models.BooleanField(default=False)
    ai_sessions_enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Template long-document defaults"
        verbose_name_plural = "Template long-document defaults"

    def __str__(self) -> str:
        return f"LongDoc defaults for {self.template}"


class TemplateOutlineItem(models.Model):
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="outline_items")
    order = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=500)
    level = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, default="missing")
    expected_pages = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["template_id", "order"]
        unique_together = [("template", "order")]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.order}:{self.title}"


class TemplateRequirement(models.Model):
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="requirements")
    req_id = models.CharField(max_length=50)
    description = models.TextField()

    class Meta:
        ordering = ["template_id", "req_id"]
        unique_together = [("template", "req_id")]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.req_id}"


class TemplateContextFile(models.Model):
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="context_files")
    filename = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    content = models.TextField()

    class Meta:
        ordering = ["template_id", "filename"]
        unique_together = [("template", "filename")]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.filename}"


class TemplateTask(models.Model):
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="tasks")
    description = models.TextField()

    class Meta:
        ordering = ["template_id", "id"]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.description[:60]}"


class TemplateNoteSection(models.Model):
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="note_sections")
    heading = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["template_id", "order"]
        unique_together = [("template", "heading")]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.heading}"
