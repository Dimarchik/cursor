# Ресерч: сеть и готовый бот для стратегии «снайп новой монеты → вернуть вложение на 2x → держать остаток»

Дата: 31.10.2026

## 1. Стратегия (как я её понял)

1. Следить за блокчейном и ловить момент создания новой монеты.
2. Сразу купить на маленькую сумму (например, $0.10).
3. Когда цена вырастет примерно в 2 раза (с запасом на комиссии) — продать ровно такую часть, чтобы вернуть вложенное.
4. Остаток («moonbag») держать бесконечно в надежде на 100x–1000x.
5. Приложение показывает баланс и все позиции.

## 2. Какая сеть сейчас подходит

| Сеть | Лаунчпады | Состояние на осень 2026 | Вывод |
|---|---|---|---|
| **Solana** | pump.fun, letsbonk.fun, StonkFun | ~30 000 новых монет в день, крупнейший мем-сегмент (~$3.8 млрд против ~$0.3 млрд у Base). Комиссии сети — доли цента. Больше всего готовых open-source инструментов. | **Основной выбор** |
| **Robinhood Chain** | Noxa, pump.fun (поддержка с 08.07.2026), Pools.trade (Uniswap) | Мейннет запущен 01.07.2026 (Ethereum L2). Задумывалась под токенизированные акции, но мемкоины забрали почти весь объём (флагман — CASHCAT). Летом был пик, в сентябре–октябре объёмы, комиссии и число транзакций сильно упали. EVM-сеть, поэтому много honeypot-скамов (купить можно, продать нельзя). | Хайповая, но уже остывает. Готовых надёжных ботов почти нет. Вариант на второй этап. |
| **BNB Chain** | four.meme | В октябре 2025 ненадолго обогнала pump.fun по объёму. Свежих надёжных данных за 2026 нет. | Запасной вариант |
| **Base** | Zora, Clanker, Virtuals (AI-агенты) | Ниша AI-токенов, мем-объём на порядок меньше Solana | Не приоритет |

**Рекомендация: начинаем с Solana / pump.fun.** Там больше всего запусков, самые дешёвые транзакции и есть единственный найденный бот, которому можно доверять (см. ниже).

### Неприятная статистика pump.fun (её нужно учитывать)
- Из ~9 млн монет (ноябрь 2025 – октябрь 2026) **79% торговались только в день запуска**.
- До «выпуска» на DEX (graduation) доходит примерно **1 монета из 58**, а в плохие периоды — 0.2–0.3% монет (июнь 2026).
- Первые блоки после создания монеты забирают профессиональные снайперы с Jito-бандлами и Geyser/shred-стримами, а часто и сам создатель (dev-bundle). Бот на обычном RPC почти всегда покупает **после** них, то есть по более высокой цене, и часто становится их «выходной ликвидностью».
- Независимых данных о прибыльности снайпинга в 2026 году нет. Все цифры про «+X% ROI» взяты из рекламы ботов.

## 3. Важная экономика: депозит за токен-аккаунт (rent)

На Solana токены хранятся не прямо в кошельке, а в отдельном **токен-аккаунте** для каждой монеты. Сеть требует положить на него залог **0.00203928 SOL** (≈ $0.20 при SOL ≈ $100). Это не догадка: в коде бота это константа `TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE = 2_039_280` lamports (`bot/src/core/pubkeys.py:17`), а их модуль `cleanup/` существует как раз для того, чтобы этот залог возвращать.

- **Купил и продал всё** → аккаунт пустой, его закрывают, и залог возвращается целиком. Здесь вы правы: SOL ушёл и вернулся.
- **Купил, продал часть, остаток держишь** (это и есть наша стратегия) → аккаунт закрыть нельзя, пока в нём есть токены, и залог ~$0.20 **лежит в нём, пока держишь монету**. Он не пропадает: продашь остаток — вернётся.

При покупке на $0.10 залог в 2 раза больше самой покупки. С $1 на кошельке бот успеет открыть всего 3–4 позиции, после чего SOL кончится: всё лежит в залогах.

Остальные комиссии небольшие: сеть ~$0.001, priority fee ~$0.002, комиссия pump.fun ~1–1.25% на покупку и на продажу.

**Реалистичный минимум для теста:** 0.1–0.2 SOL ($10–20) на кошельке и покупка по 0.005 SOL (~$0.5) на монету. Тогда залог — заметная, но не основная часть.

**Сколько продавать на 2x:** при `recover_buffer: 0.05` бот продаёт 52.5% позиции. Это возвращает вложенное с запасом 5% на комиссии; ~47.5% остаётся в холде.

## 4. Готовые проекты на GitHub

### ✅ Рекомендуемый: [chainstacklabs/pumpfun-bonkfun-bot](https://github.com/chainstacklabs/pumpfun-bonkfun-bot)
- Сделан Chainstack — известной компанией-провайдером RPC-нод. ~1 000 ⭐, Apache 2.0, активно поддерживается (последний коммит 04.10.2026, обновления в октябре 2026).
- Python. Зависимости чистые и стандартные: `solana`, `solders`, `websockets`, `aiohttp`, `grpcio` и т.п. Подозрительных пакетов нет.
- Работает с pump.fun, letsbonk.fun и StonkFun **напрямую, без сторонних API**.
- Ловит новые монеты через logs / blocks / Geyser / shreds. Есть фильтры (возраст, имя, создатель), TP/SL, выход по времени, `marry_mode` (купить и держать), `yolo_mode` (торговать непрерывно), очистка аккаунтов.
- **Чего не хватает под нашу стратегию:**
  - частичной продажи (сейчас TP продаёт всю позицию). Нужен режим «на ×2 продать ~52%, остальное держать»;
  - режима paper-trading / dry-run (сейчас торговля сразу реальными деньгами);
  - дашборда с балансом и списком moonbag-позиций (сейчас есть только логи).
