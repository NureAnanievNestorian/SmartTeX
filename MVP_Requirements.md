# MVP — Генератор звітів через LaTeX
## Технічний стек: Django + React/Vanilla JS

---

## 1. Загальний опис

Веб-платформа для створення студентських звітів у форматі LaTeX з можливістю компіляції в PDF прямо в браузері. Платформа містить бібліотеку шаблонів (лабораторні, курсові тощо), редактор коду з підсвіткою синтаксису, попередній перегляд PDF та MCP-сервер для інтеграції зі штучним інтелектом.

---

## 2. Функціональні вимоги MVP

### 2.1 Авторизація
- Реєстрація та вхід через email + пароль (django-allauth або вбудований Django auth)
- OAuth через Google (опціонально для MVP, але бажано)
- Сесії через стандартний Django session framework
- Захищені маршрути — неавторизований користувач перенаправляється на сторінку входу

### 2.2 Шаблони
- Адміністратор може створювати/редагувати/видаляти шаблони через Django Admin
- Кожен шаблон має:
  - назву (наприклад, «Лабораторна робота ХНЕУ»)
  - опис
  - категорію (лабораторна / курсова / звіт з практики / інше)
  - вміст — повний `.tex` файл-основа
  - дату створення та оновлення
- Сторінка зі списком шаблонів доступна авторизованому користувачу
- Користувач може переглянути шаблон перед створенням проєкту на його основі

### 2.3 Проєкти
- Користувач може створити проєкт на основі шаблону
- Кожен проєкт має:
  - назву
  - прив'язаний шаблон (або «без шаблону»)
  - один головний `.tex` файл (для MVP — один файл, без підфайлів)
  - статус останньої компіляції (success / error / pending)
  - дату створення та останнього редагування
- Список проєктів користувача — особистий кабінет
- Перейменування та видалення проєкту
- Проєкти ізольовані між користувачами — чужі проєкти недоступні

### 2.4 Редактор
- CodeMirror 6 з підсвіткою синтаксису LaTeX
- Автозбереження кожні 30 секунд (або за зміною, з дебаунсом)
- Кнопка «Компілювати» — запускає компіляцію вручну
- Відображення помилок компіляції у окремій панелі (виведення з `.log` файлу)
- Базові налаштування редактора: розмір шрифту, тема (світла/темна)

### 2.5 Компіляція
- Запуск через Django view → subprocess або через Celery task
- Компілятор: LuaLaTeX (образ `latex-ua` на VPS)
- Кожна компіляція виконується в ізольованому Docker-контейнері
- Обмеження контейнера: `--memory=600m`, `--cpus=1.0`, `--network none`
- Timeout: 60 секунд — якщо перевищено, процес зупиняється
- Одночасно не більше 3 компіляцій на сервері (черга через Celery + Redis або простий семафор для MVP)
- Результат: PDF файл або повідомлення про помилку з логом

### 2.6 Перегляд PDF
- Вбудований PDF-переглядач через `pdf.js` або `<iframe>` з посиланням на файл
- PDF оновлюється після успішної компіляції
- Кнопка «Завантажити PDF»

### 2.7 MCP-сервер
- Окремий Python або TypeScript процес, що реалізує MCP протокол
- Доступний для підключення в Claude Desktop або через API
- Інструменти (tools) які надає MCP-сервер:

| Tool | Опис |
|---|---|
| `list_projects` | Повертає список проєктів користувача |
| `get_project_file` | Повертає вміст `.tex` файлу проєкту |
| `update_project_file` | Замінює вміст `.tex` файлу |
| `compile_project` | Запускає компіляцію, повертає статус та URL PDF |
| `get_compile_log` | Повертає лог останньої компіляції |
| `list_templates` | Повертає список доступних шаблонів |

- Автентифікація MCP-сервера до Django API через токен (простий API Token, не OAuth для MVP)
- MCP-сервер звертається до Django REST API — не до БД напряму

---

## 3. Нефункціональні вимоги

### 3.1 Продуктивність
- Час компіляції реального звіту (~10 стор.): до 15 секунд
- Час відповіді API (збереження файлу, отримання списку): до 500 мс
- Максимум 3 одночасні компіляції (решта — в черзі)

### 3.2 Безпека
- Docker контейнер для компіляції: `--network none`, read-only filesystem крім робочої директорії
- Робоча директорія кожного проєкту ізольована від інших
- Всі API endpoints захищені — лише для авторизованих користувачів
- Користувач має доступ лише до своїх проєктів (перевірка `project.owner == request.user`)
- Захист від path traversal при роботі з файлами
- CSRF захист (стандартний Django)
- Розмір `.tex` файлу — не більше 1 MB

