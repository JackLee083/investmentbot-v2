FROM python:3.11-slim
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p logs var
# -w 1 is MANDATORY, not a tuning choice: APScheduler runs in-process inside
# the app (bot_server.create_app() starts it), so 2 workers = 2 schedulers =
# every tick runs twice = DUPLICATE ORDERS. The tick_lock lease in
# jobs/tick.py is only the last-resort guard for this; do not rely on it.
# --threads 8 keeps the dashboard responsive while a tick is running.
# bot_server:app is the module-level `app = create_app()` object.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:5000", "bot_server:app"]
