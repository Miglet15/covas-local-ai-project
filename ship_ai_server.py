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

def rotate_session_log(max_sessions: int = 5):
    """
    Trim covas_session.log so it never holds more than *max_sessions* past
    sessions.  The current (new) session is not counted — it hasn't been
    written yet when this is called.  If the file doesn't exist or has fewer
    sessions than the limit, nothing changes.
    """
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        # Split on the SESSION START banner (the line of '=' chars that
        # immediately precedes it acts as the real boundary).
        SESSION_MARKER = "=" * 60 + "\n"
        parts = content.split(SESSION_MARKER)
        # parts[0]  → any content before the very first banner (may be empty)
        # parts[1+] → "SESSION START…\n…\n" + log lines until next banner
        # Reconstruct sessions: each session is SESSION_MARKER + its content
        sessions = []
        i = 1                        # skip parts[0] (pre-first-session noise)
        while i < len(parts):
            sessions.append(SESSION_MARKER + parts[i])
            i += 1

        if len(sessions) <= max_sessions:
            return                   # nothing to prune

        kept   = sessions[-max_sessions:]
        pruned = len(sessions) - max_sessions
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("".join(kept))

        print(f"  [Log] Pruned {pruned} old session(s) — keeping last {max_sessions}.")
    except Exception as e:
        print(f"  [Log] WARN: Could not rotate session log: {e}")

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
    "history_gap_minutes":   8,
    "memory_enabled":        True,
    "memory_max_entries":    120,
    "memory_min_keywords":   2,
    "log_max_sessions":      5,
    "max_tool_iterations":   5,
    "max_search_results":    5,
    "max_memories_recalled": 5
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
HISTORY_GAP_SECONDS  = int(config.get("history_gap_minutes", 8)) * 60
MEMORY_ENABLED       = bool(config.get("memory_enabled", True))
MEMORY_MAX_ENTRIES   = int(config.get("memory_max_entries", 120))
MEMORY_MIN_KEYWORDS  = int(config.get("memory_min_keywords", 2))
LOG_MAX_SESSIONS     = int(config.get("log_max_sessions", 5))
MAX_TOOL_ITERATIONS  = int(config.get("max_tool_iterations", 5))
MAX_SEARCH_RESULTS   = int(config.get("max_search_results", 5))
MAX_MEMORIES_RECALLED = int(config.get("max_memories_recalled", 5))

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

# ── Live Ship State (parsed from COVAS:NEXT status blocks) ──────────────────
# COVAS:NEXT injects the current ship/mission/location state as a system
# message in every API call. We parse it out and cache it here so the
# system prompt always reflects live game data without reading a static file.

_live_ship_state: dict = {}   # latest parsed state
_ship_state_lock = threading.Lock()

# ── Mission Name Translation ──────────────────────────────────────────────────
# Maps internal COVAS:NEXT mission-type strings → human-readable labels.
# The Mission_ prefix is stripped before matching. Matching is case-insensitive
# and falls back to a cleaned version of the raw token if nothing matches.
_MISSION_TYPE_MAP = {
    # Massacre / kill missions
    "massacre":                     "Massacre",
    "massacrewing":                 "Wing Massacre",
    "massacrethargoid":             "Thargoid Massacre",
    # Assassination
    "assassinate":                  "Assassination",
    "assassinate_planetary_expansion": "Assassination: Counter Expansion",
    "assassinate_expansion":        "Assassination: Counter Expansion",
    "assassinate_planetary":        "Assassination: Planetary Target",
    # Courier / delivery
    "courier":                      "Courier",
    "courierwing":                  "Wing Courier",
    "delivery":                     "Delivery",
    "deliverywing":                 "Wing Delivery",
    # Cargo / hauling
    "cargo":                        "Cargo Hauling",
    "cargolong":                    "Long-Haul Cargo",
    "altruism":                     "Altruism (Donation)",
    # Mining
    "mining":                       "Mining",
    "miningwing":                   "Wing Mining",
    # Salvage / rescue
    "salvage":                      "Salvage",
    "rescue":                       "Rescue",
    # Passengers
    "passenger":                    "Passenger Transport",
    "passengervip":                 "VIP Passenger Transport",
    "passengerevacuation":          "Passenger Evacuation",
    # Bounty / combat
    "collect":                      "Bounty Collection",
    "scan":                         "Scan Target",
    "hack":                         "Hack Data Point",
    # Expansion / powerplay
    "expansion":                    "Expansion Support",
    "retreat":                      "Retreat Support",
    "invest":                       "Investment",
}

