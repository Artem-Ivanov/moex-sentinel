# Operator Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Защитить пользовательский API и интерфейс одной серверной учётной записью до публикации VPS.

**Architecture:** Отдельный модуль backend хранит verifier и сессии в памяти одного процесса. Middleware проверяет все `/api/*`, кроме health и входа; `/internal/*` сохраняет сервисный контракт. Vue держит сессию в памяти и использует общий fetch для CSRF и реакции на 401.

**Tech Stack:** Python 3.12, FastAPI, stdlib scrypt; Vue 3, Vue Router, Vitest; Docker Compose.

**Spec:** `docs/superpowers/specs/operator-authentication-design.md`

## Global Constraints

- Не затрагивать пользовательские staged изменения, рабочие БД и volumes.
- Никаких новых зависимостей и хранения пароля в браузере.
- Production startup отказывает при пустых/невалидных учётных данных; тестовая замена только через `create_app`.
- Отдельный backend порт не публикуется; remote HTTP с локальной cookie запрещён.

## Review Focus

- Прямой анонимный `/api/brokers` не раскрывает токен: `tests/api/test_auth.py`.
- Ошибочный логин и перегрузка scrypt не блокируют event loop и не раскрывают имя: `tests/api/test_auth.py`.
- Чужой Host не получает локальную cookie, CSRF проверяется до мутации: `tests/api/test_auth.py`.
- Обновление страницы восстанавливает сессию, выход и 401 закрывают UI: `frontend/src/auth.spec.ts`.
- Compose не публикует backend и remote запрещает HTTP cookie: `tests/test_compose_config.py` и preflight.

---

## Task 1: Backend verifier and session boundary

**Files:** `src/moex_sentinel/api/auth.py`, `src/moex_sentinel/config.py`, `src/moex_sentinel/api/app.py`, `tests/api/test_auth.py`, `tests/conftest.py`.

- [x] Написать тесты для fail-closed запуска, CLI hash, входа, анонимного `/api`, session/logout, CSRF, истечения, Host и внутренних маршрутов. Запустить и увидеть ожидаемый RED.
- [x] Реализовать verifier, лимит одновременного scrypt, сессии и HTTP middleware/routes. Запустить GREEN.
- [x] Дать существующим бизнес-тестам явный test-only bypass без production-переключателя; прогнать backend suite.

## Task 2: Frontend session and login

**Files:** `frontend/src/auth.ts`, `frontend/src/api/request.ts`, `frontend/src/router.ts`, `frontend/src/App.vue`, `frontend/src/views/LoginView.vue`, существующие `frontend/src/api/*.ts`, `frontend/src/auth.spec.ts`.

- [x] Написать Vitest тесты session restore, CSRF, 401, login/logout и guard; выполнить после подготовки toolchain. Первичный RED фронтенда не зафиксирован: Node отсутствовал до реализации агентом.
- [x] Реализовать общий fetch, состояние сессии, страницу входа, guard и выход; проверить GREEN.
- [x] Прогнать полную frontend suite и build, исправить вызванные регрессии.

## Task 3: Configuration and deployment guard

**Files:** `compose.yml`, `deploy/remote/compose.remote.yml`, `deploy/remote/*`, `.env.example`, `frontend/package.json`, `tests/test_compose_config.py`, тематическая документация.

- [x] Добавить тесты и проверку конфигурации auth, затем явные Compose env и remote preflight.
- [x] Проверить `docker compose config`, backend/frontend tests, lint и сборку. Деплой ждёт конкретных SSH реквизитов.

## Task 4: Independent review and integration

- [x] Передать diff независимому агенту на проверку контрактов, безопасности, простоты и тестов.
- [x] Исправить существенные замечания и получить повторную проверку.
- [x] Обновить `develop/CURRENT.md` и локальный `develop/WORK_LOG.md`; сохранить результаты проверок.
