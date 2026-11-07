FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/matplotlib \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-compile --only-binary=:all: -r requirements.txt \
    && python -m pip check

COPY run_agent.py harness.py planner.py budget_manager.py llm_gateway.py llm_fallback.py \
     math_solver.py search_solver.py data_solver.py coding_solver.py safe_files.py /app/
COPY Dockerfile .dockerignore /app/

ENTRYPOINT ["python"]
CMD ["/app/run_agent.py", "--tasks-dir", "/data/tasks"]
