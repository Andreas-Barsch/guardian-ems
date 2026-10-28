FROM python:3.12-alpine
WORKDIR /app
COPY hycube_sim /app/hycube_sim
COPY addon_entry.py /app/addon_entry.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/addon_entry.py"]
