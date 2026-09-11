# ADR 0003: T-Invest SDK через изолированный async adapter

**Статус:** принято  
**Дата:** 2026-08-04

## Контекст

MVP должен работать только с T-Invest Sandbox и поддерживать портфель, позиции, операции, свечи, котировки, стакан, streaming и sandbox-заявки. Рассматривались официальный Python SDK, прямой gRPC, REST и WebSocket.

Официальный registry T-Bank на дату решения публикует `t-tech-investments 1.49.3`. Публичная версия PyPI устарела и не используется.

## Решение

- использовать `t-tech-investments>=1.49.3,<1.50.0` из официального registry;
- использовать `AsyncSandboxClient` внутри `TTechInvestAsyncAdapter`;
- доменные и прикладные слои зависят только от собственного порта `BrokerClient`;
- преобразовывать SDK `MoneyValue` и `Quotation` в доменные `Decimal`-типы внутри adapter;
- реализовать собственные policies для deadline, rate limit, retry, reconnect и reconciliation;
- создавать UUID идемпотентности до первой отправки заявки;
- не повторять command-вызов с новым UUID после неопределённого результата;
- тестировать adapter на синтетических SDK-ответах без токена и сети.

## Почему не direct API

Прямой gRPC требует поддерживать proto, code generation, metadata, error mapping и streaming lifecycle без функционального преимущества для первого Python MVP. REST и WebSocket добавляют ручное JSON-преобразование чисел и сообщений; для sandbox WebSocket endpoint недостаточно подтверждён официальной документацией.

Direct gRPC остаётся допустимым новым adapter, если SDK блокирует требуемый метод, отстаёт от proto или не позволяет контролировать transport/performance.

## Последствия

- SDK не проникает за инфраструктурную границу;
- замена transport не меняет торговую логику;
- официальный registry обязателен в локальной установке, Docker и CI;
- версия и хэш должны закрепляться при появлении lock/constraints workflow;
- streaming после reconnect всегда выполняет resubscribe и reconciliation snapshot.

## Официальные источники

- [Python SDK и установка](https://developer.tbank.ru/invest/sdk/python_sdk/faq_python)
- [Каталог T-Invest API](https://developer.tbank.ru/invest/api)
- [Протоколы](https://developer.tbank.ru/invest/intro/developer/protocols/)
- [gRPC](https://developer.tbank.ru/invest/intro/developer/protocols/grpc/)
- [Лимиты](https://developer.tbank.ru/invest/intro/intro/limits)
- [Stream-соединения](https://developer.tbank.ru/invest/intro/developer/stream)
- [OrdersService](https://developer.tbank.ru/invest/services/orders/methods)
- [Асинхронные заявки](https://developer.tbank.ru/invest/services/orders/async)
