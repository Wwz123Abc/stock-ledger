FROM python:3.12-slim
# tzdata: time zones for the market-hours logic, in case the image has no system zoneinfo
RUN pip install --no-cache-dir tzdata && useradd --system --uid 1000 ledger
WORKDIR /app
COPY app.py ./
COPY static ./static
RUN mkdir data && chown ledger:ledger data
USER ledger
VOLUME /app/data
EXPOSE 5090
CMD ["python", "app.py", "serve", "--host", "0.0.0.0", "--port", "5090"]
