# Установка DataLabs

Инструкция для агента Пульса после `pulse plugins install datalabs`. Пройди её целиком.

## 1. Проверь, что плагин запустился

```bash
pulse plugins list            # у datalabs — status: running
pulse datalabs check          # файлы владельца, Jupyter, ядро Python и версии пакетов, лимит памяти
```

Нужен Docker: при установке собирается образ с Jupyter и data-стеком (первый раз — 5–15 минут, около 4 ГБ
на диске). Нужен Пульс с контрактом плагинов 1.11 (дерево «Файлов» в контейнере, своя сеть, лимит памяти).
Status error или check не ok — `pulse plugins logs datalabs --lines 80`, расскажи владельцу, что в журнале, и
остановись. «Файлы владельца» не ok — `pulse plugins restart datalabs`.

## 2. Закрепи страницу

```bash
pulse plugins pin datalabs main
```

## 3. Покажи владельцу

Создай пример и выполни его:

```bash
pulse datalabs exec --session demo --code 'import numpy as np, pandas as pd, matplotlib.pyplot as plt
df = pd.DataFrame({"день": pd.date_range("2026-10-01", periods=30), "значение": np.random.randn(30).cumsum()})
df.plot(x="день", y="значение", title="Пример"); df.describe()'
pulse datalabs save --session demo --path "Plugins/DataLabs/Пример.ipynb" --title "Пример DataLabs" --overwrite true
pulse datalabs close --session demo
```

Расскажи владельцу коротко:
- ноутбуки `.ipynb` открываются прямо в «Файлах» — [Пример](/files?path=Plugins/DataLabs/Пример.ipynb); новый — «Создать ▾ →
  Ноутбук»; весь JupyterLab (вкладки, терминал, файлы) — пункт меню [DataLabs](/p/datalabs/main);
- код ноутбука видит все его «Файлы»: рабочая папка — папка ноутбука, корень — `os.environ["PULSE_FILES"]`;
- агенты считают в живом ядре и собирают из этого ноутбуки, выполняют и выгружают его ноутбуки в HTML/PDF, а
  отчёт по расписанию ставят заданием планировщика (`pulse datalabs report`);
- пакеты сверх стандартных ставятся `%pip install …` в ячейке и остаются;
- память всех ядер вместе — до 2 ГБ, простаивающее ядро останавливается через час (Настройки → Плагины → DataLabs).

## Обновление

`pulse plugins update datalabs` — ноутбуки лежат в «Файлах» владельца и обновлением не затрагиваются; пакеты из
`%pip install` и настройки JupyterLab — в данных плагина, тоже остаются.
