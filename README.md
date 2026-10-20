# DataLabs для «Пульса»

Ноутбуки Jupyter в «Пульсе» — для владельца и для агентов.

- **В «Файлах»** `.ipynb` открывается в Jupyter прямо в панели просмотра: ячейки, вывод, графики, запуск
  («Развернуть» — на всё окно). На телефоне — просмотр. «Создать ▾ → Ноутбук» — новый из шаблона.
- **JupyterLab** — пункт меню «DataLabs»: вкладки, файловый браузер по всем «Файлам», консоль, терминал,
  русский интерфейс, тема как у Пульса.
- **Агенты** считают в живом ядре Python (`pulse datalabs exec`), собирают из выполненных ячеек ноутбук для
  владельца (`save`), выполняют готовые ноутбуки с параметрами (`run`, papermill), читают их текстом (`read`),
  выгружают в HTML, PDF, py, md (`export`).
- **Отчёты по расписанию** — задание планировщика с `pulse datalabs report`: копия ноутбука выполняется с
  параметрами, выгружается в HTML/PDF в папку отчётов, владельцу приходит ссылка в Telegram и Пульс.

## Как устроено

- Docker-образ на `quay.io/jupyter/scipy-notebook` (pandas, numpy, scipy, scikit-learn, statsmodels, matplotlib,
  seaborn, openpyxl, ipywidgets) плюс polars, duckdb, plotly, pyarrow, papermill, nbconvert (PDF — Chromium).
  `%pip install` из ноутбука ставит пакеты в данные плагина — они переживают перезапуск и обновление.
- В контейнере — Jupyter Server (JupyterLab + Notebook 7) на 127.0.0.1 и сервер плагина. Страницы Jupyter
  открываются на адресе Пульса (`/plugin-apps/datalabs/jupyter/…`, право `embed`) за сессией владельца; токен
  Jupyter подставляет прокси плагина, в браузер он не уходит. Прокси пускает только Пульс и процессы сервера
  (проверка Host и Origin) — страница чужого сайта в другом браузере на сервере до ядра не дотянется.
- Корень Jupyter — все «Файлы» владельца (право `files:mount`, контракт 1.11): каталог, карточки, хранилища `@…`
  — то же дерево, что у агентов. Рабочая папка ядра — папка ноутбука, корень — `os.environ["PULSE_FILES"]`.
  Контрольные точки Jupyter — в данных плагина, а не папками `.ipynb_checkpoints` по «Файлам».
- Контейнер — в своей сети (`runtime.network: isolated`): интернет есть, сервисы сервера (Mongo, порты
  127.0.0.1) коду ноутбука не видны; лимит — 2 ГБ памяти и 2 CPU на все ядра. Токен и секреты плагина в
  окружение ядер не попадают.
- Ядра агентов — обычные ядра Jupyter (сессии «агент: <имя>»): владелец видит их в JupyterLab; простаивающие
  останавливаются (`kernel_idle_minutes`).

## Команды агентов

```bash
pulse datalabs exec --code '…' [--session S] [--cwd ПАПКА] [--timeout 300] | --code_file ПУТЬ
pulse datalabs sessions | save --path P.ipynb [--title …] | reset | close --session S
pulse datalabs run --path P.ipynb [--params '{"k": "v"}'] [--output P2.ipynb]
pulse datalabs read --path P.ipynb [--cells 2-7] [--outputs false]
pulse datalabs export --path P.ipynb --to html|pdf|py|md [--no_input true] [--output …]
pulse datalabs report --path P.ipynb [--params …] [--to ПАПКА] [--format pdf] [--code true] [--notify false]
pulse datalabs check
```

Правила для агентов — `skills/datalabs.md`.

## Разработка

```bash
pip install -r backend/requirements-dev.txt
pulse-plugin check
cd backend && python -m pytest          # без Jupyter и Docker
```

Проверка на живом Jupyter — образ (`docker build`) и `docker run` с `PULSE_PLUGIN_FS_DIR`, см. `CHANGELOG.md`.
Нужен Пульс с контрактом плагинов 1.11.
