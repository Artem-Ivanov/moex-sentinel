# D1 author report — scoped execution diagnostics

**Статус:** реализация и focused проверки готовы к итоговой интеграции. Независимый повторный code review и общий backend/isolated-PostgreSQL прогон после последнего изменения остаются за root. В этом отчёте локальный PG PASS относится к более раннему состоянию теста, до последних assert-различения execution ID/fact ID.

## Итог

- Worker добавляет optional top-level `execution_diagnostics`, собираемый отдельным SQLite `mode=ro`/`query_only` reader через `NullPool`. Reader ограничен общим бюджетом 100 ms, connect/busy до 50 ms и SQLite progress handler; при timeout отдаёт UNKNOWN без частичных строк. Не использует основной pool или finalize lock, закрывает handler/transaction/connection независимо.
- Core объявляет capability только на internal runtime route; public runtime shape сохранён. Старый Worker опускает новое поле полностью, включая `None`; некорректная optional projection не отвергает control heartbeat. На Core добавлен authenticated `no-store` GET со scoped worker observation и bounded persisted projection. Глобальный WorkerObservation и старые status endpoints не включают financial rows.
- Persisted projection читает scalar allowlist из orders/events/executions/reservations/owner без `FOR UPDATE` и broker RPC. Worker `fact_execution_id` означает бизнес `execution_id`; acceptance доказывается цепочкой `execution_id -> Core executions[].fact_id -> accepted_envelopes[].fact_id/event_id`, с точным kind/sequence/received_at. Эти ID не подменяются друг другом.
- Исторический order lot size берётся из сохранённого decision; отсутствующее значение остаётся неизвестным. Текущие catalog instrument/lot/currency остаются отдельной scope metadata.

## Исправления раннего review

| Finding | Результат |
|---|---|
| W1 active ordering/policy | Reader повторяет environment-specific active predicate; добавлен параметризованный тест PROD unfinalized terminal + пять более новых finalized, TEST CANCELLED/REJECTED/EXPIRED inactive, TEST FILLED-unemitted active, FAILED с terminal timestamp inactive. |
| W2 scope binding | Core сверяет broker/account/automation/external instrument/internal fact instrument UUID/current lot size; несовпадение даёт UNKNOWN без financial rows. |
| W3 executed price | Worker projection сохраняет `executed_price` отдельно от `limit_price`, оба значения тестируются. |
| W4 cleanup | Handler clear, rollback и close выполняются независимыми cleanup шагами; тест timeout проверяет закрытие соединения и успешное повторное чтение. |
| W5 reason priority | Для TEST без trusted principal возвращается `UNAUTHENTICATED_SOURCE` раньше owner mismatch. |
| W6 bounds | Размеры верхнего уровня проверяются до вложенной Pydantic validation; malformed oversized payload остаётся локальным UNKNOWN projection. |
| W7 explicit null | `execution_diagnostics: null` сохраняется как отсутствие observation. |
| C1 reservation Rows | Scoped query использует `execute().all()` и scalar allowlist; тест содержит реальную reservation с financial/origin полями. |
| C2 exact acceptance | Envelope query ограничена ID видимых order/event/execution rows и выдаёт до `3*limit`; лимитный тест различает event, order, execution, второе execution и несвязанный более поздний envelope. |
| C3 persisted fields | В projection включены decision/process, исторический decision lot size, цены/комиссии, execution fees и reservation runtime/generation; payload/strategy не выбираются. |

Корреляция envelope использует разные идентификаторы согласно уточнённому контракту: Worker `fact_execution_id` остаётся `TradeExecutionDraft.id`; Core `TradeExecutionDraft.fact_id` — ID envelope. SQLite/PG storage tests проверяют `execution_id != fact_id` и exact envelope linkage.

## Проверки автора

Последний targeted запуск:

