<!-- docs/FRONTEND_GUIDELINES.md -->

# Frontend Guidelines: HTML, CSS, BEM, JavaScript, Templates and Accessibility

> **Назначение:** обязательный frontend source of truth для проектов,
> при задачах, затрагивающих HTML, CSS, JavaScript, templates или browser UI.
> Само наличие frontend не расширяет область инфраструктурной задачи.
>
> Этот документ применяется **вместе** с `ENGINEERING_GUIDELINES.md`.
> Общие правила архитектуры, документирования файлов и функций, тестирования,
> observability, configuration и quality gates не дублируются здесь и
> продолжают действовать полностью.
>
> Ключевые слова:
>
> - **MUST** — обязательное правило;
> - **SHOULD** — правило по умолчанию, отклонение требует понятной причины;
> - **MAY** — допустимый вариант.

---

## 0. Обязательное чтение

Если задача затрагивает хотя бы один из следующих типов файлов или областей:

```text
HTML
CSS
JavaScript / TypeScript
templates
browser UI
frontend components
frontend assets
```

LLM / AI coding agent MUST полностью прочитать этот документ **до**
предложения frontend-структуры или генерации frontend-кода.

Нельзя считать краткое упоминание frontend в общем engineering document
заменой чтению этого файла.

Если frontend уже существует, перед изменениями LLM MUST дополнительно изучить:

```text
base template;
существующие templates/partials/components;
основной CSS entrypoint;
структуру CSS modules/blocks;
существующую BEM-схему;
общие JavaScript modules;
feature JavaScript modules;
package.json и frontend toolchain, если есть;
frontend tests, если есть.
```

---

## 0.1. Критические frontend rules

Эти правила MUST проверяться при каждой frontend-задаче:

1. MUST использовать semantic HTML там, где есть подходящий native element.
2. MUST соблюдать BEM для проектных CSS classes.
3. MUST сохранять один основной `style.css` entrypoint, если проект не использует
   другой явно принятый build-system pattern.
4. CSS MUST делиться по reusable blocks/components/features, а не превращаться
   в один монолитный stylesheet.
5. Повторяемая markup MUST выноситься в base template / partials / components.
6. JavaScript MUST быть отделён от visual styling через стабильные `data-*`
   hooks там, где это разумно.
7. Inline `onclick` и аналогичные inline event handlers MUST NOT использоваться.
8. Обычные scripts MUST подключаться через `defer`; modules используют
   `type="module"`.
9. Недоверенный пользовательский текст MUST NOT вставляться через raw
   `innerHTML`.
10. Accessibility MUST NOT ухудшаться при frontend-изменении.

LLM MUST NOT завершать frontend-задачу, не проверив этот список.

---

## 0.2. Связь с общими Engineering Guidelines

`ENGINEERING_GUIDELINES.md` остаётся source of truth для:

```text
относительных путей файлов;
русской документации файлов/classes/functions;
backend architecture;
SOLID / DI;
data access;
transactions;
logging / timing decorator;
rotation / retention logs;
configuration;
обязательного покрытия ключевой логики tests;
CI / quality gates.
```

Если правило из этого документа и общее engineering rule кажутся
противоречащими друг другу, LLM MUST явно показать конфликт и не выбирать
вариант молча.

---

# Часть IX. Frontend architecture

## 32. Главный frontend-принцип

Frontend должен быть разделён по ответственности так же, как backend.

Нельзя превращать:

```text
style.css
app.js
index.html
```

в три монолитных файла со всем приложением.

---

## 33. HTML semantics

Использовать семантические элементы по назначению:

```html
<header>
<nav>
<main>
<section>
<article>
<aside>
<footer>
<form>
<label>
<button>
```

Не использовать `<div>` только потому, что он привычнее, если существует подходящий semantic element.

### Кнопка и ссылка

Действие:

```html
<button type="button">
```

Навигация:

```html
<a href="/profile">
```

Не делать ссылку через `onclick` на `<div>`.

---

## 34. Базовая HTML структура

Рекомендуется base template:

```html
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <link
        rel="stylesheet"
        href="/static/css/style.css"
    >

    <script
        src="/static/js/api.js"
        defer
    ></script>

    <script
        src="/static/js/app.js"
        defer
    ></script>

    {% block scripts %}{% endblock %}
</head>

<body class="page">
    <header class="header">
        ...
    </header>

    <main class="page__content">
        {% block content %}{% endblock %}
    </main>

    <footer class="footer">
        ...
    </footer>
</body>
</html>
```

Common scripts идут в base template.

Feature/page entry scripts подключаются через `scripts` block.

---

## 35. JavaScript scripts

Для обычных JS scripts MUST использоваться:

