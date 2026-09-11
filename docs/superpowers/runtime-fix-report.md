# Runtime bootstrap: атомарность и подтверждение ревизии

Дата: 2026-09-08.

Исправлены два нарушения инвариантов текущей clean-slate вехи:

- Worker передаёт четыре начальных bootstrap-факта одной группой. Размер batch остаётся целевым: только последняя выбранная группа может превысить его, максимум на три факта. Retry одного участника откладывает всю группу, сохраняя доставку независимых автоматизаций. Обычный runtime backlog не расширяется до размера всей автоматизации.
- Core проверяет начальную группу до записи: cycle, lot и bootstrap audit соответствуют сохранённому snapshot, IN_WORK находится последним. Неполная или противоречивая группа получает `INVALID_FACT_STATE` без частичных записей. Повтор принятой группы остаётся идемпотентным.
- Факты того же автомата, записанные после bootstrap, доставляются следующим запросом: последующий HOLD не добавляется после IN_WORK в начальную транзакцию.
- ACK непрерывного префикса больше не уменьшает локальную ревизию, уже увеличенную последующими неподтверждёнными state-фактами. Новый факт сохраняет правильный `expected_revision`.

DTO, HTTP-протокол и схемы БД не менялись.

## RED / GREEN

RED: до изменения production-кода получены **11 ожидаемых падений, 12 успешных тестов**. Новые падения воспроизводили разрыв bootstrap при лимитах 1/2/3, retry внутри группы, перемешивание двух bootstrap, принятие Core неполной группы/одиночной активации/неверного количества и потерю ревизии после частичного ACK с последующим append.

GREEN: целевой набор — **23 passed**. Расширенный набор с соседним обычным фактом на границе batch и независимой автоматизацией при retry — **60 passed**:

```sh
.venv/bin/pytest -q tests/services/test_trading_fact_ingress.py tests/integration/test_open_position_bootstrap.py tests/trading_automaton/storage tests/trading_automaton/services/test_position_bootstrap_service.py tests/trading_automaton/services/test_fact_synchronization.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py --tb=short
```

После review отдельно воспроизведён RED: начальный batch включал пятый HOLD-факт того же автомата. GREEN: integration-тест подтверждает две отдельные Core-транзакции, единственный lot и итоговую revision 3.

Ruff и Black проходят для перечисленных ниже Python-файлов. `mypy` отсутствует в окружении и в зависимостях проекта; отдельная проверка типов не выполнена. Проверки используют синтетические SQLite fixtures, без брокерских API и реальных хранилищ. Общие gates проекта и PostgreSQL acceptance выполняются отдельно координатором вехи.

## Изменённые файлы

- `src/trading_automaton/storage/repository.py`
- `src/moex_sentinel/services/trading_fact_ingress.py`
- `tests/integration/test_open_position_bootstrap.py`
- `tests/trading_automaton/services/test_position_bootstrap_service.py`
- `tests/trading_automaton/storage/test_fact_outbox.py`
- `docs/superpowers/runtime-fix-report.md`

## Повторное принятие после CLOSED

Новая позиция после завершения прежней автоматизации получает следующие детерминированные automation/cycle/lot UUID. Начальный UUID сохранён. Поколения образуют цепочку от предыдущего automation UUID; выбор не зависит от порядка или совпадения timestamps. Закрытые записи не изменяются. Повторная синхронизация сравнивает immutable business values активного snapshot, поэтому новая дата наблюдения не создаёт дубликат. При конкурентном нарушении уникальности repository перечитывает активную запись и проверяет snapshot.

RED: **3 failed, 5 passed**. GREEN: **8 passed** в `tests/storage/test_position_adoption_repository.py`. Покрыты три последовательных закрытия с одинаковым временем, неизменность прошлых snapshots, прежняя/новая цена, later retry, конфликт активного snapshot и unique-conflict после устаревшего первого чтения.

Дополнительные файлы:

- `src/moex_sentinel/domain/position_adoption.py`
- `src/moex_sentinel/storage/repositories/position_adoption.py`
- `tests/storage/test_position_adoption_repository.py`

Финальная объединённая проверка после suffix review и повторного принятия: **76 passed**; Ruff и Black проходят для всех восьми изменённых Python-файлов.

```sh
.venv/bin/pytest -q tests/services/test_trading_fact_ingress.py tests/integration/test_open_position_bootstrap.py tests/trading_automaton/storage tests/trading_automaton/services/test_position_bootstrap_service.py tests/trading_automaton/services/test_fact_synchronization.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py tests/storage/test_position_adoption_repository.py tests/services/test_position_adoption_service.py tests/usecases/test_position_adoption_usecase.py --tb=short
```

Readiness перед начальным IN_WORK передан координатору и отдельному исполнителю. Старые volumes, Git index и коммиты не изменялись.
