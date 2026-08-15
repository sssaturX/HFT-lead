# hftv2

Paper-бот lead-lag: Binance/Bitget ведут, MEXC отстаёт. Live-ордера через webtoken **выключены** и не подключены к `record`.

## Установка

```powershell
cd D:\CODING\hftv2.0
python -m pip install -e .
python -m pytest tests -q
```

Нужен Python 3.11+.

---

## 1. Paper (это запускать завтра на US-сессии)

Ордера на MEXC **не отправляются**. Пишутся котировки + paper fills.

```powershell
python run.py record --hours 1.5
```

Лучшее окно для акций: примерно **15:30–22:00 CEST** (открытие NYSE). After-hours импульсов мало.

Остановить раньше: `Ctrl+C`. Папка прогона: `data\run-YYYYMMDD-HHMMSS`.

Короткий смоук (фиды живы):

```powershell
python run.py record --seconds 10 --skip-quotes
```

`--skip-quotes` не пишет сырые котировки (меньше диск, нельзя потом replay). Для нормального прогона **не ставь** `--skip-quotes`.

### Отчёт по готовому прогону

```powershell
python run.py report data\run-20260814-200923
```

### Подобрать пороги без рынка (нужен quotes.jsonl)

```powershell
python run.py replay data\run-20260814-200923
```

---

## 2. Live webtoken (ручной ордер, не автобот)

Токен **только** в `.env`, не в yaml:

1. Войти в [MEXC futures](https://www.mexc.com) в браузере.
2. F12 → Network → любой запрос на `futures.mexc.com`.
3. Заголовок `authorization`, значение начинается с `WEB`.
4. Файл `.env` в корне проекта:

```
MEXC_WEB_TOKEN=WEB_вставь_сюда
```

Проверить баланс (чтение, ордер не шлёт):

```powershell
python run.py live-status
```

Замерить скорость до MEXC (токен не нужен):

```powershell
python run.py live-ping
```

Клиент держит одно TLS-соединение (keep-alive): первый запрос ~1s (рукопожатие),
дальше каждый ордер = один тёплый round-trip (~200ms из Европы). Спецификация
контракта (тик, minVol) качается один раз и кэшируется.

Посмотреть, какой ордер **был бы** (сейчас `dry_run: true` — **не отправит**):

```powershell
python run.py live-order --symbol SNDKSTOCK_USDT --side long --vol 1 --market --confirm
```

В JSON должны быть `takeProfitPrice` / `stopLossPrice` и `"sent": false`.

### Чтобы ордер реально ушёл

В `config.yaml`:

```yaml
live:
  enabled: true
  dry_run: false
```

И та же команда с `--confirm`. Нужны **все три**: `enabled true` + `dry_run false` + `--confirm`.

Закрыть позицию (сам найдёт сторону, `positionId` и объём — как кнопка Close на сайте):

```powershell
python run.py live-close --symbol SNDKSTOCK_USDT --confirm
```

Частичное закрытие — `--vol N`. Ручной вариант через `live-order --side close_long --position-id ID` тоже остался.

`vol` — **контракты MEXC**, не доллары. У SNDK `contractSize=0.001`, 1 контракт ≈ цена × 0.001 USDT (на ~$1650 это ≈ $1.65).

---

## Конфиг сейчас (`config.yaml`)

| Что | Значение |
|---|---|
| Paper старт | $20, **50x**, delay **100±50ms**, стоп **1%** |
| Fill-check | не входить, если лаг уже съеден |
| Frequent | SKHYNIX, SNDK — impulse/edge **3.0 / 3.0** |
| Strict | SPCX/SOXL/AMD **8/8**, SUI/PEPE **4/4** |
| Live TP | **3 bps** от входа, по тику контракта |
| Live SL | **100 bps (1%)** |
| Live | `enabled: false`, `dry_run: true` |

`record` **никогда** не вызывает live-клиент. Даже если включишь `live.enabled`, paper останется paper.

---

## Команды одной памяткой

```powershell
python -m pytest tests -q
python run.py record --hours 1.5
python run.py report data\run-XXXX
python run.py replay data\run-XXXX
python run.py live-status
python run.py live-ping
python run.py live-order --symbol SNDKSTOCK_USDT --side long --vol 1 --market --confirm
python run.py live-close --symbol SNDKSTOCK_USDT --confirm
```
