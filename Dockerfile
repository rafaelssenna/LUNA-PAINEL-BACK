FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip  && pip install --no-cache-dir -r requirements.txt

COPY . .

# Porta do app (pode sobrepor com PORT no runtime)
EXPOSE 8000

# Logs detalhados p/ ver qualquer erro de startup do FastAPI/DB
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level debug"]