def _translate_mission_name(raw: str) -> str:
    """
    Convert an internal mission type string like 'Mission_MassacreWing' or
    'Mission_Assassinate_Planetary_Expansion' into a human-readable label.
    """
    # Strip leading 'Mission_' (case-insensitive)
    cleaned = re.sub(r"^Mission_", "", raw, flags=re.IGNORECASE).strip()
    key = cleaned.lower().replace(" ", "_")
    if key in _MISSION_TYPE_MAP:
        return _MISSION_TYPE_MAP[key]
    # Try progressively shorter prefix matches (longest first)
    parts = key.split("_")
    for length in range(len(parts), 0, -1):
        prefix = "_".join(parts[:length])
        if prefix in _MISSION_TYPE_MAP:
            rest = " ".join(p.capitalize() for p in parts[length:])
            base = _MISSION_TYPE_MAP[prefix]
            return (base + ": " + rest).rstrip(": ")
    # Fallback: convert underscores to spaces and title-case
    return cleaned.replace("_", " ").title()

# Regex to find Mission_Xxxx tokens anywhere in a message
_MISSION_TOKEN_RE = re.compile(r"Mission_[A-Za-z_]+", re.IGNORECASE)

def translate_mission_names_in_text(text: str) -> str:
    """Replace all Mission_Xxxx tokens in a text block with readable names."""
    def _replace(m):
        return _translate_mission_name(m.group(0))
    return _MISSION_TOKEN_RE.sub(_replace, text)

def parse_live_ship_state(messages) -> dict:
    """
    Scan all system messages sent by COVAS:NEXT and extract live ship state.
    Returns a dict with keys: ship_type, ship_name, location, station, missions, etc.
    Updates _live_ship_state in place and returns the current value.
    """
    global _live_ship_state
    state = {}

    for msg in messages:
        role    = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        if not content or role not in ("system", None):
            continue
        text = str(content)

        # Ship type and name — COVAS:NEXT uses patterns like:
        # "Current ship: Cobra MkIII (The Debt Collector)"
        # "You are flying a Kestrel Mark 2"
        m = re.search(
            r"(?:current ship|flying(?:\s+a)?|ship(?:\s+name)?)\s*[:\-]?\s*"
            r"([A-Za-z0-9\s\.]+?)\s*(?:\(([^)]+)\))?(?:\n|$|,|\.|;)",
            text, re.IGNORECASE
        )
        if m:
            state["ship_type"] = m.group(1).strip()
            if m.group(2):
                state["ship_name"] = m.group(2).strip()

        # Location / system
        m = re.search(
            r"(?:location|current(?:ly)? (?:in|at)|system)\s*[:\-]?\s*([A-Za-z0-9\s\.\-']+?)(?:\n|$|,|\.|;)",
            text, re.IGNORECASE
        )
        if m:
            state["location"] = m.group(1).strip()

        # Station / dock
        m = re.search(
            r"(?:docked(?:\s+at)?|station|port|outpost)\s*[:\-]?\s*([A-Za-z0-9\s\.\-']+?)(?:\n|$|,|\.|;)",
            text, re.IGNORECASE
        )
        if m:
            state["station"] = m.group(1).strip()

        # Active missions — grab all Mission_Xxxx tokens and translate
        mission_tokens = _MISSION_TOKEN_RE.findall(text)
        if mission_tokens:
            state["missions"] = [_translate_mission_name(t) for t in mission_tokens]

    # Merge with any previously cached state, new values take precedence
    with _ship_state_lock:
        _live_ship_state.update({k: v for k, v in state.items() if v})
        return dict(_live_ship_state)

