# Исходная точка C2/C3, 8 ноября 2026

HEAD: 854b4da0736777572f9d26cc43fd83438f977244

```text
## feature/harness-v1...origin/feature/harness-v1
 M .dockerignore
 M .gitignore
 M Dockerfile
 M README.md
 M data_solver.py
 M harness.py
 M llm_fallback.py
 M planner.py
 M run_agent.py
 M tests/docker_smoke.py
 M tests/test_runtime.py
?? diagnostics/
?? reports/
?? runtime_limits.py
?? tests/fixtures/
?? tests/test_a1_resume.py
?? tests/test_data_groups.py
?? tests/test_diagnostics.py
?? tests/test_fallback_pilot.py
?? tests/test_runtime_limits.py
```

```text
 .dockerignore         |   1 +
 .gitignore            |   8 ++++
 Dockerfile            |   2 +-
 README.md             |  73 +++++++++++++++++++++++++++++-----
 data_solver.py        |  71 ++++++++++++++++++++++++++-------
 harness.py            | 108 ++++++++++++++++++++++++++++++++++----------------
 llm_fallback.py       |  15 ++++---
 planner.py            |  19 ++++++++-
 run_agent.py          |  10 ++++-
 tests/docker_smoke.py |   3 +-
 tests/test_runtime.py |   2 +-
 11 files changed, 242 insertions(+), 70 deletions(-)
```

Локальный baseline из проверенного C1: 41 correct, 0 wrong, 87 skipped, 230/503.
WSL/Docker C1: 420/420. Search: 12/32. Квалификация: 4/20 local, 9/20 mock-good.
Снимок файлов и сырые результаты этой сессии: /tmp/harness-c2-c3-mhl1hitf/.
Новые реальные запросы запрещены; исторические отчёты не перезаписываются.
