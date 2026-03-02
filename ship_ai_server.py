import sys
import os
import json
import re
import time
import threading
from datetime import datetime
from collections import OrderedDict

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "covas_session.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_log_lock = threading.Lock()

def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def log_exchange(role: str, content: str):
    """Log a full Commander/COVAS exchange to the session log."""
    separator = "─" * 60
    with _log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{separator}\n")
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {role.upper()}\n")
                f.write(content + "\n")
        except Exception:
            pass
    # Feed exchange into memory buffer (non-blocking — _memory handles threading)
    if _memory is not None and content.strip():
        _memory.append(f"[{datetime.now().strftime('%H:%M:%S')}] {role.upper()}: {content}")

def pause_and_exit(code=1):
    print("\n" + "="*60)
    print("Server has stopped. Press any key to close this window...")
    print("="*60)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.getch()
        else:
            import tty, termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        input()
    sys.exit(code)

# ── Dependency check ──────────────────────────────────────────────────────────
REQUIRED = {
    "fastapi":           "fastapi",
    "uvicorn":           "uvicorn",
    "pydantic":          "pydantic",
    "langchain_ollama":  "langchain-ollama",
    "langchain_core":    "langchain-core",
    "ddgs":              "ddgs",
    "requests":          "requests",
    "httpx":             "httpx",
}

missing = []
for module, package in REQUIRED.items():
    try:
        __import__(module)
    except ImportError:
        missing.append(package)
        print(f"  [MISSING] {package}")

if missing:
    print("\n[ERROR] Missing required packages. Run:\n")
    print(f"  pip install {' '.join(missing)}\n")
    pause_and_exit(1)

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse, HTMLResponse
    from pydantic import BaseModel
    import uvicorn
    from langchain_ollama import ChatOllama
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
    from ddgs import DDGS
    import httpx
    log("All imports successful.")
except Exception as e:
    print(f"\n[ERROR] Import failed:\n  {e}")
    pause_and_exit(1)


# ── Load Config ───────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(SCRIPT_DIR, "data")
LOGS_DIR    = os.path.join(SCRIPT_DIR, "logs")
THEMES_DIR  = os.path.join(SCRIPT_DIR, "themes")
os.makedirs(THEMES_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config", "config.json")

DEFAULT_CONFIG = {
    "ollama_base_url":       "http://localhost:11434",
    "ollama_model":          "llama3.1:8b",
    "server_host":           "0.0.0.0",
    "server_port":           8080,
    "temperature":           0.7,
    "max_history_messages":  10,
    "request_timeout_sec":   120,
    "search_cache_size":     50,
    "max_search_results":    5,
    "log_max_sessions":      5,
    "memory_service_url":    "http://192.168.1.65:8100",
    "memory_interval_sec":   300,
    "memory_enabled":        True
}

if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        config = {**DEFAULT_CONFIG, **user_config}
        log(f"Config loaded from config.json")
    except Exception as e:
        log(f"WARN: Could not read config.json ({e}) — using defaults.")
        config = DEFAULT_CONFIG.copy()
else:
    config = DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log(f"Created default config.json — edit it to customise settings.")
    except Exception as e:
        log(f"WARN: Could not write default config.json: {e}")

OLLAMA_BASE_URL      = config["ollama_base_url"]
OLLAMA_MODEL         = config["ollama_model"]
SERVER_HOST          = config["server_host"]
SERVER_PORT          = config["server_port"]
TEMPERATURE          = float(config["temperature"])
MAX_HISTORY_MESSAGES = int(config["max_history_messages"])
REQUEST_TIMEOUT      = int(config["request_timeout_sec"])
SEARCH_CACHE_SIZE    = int(config["search_cache_size"])
MAX_SEARCH_RESULTS   = int(config["max_search_results"])
LOG_MAX_SESSIONS     = int(config["log_max_sessions"])
MEMORY_SERVICE_URL   = config["memory_service_url"]
MEMORY_INTERVAL_SEC  = int(config["memory_interval_sec"])
MEMORY_ENABLED       = bool(config["memory_enabled"])
# ── Memory Client (optional — graceful fallback if not present) ───────────────
_memory = None
if MEMORY_ENABLED:
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "covas_memory_client",
            os.path.join(SCRIPT_DIR, "covas_memory_client.py")
        )
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _MemoryClient = _mod.MemoryClient
        _memory = _MemoryClient(
            service_url=MEMORY_SERVICE_URL,
            interval=MEMORY_INTERVAL_SEC
        )
        log(f"Memory client initialised → {MEMORY_SERVICE_URL}")
    except FileNotFoundError:
        log("WARN: covas_memory_client.py not found — memory system disabled.")
    except Exception as _e:
        log(f"WARN: Memory client failed to load ({_e}) — memory system disabled.")

# ── Load Lore Book ────────────────────────────────────────────────────────────
LORE_FILE     = os.path.join(DATA_DIR, "elite_lore.md")
LORE_SECTIONS = {}

if os.path.exists(LORE_FILE):
    try:
        with open(LORE_FILE, "r", encoding="utf-8") as f:
            raw_lore = f.read()
        parts = re.split(r"\n(?=## )", raw_lore)
        for part in parts:
            if not part.strip():
                continue
            title = part.splitlines()[0].strip().lstrip("#").strip().lower()
            LORE_SECTIONS[title] = part.strip()
        log(f"Lore indexed: {len(LORE_SECTIONS)} sections  |  {len(raw_lore):,} total chars")
    except Exception as e:
        log(f"WARN: Could not load lore book: {e}")
else:
    log(f"WARN: No lore book found — place elite_lore.md in the data/ folder.")

# ── Load Commander Profile ────────────────────────────────────────────────────
PROFILE_FILE    = os.path.join(DATA_DIR, "commander_profile.md")
COMMANDER_PROFILE = ""

if os.path.exists(PROFILE_FILE):
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            COMMANDER_PROFILE = f.read().strip()
        log(f"Commander profile loaded ({len(COMMANDER_PROFILE):,} chars).")
    except Exception as e:
        log(f"WARN: Could not load commander profile: {e}")
else:
    log(f"WARN: No commander profile found — place commander_profile.md in the data/ folder.")


def _rotate_log():
    """Trim the session log to keep only the last LOG_MAX_SESSIONS sessions."""
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        marker = "=" * 60 + "\n"
        # Split on session start markers
        parts = text.split(marker)
        # Rebuild into (header + body) pairs — each session is 3 parts: empty, header, body
        # Simpler: find all positions of SESSION START
        idx = [i for i, p in enumerate(parts) if "SESSION START" in p]
        if len(idx) <= LOG_MAX_SESSIONS:
            return  # Nothing to trim
        # Keep only the last LOG_MAX_SESSIONS session blocks
        keep_from = idx[-LOG_MAX_SESSIONS]
        kept = marker.join(parts[keep_from:])
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(kept)
        log(f"Log rotated — kept last {LOG_MAX_SESSIONS} sessions.")
    except Exception as e:
        log(f"WARN: Log rotation failed: {e}")

# ── Lore Keyword Map ──────────────────────────────────────────────────────────
KEYWORD_MAP = {
    "ship":           ["ships", "ship modules & engineering"],
    "module":         ["ship modules & engineering"],
    "engineer":       ["ship modules & engineering"],
    "engineered":     ["ship modules & engineering"],
    "weapon":         ["combat", "ship modules & engineering"],
    "combat":         ["combat"],
    "fight":          ["combat", "the thargoids"],
    "bounty":         ["combat"],
    "conflict zone":  ["combat"],
    " res ":          ["combat"],
    "haz res":        ["combat"],
    "explor":         ["exploration"],
    "scan":           ["exploration"],
    "jump range":     ["exploration", "ship modules & engineering"],
    "neutron":        ["exploration"],
    "trade":          ["trading & economy"],
    "cargo":          ["trading & economy", "ships"],
    "market":         ["trading & economy"],
    "commodity":      ["trading & economy"],
    "mine":           ["mining"],
    "mining":         ["mining"],
    "asteroid":       ["mining"],
    "thargoid":       ["the thargoids"],
    "xeno":           ["the thargoids"],
    " ax ":           ["the thargoids"],
    "interceptor":    ["the thargoids"],
    "guardian":       ["the guardians"],
    "faction":        ["factions & superpowers"],
    "federation":     ["factions & superpowers"],
    "empire":         ["factions & superpowers"],
    "alliance":       ["factions & superpowers"],
    "powerplay":      ["powerplay"],
    "community goal": ["community goals (cgs)"],
    "rank":           ["pilots federation"],
    "galaxy":         ["the galaxy & setting"],
    "what is":        ["terminology & slang", "the galaxy & setting"],
    "what does":      ["terminology & slang"],
    "what are":       ["terminology & slang"],
    "pip":            ["controls & mechanics"],
    "heat":           ["controls & mechanics"],
    "supercruise":    ["controls & mechanics"],
    "credit":         ["credits & economy tips"],
    "money":          ["credits & economy tips"],
    "earn":           ["credits & economy tips"],
    "how do i make":  ["credits & economy tips"],
}

