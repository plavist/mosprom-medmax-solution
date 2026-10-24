FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Корневой/промежуточные УЦ Минцифры — нужны только для MAX API (platform-api2.max.ru)
COPY certs/russian_trusted_ca.pem /certs/russian_trusted_ca.pem
ENV MAX_CA_BUNDLE=/certs/russian_trusted_ca.pem
COPY app app
COPY assets assets
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