def build_live_ship_context(state: dict) -> str:
    """Format the parsed live ship state into a short context block."""
    if not state:
        return ""
    lines = []
    if state.get("ship_type"):
        line = "Current ship: " + state["ship_type"]
        if state.get("ship_name"):
            line += " (" + state["ship_name"] + ")"
        lines.append(line)
    if state.get("location"):
        lines.append("Current system: " + state["location"])
    if state.get("station"):
        lines.append("Docked at: " + state["station"])
    if state.get("missions"):
        lines.append("Active missions: " + ", ".join(state["missions"]))
    return "\n".join(lines)


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

def build_system_prompt(user_message: str, live_state: dict = None) -> str:
    parts = [BASE_SYSTEM_PROMPT]

    # ── Live ship state (from COVAS:NEXT, overrides static profile for ship fields) ──
    ship_ctx = build_live_ship_context(live_state or _live_ship_state)
    if ship_ctx:
        parts.append(
            "\n\n══════════════════════════════════════════\n"
            "LIVE SHIP STATUS (current as of this request)\n"
            "══════════════════════════════════════════\n"
            + ship_ctx
            + "\n══════════════════════════════════════════"
        )
    elif COMMANDER_PROFILE:
        # Fall back to static profile if no live data has arrived yet
        parts.append(
            "\n\n══════════════════════════════════════════\n"
            "COMMANDER PROFILE\n"
            "══════════════════════════════════════════\n"
            + COMMANDER_PROFILE
            + "\n══════════════════════════════════════════"
        )

    memories = get_relevant_memories(user_message)
    if memories:
        parts.append(
            "\n\n══════════════════════════════════════════\n"
            "RELEVANT PAST MISSION MEMORY\n"
            "Use these recalled facts naturally if relevant. Do not recite them verbatim.\n"
            "══════════════════════════════════════════\n"
            + memories
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
        results = _ddgs_search(query, max_results=MAX_SEARCH_RESULTS)
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
    log("Model ready.")
except Exception as e:
    print(f"\n[ERROR] Failed to initialize model:\n  {e}")
    pause_and_exit(1)

# ── Raw JSON Tool Call Detection ──────────────────────────────────────────────
# Note: we use a brace-counting extractor rather than a flat regex so that
# nested objects like {"name":"web_search","parameters":{"query":"..."}}
# are correctly detected and stripped even when the query value contains {}.

def _extract_json_objects(text: str) -> list:
    """
    Walk the string character-by-character and return every top-level JSON
    object (i.e. balanced {...} blocks) found in the text.
    Handles nested braces and string escapes correctly.
    """
    objects = []
    depth   = 0
    start   = -1
    in_str  = False
    esc     = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                objects.append((start, i + 1, text[start:i + 1]))
                start = -1
    return objects   # list of (start_idx, end_idx, json_str)


def find_raw_tool_call(content: str) -> dict | None:
    """
    Scan the response text for any JSON object that looks like a tool call.
    Returns a normalised dict or None.
    """
    for _start, _end, obj_str in _extract_json_objects(content):
        try:
            data = json.loads(obj_str)
        except (json.JSONDecodeError, ValueError):
            continue
        name   = data.get("name") or data.get("function")
        params = (data.get("parameters") or data.get("args")
                  or data.get("arguments") or {})
        if name and isinstance(params, dict):
            return {"name": name, "args": params,
                    "id": "fallback-tool-call", "raw_match": obj_str}
    return None


def strip_json_tool_calls(content: str) -> str:
    """
    Remove all JSON tool-call objects from a response string.
    Also strips fenced ```json ... ``` blocks.
    """
    # Remove fenced code blocks first
    content = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", content, flags=re.DOTALL)
    # Remove every top-level JSON object that looks like a tool call
    for start, end, obj_str in reversed(_extract_json_objects(content)):
        try:
            data = json.loads(obj_str)
            if data.get("name") or data.get("function"):
                content = content[:start] + content[end:]
        except (json.JSONDecodeError, ValueError):
            continue
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
def run_with_tools(messages: list, use_tools: bool = True, max_iterations: int = MAX_TOOL_ITERATIONS) -> str:
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

# ── Time-Gap Amnesia ─────────────────────────────────────────────────────────
# Tracks when the last request was processed. If the incoming request arrives
# more than HISTORY_GAP_SECONDS after the last one, the conversation history
# sent by COVAS:NEXT is trimmed to just the current message so stale context
# (like a system name from a session hours ago) can't pollute the response.

_last_request_time: float = 0.0   # unix timestamp, updated after each request

def apply_time_gap_amnesia(messages: list, current_time: float) -> tuple:
    """
    Returns (pruned_messages, gap_detected).
    If the gap since the last request exceeds the threshold, only the current
    user message (plus the system prompt) is kept.
    """
    global _last_request_time
    gap = current_time - _last_request_time if _last_request_time > 0 else 0
    if gap > HISTORY_GAP_SECONDS and _last_request_time > 0:
        gap_min = int(gap // 60)
        log(f"Time gap detected: {gap_min}m since last message — history cleared.")
        # Keep only SystemMessages and the final user message
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        user_msgs   = [m for m in messages if isinstance(m, HumanMessage)]
        last_user   = [user_msgs[-1]] if user_msgs else []
        return system_msgs + last_user, True
    return messages, False


# ── Long-Term Memory Store ────────────────────────────────────────────────────
# After each real exchange, a background thread summarises it and stores a
# compact memory segment. On new requests, relevant memories are retrieved by
# keyword match and injected into the system prompt.

MEMORY_FILE = os.path.join(SCRIPT_DIR, "covas_memories.json")

_memory_lock = threading.Lock()
_memories: list = []   # list of dicts: {timestamp, summary, keywords}

# Common English stop-words and Elite noise words to ignore during keyword extraction
_STOP_WORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "is","was","are","were","be","been","being","have","has","had","do",
    "does","did","will","would","could","should","may","might","shall",
    "i","you","we","he","she","it","they","me","him","her","us","them",
    "my","your","our","his","its","their","this","that","these","those",
    "what","which","who","how","when","where","why","yes","no","not","so",
    "just","also","then","than","more","some","any","all","if","as","up",
    "out","now","can","get","got","let","ok","okay","yeah","there","here",
    # game event noise
    "game","event","important","mike","commander","covas","ship","less",
    "than","minute","ago","frame","shift","drive","supercruise","system",
    "performed","scan","message","received","channel","npc","pilot","has",
    "entered","dropped","from","near","station","outpost","fleet","carrier",
    "currently","docked","undocked","legal","state","now","weapons","target",
    "lock","lost","charging","preparing","jump","cleared","navigation","route",
    "shields","online","offline","combat","danger","detected","scanners",
    "resurrected","purchased","ammunition","credits","redeemed","bounty",
    "voucher","repaired","damage","stabilizer","engaged","drift","ending",
}

def _extract_keywords(text: str) -> list:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_'-]{2,}", text.lower())
    seen, result = set(), []
    for w in words:
        if w not in _STOP_WORDS and w not in seen:
            seen.add(w)
            result.append(w)
    return result

def _load_memories():
    global _memories
    if not os.path.exists(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            _memories = json.load(f)
        log(f"Long-term memory loaded: {len(_memories)} segment(s).")
    except Exception as e:
        log(f"WARN: Could not load memories: {e}")
        _memories = []

def _save_memories():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_memories, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"WARN: Could not save memories: {e}")

def _add_memory(summary: str, keywords: list, timestamp: str):
    with _memory_lock:
        _memories.append({
            "timestamp": timestamp,
            "summary":   summary,
            "keywords":  keywords,
        })
        # Trim to max — drop oldest
        if len(_memories) > MEMORY_MAX_ENTRIES:
            del _memories[:len(_memories) - MEMORY_MAX_ENTRIES]
        _save_memories()
    log(f"Memory stored ({len(_memories)} total): {summary[:80]}")

def summarise_exchange_background(user_msg: str, covas_response: str, timestamp: str):
    """
    Runs in a background thread after a response is sent.
    Asks the model to produce a single-sentence memory of the exchange,
    then extracts keywords and stores it.
    """
    if not MEMORY_ENABLED:
        return
    # Skip very short or purely conversational exchanges
    if len(covas_response.strip()) < 30:
        return

    prompt = (
        "Summarise the following exchange between a Commander and their ship AI "
        "in ONE concise sentence (max 25 words). Focus on facts: locations, ships, "
        "missions, targets, events. Omit filler. Output only the summary sentence, nothing else.\n\n"
        f"Commander: {user_msg.strip()}\n"
        f"COVAS: {covas_response.strip()}"
    )
    try:
        result = llm_without_tools.invoke([HumanMessage(content=prompt)])
        summary = result.content.strip().strip('"').strip("'")
        if not summary or len(summary) < 10:
            return
        # Combine keywords from user message, response, and summary
        keywords = _extract_keywords(user_msg + " " + covas_response + " " + summary)
        if len(keywords) < MEMORY_MIN_KEYWORDS:
            return
        _add_memory(summary, keywords[:30], timestamp)
    except Exception as e:
        log(f"Memory summarisation error: {e}")

def get_relevant_memories(user_msg: str, max_memories: int = MAX_MEMORIES_RECALLED) -> str:
    """
    Returns a block of relevant past memories to inject into the system prompt.
    Matches by keyword overlap between the incoming message and stored memories.
    """
    if not MEMORY_ENABLED or not _memories:
        return ""

    msg_keywords = set(_extract_keywords(user_msg))
    if not msg_keywords:
        return ""

    scored = []
    with _memory_lock:
        for mem in _memories:
            mem_kw = set(mem.get("keywords", []))
            overlap = len(msg_keywords & mem_kw)
            if overlap >= MEMORY_MIN_KEYWORDS:
                scored.append((overlap, mem))

    if not scored:
        return ""

    # Sort by score desc, then recency (later index = more recent)
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_memories]

    lines = [m["summary"] for _, m in top]
    block = "\n".join(f"- {l}" for l in lines)
    log(f"Memory recalled: {len(top)} segment(s) matched.")
    return block



