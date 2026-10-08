# Профили агентов

Восемь редактируемых профилей задают роли и стартовые настройки. Названия нейтральные: `architect`, `reviewer`, `security_auditor`, `backend_developer`, `frontend_developer`, `junior_backend_developer`, `junior_frontend_developer`, `worker`. Backend/frontend — лишь роли, не требование к стеку.

| Профиль | Назначение | Модель / reasoning |
| --- | --- | --- |
| [architect](architect.toml) | Исследует реальные потоки и предлагает проверяемый план | gpt-6.1-sol / high |
| [reviewer](reviewer.toml) | Независимое read-only ревью плана, diff и доказательств | gpt-6.1-sol / high |
| [security_auditor](security_auditor.toml) | Назначенный read-only аудит безопасности и trust boundaries | gpt-6.1-sol / high |
| [backend_developer](backend_developer.toml) | Senior для назначенных серверных задач и интеграции junior | gpt-6-luna / high |
| [frontend_developer](frontend_developer.toml) | Senior для назначенных клиентских задач и интеграции junior | gpt-6-luna / high |
| [junior_backend_developer](junior_backend_developer.toml) | Маленький согласованный backend leaf-пакет | gpt-6-luna / low |
| [junior_frontend_developer](junior_frontend_developer.toml) | Маленький согласованный frontend leaf-пакет | gpt-6-luna / low |
| [worker](worker.toml) | Ограниченная универсальная задача кода, проверки или документации | gpt-6-luna / low |

Модель — стартовое предложение; доступность зависит от аккаунта. Root вправе назначить тот же profile с `medium` для большего, но ясного leaf-пакета или `xhigh` для сложной архитектуры/security review. Выбор фиксируется в brief. Эти настройки не измеряют стоимость, скорость или качество.

В этом шаблоне `.codex/config.toml` регистрирует все восемь профилей через `config_file`. Измените описания или путь, если структура проекта отличается. Например:

```toml
[agents.architect]
config_file = "../.agents/architect.toml"
description = "Исследование архитектуры и планирование"
```

Синтаксис описан в [документации конфигурации](https://learn.chatgpt.com/docs/config-file/config-reference). Конфигурация в шаблоне подготовлена, но её подключение в целевом проекте не проверялось. Параметр `sandbox_mode` задаёт настройку профиля, но не выдаёт права сверх политики машины и runtime.
