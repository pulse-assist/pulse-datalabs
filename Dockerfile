# DataLabs: Jupyter Server (JupyterLab + Notebook 7) с data-стеком Python и сервер плагина.
# Установщик Пульса кладёт pulse_plugin в .pulse_plugin_sdk/ перед сборкой.
#
# scipy-notebook: pandas, numpy, scipy, scikit-learn, statsmodels, matplotlib, seaborn, openpyxl, ipywidgets.
FROM quay.io/jupyter/scipy-notebook:2026-09-29@sha256:1220239dc7ad223ebab9f28ae6e44f1b5861f21145f449dc7b1773b6e27a6c11

USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# сверх образа: polars, duckdb, plotly, pyarrow; papermill и nbconvert (PDF — Chromium через playwright);
# русский интерфейс JupyterLab
COPY backend/requirements.txt /plugin/backend/requirements.txt
RUN pip install --no-cache-dir polars duckdb plotly pyarrow jupyterlab-language-pack-ru-RU \
        -r /plugin/backend/requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY .pulse_plugin_sdk /tmp/pulse_plugin
RUN pip install --no-cache-dir /tmp/pulse_plugin && rm -rf /tmp/pulse_plugin

# настройки JupyterLab по умолчанию: русский, без проверки обновлений и новостей
COPY jupyter/overrides.json /opt/conda/share/jupyter/lab/settings/overrides.json

COPY . /plugin
WORKDIR /plugin/backend

# %pip install из ноутбука — в данные плагина: пакеты переживают перезапуск и обновление; там же секрет подписи
# доверенных ноутбуков (общий у сервера плагина и Jupyter)
ENV PYTHONUSERBASE=/plugin/data/python \
    JUPYTER_DATA_DIR=/plugin/data/jupyter/data \
    PIP_USER=1 \
    PULSE_PLUGIN_PORT=8080

# процессы — от root: дерево «Файлов» (FUSE) Пульса открыто только его владельцу. start.sh образа при запуске
# от root переключился бы на jovyan — поэтому только tini
ENTRYPOINT ["tini", "-g", "--"]
CMD ["sh", "-c", "exec uvicorn datalabs_plugin.app:app --host 0.0.0.0 --port ${PULSE_PLUGIN_PORT}"]
