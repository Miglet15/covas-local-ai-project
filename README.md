# COVAS Local AI — Ship AI Bridge

A local AI bridge server for [COVAS:NEXT](https://www.covasnext.com/), powered by Ollama. Provides an OpenAI-compatible `/v1/chat/completions` endpoint that COVAS:NEXT connects to, with Elite Dangerous lore injection, long-term memory, live ship state parsing, and tiered web search via INARA/DuckDuckGo.

---

## Project Structure

```
covas-local-ai-project/
├── config/
│   └── config.json              # All tunable settings — edit this
├── data/
│   ├── commander_profile.md     # Your commander's background & preferences
│   ├── elite_lore.md            # Elite Dangerous lore injected into prompts
│   └── covas_memories.json      # Runtime: long-term memory store (auto-generated)
├── logs/
│   └── covas_session.log        # Runtime: session log (auto-generated, gitignored)
├── ship_ai_server.py            # Main server
├── covas_memory_client.py       # Memory service client (Apollo integration)
├── start_ship_ai.bat            # Windows launcher
└── README.md
```

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally with your chosen model pulled

```bash
pip install fastapi uvicorn pydantic langchain-ollama langchain-core ddgs
```

---

## Setup

1. Edit `config/config.json` to set your model, port, and preferences.
2. Place your commander profile in `data/commander_profile.md`.
3. Run the server:

```bash
python ship_ai_server.py
# or on Windows: double-click start_ship_ai.bat
```

4. Point COVAS:NEXT at `http://localhost:<server_port>/v1`.

Status page available at `http://localhost:<server_port>/`.

---

## Config Reference

| Key | Default | Description |
|---|---|---|
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
| `memory_max_entries` | `120` | Max stored memory segments |