```html
<script
    src="/static/js/profile.js"
    defer
></script>
```

Scripts SHOULD размещаться в `<head>`.

Причины:

- HTML не блокируется выполнением script;
- порядок исполнения предсказуем;
- зависимости подключены централизованно;
- template не засоряется script tags внизу `body`.

Для `type="module"` браузер уже использует deferred semantics, но структура подключения всё равно должна оставаться централизованной.

---

## 36. Запрещённый inline JavaScript

Не использовать:

```html
<button onclick="save()">Save</button>
```

Использовать stable JS hook:

```html
<button
    type="button"
    data-save-document
>
    Save
</button>
```

```javascript
const saveButton = document.querySelector("[data-save-document]");

saveButton?.addEventListener("click", handleSave);
```

---

## 37. CSS entrypoint

HTML SHOULD подключать только один основной CSS entrypoint:

```html
<link
    rel="stylesheet"
    href="/static/css/style.css"
>
```

`style.css` — агрегатор imports.

Он SHOULD содержать минимум собственных selectors.

Пример:

```css
/* tokens / base */
@import url("./variables.css");
@import url("./global.css");

/* layout */
@import url("./blocks/page.css");
@import url("./blocks/header.css");
@import url("./blocks/sidebar.css");

/* reusable UI */
@import url("./blocks/button.css");
@import url("./blocks/form.css");
@import url("./blocks/modal.css");
@import url("./blocks/table.css");

/* feature blocks */
@import url("./blocks/upload.css");
@import url("./blocks/analysis-result.css");
@import url("./blocks/knowledge-base.css");

/* responsive overrides */
@import url("./responsive.css");
```

---

## 38. CSS modules — блоки, а не страницы

Целевая структура:

```text
static/
└── css/
    ├── style.css
    ├── variables.css
    ├── global.css
    ├── responsive.css
    └── blocks/
        ├── page.css
        ├── header.css
        ├── sidebar.css
        ├── button.css
        ├── form.css
        ├── modal.css
        ├── table.css
        ├── upload.css
        ├── analysis-result.css
        └── knowledge-base.css
```

Файл SHOULD соответствовать:

- reusable block;
- layout block;
- cohesive feature section;
- shared UI component.

Не создавать по умолчанию:

```text
index.css
profile-page.css
admin-page-all.css
everything.css
```

если файл просто собирает несвязанные стили всей страницы.

Page-specific stylesheet допустим только когда страница сама является отдельной изолированной feature area и файл всё равно сохраняет одну ответственность.

---

## 39. BEM

Базовое именование:

```text
block
block__element
block--modifier
block__element--modifier
```

Пример:

```html
<section class="analysis-card analysis-card--warning">
    <h2 class="analysis-card__title">
        Риск
    </h2>

    <p class="analysis-card__description">
        ...
    </p>
</section>
```

Не рекомендуется:

```css
#main .content div.item span.red.active
```

Предпочтительно:

```css
.analysis-card__description
```

---

## 40. State classes

Для UI-state MAY использоваться отдельные классы:

```text
is-hidden
is-loading
is-disabled
has-error
```

State class не заменяет BEM block, а описывает временное состояние.

---

## 41. CSS variables

Повторяющиеся design values SHOULD быть tokens:

```css
:root {
    --color-background: #ffffff;
    --color-text: #1f2937;
    --space-1: 0.25rem;
    --space-2: 0.5rem;
    --radius-md: 0.5rem;
}
```

Не дублировать один и тот же magic color/spacing десятки раз.

---

## 42. Inline CSS

Не использовать:

```html
<div style="margin-top: 17px; color: red;">
```

Исключение — действительно динамическое значение, которое невозможно разумно выразить классом/custom property.

Не размещать большие `<style>` blocks в templates.

---

# Часть X. Frontend JavaScript

## 43. Разделение JS

Рекомендуется:

```text
static/js/
├── api.js
├── app.js
├── auth.js
├── components/
│   ├── modal.js
│   └── notifications.js
└── features/
    ├── upload.js
    ├── analysis.js
    ├── history.js
    └── knowledge-base.js
```

`api.js`:

- HTTP requests;
- common error parsing;
- auth headers/cookies policy.

Feature module:

- DOM orchestration конкретной feature;
- feature state;
- event handlers.

Не смешивать все страницы в одном огромном `app.js`.

---

## 44. JS hooks отдельно от CSS styling

Для JS предпочтительно использовать:

```text
data-*
```

Пример:

```html
<button
    class="button button--primary"
    data-analysis-start
>
    Анализировать
</button>
```

CSS использует:

```text
.button
.button--primary
```

JS использует:

```text
[data-analysis-start]
```