def get_relevant_lore(user_message: str) -> str:
    if not LORE_SECTIONS:
        return ""
    msg_lower = user_message.lower()
    sections_to_add = set()
    for keyword, titles in KEYWORD_MAP.items():
        if keyword in msg_lower:
            for title in titles:
                if title in LORE_SECTIONS:
                    sections_to_add.add(title)
    if not sections_to_add:
        return ""
    text = "\n\n".join(LORE_SECTIONS[s] for s in sections_to_add)
    log(f"Lore injected: {', '.join(sections_to_add)}  ({len(text):,} chars)")
    return text

# ── Conversational Guard ──────────────────────────────────────────────────────
CONVERSATIONAL_PATTERNS = [
    # Greetings / check-ins
    r"\bhow are you\b",
    r"\bhow('re| are) (your )?systems\b",
    r"\bare you (online|active|running|there|ready|awake|back)\b",
    r"\b(hello|hi|hey|greetings|good (morning|evening|afternoon))\b",
    r"\bwelcome back\b",
    r"\bback online\b",
    r"\bintroduce yourself\b",
    r"\bwhat('s| is) your name\b",
    r"\bwho are you\b",
    r"\btest(ing)?\b",
    # Ship / system status — handled natively by COVAS:NEXT
    r"\b(full |ship |system |run a )?system(s)? (report|check|status|diagnostic|readout|overview)\b",
    r"\b(display|show)( me)?( a| the)? (full |ship |system )?( report| status|diagnostic)\b",
    r"\brun diagnostics?\b",
    r"\ball systems\b",
    r"\bship status\b",
    r"\bstatus (report|check|update)\b",
    r"\bsystems (check|nominal|online|status|report)\b",
    r"\b(how('s| is)|what('s| is)) (the )?(ship|hull|shields?|power|fuel|cargo)\b",
    r"\b(you should be|you're|you are) (back )?(online|active|running|up)\b",
    r"\b(power|shields?|hull|fuel|cargo|thrusters?) (status|level|check|reading|report)\b",
    # Small talk / affirmations that should never trigger a search
    r"^\s*(mm+[-h]*|hmm+|yep|nope|yup|sure|ok+a*y*|alright|fair enough|cool|nice|perfect|great|awesome|roger|copy|understood|noted|right|fair|exactly|indeed|absolutely|agreed)\s*[.!]*\s*$",
    r"^\s*(so|we|yes|no|good|bad|fine|done|nice|wow|oh|ah|hm+|uh+|er+)\s*[.!]*\s*$",
    # Gratitude and social pleasantries
    r"\b(thank you|thanks|cheers|appreciate it|much appreciated|thank)\b",
    r"\b(good job|well done|nice work|good work|excellent)\b",
    r"\b(sorry|my bad|apologies|nevermind|never mind|forget it|ignore that)\b",
    r"\bno need\b",
    r"\bplease do\b",
    r"\bthat\'s (fine|ok|okay|good|correct|right|fair)\b",
    r"\bwe\'re (good|ok|okay|fine|set|ready)\b",
    r"\bgot it\b",
    r"\byou (hallucinated|made that up|invented|fabricated)\b",
    r"\bthat makes (no |doesn\'t make )?sense\b",
]

def is_conversational(message: str) -> bool:
    msg_lower = message.lower()
    return any(re.search(p, msg_lower) for p in CONVERSATIONAL_PATTERNS)

# ── Game Event Detection ──────────────────────────────────────────────────────
# COVAS:NEXT sends automated game state messages with these prefixes.
# These are context-only — never need a web search.
_GAME_EVENT_RE = re.compile(
    r"^\s*\[(IMPORTANT\s+)?Game Event[^\]]*\]",
    re.IGNORECASE
)

# Specific game events that need no response at all — just silent context
_SILENT_EVENTS = [
    r"has entered supercruise",
    r"is preparing to enter supercruise",
    r"frame shift drive charging",
    r"has cleared the current navigation route",
    r"flight stabilizer engaged",
    r"is charging fsd for hyperspace jump",
]

def is_game_event(message: str) -> bool:
    return bool(_GAME_EVENT_RE.match(message.strip()))

def is_silent_event(message: str) -> bool:
    msg_lower = message.lower()
    return is_game_event(message) and any(re.search(p, msg_lower) for p in _SILENT_EVENTS)

# ── System Prompt Builder ─────────────────────────────────────────────────────
BASE_SYSTEM_PROMPT = """You are COVAS, the onboard AI of an Elite Dangerous spacecraft.
You speak with the calm authority of a ship intelligence — precise, composed, and loyal to your Commander.
Respond in plain conversational sentences. Never use bullet points, numbered lists, headers, or bold text.
Keep all responses short — 1 to 3 sentences unless the Commander asks for detail.

GAME EVENTS: Messages starting with [Game Event] or [IMPORTANT Game Event] are automated status
notifications from ship systems. Acknowledge them briefly or stay silent — never search the web for them.
NPC names, pilot names, and ship names in game events are not searchable — do not attempt to look them up.

TOOL USE — web_search is available for one purpose only:
  Use it ONLY when the Commander explicitly asks you to look something up, find current prices,
  or check live data that cannot be answered from existing knowledge.
  NEVER use it for: game events, NPC names, pilot names, status updates, combat events,
  FSD jumps, supercruise events, docking events, greetings, or anything you can answer yourself.
  If in doubt — do not search.

IMPORTANT: Never mention, reference, or hint at your decision to search or not search.
Never say phrases like "I will not make a function call", "this doesn't warrant a search",
"since I can infer", or any similar internal reasoning. Just respond naturally in character.

When you do retrieve external data, present it as GalNet intelligence or long-range sensor data.
Never reference the internet, web searches, or external tools by name.

You have deep knowledge of Elite Dangerous: ships, modules, engineering, combat, exploration,
trading, factions, lore, and mechanics. Use that knowledge directly — always."""

def build_system_prompt(user_message: str) -> str:
    parts = [BASE_SYSTEM_PROMPT]

    if COMMANDER_PROFILE:
        parts.append(
            "\n\n══════════════════════════════════════════\n"
            "COMMANDER PROFILE\n"
            "══════════════════════════════════════════\n"
            + COMMANDER_PROFILE
            + "\n══════════════════════════════════════════"
        )

    lore = get_relevant_lore(user_message)
    if lore:
        parts.append(
            "\n\n══════════════════════════════════════════\n"
            "RELEVANT SHIP KNOWLEDGE DATABASE ENTRIES\n"
            "══════════════════════════════════════════\n"
            + lore
            + "\n══════════════════════════════════════════"
        )

    return "".join(parts)

# ── Verify Ollama is reachable ────────────────────────────────────────────────
import urllib.request, urllib.error

log(f"Checking Ollama at {OLLAMA_BASE_URL} ...")
try:
    urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
    log("Ollama is running.")
except urllib.error.URLError as e:
    print(f"\n[ERROR] Cannot reach Ollama at {OLLAMA_BASE_URL}")
    print(f"  Reason: {e.reason}")
    print("\n  Make sure Ollama is running:")
    print("    ollama serve")
    print(f"    ollama pull {OLLAMA_MODEL}")
    pause_and_exit(1)
except Exception as e:
    print(f"\n[ERROR] Unexpected error contacting Ollama: {e}")
    pause_and_exit(1)

