FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY sekmet ./sekmet
RUN pip install --no-cache-dir .
COPY config ./config
RUN mkdir -p data keys reports scenarios
EXPOSE 8090
CMD ["sekmet", "serve"]
