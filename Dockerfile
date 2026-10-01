FROM python:3.12-slim
WORKDIR /app
COPY boatdata /app/boatdata
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "boatdata", "--data", "/data", "daemon"]