_load_memories()

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
    <tr><td>Long-Term Memory</td><td>{len(_memories)} segment(s)</td></tr>
    <tr><td>Gap Amnesia</td><td>{int(HISTORY_GAP_SECONDS // 60)}m threshold</td></tr>
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
    now_ts    = t_total
    now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
        # Parse live ship/mission state from the messages COVAS:NEXT sent us
        live_state = parse_live_ship_state(req.messages)
        if live_state:
            log(f"Live ship state: {', '.join(f'{k}={v}' for k,v in live_state.items() if k != 'missions')}")

        system_content = build_system_prompt(user_msg, live_state)

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

        # Apply time-gap amnesia — drop stale history if gap exceeded
        lc_messages, gap_hit = apply_time_gap_amnesia(lc_messages, now_ts)

        response_text = run_with_tools(lc_messages, use_tools=not disable_tools)
        _stats["requests_ok"] += 1

    except Exception as e:
        response_text = "COVAS systems are experiencing a fault, Commander."
        log(f"ERROR: {e}")
        _stats["requests_failed"] += 1

    # Translate any Mission_Xxxx tokens that leaked into the response text
    response_text = translate_mission_names_in_text(response_text)

    log_exchange("COVAS", response_text)
    log(f"── Done in {time.time() - t_total:.1f}s total\n")

    # Update the last request timestamp for gap detection
    global _last_request_time
    _last_request_time = now_ts

    # Fire memory summarisation in the background (non-blocking)
    if response_text and not conversational and not game_event and not silent_event:
        threading.Thread(
            target=summarise_exchange_background,
            args=(user_msg, response_text, now_label),
            name="COVAS-Memory",
            daemon=True,
        ).start()

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
    print(f"  Memory   : {len(_memories)} segments loaded  |  gap={int(HISTORY_GAP_SECONDS//60)}m")
    print(f"  COVAS endpoint: http://localhost:{SERVER_PORT}/v1")
    print(f"{'='*60}\n")
    print("  Press CTRL+C to stop.\n")

    # Prune old sessions before starting a new one
    rotate_session_log(max_sessions=LOG_MAX_SESSIONS)

    # Write a session start marker to the log
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"SESSION START — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Model: {OLLAMA_MODEL} | Temp: {TEMPERATURE}\n")
        f.write(f"{'='*60}\n")

    try:
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
    except KeyboardInterrupt:
        print("\n[*] Stopped by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Server crashed:\n  {e}")
        pause_and_exit(1)
