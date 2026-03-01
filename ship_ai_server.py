import sys
import os
import json
import re
import time
import threading
from datetime import datetime
from collections import OrderedDict

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covas_session.log")
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
    log("All imports successful.")
except Exception as e:
    print(f"\n[ERROR] Import failed:\n  {e}")
    pause_and_exit(1)


# ── Load Config ───────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

DEFAULT_CONFIG = {
    "ollama_base_url":       "http://localhost:11434",
    "ollama_model":          "llama3.1:8b",
    "server_host":           "0.0.0.0",
    "server_port":           8080,
    "temperature":           0.7,
    "max_history_messages":  10,
    "request_timeout_sec":   120,
    "search_cache_size":     50,
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
MEMORY_SERVICE_URL   = config["memory_service_url"]
MEMORY_INTERVAL_SEC  = int(config["memory_interval_sec"])
MEMORY_ENABLED       = bool(config["memory_enabled"])
# ── Memory Client (optional — graceful fallback if not present) ───────────────
_memory = None
if MEMORY_ENABLED:
    try:
        import importlib.util, sys as _sys
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
LORE_FILE     = os.path.join(SCRIPT_DIR, "elite_lore.md")
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
    log(f"WARN: No lore book found — place elite_lore.md next to this script.")

# ── Load Commander Profile ────────────────────────────────────────────────────
PROFILE_FILE    = os.path.join(SCRIPT_DIR, "commander_profile.md")
COMMANDER_PROFILE = ""

if os.path.exists(PROFILE_FILE):
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            COMMANDER_PROFILE = f.read().strip()
        log(f"Commander profile loaded ({len(COMMANDER_PROFILE):,} chars).")
    except Exception as e:
        log(f"WARN: Could not load commander profile: {e}")
else:
    log(f"WARN: No commander profile found — place commander_profile.md next to this script.")

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

def _store_cache(key: str, result: str):
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
        results = _ddgs_search(inara_query, max_results=5)
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
        results = _ddgs_search(query, max_results=4)
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
    llm = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=TEMPERATURE)
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
def status_page():
    uptime  = datetime.now() - _stats["start_time"]
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    mins, secs  = divmod(rem, 60)
    uptime_str  = f"{hours}h {mins}m {secs}s"
    lore_status = f"{len(LORE_SECTIONS)} sections loaded" if LORE_SECTIONS else "Not loaded"
    profile_status = f"{len(COMMANDER_PROFILE):,} chars" if COMMANDER_PROFILE else "Not loaded"
    cache_count = len(_search_cache)
    last_req = _stats["last_request"].strftime("%H:%M:%S") if _stats["last_request"] else "None"
    memory_session = _memory.session_id if _memory is not None else "—"

    # Ping Apollo memory service for live stats
    mem_online = False
    mem_total  = mem_sessions = mem_missions = mem_last = mem_errors = "—"
    if _memory is not None:
        try:
            import requests as _req
            _r = _req.get(f"{MEMORY_SERVICE_URL}/stats", timeout=2)
            if _r.status_code == 200:
                _ms = _r.json()
                mem_online   = True
                mem_total    = _ms.get("total_memories", "—")
                mem_sessions = _ms.get("total_sessions", "—")
                mem_missions = _ms.get("active_missions", "—")
                mem_last     = _ms.get("last_ingest") or "None"
                mem_errors   = _ms.get("errors", "—")
        except Exception:
            pass
    mem_status_txt = (f"● Online → {MEMORY_SERVICE_URL}" if mem_online
                      else ("● Unreachable" if _memory is not None else "Disabled"))
    mem_cls = "ok" if mem_online else ("err" if _memory is not None else "warn")

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>COVAS Ship AI — Status</title>
  <meta http-equiv="refresh" content="10">
  <style>
    body {{ font-family: 'Courier New', monospace; background: #0a0a0f; color: #e0a020; margin: 40px; }}
    h1 {{ color: #ff6a00; letter-spacing: 3px; }}
    h2 {{ color: #cc8800; border-bottom: 1px solid #333; padding-bottom: 6px; }}
    table {{ border-collapse: collapse; width: 500px; }}
    td {{ padding: 6px 16px; border: 1px solid #333; }}
    td:first-child {{ color: #888; width: 200px; }}
    .ok {{ color: #44ff88; }} .warn {{ color: #ffaa00; }} .err {{ color: #ff4444; }}
    .footer {{ color: #444; font-size: 11px; margin-top: 30px; }}
    a {{ color: #cc8800; }}
  </style>
</head>
<body>
  <h1>◈ COVAS SHIP AI — STATUS</h1>
  <h2>System</h2>
  <table>
    <tr><td>Status</td><td class="ok">● ONLINE</td></tr>
    <tr><td>Model</td><td>{OLLAMA_MODEL}</td></tr>
    <tr><td>Temperature</td><td>{TEMPERATURE}</td></tr>
    <tr><td>Uptime</td><td>{uptime_str}</td></tr>
    <tr><td>Last Request</td><td>{last_req}</td></tr>
  </table>
  <h2>Requests</h2>
  <table>
    <tr><td>Total</td><td>{_stats['requests_total']}</td></tr>
    <tr><td>Successful</td><td class="ok">{_stats['requests_ok']}</td></tr>
    <tr><td>Failed</td><td class="{'err' if _stats['requests_failed'] > 0 else 'ok'}">{_stats['requests_failed']}</td></tr>
    <tr><td>INARA Searches</td><td>{_stats['searches_inara']}</td></tr>
    <tr><td>General Searches</td><td>{_stats['searches_general']}</td></tr>
    <tr><td>Cache Entries</td><td>{cache_count} / {SEARCH_CACHE_SIZE}</td></tr>
  </table>
  <h2>Data</h2>
  <table>
    <tr><td>Lore Book</td><td class="{'ok' if LORE_SECTIONS else 'warn'}">{lore_status}</td></tr>
    <tr><td>Commander Profile</td><td class="{'ok' if COMMANDER_PROFILE else 'warn'}">{profile_status}</td></tr>
    <tr><td>Max History</td><td>{MAX_HISTORY_MESSAGES} messages</td></tr>
    <tr><td>Session Log</td><td>{os.path.basename(LOG_FILE)}</td></tr>
  </table>
  <h2>Apollo Memory Service</h2>
  <table>
    <tr><td>Connection</td><td class="{mem_cls}">{mem_status_txt}</td></tr>
    <tr><td>Current Session</td><td style="font-size:10px">{memory_session}</td></tr>
    <tr><td>Total Memories</td><td>{mem_total}</td></tr>
    <tr><td>Sessions Stored</td><td>{mem_sessions}</td></tr>
    <tr><td>Active ED Missions</td><td>{mem_missions}</td></tr>
    <tr><td>Last Ingest</td><td>{mem_last}</td></tr>
    <tr><td>Processing Errors</td><td class="{'err' if mem_errors not in ('—', 0, '0') else 'ok'}">{mem_errors}</td></tr>
    <tr><td>Full Memory Dashboard</td><td><a href="{MEMORY_SERVICE_URL}" target="_blank">{MEMORY_SERVICE_URL}</a></td></tr>
  </table>
  <p class="footer">Page auto-refreshes every 10 seconds. Server: http://{SERVER_HOST}:{SERVER_PORT}</p>
</body>
</html>"""
    return HTMLResponse(content=html)

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