# ── ED Topic Detection ───────────────────────────────────────────────────────
# Terms that indicate the query is Elite Dangerous specific.
# If matched, the search will be directed to INARA first.
ED_TOPIC_PATTERNS = [
    r"\b(star system|system|station|outpost|settlement|carrier|fleet carrier)\b",
    r"\b(ship|ships|module|modules|outfit|outfitting|weapon|thruster|fsd|shield)\b",
    r"\b(engineer|engineers|blueprint|grade|experimental effect|synthesis)\b",
    r"\b(commodity|commodities|trade route|market|demand|supply|price)\b",
    r"\b(material|materials|raw material|manufactured|encoded|data)\b",
    r"\b(mission|bounty|conflict zone|massacre|assassination|salvage)\b",
    r"\b(thargoid|guardian|barnacle|maelstrom|titan|interceptor|scout)\b",
    r"\b(powerplay|power|merits|fortify|undermine|expansion)\b",
    r"\b(community goal|cg|galnet|news|event)\b",
    r"\b(permit|faction|minor faction|allegiance|influence|state)\b",
    r"\b(nebula|black hole|neutron star|white dwarf|earth.like|water world|ammonia)\b",
    r"\b(explorer|exploration|cartography|mapping|scanning|fss|dss)\b",
    r"\b(mining|asteroid|core mining|laser mining|void opal|ltd|low temperature diamond)\b",
    r"\b(combat|pvp|ganker|wing|squadron|rank|elite|dangerous|cmdr|commander)\b",
    r"\belite.dangerous\b",
    r"\b(inara|spansh|eddb|edsm)\b",
]

def is_ed_topic(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in ED_TOPIC_PATTERNS)

# ── Search Cache ──────────────────────────────────────────────────────────────
_search_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()   # Guards all OrderedDict mutations

def _store_cache(key: str, result: str):
    with _cache_lock:
        _search_cache[key] = result
        _search_cache.move_to_end(key)
        if len(_search_cache) > SEARCH_CACHE_SIZE:
            _search_cache.popitem(last=False)

def _ddgs_search(query: str, max_results: int = 5) -> list:
    """Raw DDGS search — returns list of result dicts."""
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))

def _format_results(results: list) -> str:
    return "\n\n".join(
        f"Title: {r['title']}\nURL: {r['href']}\nSummary: {r['body']}"
        for r in results
    )

def _search_inara(query: str) -> str:
    """Search INARA directly using site-restricted DDG query."""
    try:
        inara_query = f"site:inara.cz {query}"
        results = _ddgs_search(inara_query, max_results=MAX_SEARCH_RESULTS)
        if results:
            log(f"INARA search returned {len(results)} result(s).")
            _stats["searches_inara"] += 1
            return _format_results(results)
        return "NO_RESULTS"
    except Exception as e:
        log(f"INARA search error: {e}")
        return f"SEARCH_FAILED: {str(e)}"

def _search_general(query: str) -> str:
    """Fallback general web search."""
    try:
        results = _ddgs_search(query, max_results=max(1, MAX_SEARCH_RESULTS - 1))
        if results:
            log(f"General search returned {len(results)} result(s).")
            _stats["searches_general"] += 1
            return _format_results(results)
        return "NO_RESULTS"
    except Exception as e:
        log(f"General search error: {e}")
        return f"SEARCH_FAILED: {str(e)}"

def _tiered_search(query: str) -> str:
    """
    Tiered search strategy:
      1. ED topic detected → try INARA first
      2. INARA returns nothing → fall back to general web search
      3. Non-ED topic → skip INARA, go straight to general search
    """
    if is_ed_topic(query):
        log(f'ED topic detected, querying INARA: "{query}"')
        result = _search_inara(query)
        if result not in ("NO_RESULTS",) and not result.startswith("SEARCH_FAILED"):
            return result
        log("INARA returned no results — falling back to general search.")

    return _search_general(query)

def cached_search(query: str) -> str:
    key = query.strip().lower()
    with _cache_lock:
        if key in _search_cache:
            log(f'Cache hit: "{query}"')
            _search_cache.move_to_end(key)
            return _search_cache[key]
    result = _tiered_search(query)
    _store_cache(key, result)
    return result

def run_web_search(query: str) -> str:
    """Public search entry point with in-character error handling."""
    result = cached_search(query)
    if result == "NO_RESULTS":
        return "[GalNet returned no data for this query. Advise Commander accordingly in character.]"
    if result.startswith("SEARCH_FAILED"):
        return "[GalNet link is not responding. Inform the Commander that the long-range network is unavailable, in character, without breaking immersion.]"
    return result


# ── Tool definition ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Retrieve external intelligence data from the GalNet long-range network. "
                "Use ONLY as a last resort when the Commander explicitly requests a search, "
                "or when real-time data is required such as current commodity prices, live "
                "community goals, or recent patch notes that cannot be answered from existing "
                "knowledge. Do NOT use for general game knowledge, lore, mechanics, greetings, "
                "status checks, or anything already known. When in doubt, do not search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"]
            }
        }
    }
]

# ── Build LLM ─────────────────────────────────────────────────────────────────
log(f"Loading model '{OLLAMA_MODEL}' (temperature={TEMPERATURE}) ...")
try:
    llm = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=TEMPERATURE, timeout=REQUEST_TIMEOUT)
    llm_with_tools    = llm.bind_tools(TOOLS)
    llm_without_tools = llm
    log("Model ready.")
except Exception as e:
    print(f"\n[ERROR] Failed to initialize model:\n  {e}")
    pause_and_exit(1)

# ── Raw JSON Tool Call Detection ──────────────────────────────────────────────
_JSON_TOOL_RE = re.compile(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*\}', re.DOTALL)

def find_raw_tool_call(content: str) -> dict | None:
    for match in _JSON_TOOL_RE.finditer(content):
        try:
            data   = json.loads(match.group(0))
            name   = data.get("name") or data.get("function")
            params = data.get("parameters") or data.get("args") or data.get("arguments") or {}
            if name and isinstance(params, dict):
                return {"name": name, "args": params, "id": "fallback-tool-call", "raw_match": match.group(0)}
        except (json.JSONDecodeError, AttributeError):
            continue
    return None

def strip_json_tool_calls(content: str) -> str:
    content = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", content, flags=re.DOTALL)
    content = _JSON_TOOL_RE.sub("", content)
    return content.strip()

# ── Conversation History Truncation ──────────────────────────────────────────
def truncate_history(messages: list) -> list:
    """
    Keep the system message(s) at the front intact.
    Truncate the remaining conversation to MAX_HISTORY_MESSAGES,
    always keeping the most recent messages.
    """
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs  = [m for m in messages if not isinstance(m, SystemMessage)]

    if len(other_msgs) > MAX_HISTORY_MESSAGES:
        dropped = len(other_msgs) - MAX_HISTORY_MESSAGES
        other_msgs = other_msgs[-MAX_HISTORY_MESSAGES:]
        log(f"History truncated: dropped {dropped} old message(s), keeping last {MAX_HISTORY_MESSAGES}.")

    return system_msgs + other_msgs

# ── Ollama Watchdog ───────────────────────────────────────────────────────────
def check_ollama_alive() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return True
    except Exception:
        return False