```text
BROKER_ACCESS_MODE=READ_ONLY .venv/bin/pytest -q \
  tests/contracts/test_execution_diagnostics.py \
  tests/trading_automaton/storage/test_execution_diagnostics.py \
  tests/trading_automaton/runtime/test_execution_diagnostics_budget.py \
  tests/trading_automaton/adapters/test_core_client.py \
  tests/services/test_runtime_versions.py \
  tests/api/test_runtime_versions.py \
  tests/api/test_internal_runtime.py \
  tests/api/test_execution_diagnostics.py \
  tests/storage/test_execution_diagnostics.py \
  tests/api/test_auth.py::test_two_contours_share_host_but_reject_each_others_origin_and_csrf \
  --tb=short
97 passed, 17 warnings in 7.59s
```

`ruff check` по всем перечисленным ниже D1 source/test файлам: PASS. `black --check` по тем же файлам: PASS (28 files unchanged). SDK/Starlette deprecation warnings — существующие предупреждения; ошибок проверок нет.

Ранее root подтвердил 83 Worker/runtime/auth focused PASS и isolated PG test 1 PASS. Они предшествуют последнему расширению проверки W1 и различению execution ID/fact ID; root должен повторить PG и общий backend suite на текущем freeze.

## Файлы и SHA-256

### Source

```text
4dabe175f3c9b9fccdb4e4bc6013228353968be83291e545b468c05593177d26  src/sentinel_contracts/worker_diagnostics.py
4c958effe0b89dc39c475891b87284b8df0ac220208b19d20b1957806aa3d23c  src/trading_automaton/storage/execution_diagnostics.py
7f1b7d51d7b71da49f4d1ea7dc4e23791bd43ab684303c4bafb3ca43d2097fba  src/trading_automaton/runtime/streaming_coordinator.py
48c2b8bfb4a8514a11e659513d8e4c8f26bdf0403a531eb36be4ad17e4e9ba1e  src/trading_automaton/composition.py
3c5b66c9af11668c318c2fc6f7461802a04de665f711ea98714a001145375246  src/trading_automaton/adapters/core_client.py
bba4fa597c47711c4c1a7d798b61ccabcfb40a3083790447b4108f24fc374bc2  src/moex_sentinel/api/app.py
6ff8a25812b4da00416c6073d3d95db7814ee52f6beba5e985842f3684112ee6  src/moex_sentinel/views/internal_automaton.py
b398e6b4fe8c7d1123b936ef64624ba322805cd4f25b4ed8cf0df075deaa0156  src/moex_sentinel/services/runtime_versions.py
15f55635198ba377fef0e12786fe4b7f1b40dbb70fac827eabfb91e51e3552f1  src/moex_sentinel/composition.py
fcf614fc2a925c08cd5d9a61b990324d32e447f3211d4834716279876f2e58aa  src/moex_sentinel/views/automations.py
d25b4ae7bc5c4a770beaba48405e72d70d674a88ed422787ca796dead124a86a  src/moex_sentinel/storage/repositories/order_facts.py
21241b257bc496cf69f601dab1e8f275b009e0a88545aedc27db5c49ebff71ef  src/moex_sentinel/storage/repositories/trading_facts_uow.py
c613534c42f7693fcfa9925d8d804b3ec57d5c0d6ec93bc716225c8b095d688d  src/moex_sentinel/services/trading_fact_ports.py
8a2dc74b2b66a66455b74c799665785087c12615288cbb2e1598fd81eb56dc5e  src/moex_sentinel/domain/execution_diagnostics.py
a842373cda18436cfef0a4168756d1dcbcf8a872a111baff88a23182a53e1793  src/moex_sentinel/usecases/execution_diagnostics.py
8e14d56a3d134812cdb112ed5757ad38ba23d692c945de612a685c44c2cf28c1  src/moex_sentinel/views/schemas/execution_diagnostics.py
b7adfd06695b7ade0514be89fc413373eafefaa537f5c7f661f496b99d42e9a1  src/moex_sentinel/views/schemas/automations.py
```

### Tests

