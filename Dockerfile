FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home colors && mkdir /data && chown colors:colors /data
COPY colors_server ./colors_server
COPY rules.example.json .
COPY tools/backup_database.py /app/backup_database.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "colors_server", "--host", "0.0.0.0", "--database", "/data/colors.sqlite3", "--tile-cache", "/data/tiles", "--rules", "/app/rules.example.json"]