# ── Tool Execution Loop ───────────────────────────────────────────────────────
def run_with_tools(messages: list, use_tools: bool = True, max_iterations: int = 5) -> str:
    active_llm = llm_with_tools if use_tools else llm_without_tools
    messages   = truncate_history(messages)

    for iteration in range(max_iterations):
        log(f"Thinking... (step {iteration + 1}/{max_iterations})")
        t_start = time.time()

        try:
            response = active_llm.invoke(messages)
        except Exception as e:
            err = str(e).lower()
            if "timeout" in err or "connection" in err:
                log(f"Ollama connection error: {e}")
                if check_ollama_alive():
                    log("Ollama still alive — retrying once...")
                    time.sleep(2)
                    try:
                        response = active_llm.invoke(messages)
                    except Exception as e2:
                        log(f"Retry failed: {e2}")
                        return "COVAS systems are experiencing interference, Commander. Please stand by."
                else:
                    log("Ollama is not responding.")
                    return "COVAS is offline — ship AI core is not responding, Commander."
            else:
                log(f"Model error: {e}")
                return f"COVAS encountered an error: {str(e)}"

        elapsed = time.time() - t_start
        log(f"Model responded in {elapsed:.1f}s")
        messages.append(response)

        # Path 1: Structured tool calls
        if use_tools and response.tool_calls:
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]
                log(f"[TOOL] {tool_name}({tool_args})")
                if tool_name == "web_search":
                    log("Retrieving intelligence data...")
                    t_s = time.time()
                    result = run_web_search(tool_args.get("query", ""))
                    log(f"Data retrieved in {time.time() - t_s:.1f}s")
                else:
                    result = f"Unknown tool: {tool_name}"
                messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
            continue

        # Path 2: Raw JSON embedded in response text
        if use_tools and response.content:
            raw_tc = find_raw_tool_call(response.content)
            if raw_tc:
                tool_name = raw_tc["name"]
                tool_args = raw_tc["args"]
                log(f"[TOOL-FALLBACK] {tool_name}({tool_args})")

                # Normalize hallucinated search-like tool names to web_search
                SEARCH_ALIASES = {
                    "retrieve", "retrieve external intelligence data",
                    "galnet", "galnet search", "intelligence", "search",
                }
                # Tool calls that are hallucinated ship actions — just strip and continue
                SHIP_ACTION_NAMES = {
                    "dockingrequest", "set course", "navigate", "kovax_planner",
                    "prepare for jump", "calculate navigation", "navigation",
                    "dock", "undock", "jump", "plotroute", "route",
                }
                normalized = tool_name.lower().strip()

                if normalized in SHIP_ACTION_NAMES:
                    # Model hallucinated a ship action — strip it, return nothing
                    log(f"[TOOL-FALLBACK] Hallucinated ship action stripped: {tool_name}")
                    cleaned = strip_json_tool_calls(response.content)
                    if cleaned:
                        return cleaned
                    # If nothing left after strip, let the model try again without tools
                    messages.append(
                        HumanMessage(content="[System: respond in plain text only, no JSON or function calls]")
                    )
                    continue

                elif normalized == "web_search" or normalized in SEARCH_ALIASES:
                    query = tool_args.get("query", "") or tool_args.get("q", "")
                    if not query:
                        log("[TOOL-FALLBACK] Empty query — skipping search")
                        messages.append(
                            HumanMessage(content="[System: respond in plain text only, no JSON or function calls]")
                        )
                        continue
                    log("Retrieving intelligence data (fallback)...")
                    t_s = time.time()
                    result = run_web_search(query)
                    log(f"Data retrieved in {time.time() - t_s:.1f}s")
                    messages.append(
                        HumanMessage(content=f"[Intelligence retrieved]\n{result}\n\nRespond to the Commander in plain sentences, no bullet points or lists.")
                    )
                else:
                    log(f"[TOOL-FALLBACK] Unknown tool stripped: {tool_name}")
                    messages.append(
                        HumanMessage(content="[System: respond in plain text only, no JSON or function calls]")
                    )
                continue

        # Path 3: Normal text response
        cleaned = strip_json_tool_calls(response.content) if response.content else ""
        log(f"Response ready ({len(cleaned)} chars)")
        return cleaned

    cleaned = strip_json_tool_calls(response.content) if response.content else ""
    log("Max iterations reached.")
    return cleaned

# ── Streaming Response Builder ────────────────────────────────────────────────
def make_stream_chunk(content: str, finish: bool = False) -> str:
    chunk = {
        "id": "chatcmpl-local-stream",
        "object": "chat.completion.chunk",
        "model": OLLAMA_MODEL,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if not finish else {},
            "finish_reason": "stop" if finish else None
        }]
    }
    return f"data: {json.dumps(chunk)}\n\n"

async def stream_response(text: str):
    # Yield the response in small chunks so COVAS:NEXT can start processing early
    words = text.split(" ")
    buffer = []
    for word in words:
        buffer.append(word)
        if len(buffer) >= 5:
            yield make_stream_chunk(" ".join(buffer) + " ")
            buffer = []
            await __import__("asyncio").sleep(0)
    if buffer:
        yield make_stream_chunk(" ".join(buffer))
    yield make_stream_chunk("", finish=True)
    yield "data: [DONE]\n\n"

# ── Server Stats (for status page) ───────────────────────────────────────────
_stats = {
    "start_time":       datetime.now(),
    "requests_total":   0,
    "requests_ok":      0,
    "requests_failed":  0,
    "searches_inara":   0,
    "searches_general": 0,
    "cache_hits":       0,
    "last_request":     None,
}

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="COVAS Ship AI Bridge")

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str = "ship-ai"
    messages: list[Message]
    stream: bool = False