- Сами авторы предупреждают: «NOT FOR PRODUCTION, для обучения».
- Также у Chainstack есть [pumpfun-cli](https://github.com/chainstacklabs/pumpfun-cli) — CLI для ручной покупки и продажи (пригодится для управления moonbag-позициями).

### ⚠️ Устаревший, но честный: [warp-id/solana-trading-bot](https://github.com/warp-id/solana-trading-bot)
- ~2 300 ⭐, TypeScript, но последний коммит в августе 2024 и он под Raydium, а не pump.fun. Для нас не подходит.

### ⚠️ Архивный: [1fge/pump-fun-sniper-bot](https://github.com/1fge/pump-fun-sniper-bot)
- Go, ~300 ⭐, проект в архиве, протокол pump.fun с тех пор менялся. Подходит только чтобы изучить код.

### ❌ Признаки скама — НЕ запускать
Поиск по «pump.fun sniper» выдаёт много репозиториев с типичными признаками ловушек:
- репозиторий «создан» в 2013–2014 году, хотя pump.fun появился в 2024 (захваченный или перепрофилированный старый аккаунт): `mortdeus/solana-copy-sniper-mev-trading-bot` (4 500 ⭐!), `coffellas-cto/Solana-Copy-Trading-Bot`;
- описание забито ключевыми словами, есть ссылка на Telegram «для покупки полной версии»;
- накрученные звёзды и форки.

Задокументированный случай ([SlowMist, 2025](https://www.bitget.cloud/news/detail/12560604846548)): «solana-pumpfun-bot» на GitHub с накрученными звёздами подтягивал вредоносный npm-пакет (`crypto-layout-utils`, `bs58-encrypt-utils`), который искал на компьютере файлы с приватными ключами и опустошал кошельки. Правила:
- приватный ключ только от **отдельного тестового кошелька** с минимальной суммой;
- перед запуском проверять зависимости и lock-файл;
- звёзды и форки ничего не доказывают.

## 5. Что сделано

Бот Chainstack скопирован в `bot/` и доработан под стратегию (`exit_strategy: "recover_and_hold"`). Подробности в [README.md](README.md).

## 6. План дальше

1. ~~Форк Chainstack-бота + стратегия `recover_and_hold`~~ — сделано.
2. ~~Просмотр холд-позиций и баланса~~ — сделано: `tools/moonbags.py` (CLI).
3. **Paper-trading (dry-run):** бот ловит реальные монеты и считает виртуальные сделки без отправки транзакций. Так за 1–2 дня можно бесплатно понять, сколько монет доходит до ×2.
4. Фильтры против явного скама: dev-bundle, доля создателя, повторяющиеся создатели-рагпуллеры.
5. Нужен **платный RPC** с WebSocket (Chainstack, Helius, QuickNode — у многих есть бесплатный тариф). Публичный RPC Solana не выдержит нагрузку.

## Источники
- [KuCoin: Meme coins & launchpads to watch, Oct 2026](https://www.kucoin.com/news/flash/meme-coins-still-matter-7-assets-and-launchpads-to-watch-in-october-2026)
- [crypto.news: Memecoin outlook Q4 2026](https://crypto.news/memecoin-outlook-for-q4-2026-can-the-sector-find-lasting-buyers/)
- [CoinCentral: Memecoin statistics 2026](https://coincentral.com/memecoin-statistics-2026-market-cap-returns-lifespan-data-and-why-97-fail)
- [Solana Compass: pump.fun 42 000 tokens in one day](https://solanacompass.com/news/pumpfun-launched-42000-tokens-in-one-day-fewer-than-2-will-ever-reach-a-dex)
- [arXiv 2607.02823: pump.fun survival analysis](https://arxiv.org/abs/2607.02823)
- [DEXTools: pump.fun graduation rate 0.26%](https://www.dextools.io/news/pump-fun-graduation-collapse-solana-fees-2026)
- [The Block: graduation rate jumps after BOOST](https://theblock.co/news/defi/2026-07-29-pump-fun-token-graduation-rate-jumps-boost-changes-launch-incentives-409815)
- [Fortune: Robinhood Chain memecoins](https://fortune.com/crypto/2026/07/13/robinhood-chain-memecoin-trading-cash-cat-vlad-tenev-crypto/)
- [crypto.news: Robinhood built an RWA chain. Memecoins took it.](https://crypto.news/robinhood-chain-memecoins-tokenized-stocks/)
- [CryptoTicker: Robinhood Chain memecoins guide (Oct 2026)](https://cryptoticker.io/en/robinhood-chain-memecoins-explained/)
- [The Block: Robinhood Chain volume and users fade](https://theblock.co/post/409813/robinhood-chain-deposits-climb-volume-users-fade-memecoin-fueled-launch)
- [Crypto Briefing: Robinhood Chain memecoin trading fell 96%](https://cryptobriefing.com/robinhood-chain-memecoin-trading-surge-collapse/)
- [The Defiant: FourMeme surpasses Pumpfun](https://thedefiant.io/news/defi/fourmeme-surpasses-pumpfun-with-usd43-million-in-monthly-revenue)
- [SlowMist via Bitget: fake Solana bot on GitHub steals keys](https://www.bitget.cloud/news/detail/12560604846548)
- [Dysnix: stack of competitive Solana sniper bots](https://dysnix.com/blog/complete-stack-competitive-solana-sniper-bots)
