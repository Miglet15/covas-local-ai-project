# COVAS Local AI — Ship AI Bridge

> a local LLM middleware layer for [COVAS:NEXT](https://github.com/RatherRude/Elite-Dangerous-AI-Integration), because paying per-token to talk to my spaceship felt morally incorrect  
> vibe-coded with Claude. it works. mostly. we don't talk about the parts that don't.

---

## what this is

[COVAS:NEXT](https://github.com/RatherRude/Elite-Dangerous-AI-Integration) is an Elite Dangerous AI companion — it reads your game state, listens to your voice, and responds as your ship's computer in actual spoken words like we're living in the 34th century and not at our desks at 2am pretending we have our lives together. it supports speech-to-text, text-to-speech, and an LLM backend of your choosing. normally that backend is OpenAI or some other cloud service you're quietly paying too much for.

this project is that backend, running entirely on your own machine.

it's a Python FastAPI server that presents an OpenAI-compatible `/v1/chat/completions` endpoint. COVAS:NEXT doesn't know the difference. under the hood it's talking to [Ollama](https://ollama.com/), with a few layers of extra context piled on top:

- **lore injection** — Elite Dangerous universe context automatically added to every prompt so your local model actually knows what a Thargoid is
- **commander profile** — your character's background, preferences, and personality, loaded into context. yes, this is where you get to decide who your commander is. take your time with it
- **long-term memory** — notable things from past sessions get stored and recalled later so your AI doesn't develop amnesia every time you close the game and pretend you weren't just doing this for three hours
- **live ship state** — parses current game state into the prompt (where you are, what you're flying, etc.)
- **web search** — tiered INARA / DuckDuckGo lookups so it can answer questions about systems, stations, and factions without confidently making things up

the end result: a ship's computer that runs locally, costs nothing per query, actually has continuity between sessions, and will address your commander however you told it to. no notes.

---

## this is a two-repo system

> **this server doesn't run alone.** long-term memory is handled by a separate companion service.

the memory backend lives in **[covas-apollo-project](https://github.com/Miglet15/covas-apollo-project/)** — a microservice that runs on a home server (or any always-on machine you have sitting around), receives session transcripts from this server, runs them through a small local LLM to extract structured memories, and stores them in SQLite.

**why split it out?** because the gaming PC is doing enough already, and a home server with Docker can quietly handle memory extraction in the background without touching your framerates. also it's a better architecture than a json file that gets read on every request. (the json file is still there for fallback. i know. i know.)

### full architecture

```
COVAS:NEXT
    │  voice I/O, game integration, UI
    │  HTTP → localhost
    ▼
covas-local-ai-project  (this repo — gaming PC)
    │  FastAPI, Ollama, lore + ship state injection, web search
    │
    ├──► Ollama  (local model, inference)
    │
    └──► covas-apollo-project  (home server — Docker)
              FastAPI memory service, Phi-3 Mini, SQLite
              ← stores structured memories from session transcripts
              → recalled into context on next session
```

you need both repos running for the full experience. if the memory service is unreachable, the server degrades gracefully — it'll keep going, just without persistent memory. sometimes things work best when they're allowed to be incomplete.

---

## requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally with your chosen model pulled
- [COVAS:NEXT](https://github.com/RatherRude/Elite-Dangerous-AI-Integration) installed
- **[covas-apollo-project](https://github.com/Miglet15/covas-apollo-project/)** running somewhere on your network (required for memory)

```bash
pip install fastapi uvicorn pydantic langchain-ollama langchain-core ddgs
```

---

## setup

**1. get the memory service running first**

see [covas-apollo-project](https://github.com/Miglet15/covas-apollo-project/) for setup. it's Docker-based so it's mostly just `docker compose up -d --build` once you've placed the files. you'll need Ollama on that machine too, pulling `phi3:mini` for memory extraction.

once it's up, confirm it's healthy:
```bash
curl http://your-server-ip:8100/health
# {"status":"ok"}
```

**2. pull a model for this server**

something with decent context length and instruction following. `llama3.1:8b` is the default and roughly the floor for tool-calling to work reliably. 14b+ is more comfortable if your hardware can do it. bigger isn't always better but it helps here.

```bash
ollama pull llama3.1:8b
```

**3. configure**

edit `config/config.json`. at minimum: model name, port, and the memory service address. full reference below.

**4. write your commander profile**

`data/commander_profile.md` — freeform markdown, completely up to you. this is the part that makes it actually yours rather than a generic spaceship voice. name, background, faction allegiances, how you want it to address you, what tone you want it to take, what it should and shouldn't bring up. it gets injected into every system prompt.

this is also where you can tell it things about your commander that matter to you — pronouns, titles, whatever. it'll use them. that's kind of the whole point of having a profile at all.

spend time on this one. it pays off.

**5. start the server**

```bash
python ship_ai_server.py
```

or on windows, double-click `start_ship_ai.bat`.

once running, the server exposes a status dashboard at `http://localhost:<port>/` — see the [status page](#status-page) section below for what it shows and how it's styled.

**6. point COVAS:NEXT at it**

in COVAS:NEXT settings, set your LLM provider to custom/OpenAI-compatible and point it at:

```
http://localhost:<port>/v1
```

that's it. COVAS:NEXT will talk to this server exactly like it would any other API. it genuinely doesn't care what's behind the endpoint, which is very useful for our purposes.

---

## project structure

```
covas-local-ai-project/
├── config/
│   └── config.json              # all the knobs — edit this
├── data/
│   ├── commander_profile.md     # your character — this one actually matters
│   ├── elite_lore.md            # ED universe context injected into every prompt
│   └── covas_memories.json      # fallback memory store (auto-generated)
├── logs/
│   └── covas_session.log        # session log (auto-generated, gitignored)
├── ship_ai_server.py            # the server
├── covas_memory_client.py       # memory client — talks to the memory service
├── start_ship_ai.bat            # windows launcher
└── README.md
```

---

## status page

the server serves a local status dashboard at `http://localhost:<port>/` that gives you a live at-a-glance view of what's running:

- server health and current config (model, port, temperature)
- memory service connection status
- recent session activity and memory count
- current ship state if one has been received

it auto-refreshes so you can leave it open in a browser tab while you play and feel like you're operating a real ship computer rather than a python process on your gaming rig. which is essentially what this entire project is about.

**EDHM-UI theming** — the page is styled using theme files from [EDHM-UI](https://github.com/BlueMystical/EDHM_UI), the HUD mod UI for Elite Dangerous. so instead of looking like a generic FastAPI debug page, it actually matches the aesthetic of the game — dark panels, the right kind of orange/amber tones, feels like something that belongs in the cockpit. the theme files live in the repo and are served statically.

---

## config reference

| key | default | description |
|-----|---------|-------------|
| `ollama_model` | `llama3.1:8b` | which Ollama model to use |
| `temperature` | `0.7` | model temperature |
| `server_port` | `8080` | port COVAS:NEXT connects to |
| `max_history_messages` | `10` | conversation turns kept per request |
| `history_gap_minutes` | `8` | inactivity gap before history clears |
| `log_max_sessions` | `5` | past sessions retained in log file |
| `max_tool_iterations` | `5` | tool-call rounds per request |
| `max_search_results` | `5` | web search results fetched per query |
| `max_memories_recalled` | `5` | memory segments injected per prompt |
| `memory_enabled` | `true` | toggle long-term memory on/off |
| `memory_max_entries` | `120` | max stored memory segments (fallback store) |

---

## notes / known state of things

- the `dev` branch is active. it may lag behind my local copy after a long session — i push when things feel stable enough to share, which is not always immediately
- model quality matters a lot. smaller models will fumble the tool-calling required for web search and memory recall. 8b is the rough floor, 14b+ is noticeably better at staying coherent through multi-step tool use
- if the memory service is unreachable the client fails gracefully and falls back to the local JSON store. you lose cross-session persistence but nothing actually breaks, which is more than you can say for most things
- INARA handles Elite Dangerous-specific queries (systems, stations, factions). DuckDuckGo handles everything else
- the fallback memory is a flat JSON file. it's fine. not everything needs to be a whole thing

---

## related

- **[covas-apollo-project](https://github.com/Miglet15/covas-apollo-project/)** — the companion memory service. run this too
- [COVAS:NEXT](https://github.com/RatherRude/Elite-Dangerous-AI-Integration) — the actual Elite Dangerous integration this bridges to. the real project. go star it
- [EDHM-UI](https://github.com/BlueMystical/EDHM_UI) — HUD color mod for Elite Dangerous, theme files used for the status page
- [Ollama](https://ollama.com/) — local model runtime
- [INARA](https://inara.cz/) — Elite Dangerous companion site, used for faction/system lookups

---

*Elite Dangerous and related assets are property of Frontier Developments.*
