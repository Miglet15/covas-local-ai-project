# COVAS Local AI — Ship AI Bridge

A local AI co-pilot system for [COVAS:NEXT](https://www.covasnext.com/), powered by Ollama.
Provides an OpenAI-compatible `/v1/chat/completions` endpoint that COVAS:NEXT connects to,
with Elite Dangerous lore injection, long-term memory via a dedicated memory service,
live ship state parsing from ED journal files, and tiered web search via INARA/DuckDuckGo.

---

## Architecture — Two Servers

This project spans two separate machines with distinct roles.

### UNIT-01 — Gaming & AI Server
UNIT-01 is the machine where everything game-related runs:

- **Elite Dangerous** — the game itself
- **COVAS:NEXT** — the voice co-pilot frontend that connects to the AI bridge
- **`ship_ai_server.py`** — the COVAS AI bridge server (this repo's main server)
- **`covas_memory_client.py`** — background client that forwards session transcripts to Apollo

UNIT-01 hosts the AI bridge on port `8080` (configurable). COVAS:NEXT points at `http://localhost:8080/v1`.

### Apollo — Memory & Summarisation Server
Apollo is a separate machine running Ubuntu with Docker. Its sole purpose is long-term memory:

- Receives session transcripts from UNIT-01 at regular intervals and on session end
- Uses **Phi-3 Mini via Ollama** to extract and structure memories from raw conversation logs
- Stores everything in a persistent SQLite database
- Exposes a REST API that UNIT-01 queries to inject relevant memories into each prompt

Apollo runs on port `8100`. UNIT-01 reaches it over the local network or Tailscale.

```
┌─────────────────────────────────────────┐     ┌──────────────────────────────────┐
│               UNIT-01                   │     │              Apollo              │
│                                         │     │                                  │
│  Elite Dangerous ──► COVAS:NEXT         │     │  Docker                          │
│                          │              │     │  ├── ollama  (phi3:mini)          │
│                          ▼              │     │  └── covas-memory  :8100         │
│               ship_ai_server.py :8080   │────►│       ├── /ingest                │
│               covas_memory_client.py    │◄────│       ├── /stats                 │
│                          │              │     │       ├── /errors                │
│                          ▼              │     │       ├── /memories/recent        │
│               Ollama (llama3.1:8b)      │     │       └── /ed/missions/active     │
└─────────────────────────────────────────┘     └──────────────────────────────────┘
```

---

## Project Structure

```
covas-local-ai-project/
│
│  ── UNIT-01 files ──────────────────────────────────────
├── ship_ai_server.py            # Main AI bridge server
├── covas_memory_client.py       # Memory service client (sends to Apollo)
├── start_ship_ai.bat            # Windows launcher
│
├── config/
│   └── config.json              # All tunable settings
├── data/
│   ├── commander_profile.md     # Commander background & preferences
│   ├── elite_lore.md            # Elite Dangerous lore injected into prompts
│   └── covas_memories.json      # Runtime memory store (auto-generated)
├── logs/
│   └── covas_session.log        # Session log (auto-generated, gitignored)
│
│  ── Apollo (Docker) files ──────────────────────────────
├── docker-compose.yml           # Spins up Ollama + covas-memory
└── covas-memory/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py                  # FastAPI memory service
    ├── storage.py               # SQLite layer
    ├── summarizer.py            # Ollama/Phi-3 memory extraction
    └── models.py                # Pydantic schemas
```

---

## UNIT-01 Setup

### Requirements
- Python 3.11+
- [Ollama](https://ollama.com/) running locally with your chosen model pulled
- COVAS:NEXT installed

```bash
pip install fastapi uvicorn pydantic langchain-ollama langchain-core ddgs requests httpx
```

### 1. Configure
Edit `config/config.json` (created automatically on first run with defaults):

| Key | Default | Description |
|-----|---------|-------------|
| `ollama_model` | `llama3.1:8b` | Ollama model to use |
| `temperature` | `0.7` | Model temperature |
| `server_port` | `8080` | Port COVAS:NEXT connects to |
| `max_history_messages` | `10` | Conversation turns kept per request |
| `history_gap_minutes` | `8` | Inactivity gap before history is cleared |
| `log_max_sessions` | `5` | Past sessions retained in the log file |
| `max_tool_iterations` | `5` | Tool-call rounds per request |
| `max_search_results` | `5` | Web search results fetched per query |
| `max_memories_recalled` | `5` | Memory segments injected per prompt |
| `memory_enabled` | `true` | Enable/disable long-term memory |
| `memory_service_url` | `http://192.168.1.65:8100` | Apollo's address |
| `memory_interval_sec` | `300` | How often to push transcripts to Apollo |

### 2. Add your Commander profile
Place your background and preferences in `data/commander_profile.md`.

### 3. Run
```bash
python ship_ai_server.py
# or on Windows:
start_ship_ai.bat
```

### 4. Point COVAS:NEXT at it
Set the API endpoint in COVAS:NEXT to: `http://localhost:8080/v1`

Status page: `http://localhost:8080/`

---

## Apollo Setup

### Requirements
- Docker + Docker Compose
- The `covas-memory/` folder and `docker-compose.yml` from this repo

### 1. Pull the Phi-3 Mini model
```bash
docker exec -it ollama ollama pull phi3:mini
```

### 2. Place files on Apollo
```
/home/mike/covas/
├── docker-compose.yml
└── covas-memory/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py
    ├── storage.py
    ├── summarizer.py
    └── models.py
```

### 3. Build and start
```bash
cd /home/mike/covas
docker compose up -d --build
```

### 4. Verify
```bash
curl http://localhost:8100/health
# {"status": "ok", "time": "..."}
```

Status page: `http://localhost:8100/`

---

## Apollo API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Service health check |
| `GET` | `/stats` | Runtime stats — polled by UNIT-01 status page |
| `POST` | `/ingest` | Receive session transcript from UNIT-01 |
| `POST` | `/memories/query` | Search/filter memories |
| `GET` | `/memories/recent` | Most recent memories |
| `GET` | `/memories/session/{id}` | All memories for a session |
| `GET` | `/ed/missions/active` | Active ED missions |
| `PATCH` | `/ed/missions/{id}/status` | Update mission status |
| `GET` | `/errors` | Full error log (persisted across restarts) |
| `DELETE` | `/errors` | Clear all errors from log and DB |

---

## Memory Categories

| Category | Used for |
|----------|----------|
| `general` | General conversation |
| `elite_dangerous` | Game events, exploration, combat |
| `person` | Named individuals (NPCs or real) |
| `place` | Systems, stations, locations |
| `preference` | Commander preferences and settings |
| `task` | Tasks or follow-ups |

---

## UNIT-01 Status Page

The status page at `http://localhost:8080/` provides a live dashboard showing:

- UNIT-01 and Apollo online status
- Request, search, and memory counters
- Session log viewer (with syntax highlighting and line count control)
- Recent memories table
- Active ED missions
- Memory error log with **Clear All** to dismiss stale errors after a planned restart
- EDHM theme engine — import any `ThemeSettings.json` from the EDHM UI mod to recolour the entire UI

The page auto-refreshes every 10 seconds but **pauses** while any panel or the theme drawer is open so you aren't interrupted mid-read.

---

## Database (Apollo)

SQLite stored at `/data/covas_memory.db` inside the container, mapped to a Docker named volume (`memory_data`) for persistence across restarts and rebuilds.

```bash
# Inspect directly on Apollo
docker exec -it covas-memory sqlite3 /data/covas_memory.db ".tables"
docker exec -it covas-memory sqlite3 /data/covas_memory.db "SELECT * FROM memories ORDER BY created_at DESC LIMIT 5;"
docker exec -it covas-memory sqlite3 /data/covas_memory.db "SELECT * FROM error_log ORDER BY id DESC LIMIT 10;"
```