Так визуальный refactoring не ломает JavaScript.

---

## 45. DOM safety

Не вставлять недоверенный пользовательский текст через `innerHTML`.

Предпочитать:

```javascript
element.textContent = value;
```

Если HTML действительно нужен, входные данные MUST быть sanitised подходящим инструментом.

---

## 46. Accessibility

Frontend SHOULD обеспечивать:

- связанный `label` для inputs;
- доступность keyboard navigation;
- `aria-label` для icon-only controls;
- `role="status"` / `aria-live` для динамического статуса, когда это нужно;
- видимый focus;
- корректный `disabled`;
- достаточную semantic structure;
- отсутствие кликабельных `<div>` вместо button/link.

ARIA не должна заменять корректный native semantic element.

---

# Часть XI. Templates

## 47. Base template и partials

Повторяющаяся разметка SHOULD выноситься:

```text
templates/
├── base.html
├── components/
│   ├── header.html
│   ├── modal.html
│   └── pagination.html
└── pages/
```

Нельзя копировать одинаковый header/modal/forms markup между страницами.

---

## 48. Templates не содержат business logic

Допустимо:

```jinja2
{% if user.is_authenticated %}
```

Не стоит переносить в Jinja сложные вычисления, выбор бизнес-стратегии или data-access.

Template получает подготовленный context/DTO.

---

# Часть XII. API и frontend

## 49. API layer

JS SHOULD обращаться к backend через выделенный API helper.

Плохо:

```javascript
fetch(...)
```

с разной обработкой ошибок в двадцати обработчиках.

Лучше:

```javascript
await apiRequest("/api/v1/documents", {
    method: "POST",
    body: formData,
});
```

Common API client должен централизовать:

- base URL;
- headers;
- credentials;
- JSON parsing;
- standard error shape.

---

---

# Frontend testing and review

## 49.1. Frontend tests

Общее правило из `ENGINEERING_GUIDELINES.md` сохраняется:

> вся ключевая логика проекта MUST быть покрыта автоматическими tests.

Если frontend содержит нетривиальную client-side logic, SHOULD тестироваться
минимум:

```text
state transitions;
валидация форм;
преобразование данных;
формирование request payload;
обработка API error/success states;
permission-dependent UI behavior;
критичные event handlers;
компоненты с нетривиальным поведением.
```

Если проект уже использует frontend test toolchain, новая ключевая frontend
logic MUST использовать тот же принятый test stack.

Critical user flows SHOULD иметь integration/E2E tests, если проект
поддерживает соответствующую инфраструктуру.

---

## 49.2. Обязательный frontend review checklist

Перед завершением frontend-изменения LLM/developer MUST проверить:

- [ ] Используется semantic HTML.
- [ ] Повторяемая разметка вынесена в base/partials/components.
- [ ] BEM соблюдён.
- [ ] Нет необоснованных больших inline `<style>`.
- [ ] Нет inline `onclick` и аналогичных inline handlers.
- [ ] HTML подключает основной `style.css` либо утверждённый project entrypoint.
- [ ] `style.css` остаётся entrypoint/aggregator, если это принятая структура проекта.
- [ ] CSS files соответствуют blocks/components/features, а не случайным страницам.
- [ ] Нет нового монолитного CSS-файла без обоснования.
- [ ] Scripts подключены через `defer` или `type="module"`.
- [ ] Common JS и feature JS разделены.
- [ ] JS использует `data-*` hooks там, где styling classes не должны быть API для JS.
- [ ] Недоверенные данные не вставляются через raw `innerHTML`.
- [ ] Keyboard navigation и focus behavior не ухудшены.
- [ ] Inputs имеют связанные labels.
- [ ] Icon-only controls имеют доступное имя.
- [ ] Dynamic status использует `aria-live` / `role="status"`, когда это нужно.
- [ ] Изменённая ключевая frontend logic покрыта tests.
- [ ] Относительные пути frontend files указаны по общему Engineering Guidelines.
- [ ] Документация изменённых frontend source files актуальна и написана на русском языке.

---

# Итоговый frontend pattern

```text
templates/
├── base.html
├── components/
└── pages/

static/
├── css/
│   ├── style.css
│   ├── variables.css
│   ├── global.css
│   ├── responsive.css
│   └── blocks/
└── js/
    ├── api.js
    ├── app.js
    ├── components/
    └── features/
```

Конкретный framework MAY менять физические директории, но MUST сохраняться
смысл правил:

```text
semantic HTML;
BEM;
modular CSS;
central CSS entrypoint;
base/partials/components;
deferred/module scripts;
stable JS hooks;
safe DOM updates;
accessibility;
tested key frontend logic.
```

