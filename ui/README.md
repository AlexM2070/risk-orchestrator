# UI и визуализация (`ui/`) — Risk Orchestrator

Интерфейс на **NiceGUI + Plotly** (роль: Максим, ветка `feat/ui`).

## Быстрый запуск (автономно на `MockOrchestrator`)

Пока `core/` и `services/` разрабатываются в параллельных ветках, интерфейс полностью работает на встроенном `MockOrchestrator` и `MockReglamentScheduler`, строго реализующих контракты Раздела 5:

```bash
pip install -r requirements.txt
python -m ui.app
```

Дополнительные параметры:
- `--port 8080` — порт сервера (по умолчанию `8080`)
- `--host 127.0.0.1` — адрес прослушивания
- `--native` — запуск как десктопное окно (`native=True`)
- `--no-show` — не открывать браузер автоматически

## Подключение реального ядра и сервисов (`main.py`)

После мержа `feat/core` и `feat/services` код внутри `ui/` менять не требуется — `ui/contracts.py` автоматически подхватит типы из `core.models` и `services.scenarios`, а `main.py` просто передаёт реальные объекты в `run_ui`:

```python
from core.orchestrator import Orchestrator
from core.scheduler import ReglamentScheduler
from services.microservices import SharedDataLayer, create_services
from services.scenarios import generate_random_batch, get_demo_scenarios
from ui.app import run_ui

db = SharedDataLayer()
services = create_services(db)
orchestrator = Orchestrator(services=services, ...)
scheduler = ReglamentScheduler(orchestrator=orchestrator, ...)

run_ui(
    orchestrator=orchestrator,
    scheduler=scheduler,
    scenarios=get_demo_scenarios(),
    random_batch_fn=generate_random_batch,
)
```

## Структура пакета

- `ui/app.py` — главный экран, подписка на события оркестратора без утечек, точка входа `run_ui` / `create_app`
- `ui/contracts.py` — мост контрактов (`core.models` / `services.scenarios` с fallback на спецификацию Раздела 5)
- `ui/mock_backend.py` — полноценный `MockOrchestrator`, `MockReglamentScheduler` и 4 демо-сценария
- `ui/components/control_panel.py` — тумблеры «С оркестратором / Без оркестратора», «Регламент» + интервал, Пауза/Сброс, 4 кнопки демо-сценариев и случайный батч
- `ui/components/manual_form.py` — форма ручного запуска (процесс + превью READ/WRITE, дата, приоритет, тип запуска)
- `ui/components/dashboard.py` — 6 KPI-карточек, сравнение прогонов «До / После» и 3 колонки (`RUNNING`, `QUEUED` с `blocked_by`, `COMPLETED / FAILED`)
- `ui/components/gantt_chart.py` — live-диаграмма Ганта на Plotly (`queued_at`, `started_at`, `finished_at`, фазы ожидания и выполнения)
- `ui/components/event_log.py` — состояние 4 витрин, активные ключи `LockManager` и фильтруемый журнал событий
- `ui/components/conflict_matrix.py` — динамическая матрица ресурсных контрактов и попарных конфликтов READ/WRITE
- `ui/assets/styles.css` — кастомные стили и поддержка светлой/тёмной темы
