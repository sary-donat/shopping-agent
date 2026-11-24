"""All project settings in one place. Values can be overridden in the .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent  # the server folder
load_dotenv(ROOT / ".env")  # server/.env, wherever the command is run from
DB_PATH = ROOT / "shopping.db"
DUMPS_DIR = ROOT / "dumps"

CITY = os.getenv("CITY", "בני ברק")

# Chains to download (names from il_supermarket_scarper.ScraperFactory), e.g. CHAINS=SHUFERSAL,RAMI_LEVY in .env.
# Empty = every chain the download library knows (data.prices.chains() resolves it).
CHAINS = [c.strip() for c in os.getenv("CHAINS", "").split(",") if c.strip()]

MODEL = os.getenv("MODEL", "claude-opus-5-5")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v3")  # v1 = before, v2 = first improvement, v3 = current

MAX_CANDIDATES = 15          # products shown to the model per list item
MAX_QUESTION_ROUNDS = 3      # loop limit: how many times the agent may ask the user
MAX_PARALLEL_CALLS = 8       # list items sent to the model at the same time (agent/graph.py: match)
# Limits against runaway cost: each list item is a model call
MAX_ITEMS = 40               # list items checked per request; the rest are reported, not checked
MAX_REQUEST_CHARS = 2000     # length of a message the user may send (server.py, app.py)
MAX_BODY_BYTES = 100_000     # size of an HTTP request body (server.py)
SPLIT_MIN_SAVING = float(os.getenv("SPLIT_MIN_SAVING", "15"))  # ₪ - below this, don't suggest a 2nd store

DATASET_NAME = "shopping-matcher"

# ---- location (OpenStreetMap) ----
POINT_RADIUS_M = int(os.getenv("POINT_RADIUS_M", "700"))         # around an exact address ("רבי עקיבא 20")
CORRIDOR_WIDTH_M = int(os.getenv("CORRIDOR_WIDTH_M", "300"))     # on each side of a street the user walks along
TURN_REACH_M = 500              # after a turn without an end point: this far from the junction, both directions
JUNCTION_MAX_GAP_M = 60         # two streets "meet" if their lines come this close
LANDMARK_MAX_FROM_STREET_M = 300  # "מאזור מכבי" must be found this close to the street
CITY_RADIUS_M = 6000            # streets are searched within this distance of the city center
CHEAP_VS_NEAR_MIN_SAVING = 5.0  # ₪ - ask "cheap or near?" only if the far store saves at least this
EXTRA_STORES_TO_RESEARCH = 3    # nearby stores without price data that get a web search on their price reputation
REPUTATION_MAX_AGE_DAYS = 30    # reuse a saved web search result for this long
USER_AGENT = "shopping-agent-student-project/1.0"  # OpenStreetMap asks every app to identify itself

# Names of the chains we already have prices for, to tell them apart from other nearby shops on the map
# (not נתיב החסד / ברכל: their price server is down, so they are shown as shops without prices - data/manual_shops.py)
CHAIN_NAMES_HE = ["שופרסל", "רמי לוי", "אושר עד", "יוחננוף", "חצי חינם", "ויקטורי",
                  "יש חסד", "יש בשכונה", "מעיין 2000", "שוק העיר", "שפע ברכת השם", "גוד פארם", "סופר-פארם",
                  "סופר פארם", "גוד מרקט"]
