# COVAS Local AI — Ship AI Bridge

A local AI bridge server for [COVAS:NEXT](https://www.covasnext.com/), powered by Ollama. Provides an OpenAI-compatible `/v1/chat/completions` endpoint that COVAS:NEXT connects to, with Elite Dangerous lore injection, long-term memory via a dedicated memory microservice, live ship state parsing, and tiered web search via INARA/DuckDuckGo.

---

## Project Structure

```
covas-local-ai-project/
├── config/
│   └── config.json              # All tunable settings — auto-created on first run
├── data/
│   ├── commander_profile.md     # Your commander's background & preferences
│   └── elite_lore.md            # Elite Dangerous lore injected into context
├── logs/
│   └── covas_session.log        # Runtime: session log (auto-generated, gitignored)
├── themes/
│   └── <ThemeName>/
│       └── ThemeSettings.json   # EDHM-UI theme export — drop folders here
├── ship_ai_server.py            # Main server (UNIT-01)
├── covas_memory_client.py       # Apollo memory service client
├── start_ship_ai.bat            # Windows launcher
└── README.md
```

### Apollo Memory Service (separate machine / Docker)

```
covas-memory/
├── main.py          # FastAPI service — exposes /ingest, /memories/*, /ed/missions/*
├── storage.py       # SQLite layer (WAL mode, sessions/memories/entities/ed_missions)
├── summarizer.py    # Ollama/Phi-3 Mini extraction of structured memories from raw text
├── models.py        # Pydantic schemas
├── Dockerfile
└── requirements.txt
```

---

## Requirements

### UNIT-01 (main machine)
- Python 3.11+
- [Ollama](https://ollama.com/) running locally with your chosen model pulled

```
pip install fastapi uvicorn pydantic langchain-ollama langchain-core ddgs requests httpx
```

### Apollo (memory microservice machine)
- Docker + Docker Compose
- Any model supported by Ollama (phi3:mini recommended for low-spec hardware)

---

## Setup

### UNIT-01

1. Edit `config/config.json` to set your model, port, and Apollo address (auto-created on first run with defaults).
2. Optionally place `data/commander_profile.md` and `data/elite_lore.md`.
3. Run the server:

```
python ship_ai_server.py
# or on Windows: double-click start_ship_ai.bat
```

4. Point COVAS:NEXT at `http://localhost:<server_port>/v1`.
5. Status dashboard available at `http://localhost:<server_port>/`.

### Apollo

```bash
cd covas-memory
docker compose up -d --build
docker exec -it ollama ollama pull phi3:mini
curl http://localhost:8100/health
```

Apollo's dashboard is available at `http://<apollo-ip>:8100/`.

---

## Config Reference (`config/config.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `ollama_base_url` | `http://localhost:11434` | Ollama endpoint |
| `ollama_model` | `llama3.1:8b` | Model to use |
| `server_host` | `0.0.0.0` | Host to bind |
| `server_port` | `8080` | Port COVAS:NEXT connects to |
| `temperature` | `0.7` | Model temperature |
| `max_history_messages` | `10` | Conversation turns kept per request |
| `request_timeout_sec` | `120` | Ollama request timeout |
| `search_cache_size` | `50` | Max cached search results |
| `max_search_results` | `5` | Results fetched per web search |
| `log_max_sessions` | `5` | Past sessions retained in log file |
| `memory_service_url` | `http://192.168.1.65:8100` | Apollo memory service address |
| `memory_interval_sec` | `300` | How often to snapshot session to Apollo (seconds) |
| `memory_enabled` | `true` | Enable/disable Apollo memory integration |

---

## Theming

Drop any EDHM-UI exported `ThemeSettings.json` into a named folder under `themes/`:

```
themes/
└── Blood Dimension/
    └── ThemeSettings.json
```

The status dashboard will detect it automatically. You can also upload theme files directly via the theme panel in the dashboard's bottom-right corner. Themes are saved to browser localStorage and survive page refreshes.

---

## Memory Architecture

```
UNIT-01                          Apollo (OptiPlex 3020)
────────                         ──────────────────────
COVAS session  ──── /ingest ───► FastAPI service
(raw text)                       │
                                 ├── Phi-3 Mini (Ollama)
                                 │   extracts structured JSON
                                 │
                                 └── SQLite (WAL)
                                     sessions / memories /
                                     entities / ed_missions
```

Every 300 seconds (configurable), and on clean shutdown, the session buffer is sent to Apollo. Apollo processes it asynchronously — COVAS never blocks waiting for memory. UNIT-01 can query `/memories/recent`, `/ed/missions/active`, and `/memories/query` at any time.