@app.get("/", response_class=HTMLResponse)
async def status_page():
    # ── UNIT-01 local stats ───────────────────────────────────────────────────
    uptime      = datetime.now() - _stats["start_time"]
    h, rem      = divmod(int(uptime.total_seconds()), 3600)
    m, s        = divmod(rem, 60)
    uptime_str  = f"{h}h {m}m {s}s"
    last_req    = _stats["last_request"].strftime("%H:%M:%S") if _stats["last_request"] else "---"
    cache_count = len(_search_cache)
    mem_session = _memory.session_id if _memory is not None else "---"
    total_searches = _stats['searches_inara'] + _stats['searches_general']

    lore_cls     = "ok"   if LORE_SECTIONS     else "warn"
    lore_txt     = f"{len(LORE_SECTIONS)} sections" if LORE_SECTIONS else "Not loaded"
    profile_cls  = "ok"   if COMMANDER_PROFILE else "warn"
    profile_txt  = f"{len(COMMANDER_PROFILE):,} chars" if COMMANDER_PROFILE else "Not loaded"
    fail_cls     = "err"  if _stats["requests_failed"] > 0 else "ok"

    # ── Apollo memory service stats ───────────────────────────────────────────
    apollo_ok           = False
    mem_total           = "---"
    mem_db_sessions     = "---"
    mem_entities        = "---"
    mem_db_missions     = "---"
    mem_active_missions = "---"
    mem_errors          = "---"
    mem_last_ingest     = "---"
    by_category         = {}
    recent_memories     = []
    active_missions     = []

    if _memory is not None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as _hx:
                _r = await _hx.get(f"{MEMORY_SERVICE_URL}/stats")
                if _r.status_code == 200:
                    _d                  = _r.json()
                    apollo_ok           = True
                    mem_total           = _d.get("total_memories", 0)
                    mem_db_sessions     = _d.get("total_sessions", 0)
                    mem_entities        = _d.get("total_entities", 0)
                    mem_db_missions     = _d.get("total_missions", 0)
                    mem_active_missions = _d.get("active_missions", 0)
                    mem_errors          = _d.get("errors", 0)
                    mem_last_ingest     = _d.get("last_ingest") or "None yet"
                    by_category         = _d.get("by_category") or {}
                    recent_memories     = _d.get("recent_memories") or []
                if apollo_ok:
                    _rm = await _hx.get(f"{MEMORY_SERVICE_URL}/ed/missions/active")
                    if _rm.status_code == 200:
                        active_missions = _rm.json().get("missions", [])
        except Exception:
            pass

    apollo_cls = "ok" if apollo_ok else ("err" if _memory is not None else "dim")
    apollo_lbl = "ONLINE" if apollo_ok else ("UNREACHABLE" if _memory is not None else "DISABLED")
    err_color  = "#ff4455" if mem_errors not in ("---", 0, "0") else "#3de87a"

    cat_colors = {
        "general": "#c89040", "elite_dangerous": "#ff8020",
        "person": "#60b8d8", "place": "#80c860",
        "preference": "#a878cc", "task": "#e06050",
    }

    # Build category rows
    cat_rows = ""
    for cat, count in by_category.items():
        col = cat_colors.get(cat, "#6080a0")
        cat_rows += (
            f'<div class="cat-row">'
            f'<span class="tag" style="color:{col};border-color:{col}30">{cat}</span>'
            f'<span class="cat-count">{count}</span>'
            f'</div>'
        )
    if not cat_rows:
        cat_rows = '<div class="dim-txt" style="padding:8px 0;font-size:11px">No memories recorded yet</div>'

    # Build recent memory rows
    mem_rows = ""
    for rec in recent_memories:
        ts      = str(rec.get("created_at",""))[:16].replace("T"," ")
        cat     = rec.get("category","")
        topic   = rec.get("topic","")
        summary = rec.get("summary","")
        short   = (summary[:100] + "…") if len(summary) > 100 else summary
        col     = cat_colors.get(cat, "#6080a0")
        mem_rows += (
            f'<tr><td class="ts-cell">{ts}</td>'
            f'<td><span class="tag" style="color:{col};border-color:{col}30">{cat}</span></td>'
            f'<td><div class="topic-txt">{topic}</div>'
            f'<div class="sum-txt">{short}</div></td></tr>'
        )
    if not mem_rows:
        mem_rows = '<tr><td colspan="3" class="empty-cell">NO MEMORY RECORDS FOUND</td></tr>'

    # Build mission rows
    mission_rows = ""
    for ms in active_missions:
        mission_rows += (
            f'<tr>'
            f'<td><span class="mission-tag">{ms.get("mission_type","UNKNOWN")}</span></td>'
            f'<td>{ms.get("giver","---")}</td>'
            f'<td>{ms.get("origin_system","?")} &rsaquo; {ms.get("origin_station","?")}</td>'
            f'<td>{ms.get("destination_system","?")} &rsaquo; {ms.get("destination_station","?")}</td>'
            f'<td class="credit-txt">{ms.get("reward","---")}</td>'
            f'</tr>'
        )
    if not mission_rows:
        mission_rows = '<tr><td colspan="5" class="empty-cell">NO ACTIVE MISSIONS ON RECORD</td></tr>'

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="10">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>COVAS // SYSTEM STATUS</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@300;400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:      #05070e;
      --bg2:     #080c16;
      --bg3:     #0a0f1c;
      --border:  #182038;
      --or:      #e06a10;
      --or2:     #ff8830;
      --or-glow: rgba(224,106,16,0.15);
      --am:      #c09020;
      --teal:    #20a0b0;
      --text:    #6888b0;
      --hi:      #a8c4e0;
      --ok:      #30d870;
      --err:     #f03848;
      --warn:    #e09020;
      --dim:     #283850;
      --mono:    'Share Tech Mono', monospace;
      --head:    'Rajdhani', sans-serif;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: var(--mono); background: var(--bg); color: var(--text); min-height: 100vh; overflow-x: hidden; }}
    body::before {{
      content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 100;
      background: repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,0,0,0.07) 3px,rgba(0,0,0,0.07) 4px);
    }}
    body::after {{
      content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 99;
      background: radial-gradient(ellipse 80% 80% at 50% 50%,transparent 40%,rgba(0,0,0,0.6) 100%);
    }}

    /* ── HEADER ── */
    .hdr {{ display:flex; align-items:center; justify-content:space-between; padding:0 32px; height:84px; border-bottom:1px solid var(--border); background:linear-gradient(180deg,#0b1020 0%,var(--bg) 100%); position:relative; }}
    .hdr::after {{ content:''; position:absolute; bottom:0; left:0; right:0; height:1px; background:linear-gradient(90deg,transparent,var(--or),transparent); opacity:0.4; }}
    .hdr-glow {{ position:absolute; inset:0; pointer-events:none; background:radial-gradient(ellipse 50% 150% at 50% -50%,var(--or-glow),transparent); }}
    .logo {{ display:flex; align-items:baseline; gap:12px; position:relative; }}
    .logo-main {{ font-family:var(--head); font-size:42px; font-weight:700; letter-spacing:8px; color:var(--or2); text-shadow:0 0 24px var(--or-glow),0 0 48px var(--or-glow); }}
    .logo-slash {{ color:var(--dim); font-family:var(--head); font-size:32px; font-weight:300; }}
    .logo-sub {{ font-family:var(--head); font-size:16px; font-weight:300; letter-spacing:5px; color:var(--am); text-transform:uppercase; }}
    .hdr-right {{ text-align:right; font-size:13px; color:var(--dim); line-height:2; letter-spacing:1px; }}
    .hdr-right em {{ font-style:normal; color:var(--am); }}

    /* ── STATUS BAR ── */
    .sbar {{ display:flex; align-items:stretch; border-bottom:1px solid var(--border); background:var(--bg2); height:52px; }}
    .spill {{ display:flex; align-items:center; gap:10px; padding:0 24px; font-family:var(--head); font-size:14px; font-weight:600; letter-spacing:3px; text-transform:uppercase; border-right:1px solid var(--border); }}
    .spill.right {{ margin-left:auto; border-right:none; border-left:1px solid var(--border); font-weight:300; color:var(--dim); font-size:13px; }}
    .dot {{ width:11px; height:11px; border-radius:50%; background:currentColor; flex-shrink:0; }}
    .ok  {{ color:var(--ok); }} .err {{ color:var(--err); }} .warn {{ color:var(--warn); }} .dim {{ color:var(--dim); }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
    .pulse {{ animation:pulse 2s ease-in-out infinite; }}

    /* ── BIG STATS ── */
    .bstats {{ display:grid; grid-template-columns:repeat(5,1fr); border-bottom:1px solid var(--border); }}
    .bstat {{ padding:26px 32px; border-right:1px solid var(--border); position:relative; background:var(--bg); transition:background 0.2s; }}
    .bstat:last-child {{ border-right:none; }}
    .bstat::before {{ content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,var(--or),transparent); opacity:0; transition:opacity 0.2s; }}
    .bstat:hover {{ background:#080c18; }}
    .bstat:hover::before {{ opacity:0.5; }}
    .bnum {{ font-family:var(--head); font-size:58px; font-weight:700; color:var(--or2); line-height:1; text-shadow:0 0 20px var(--or-glow); }}
    .blbl {{ font-family:var(--head); font-size:12px; font-weight:500; letter-spacing:3px; text-transform:uppercase; color:var(--dim); margin-top:6px; }}
    .bsub {{ font-size:12px; color:#1e2e48; margin-top:3px; }}

    /* ── LAYOUT ── */
    .layout {{ display:grid; grid-template-columns:380px 1fr; min-height:calc(100vh - 84px - 52px - 90px - 50px); border-bottom:1px solid var(--border); }}
    .lcol {{ border-right:1px solid var(--border); display:flex; flex-direction:column; }}
    .panel {{ border-bottom:1px solid var(--border); }}
    .panel:last-child {{ border-bottom:none; flex:1; }}
    .ptitle {{ font-family:var(--head); font-size:12px; font-weight:600; letter-spacing:3px; text-transform:uppercase; color:var(--or); padding:12px 24px; background:linear-gradient(90deg,var(--or-glow),transparent); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:8px; }}
    .ptitle::before {{ content:'◆'; font-size:7px; color:var(--or2); }}
    .bracketed {{ position:relative; }}
    .bracketed::before,.bracketed::after {{ content:''; position:absolute; width:8px; height:8px; border-color:var(--or); border-style:solid; opacity:0.3; }}
    .bracketed::before {{ top:8px; left:8px; border-width:1px 0 0 1px; }}
    .bracketed::after  {{ bottom:8px; right:8px; border-width:0 1px 1px 0; }}
    .itable {{ width:100%; border-collapse:collapse; }}
    .itable td {{ padding:9px 24px; font-size:13px; border-bottom:1px solid #0e1624; }}
    .itable tr:last-child td {{ border-bottom:none; }}
    .itable td:first-child {{ color:var(--dim); width:160px; letter-spacing:0.5px; }}
    .itable td:last-child {{ color:var(--hi); }}
    .catlist {{ padding:16px 24px; display:flex; flex-direction:column; gap:9px; }}
    .cat-row {{ display:flex; align-items:center; justify-content:space-between; }}
    .tag {{ font-size:12px; border:1px solid; border-radius:1px; padding:2px 9px; letter-spacing:1.5px; text-transform:uppercase; font-family:var(--head); font-weight:600; }}
    .cat-count {{ font-family:var(--head); font-size:20px; font-weight:600; color:var(--hi); }}
    .mtable {{ width:100%; border-collapse:collapse; }}
    .mtable th {{ font-family:var(--head); font-size:11px; font-weight:500; letter-spacing:3px; text-transform:uppercase; color:var(--dim); padding:11px 20px; border-bottom:1px solid var(--border); background:var(--bg2); text-align:left; }}
    .mtable td {{ padding:11px 20px; font-size:13px; border-bottom:1px solid #0b1020; vertical-align:top; transition:background 0.15s; }}
    .mtable tr:hover td {{ background:rgba(224,106,16,0.03); }}
    .mtable tr:last-child td {{ border-bottom:none; }}
    .ts-cell {{ color:var(--dim); white-space:nowrap; font-size:12px; }}
    .topic-txt {{ color:var(--hi); margin-bottom:2px; }}
    .sum-txt {{ color:var(--dim); font-size:12px; line-height:1.5; }}
    .mission-tag {{ font-family:var(--head); font-size:13px; font-weight:600; letter-spacing:1px; color:var(--am); text-transform:uppercase; }}
    .credit-txt {{ color:var(--ok); }}
    .empty-cell {{ text-align:center; padding:32px 20px !important; color:var(--dim); letter-spacing:3px; font-size:13px; font-family:var(--head); font-weight:400; }}
    .dim-txt {{ color:var(--dim); }}
    .mispanel {{ border-bottom:1px solid var(--border); }}

    /* ── FOOTER ── */
    .footer {{ display:flex; justify-content:space-between; align-items:center; padding:16px 40px; font-size:12px; color:var(--dim); letter-spacing:1.5px; background:var(--bg2); border-top:1px solid var(--border); }}
    .footer-brand {{ color:#1e2e48; }}
    .footer span {{ color:#283850; }}

    /* ── THEME PANEL ── */
    .theme-trigger {{
      position:fixed; bottom:0; right:0; z-index:200;
      display:flex; align-items:center; gap:10px;
      padding:10px 20px;
      background:var(--bg2); border-top:1px solid var(--border); border-left:1px solid var(--border);
      font-family:var(--head); font-size:11px; font-weight:600;
      letter-spacing:2px; text-transform:uppercase;
      color:var(--or); cursor:pointer;
      transition:background 0.2s;
      user-select:none;
    }}
    .theme-trigger:hover {{ background:var(--bg3); }}
    .theme-trigger .t-dot {{ width:8px; height:8px; border-radius:50%; background:var(--or); flex-shrink:0; }}
    .theme-trigger .t-name {{ color:var(--am); max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

    .theme-drawer {{
      position:fixed; bottom:0; right:0; z-index:201;
      width:360px;
      background:var(--bg2); border:1px solid var(--border); border-bottom:none; border-right:none;
      transform:translateY(100%);
      transition:transform 0.25s ease;
      display:flex; flex-direction:column;
    }}
    .theme-drawer.open {{ transform:translateY(0); }}
    .theme-drawer-hdr {{
      display:flex; align-items:center; justify-content:space-between;
      padding:12px 20px;
      border-bottom:1px solid var(--border);
      font-family:var(--head); font-size:12px; font-weight:600;
      letter-spacing:3px; text-transform:uppercase; color:var(--or);
      background:linear-gradient(90deg,var(--or-glow),transparent);
    }}
    .theme-close {{
      cursor:pointer; color:var(--dim); font-size:18px; line-height:1;
      padding:0 4px; transition:color 0.15s;
    }}
    .theme-close:hover {{ color:var(--or); }}
    .theme-body {{ padding:16px 20px; display:flex; flex-direction:column; gap:14px; overflow-y:auto; max-height:420px; }}

    .t-section-label {{
      font-family:var(--head); font-size:10px; font-weight:500;
      letter-spacing:3px; text-transform:uppercase; color:var(--dim);
      margin-bottom:2px;
    }}
    .theme-list {{ display:flex; flex-direction:column; gap:6px; }}
    .theme-item {{
      display:flex; align-items:center; gap:10px; padding:8px 12px;
      border:1px solid var(--border); cursor:pointer;
      transition:border-color 0.15s, background 0.15s;
      font-size:12px;
    }}
    .theme-item:hover {{ border-color:var(--or); background:rgba(224,106,16,0.04); }}
    .theme-item.active {{ border-color:var(--or); background:var(--or-glow); }}
    .theme-item .ti-swatch {{
      width:14px; height:14px; border-radius:1px; flex-shrink:0;
      border:1px solid rgba(255,255,255,0.1);
    }}
    .theme-item .ti-info {{ flex:1; min-width:0; }}
    .theme-item .ti-name {{ color:var(--hi); font-family:var(--head); font-weight:600; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .theme-item .ti-author {{ color:var(--dim); font-size:10px; margin-top:1px; }}

    .upload-zone {{
      border:1px dashed var(--dim); padding:14px 16px;
      text-align:center; cursor:pointer; transition:border-color 0.15s, background 0.15s;
      font-size:12px; color:var(--dim); letter-spacing:1px;
      position:relative;
    }}
    .upload-zone:hover {{ border-color:var(--or); color:var(--or); background:var(--or-glow); }}
    .upload-zone input {{ position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; }}
    .upload-zone .upload-icon {{ font-size:20px; margin-bottom:4px; }}

    .t-divider {{ border:none; border-top:1px solid var(--border); }}
    .t-reset-btn {{
      padding:8px 16px; background:transparent; border:1px solid var(--dim);
      color:var(--dim); font-family:var(--mono); font-size:11px; cursor:pointer;
      letter-spacing:1px; text-transform:uppercase; transition:all 0.15s; width:100%;
    }}
    .t-reset-btn:hover {{ border-color:var(--err); color:var(--err); }}

    .t-preview-bar {{
      height:4px;
      background:linear-gradient(90deg, var(--or), var(--am), var(--teal));
      margin: 0;
    }}
  </style>
</head>
<body>

<div class="hdr">
  <div class="hdr-glow"></div>
  <div class="logo">
    <div class="logo-main">COVAS</div>
    <div class="logo-slash">//</div>
    <div class="logo-sub">System Status</div>
  </div>
  <div class="hdr-right">
    <div>UNIT&#8209;01 &nbsp;&#9642;&nbsp; <em>{SERVER_HOST}:{SERVER_PORT}</em></div>
    <div>APOLLO &nbsp;&#9642;&nbsp; <em>{MEMORY_SERVICE_URL}</em></div>
    <div>UPDATED &nbsp;&#9642;&nbsp; <em>{now_str}</em></div>
  </div>
</div>

<div class="sbar">
  <div class="spill">
    <div class="dot ok pulse"></div>
    <span class="ok">UNIT&#8209;01</span>
    <span class="dim" style="font-weight:300">ONLINE</span>
  </div>
  <div class="spill">
    <div class="dot {apollo_cls}{"" if not apollo_ok else " pulse"}"></div>
    <span class="{apollo_cls}">APOLLO</span>
    <span class="dim" style="font-weight:300">{apollo_lbl}</span>
  </div>
  <div class="spill right">&#8635;&nbsp; AUTO&#8209;REFRESH 10s</div>
</div>

<div class="t-preview-bar"></div>

<div class="bstats">
  <div class="bstat">
    <div class="bnum">{_stats["requests_total"]}</div>
    <div class="blbl">Requests</div>
    <div class="bsub">{_stats["requests_ok"]} ok &middot; <span class="{fail_cls}">{_stats["requests_failed"]} failed</span></div>
  </div>
  <div class="bstat">
    <div class="bnum">{total_searches}</div>
    <div class="blbl">Searches</div>
    <div class="bsub">{_stats["searches_inara"]} inara &middot; {_stats["searches_general"]} general</div>
  </div>
  <div class="bstat">
    <div class="bnum">{mem_total}</div>
    <div class="blbl">Memories</div>
    <div class="bsub">{mem_entities} entities &middot; {mem_db_sessions} sessions</div>
  </div>
  <div class="bstat">
    <div class="bnum">{mem_active_missions}</div>
    <div class="blbl">Active Missions</div>
    <div class="bsub">{mem_db_missions} total logged</div>
  </div>
  <div class="bstat">
    <div class="bnum" style="color:{err_color}">{mem_errors}</div>
    <div class="blbl">Memory Errors</div>
    <div class="bsub">processing faults</div>
  </div>
</div>

<div class="layout">
  <div class="lcol">
    <div class="panel bracketed">
      <div class="ptitle">UNIT&#8209;01 &mdash; Ship AI</div>
      <table class="itable">
        <tr><td>Model</td>        <td>{OLLAMA_MODEL}</td></tr>
        <tr><td>Temperature</td>  <td>{TEMPERATURE}</td></tr>
        <tr><td>Uptime</td>       <td>{uptime_str}</td></tr>
        <tr><td>Last Request</td> <td>{last_req}</td></tr>
        <tr><td>Cache</td>        <td>{cache_count} / {SEARCH_CACHE_SIZE}</td></tr>
        <tr><td>History</td>      <td>{MAX_HISTORY_MESSAGES} messages</td></tr>
        <tr><td>Lore Book</td>    <td class="{lore_cls}">{lore_txt}</td></tr>
        <tr><td>Cmdr Profile</td> <td class="{profile_cls}">{profile_txt}</td></tr>
      </table>
    </div>
    <div class="panel bracketed">
      <div class="ptitle">Apollo &mdash; Memory Service</div>
      <table class="itable">
        <tr><td>Status</td>       <td class="{apollo_cls}">{apollo_lbl}</td></tr>
        <tr><td>Last Ingest</td>  <td>{mem_last_ingest}</td></tr>
        <tr><td>Session ID</td>   <td style="font-size:10px;word-break:break-all;color:var(--dim)">{mem_session}</td></tr>
      </table>
    </div>
    <div class="panel">
      <div class="ptitle">Memory by Category</div>
      <div class="catlist">{cat_rows}</div>
    </div>
  </div>
  <div>
    <div class="panel" style="height:100%">
      <div class="ptitle">Recent Memories</div>
      <table class="mtable">
        <thead><tr>
          <th style="width:110px">Timestamp</th>
          <th style="width:120px">Category</th>
          <th>Topic &amp; Summary</th>
        </tr></thead>
        <tbody>{mem_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<div class="mispanel">
  <div class="ptitle">Active Elite Dangerous Missions</div>
  <table class="mtable">
    <thead><tr><th>Type</th><th>Giver</th><th>Origin</th><th>Destination</th><th>Reward</th></tr></thead>
    <tbody>{mission_rows}</tbody>
  </table>
</div>

<div class="footer">
  <div class="footer-brand">COVAS LOCAL AI BRIDGE</div>
  <div><span>MODEL:</span> {OLLAMA_MODEL} &nbsp;&#9642;&nbsp; <span>TEMP:</span> {TEMPERATURE} &nbsp;&#9642;&nbsp; <span>PORT:</span> {SERVER_PORT}</div>
  <div><span>APOLLO:</span> {MEMORY_SERVICE_URL}</div>
</div>

<!-- THEME TRIGGER (always visible bottom-right) -->
<div class="theme-trigger" onclick="toggleThemePanel()">
  <div class="t-dot"></div>
  <span>THEME</span>
  <span class="t-name" id="active-theme-name">DEFAULT</span>
  <span style="color:var(--dim);margin-left:4px">&#9650;</span>
</div>

<!-- THEME DRAWER -->
<div class="theme-drawer" id="theme-drawer">
  <div class="theme-drawer-hdr">
    <span>◆ &nbsp;EDHM THEME ENGINE</span>
    <span class="theme-close" onclick="toggleThemePanel()">&#x2715;</span>
  </div>
  <div class="theme-body">
    <div>
      <div class="t-section-label">Server Themes</div>
      <div class="theme-list" id="server-theme-list">
        <div class="dim-txt" style="font-size:11px;padding:8px 0">Loading...</div>
      </div>
    </div>
    <hr class="t-divider">
    <div>
      <div class="t-section-label" style="margin-bottom:8px">Import EDHM Theme</div>
      <div class="upload-zone" id="upload-zone">
        <input type="file" accept=".json" id="theme-upload" onchange="handleThemeUpload(event)">
        <div class="upload-icon">&#8659;</div>
        <div>Drop ThemeSettings.json here</div>
        <div style="font-size:10px;margin-top:4px;color:var(--dim)">or click to browse</div>
      </div>
    </div>
    <hr class="t-divider">
    <button class="t-reset-btn" onclick="resetTheme()">&#x2715; &nbsp;Reset to Default</button>
  </div>
</div>

<script>
// ── THEME ENGINE ──────────────────────────────────────────────────────────────

const STORAGE_KEY = 'covas_active_theme';
const THEMES_KEY  = 'covas_themes';

// Convert signed int32 ARGB to {{r,g,b,hex}}
function int32ToRGB(value) {{
  const u = value >>> 0;
  return {{
    r: (u >> 16) & 0xFF,
    g: (u >> 8)  & 0xFF,
    b:  u        & 0xFF,
    a: (u >> 24) & 0xFF,
  }};
}}

function rgbToHex(r, g, b) {{
  return '#' + [r,g,b].map(v => v.toString(16).padStart(2,'0')).join('');
}}

function clamp(v) {{ return Math.max(0, Math.min(255, Math.round(v))); }}

// Extract dominant color from EDHM ThemeSettings.json
function extractColors(data) {{
  const counts = {{}};
  for (const group of data.ui_groups || []) {{
    for (const el of group.Elements || []) {{
      if (el.ValueType === 'Color' && el.Value !== -1 && el.Value !== 0 && el.Value != null) {{
        counts[el.Value] = (counts[el.Value] || 0) + 1;
      }}
    }}
  }}
  // Sort by frequency, skip pure-black and near-white
  const sorted = Object.entries(counts)
    .map(([v, c]) => ({{ value: parseInt(v), count: c, rgb: int32ToRGB(parseInt(v)) }}))
    .filter(e => {{
      const {{ r,g,b }} = e.rgb;
      const lum = r*0.299 + g*0.587 + b*0.114;
      return lum > 15 && lum < 245; // not pure black or white
    }})
    .sort((a,b) => b.count - a.count);

  const primary = sorted[0]?.rgb || {{ r:224, g:106, b:16 }};
  // Look for a secondary color (significantly different hue)
  const secondary = sorted.find(e => {{
    const d = Math.abs(e.rgb.r - primary.r) + Math.abs(e.rgb.g - primary.g) + Math.abs(e.rgb.b - primary.b);
    return d > 80;
  }})?.rgb || null;

  return {{ primary, secondary, raw: sorted }};
}}

// Build CSS variable set from extracted colors
function buildCSSVars(primary, secondary) {{
  const {{ r, g, b }} = primary;
  const br = clamp(r * 1.35), bg = clamp(g * 1.35), bb = clamp(b * 1.35);
  // Amber: shift hue slightly warmer
  const ar = clamp(r * 0.85 + 40), ag = clamp(g * 0.7 + 15), ab = clamp(b * 0.5);
  // Border: very dark tint of primary
  const xr = Math.max(16, clamp(r * 0.14 + 8));
  const xg = Math.max(10, clamp(g * 0.12 + 6));
  const xb = Math.max(14, clamp(b * 0.12 + 8));

  const vars = {{
    '--or':      `rgb(${{r}},${{g}},${{b}})`,
    '--or2':     `rgb(${{br}},${{bg}},${{bb}})`,
    '--or-glow': `rgba(${{r}},${{g}},${{b}},0.15)`,
    '--am':      `rgb(${{ar}},${{ag}},${{ab}})`,
    '--border':  `rgb(${{xr}},${{xg}},${{xb}})`,
  }};

  if (secondary) {{
    vars['--teal'] = `rgb(${{secondary.r}},${{secondary.g}},${{secondary.b}})`;
  }}
  return vars;
}}

// Apply a CSS variable map to :root
function applyVars(vars) {{
  const root = document.documentElement;
  for (const [k, v] of Object.entries(vars)) root.style.setProperty(k, v);
}}

// Full theme apply from parsed data
function applyTheme(data, name) {{
  const {{ primary, secondary }} = extractColors(data);
  const vars = buildCSSVars(primary, secondary);
  applyVars(vars);
  const hex = rgbToHex(primary.r, primary.g, primary.b);
  const swatchEl = document.getElementById('active-swatch');
  if (swatchEl) swatchEl.style.background = hex;
  const nameEl = document.getElementById('active-theme-name');
  if (nameEl) nameEl.textContent = (data.credits?.theme || name || 'Custom').toUpperCase();
  // Save to localStorage
  localStorage.setItem(STORAGE_KEY, JSON.stringify({{ name, vars, hex, label: data.credits?.theme || name }}));
  updateActiveHighlight(name);
}}

function resetTheme() {{
  localStorage.removeItem(STORAGE_KEY);
  const root = document.documentElement;
  ['--or','--or2','--or-glow','--am','--border','--teal'].forEach(v => root.style.removeProperty(v));
  const nameEl = document.getElementById('active-theme-name');
  if (nameEl) nameEl.textContent = 'DEFAULT';
  updateActiveHighlight(null);
}}

// Restore saved theme on page load
function restoreTheme() {{
  const saved = localStorage.getItem(STORAGE_KEY);
  if (!saved) return;
  try {{
    const {{ name, vars, label }} = JSON.parse(saved);
    applyVars(vars);
    const nameEl = document.getElementById('active-theme-name');
    if (nameEl) nameEl.textContent = (label || 'Custom').toUpperCase();
    updateActiveHighlight(name);
  }} catch(e) {{ console.warn('Theme restore failed', e); }}
}}

// ── SERVER THEME LIST ─────────────────────────────────────────────────────────

async function loadServerThemes() {{
  const list = document.getElementById('server-theme-list');
  try {{
    const res = await fetch('/api/themes');
    const {{ themes }} = await res.json();
    if (!themes.length) {{
      list.innerHTML = '<div class="dim-txt" style="font-size:11px;padding:8px 0">No themes in themes/ folder</div>';
      return;
    }}
    list.innerHTML = '';
    for (const t of themes) {{
      const item = document.createElement('div');
      item.className = 'theme-item';
      item.dataset.name = t.name;
      // Preview swatch — load the JSON to get colors
      fetch(`/api/themes/${{t.name}}`).then(r => r.json()).then(data => {{
        const {{ primary }} = extractColors(data);
        const hex = rgbToHex(primary.r, primary.g, primary.b);
        item.querySelector('.ti-swatch').style.background = hex;
      }}).catch(()=>{{}});
      item.innerHTML = `
        <div class="ti-swatch"></div>
        <div class="ti-info">
          <div class="ti-name">${{t.theme || t.name}}</div>
          <div class="ti-author">by ${{t.author || 'Unknown'}}</div>
        </div>
      `;
      item.onclick = () => loadServerTheme(t.name);
      list.appendChild(item);
    }}
    updateActiveHighlight(null); // refresh highlights
  }} catch(e) {{
    list.innerHTML = '<div class="dim-txt" style="font-size:11px;padding:8px 0">Could not load themes</div>';
  }}
}}

async function loadServerTheme(name) {{
  try {{
    const res = await fetch(`/api/themes/${{name}}`);
    const data = await res.json();
    applyTheme(data, name);
  }} catch(e) {{ console.error('Failed to load server theme', e); }}
}}

function updateActiveHighlight(activeName) {{
  document.querySelectorAll('.theme-item').forEach(el => {{
    el.classList.toggle('active', el.dataset.name === activeName);
  }});
}}

// ── FILE UPLOAD ───────────────────────────────────────────────────────────────

function handleThemeUpload(event) {{
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {{
    try {{
      const data = JSON.parse(e.target.result);
      const themeName = file.name.replace('.json','');
      applyTheme(data, themeName);
      const zone = document.getElementById('upload-zone');
      zone.querySelector('div:not(input)').style.color = 'var(--ok)';
      setTimeout(() => {{ zone.querySelector('div:not(input)').style.color = ''; }}, 2000);
    }} catch(err) {{
      alert('Invalid ThemeSettings.json file: ' + err.message);
    }}
  }};
  reader.readAsText(file);
}}

// ── PANEL TOGGLE ──────────────────────────────────────────────────────────────

function toggleThemePanel() {{
  const drawer = document.getElementById('theme-drawer');
  const isOpen = drawer.classList.toggle('open');
  if (isOpen) loadServerThemes();
}}

// ── INIT ──────────────────────────────────────────────────────────────────────
restoreTheme();
</script>

</body>
</html>"""
    return HTMLResponse(content=html)





# ── Theme API ─────────────────────────────────────────────────────────────────
import glob

@app.get("/api/themes")
def list_themes():
    """Return available themes from the themes/ directory."""
    themes = []
    for path in glob.glob(os.path.join(THEMES_DIR, "*", "ThemeSettings.json")):
        name = os.path.basename(os.path.dirname(path))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            credits = data.get("credits", {})
            themes.append({
                "name":        name,
                "theme":       credits.get("theme", name),
                "author":      credits.get("author", "Unknown"),
                "description": credits.get("description", ""),
                "preview":     credits.get("preview", ""),
            })
        except Exception:
            themes.append({"name": name, "theme": name, "author": "", "description": "", "preview": ""})
    return {"themes": themes}

@app.get("/api/themes/{name}")
def get_theme(name: str):
    """Return the full ThemeSettings.json for a given theme folder name."""
    # Sanitise name to prevent path traversal
    safe = os.path.basename(name)
    path = os.path.join(THEMES_DIR, safe, "ThemeSettings.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Theme not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "ship-ai", "object": "model", "owned_by": "local"}]}

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    _stats["requests_total"] += 1
    _stats["last_request"] = datetime.now()

    user_msg = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    log(f"── Incoming: \"{user_msg[:80]}{'...' if len(user_msg) > 80 else ''}\"")
    log_exchange("Commander", user_msg)
    t_total = time.time()

    conversational = is_conversational(user_msg)
    game_event     = is_game_event(user_msg)
    silent_event   = is_silent_event(user_msg)
    disable_tools  = conversational or game_event

    if conversational:
        log("Conversational — tools disabled.")
    elif silent_event:
        log("Silent game event — skipping response.")
    elif game_event:
        log("Game event — tools disabled, brief response only.")

    # Silent events (FSD charging, supercruise entry etc.) get no response
    if silent_event:
        _stats["requests_ok"] += 1
        return {
            "id": "chatcmpl-local-001",
            "object": "chat.completion",
            "model": req.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    try:
        system_content = build_system_prompt(user_msg)

        # For game events, add an explicit instruction to be brief and not search
        if game_event:
            system_content += (
                "\n\nCURRENT MESSAGE is an automated game event notification. "
                "Respond with one brief in-character sentence at most, or stay silent. "
                "Do not search for any names, ships, or systems mentioned in it."
            )

        lc_messages = [SystemMessage(content=system_content)]

        for msg in req.messages:
            if msg.role == "system":
                lc_messages.append(SystemMessage(content=msg.content))
            elif msg.role == "user":
                lc_messages.append(HumanMessage(content=msg.content))
            elif msg.role == "assistant":
                lc_messages.append(AIMessage(content=msg.content))

        response_text = run_with_tools(lc_messages, use_tools=not disable_tools)
        _stats["requests_ok"] += 1

    except Exception as e:
        response_text = "COVAS systems are experiencing a fault, Commander."
        log(f"ERROR: {e}")
        _stats["requests_failed"] += 1

    log_exchange("COVAS", response_text)
    log(f"── Done in {time.time() - t_total:.1f}s total\n")

    # Stream if COVAS:NEXT requests it
    if req.stream:
        return StreamingResponse(
            stream_response(response_text),
            media_type="text/event-stream"
        )

    return {
        "id": "chatcmpl-local-001",
        "object": "chat.completion",
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  COVAS:NEXT  ──  Local AI Bridge")
    print(f"  Model    : {OLLAMA_MODEL}  (temp={TEMPERATURE})")
    print(f"  Lore     : {len(LORE_SECTIONS)} sections" if LORE_SECTIONS else "  Lore     : Not loaded")
    print(f"  Profile  : Loaded" if COMMANDER_PROFILE else "  Profile  : Not loaded")
    print(f"  History  : Max {MAX_HISTORY_MESSAGES} messages per request")
    print(f"  Log file : {LOG_FILE}")
    print(f"  Server   : http://localhost:{SERVER_PORT}")
    print(f"  Status   : http://localhost:{SERVER_PORT}/")
    print(f"  COVAS endpoint: http://localhost:{SERVER_PORT}/v1")
    print(f"{'='*60}\n")
    print("  Press CTRL+C to stop.\n")

    # Rotate log before writing new session marker
    _rotate_log()

    # Write a session start marker to the log
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"SESSION START — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Model: {OLLAMA_MODEL} | Temp: {TEMPERATURE}\n")
        f.write(f"{'='*60}\n")

    # Start memory session
    if _memory is not None:
        _memory.new_session()
        log(f"Memory session started (interval={MEMORY_INTERVAL_SEC}s, endpoint={MEMORY_SERVICE_URL})")

    try:
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
    except KeyboardInterrupt:
        print("\n[*] Stopped by user.")
        if _memory is not None:
            log("Flushing memory session to Apollo...")
            _memory.send_session_end()
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Server crashed:\n  {e}")
        pause_and_exit(1)
