"""Settings. Everything you are likely to tune lives here.

Personal files (resume, criteria) and data (tracker, board registry) live under
JOBAGENT_HOME, which defaults to ./private. In CI that folder is a checkout of a
separate private repository, so this code repository can stay public.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HOME = Path(os.environ.get("JOBAGENT_HOME", "private")).resolve()
PROFILE_DIR = HOME / "profile"
DATA_DIR = HOME / "data"

RESUME_PATH = PROFILE_DIR / "resume.md"
CRITERIA_PATH = PROFILE_DIR / "criteria.md"
# Optional. One "weight  regex" per line; used to put the best-fitting jobs first.
FIT_KEYWORDS_PATH = PROFILE_DIR / "fit_keywords.txt"
# Structured CV content: the only source the tailored CVs may draw from.
CV_SOURCE_PATH = PROFILE_DIR / "cv_source.yaml"

JOBS_CSV = DATA_DIR / "jobs.csv"
SEEN_CSV = DATA_DIR / "seen.csv"
BOARDS_CSV = DATA_DIR / "boards.csv"
RUNS_CSV = DATA_DIR / "runs.csv"

# --- Run limits -------------------------------------------------------------
MAX_NEW_JOBS = int(os.environ.get("MAX_NEW_JOBS", "6"))
# US listings are plentiful and easy to verify, so they would crowd out the
# markets that matter more. Cap them per run.
MAX_US_JOBS = int(os.environ.get("MAX_US_JOBS", "1"))
# One large board (a university, a big employer) shouldn't fill a run on its own.
# Roles past the cap stay in the pool for later runs.
MAX_PER_COMPANY = int(os.environ.get("MAX_PER_COMPANY", "2"))
# Each market's share of a run's slots. A market at its share may take more only once
# every other market still under its share has no candidates left, so spare slots flow
# over and none are wasted. Singapore first (where the MSc is), then India and Canada.
# Override with MARKET_SHARES="Singapore=0.6,India=0.2,Canada=0.2".
DEFAULT_MARKET_SHARES = {"Singapore": 0.6, "India": 0.2, "Canada": 0.2}


def _parse_shares(text: str) -> dict[str, float]:
    shares = {}
    for part in (text or "").split(","):
        name, _, value = part.partition("=")
        try:
            if name.strip() and float(value) > 0:
                shares[name.strip()] = float(value)
        except ValueError:
            continue  # a malformed entry falls back to the defaults below
    return shares or dict(DEFAULT_MARKET_SHARES)


MARKET_SHARES = _parse_shares(os.environ.get("MARKET_SHARES", ""))


def market_slots(max_jobs: int | None = None) -> dict[str, int]:
    """Slots per shared market for a run: 5 jobs -> Singapore 3, India 1, Canada 1."""
    n = MAX_NEW_JOBS if max_jobs is None else max_jobs
    return {m: max(1, int(n * s + 0.5)) for m, s in MARKET_SHARES.items()}
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "2.00"))
MODEL = os.environ.get("JOBAGENT_MODEL", "sonnet")

# --- Harvester --------------------------------------------------------------
HTTP_TIMEOUT = 25.0
CONCURRENCY = 8
USER_AGENT = "job-agent/1.0 (personal job search; +https://github.com)"
# A board that fails this many runs in a row is disabled (re-enable in boards.csv).
MAX_BOARD_FAILURES = 3
# Workday tenants are huge, so we search them rather than list everything.
WORKDAY_SEARCH_TERMS = [
    "machine learning", "AI engineer", "software engineer", "computer vision",
    "research engineer", "robotics", "embedded", "perception",
]
WORKDAY_MAX_PER_TERM = 60
DESCRIPTION_CHARS = 12000

# --- MyCareersFuture (Singapore's national job portal) -------------------------
# Employers must advertise there for 14 days before applying for an Employment Pass,
# so it covers the long tail of Singapore employers, early in each posting's life.
MCF_SEARCH_TERMS = [
    "machine learning", "artificial intelligence", "AI engineer", "deep learning",
    "computer vision", "LLM", "generative AI", "NLP", "MLOps", "data scientist",
    "research engineer", "robotics", "embedded software", "software engineer",
    "backend engineer", "solutions engineer",
]
MCF_PAGES_PER_TERM = 3      # x 50 results per page
MCF_MAX_AGE_DAYS = 45       # postings older than this are ignored at harvest  # how much of a posting the model sees per get_posting

# --- Title prefilter ----------------------------------------------------------
# A title must match INCLUDE and must not match EXCLUDE. Deliberately loose:
# the model applies the real criteria. This only drops obvious non-matches.
TITLE_INCLUDE = re.compile(
    r"engineer|engineering|scientist|developer|research (engineer|scientist)|\bml\b|\bmle\b|machine learning|"
    r"\bai\b|\ba\.i\.|artificial intelligence|\bllm|computer vision|perception|robotic|"
    r"autonomy|member of technical staff|\bmts\b|architect|deploy|solutions|\bswe\b|\bsde\b|"
    r"software|embedded|firmware|real-time|applied|programmer",
    re.I,
)
TITLE_EXCLUDE = re.compile(
    r"\bintern\b|internship|\bco-?op\b|apprentice|working student|"
    r"\bsales\b|account (executive|manager)|business development|recruit|talent|"
    r"marketing|\blegal\b|counsel|paralegal|finance|accountant|accounting|payroll|"
    r"\bhr\b|people (partner|operations)|"
    r"data analyst|business analyst|business intelligence|\bbi (developer|engineer|analyst)\b|"
    r"reporting analyst|dashboard|"
    r"customer (success|support)|support specialist|"
    r"(business|sales|revenue|people|marketing|warehouse|terminal|fleet|store) operations|"
    r"operations (manager|associate|analyst|specialist|coordinator)|"
    r"(product|program|project|office|facilities) manager|"
    # Clear people-management and leadership titles. A bare "Manager" is left for the
    # model: at consulting firms and banks it is a grade, not a people-management job.
    r"engineering manager|manager,? (of )?(software )?engineering|"
    r"(success|delivery|partner|vendor|relationship|portfolio|community|content|channel|"
    r"category|brand|procurement|supply chain) manager|"
    r"\bdirector\b|\bhead of\b|\bvp\b|vice president|"
    r"salesforce|\bgtm\b|go-to-market|technical (writer|writing|documentation)|"
    r"technician|mechanic|electrician|driver\b|"
    # Academic and research-administration roles (university boards are full of these).
    r"lecturer|professor|teaching|\btutor|postdoc|post-doc|research fellow|research assistant|"
    r"research associate|project officer|assistant manager|programme (manager|management)|"
    r"program management|partnership|\bgrants?\b|"
    r"chemistry|chemical|biolog|electrocatalys|geolog|physics|materials science|"
    r"(graphic|visual|ux|ui|product) designer",
    re.I,
)

# --- Markets ------------------------------------------------------------------
# Priority tiers: lower is worked first. Singapore comes first, India and Canada share
# the second tier, the US comes last (and is capped, above).
MARKET_TIERS = {
    "Singapore": 1,
    "India": 2, "Canada": 2,
    "Remote (unspecified)": 3, "Unknown": 3,
    "United States": 4,
}
# Detection order: for a multi-city listing, the first market that matches wins,
# so this follows the priority tiers.
MARKETS: list[tuple[str, re.Pattern]] = [
    ("Singapore", re.compile(r"singapore", re.I)),
    ("India", re.compile(
        # "Little India" is a district of Singapore, not the country.
        r"(?<!little )\bindia\b|bangalore|bengaluru|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|"
        r"pune|chennai|kochi|thiruvananthapuram|trivandrum|ahmedabad|kolkata", re.I)),
    ("Canada", re.compile(
        r"canada|toronto|vancouver|montr[eé]al|ottawa|waterloo|kitchener|calgary|edmonton|"
        r"halifax|winnipeg|qu[eé]bec|victoria, bc|, ?(on|bc|ab|qc|ns|mb)\b", re.I)),
    ("United States", re.compile(
        r"united states|\busa?\b|u\.s\.|americas|"
        r"san francisco|new york|seattle|boston|austin|los angeles|chicago|mountain view|"
        r"palo alto|menlo park|sunnyvale|san jose|san mateo|redmond|pittsburgh|denver|"
        r"atlanta|washington|ann arbor|detroit|miami|dallas|houston|bay area|"
        r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
        r"georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|"
        r"maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|"
        r"nevada|new hampshire|new jersey|new mexico|north carolina|north dakota|ohio|"
        r"oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
        r"texas|utah|vermont|virginia|wisconsin|wyoming|"
        r", ?(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|"
        r"ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b",
        re.I)),
]
# Locations that are clearly somewhere else. Anything matching no market and
# none of these is kept as "Unknown" and ranked last, never silently dropped.
OTHER_PLACES = re.compile(
    r"\buk\b|united kingdom|england|london|cambridge, uk|edinburgh|manchester|ireland|dublin|"
    r"germany|berlin|munich|m[uü]nchen|hamburg|frankfurt|france|paris|netherlands|amsterdam|"
    r"belgium|brussels|switzerland|z[uü]rich|geneva|austria|vienna|poland|warsaw|krak[oó]w|"
    r"spain|madrid|barcelona|portugal|lisbon|italy|milan|rome|sweden|stockholm|denmark|"
    r"copenhagen|norway|oslo|finland|helsinki|czech|prague|romania|bucharest|hungary|budapest|"
    r"ukraine|serbia|belgrade|greece|athens|turkey|istanbul|israel|tel aviv|\buae\b|dubai|"
    r"abu dhabi|saudi|riyadh|qatar|doha|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|"
    r"cape town|johannesburg|japan|tokyo|osaka|korea|seoul|china|beijing|shanghai|shenzhen|"
    r"hangzhou|hong kong|taiwan|taipei|philippines|manila|vietnam|hanoi|ho chi minh|indonesia|"
    r"jakarta|malaysia|kuala lumpur|thailand|bangkok|australia|sydney|melbourne|brisbane|"
    r"new zealand|auckland|brazil|s[aã]o paulo|mexico|ciudad de m|argentina|buenos aires|"
    r"colombia|bogot[aá]|chile|santiago|peru|lima|pakistan|bangladesh|sri lanka|\bemea\b|"
    r"\beurope\b|\blatam\b",
    re.I,
)
REMOTE = re.compile(r"remote|anywhere|distributed|work from home|\bwfh\b", re.I)
STALE_DAYS = 90
# Freshness tiers for ranking. Early applicants are screened first, so postings up to
# FRESH_DAYS old are worked before anything else, and ones older than OLD_DAYS go last
# (still judged: an old posting for a rare, well-matched role is worth recording).
FRESH_DAYS = 7
OLD_DAYS = 30
