# A1: scoped instrument ID для операций позиции

Предложение `/root/fix_delivery`, 2026-09-16. До реализации: независимый design review и RED отдельного test author.

## Причина и минимальное решение

`ViewTradingAutomationDetailsUsecase` адресует позицию внутренним catalog ID, как candles path. `PortfolioAggregationService.view_position_operations` сейчас передаёт его брокеру без преобразования. Единственный production consumer этого метода — details usecase; standalone adapter и generic operations APIs продолжают использовать external IDs.

Расширить существующий `InstrumentCatalogLookupPort` методом `get(user_broker_id, instrument_id) -> UserBrokerCatalogInstrument`. В `view_position_operations` после нынешних enabled/environment checks разрешить scoped internal ID и передать `record.external_instrument_id` в `adapter.get_operations`. Production composition уже передаёт `instrument_repository` в этот service; новый resolver/service/usecase не нужен.

Существующий optional `instruments` используется для необязательного ticker enrichment других методов. Его отсутствие именно в position-specific методе должно дать configuration `ValueError` до broker I/O. Не менять все constructors на mandatory catalog ради этой операции и не делать fallback к internal ID. Ошибка отсутствующего/чужого catalog record достигает существующей independent operations error boundary в details usecase; candles продолжает загружаться. Документировать internal ID у usecase port и service метода, external ID у adapter boundary. Порядок enabled/environment validation сохранить.

## RED и проверки отдельным автором

- В real-composition `tests/integration/test_position_details_candles.py` fake operations записывает/проверяет ID; internal и external различны. Существующий fake не проверяет этот аргумент и маскирует дефект.
- Missing/cross-scope catalog ID: broker operations не вызывается, details сохраняет safe operations error и независимые candles semantics.
- Existing `test_portfolio_aggregation_service.py` position-operation scenario получает fake catalog; сохраняет фильтр BUY/SELL EXECUTED, сортировку и limit. Это обновление контракта метода, не weakening assertions.
- Adapter generic `get_operations` request contract остаётся external ID, cursor/date/limit не изменяются; disabled/environment приоритет прежний.

Не объединять исправление с A2 memoization или concurrent details fetching. Source scope ожидается: `services/portfolio.py` и docstring `usecases/automations.py`; tests — отдельный автор. Current-tree before snapshots до реализации; запросов рабочему брокеру не требуется.