```text
bab2f109158e5bda70118f5e6532a2c7363400395d2776c50ee85b7821f51a82  tests/contracts/test_execution_diagnostics.py
3b95e9790c65282345b16854114ab60c988ba01d95df655bd9a04d389c130e5a  tests/trading_automaton/storage/test_execution_diagnostics.py
6f6796810c4ccd73cf18db85dafef9117f508fdb1e9a4773982c592f21af1bea  tests/trading_automaton/runtime/test_execution_diagnostics_budget.py
209a1530e6e15008abd22c06c059608db371e46e5b5fcc8b0292de45211797b0  tests/trading_automaton/runtime/__init__.py
0860b875c66f3a7ec4ed219cb715419019c19be01f103e840057e1f8b5fa04d2  tests/trading_automaton/adapters/test_core_client.py
7b912a67a20705b947bd3b60cc9dfa5972c7b7d1da405a7f034064bf4aa8de6b  tests/api/test_internal_runtime.py
fbad1b5e22ac90e4322318052ee7e4ed643e180781fab3b2f445841aca75c0af  tests/services/test_runtime_versions.py
36dbdf76a12f7f57f779bc3d42cc955b811074e3708a335d9b08bdbc2aba5048  tests/api/test_runtime_versions.py
924a4c3f1583e160dd96ee0974df5f7ba1a4d84345e7da3dbfa5c0e0085b2ccf  tests/api/test_execution_diagnostics.py
681f2626d94583369515dd93d0398237fa05d538e9cb401284164fc32f00a8b5  tests/storage/test_execution_diagnostics.py
489afc61b1d3bd91c9cd7b559a76f408d2bd0c23867333ea6ea253bfabdb5dae  tests/integration/postgresql/test_execution_diagnostics.py
```

## Граница и review

### Follow-up по двум minor замечаниям независимого code review

- Удалён ложный критерий `len(accepted_envelopes) > limit`: acceptance envelope bound равен `3*limit`, поэтому полный сценарий ровно 1 order + 1 event + 1 execution при `limit=1` теперь имеет `truncated=false`. Существующий тест со вторым execution всё ещё проверяет реальное truncation.
- Добавлен authenticated-PROD HTTP regression test с deterministic injected `WorkerPrincipal`: matched heartbeat даёт 200; forged body runtime instance/environment/access mode и execution broker дают 403, при этом in-memory runtime metadata/control/diagnostic cache остаётся прежним. Тест честно проверяет route guard после установки principal middleware-ом; реальную проверку credentials отдельно покрывает `tests/api/test_worker_auth.py`.
- RED/GREEN для storage regression: до правки complete 1+1+1 возвращал `truncated=true`; после правки targeted набор `tests/api/test_runtime_versions.py tests/storage/test_execution_diagnostics.py` — 19 passed, 17 deprecation warnings. Ruff и Black для изменённых трёх файлов — PASS.
- Авторский PG rerun не завершился: sandbox запретил TCP loopback к согласованному `127.0.0.1:55434` (`Operation not permitted`). Root ранее сообщил общий freeze run 2899 passed / 2 skipped (`pg_dump`) и одно ожидаемое setup-ошибочное старое PROD DB-name guard; после этой правки root должен повторить PG test и общий suite при доступном isolated DB.
- Свежие hashes: `src/moex_sentinel/storage/repositories/order_facts.py` — `d25b4ae7bc5c4a770beaba48405e72d70d674a88ed422787ca796dead124a86a`; `tests/storage/test_execution_diagnostics.py` — `681f2626d94583369515dd93d0398237fa05d538e9cb401284164fc32f00a8b5`; `tests/api/test_runtime_versions.py` — `36dbdf76a12f7f57f779bc3d42cc955b811074e3708a335d9b08bdbc2aba5048`.

Владелец D1-файлов: `execution_diagnostics_senior`. Formal design preflight принят C0/I0. Ранний независимый review выявил перечисленные выше замечания; исправления внесены и получили focused regression tests. Независимый повторный code review после текущего freeze ещё не получен. Root должен провести повторный PG test, общий backend suite, проверить diff against сохранённый dirty baseline и запросить свежий независимый review. D3-файлы не менялись; D2 recovery-поведение не менялось. Миграций, таблиц и зависимостей нет.