### 3.3 Зберігання файлів
- Файли проєктів (`.tex`, `.pdf`, `.log`) зберігаються на диску VPS
- Структура: `/projects/{user_id}/{project_id}/main.tex`, `main.pdf`, `main.log`
- PDF зберігається після кожної успішної компіляції (перезаписується)
- Старі `.log` файли зберігаються лише від останньої компіляції

---

## 4. Моделі бази даних

```python
# Шаблон
class Template(Model):
    title       = CharField(max_length=255)
    description = TextField(blank=True)
    category    = CharField(choices=[...])  # lab / course / practice / other
    content     = TextField()               # повний .tex вміст
    created_at  = DateTimeField(auto_now_add=True)
    updated_at  = DateTimeField(auto_now=True)
    is_active   = BooleanField(default=True)

# Проєкт
class Project(Model):
    owner       = ForeignKey(User, on_delete=CASCADE)
    title       = CharField(max_length=255)
    template    = ForeignKey(Template, null=True, blank=True, on_delete=SET_NULL)
    last_status = CharField(choices=['pending','success','error'], default='pending')
    created_at  = DateTimeField(auto_now_add=True)
    updated_at  = DateTimeField(auto_now=True)

# API токен для MCP
class MCPToken(Model):
    user        = OneToOneField(User, on_delete=CASCADE)
    token       = CharField(max_length=64, unique=True)
    created_at  = DateTimeField(auto_now_add=True)
```

---

## 5. API endpoints (Django REST Framework)

```
POST   /api/auth/login/
POST   /api/auth/logout/
POST   /api/auth/register/

GET    /api/templates/                  # список шаблонів
GET    /api/templates/{id}/             # деталі шаблону

GET    /api/projects/                   # список проєктів користувача
POST   /api/projects/                   # створити проєкт
GET    /api/projects/{id}/              # деталі проєкту
PATCH  /api/projects/{id}/              # перейменувати
DELETE /api/projects/{id}/              # видалити

GET    /api/projects/{id}/file/         # отримати вміст .tex
PUT    /api/projects/{id}/file/         # зберегти вміст .tex

POST   /api/projects/{id}/compile/      # запустити компіляцію
GET    /api/projects/{id}/compile/      # статус та лог останньої компіляції
GET    /api/projects/{id}/pdf/          # отримати PDF файл

GET    /api/mcp/token/                  # отримати або згенерувати MCP токен
```

---

## 6. Структура Django-проєкту

```
latex_platform/
├── config/
│   ├── settings/
│   │   ├── base.py
│   │   ├── development.py
│   │   └── production.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── accounts/       # авторизація, профіль, MCP токени
│   ├── templates_lib/  # бібліотека шаблонів (назва не конфліктує з Django templates)
│   ├── projects/       # проєкти, файли, компіляція
│   └── mcp/            # MCP сервер (окремий процес або management command)
├── media/
│   └── projects/       # файли проєктів на диску
├── static/
│   └── editor/         # CodeMirror, pdf.js
├── requirements.txt
├── Dockerfile          # для самого Django додатку
└── docker-compose.yml
```

---

## 7. Інфраструктура на VPS

```
VPS (1 vCPU, 2GB RAM — DigitalOcean $12/міс)
├── nginx                      # reverse proxy, роздача статики та PDF
├── gunicorn                   # Django додаток (3-4 workers)
├── redis                      # черга компіляцій (або простий threading.Semaphore для MVP)
├── celery worker (опційно)    # асинхронна компіляція
└── Docker daemon
    └── latex-ua:latest        # образ для компіляції (запускається на кожен запит)
```

---

## 8. Поза межами MVP (наступні версії)

- Спільна робота кількох користувачів над одним проєктом
- Кілька файлів в одному проєкті (підфайли, зображення)
- Завантаження зображень в проєкт
- Коментарі викладача
- Версіонування файлу (git-подібна історія)
- Експорт в DOCX через pandoc
- Статистика компіляцій
- Публічні шаблони від спільноти
- OAuth для MCP (замість простого токена)

---

## 9. Порядок реалізації MVP

1. Django проєкт, моделі, міграції, Django Admin
2. Авторизація (реєстрація, вхід, вихід)
3. API шаблонів та проєктів (DRF)
4. Збереження та читання `.tex` файлів на диску
5. Компіляція через Docker subprocess + повернення PDF
6. Фронтенд: редактор CodeMirror + PDF переглядач
7. MCP сервер з базовими tools
8. nginx + gunicorn на VPS, SSL

**Орієнтовний час: 6–8 тижнів соло**
