# Шаблон правил разработки

Скопируйте документы и конфигурацию в новый проект, заполните [бриф](AGENT_BRIEF.md) и очередь [плана](docs/implementation-plan.md). `.codex/config.toml` уже регистрирует восемь профилей (см. [.agents/README.md](.agents/README.md)); hook trust настраивается вручную по [документации hooks](https://learn.chatgpt.com/docs/hooks) и инструкции в [`develop/README.md`](develop/README.md). Фактическое подключение в новом проекте не проверено.

- [Правила проекта](AGENTS.md)
- [Оркестрация](docs/agent-orchestration.md)
- [Очередь реализации](docs/implementation-plan.md)
- [Профили агентов](.agents/README.md)
- [Навык project-context](.agents/skills/project-context/SKILL.md)
- [Локальные материалы и hooks](develop/README.md)
