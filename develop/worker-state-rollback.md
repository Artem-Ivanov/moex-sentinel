# Откат ограничений состояний Worker

Ревизия SQLite `PRAGMA user_version=1` добавляет именованные CHECK к
`cached_automations.state`, `broker_intents.state`, `fact_outbox.delivery_state`.
Serialized values не меняются; финансовые строки, outbox, quota и ключи сохраняются.

Остановить Worker, использующий файл. Из корня checkout в Python-окружении проекта:

```sh
python develop/scripts/rollback_worker_states.py /absolute/path/to/worker.sqlite
```

Скрипт требует существующий файл и ревизию 1. Выполняет BEGIN IMMEDIATE, проверяет
фактический DDL, удаляет только собственные CHECK и устанавливает ревизию 0 в одной
транзакции. Дополнительные обычные колонки, индексы и собственные триггеры сохраняются,
включая созданные после upgrade. Данные новых операций не удаляются. Quota rollback
не вызывается. Неизвестная версия, изменённые собственные CHECK, generated/hidden,
AUTOINCREMENT, внешние views/triggers и FK-зависимости приводят к отказу без изменений.

Запускать совместимую предыдущую версию приложения: initializer нового кода снова
применит upgrade до 1. Этот документ не разрешает запуск на рабочей базе.
WAL/SHM и сам файл не удалять и не заменять. Проверки выполнены только на отдельной SQLite.
