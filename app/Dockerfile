FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Build context is the repository root. loadgen.py rides along so the load generator
# can run inside the cluster with the same image.
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /srv
COPY app/server.py rollout/loadgen.py ./

USER 10001
EXPOSE 8080
CMD ["python", "/srv/server.py"]
