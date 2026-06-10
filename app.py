import streamlit as st
import sqlite3
import requests
import random
from datetime import datetime, timedelta
from collections import Counter
from zoneinfo import ZoneInfo


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_BASE_FD  = "https://api.football-data.org/v4"
API_TOKEN_FD = "3cc2efd31a07449aa2f36539e9cda614"

API_BASE_SOFA                = "https://api.sofascore.com/api/v1"
SOFA_WORLD_CUP_TOURNAMENT_ID = 16

COMPETITION = "WC"
DB_PATH     = "worldcup.db"
UK_TZ       = ZoneInfo("Europe/London")
HEADERS_FD  = {"X-Auth-Token": API_TOKEN_FD}
LEAGUE_CODE = "WC2026"
ADMIN_PIN   = "9999"

GROUP_STAGES = {"group", "group stage", "group_stage", ""}

# Maps each match to one of 8 bonus buckets (one booster allowed per bucket).
def stage_bucket(match: dict) -> str:
    """
    Returns a string key identifying which booster bucket this match belongs to.
    Group-stage matches are split by matchday (1, 2, 3).
    Knockout rounds each get their own bucket.
    """
    stage = (match.get("stage") or "").lower().strip()
    if stage in GROUP_STAGES:
        # matchday field holds the round number inside the group stage
        md = str(match.get("matchday") or "").strip()
        # football-data can return "1", "2", "3" or descriptive strings
        if md in ("1", "2", "3"):
            return f"group_round_{md}"
        # Fallback: try to extract a digit from the matchday string
        import re
        digits = re.findall(r"\d+", md)
        if digits:
            return f"group_round_{digits[0]}"
        return "group_round_1"
    # Normalise knockout stage names
    stage_map = {
        "round of 32":   "round_of_32",
        "round_of_32":   "round_of_32",
        "last 32":       "round_of_32",
        "round of 16":   "round_of_16",
        "round_of_16":   "round_of_16",
        "last 16":       "round_of_16",
        "quarter_final": "quarterfinal",
        "quarter-final": "quarterfinal",
        "quarterfinal":  "quarterfinal",
        "quarter final": "quarterfinal",
        "semi_final":    "semifinal",
        "semi-final":    "semifinal",
        "semifinal":     "semifinal",
        "semi final":    "semifinal",
        "final":         "final",
    }
    return stage_map.get(stage, stage)

st.set_page_config(
    page_title="World Cup 2026 Predictor",
    page_icon="⚽",
    layout="wide",
)

# ─────────────────────────────────────────────
# DATABASE — single cached connection per session
# ─────────────────────────────────────────────
@st.cache_resource
def _get_shared_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_db():
    return _get_shared_conn()

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    UNIQUE NOT NULL,
            pin       TEXT    NOT NULL,
            total_pts INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS predictions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            match_id     TEXT    NOT NULL,
            home_goals   INTEGER,
            away_goals   INTEGER,
            booster_used INTEGER DEFAULT 0,
            first_scorer TEXT,
            first_team   TEXT,
            points       INTEGER DEFAULT 0,
            settled      INTEGER DEFAULT 0,
            UNIQUE(user_id, match_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS match_mapping (
            match_id_fd  TEXT PRIMARY KEY,
            sofascore_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS settle_log (
            match_id     TEXT PRIMARY KEY,
            attempted_at TEXT NOT NULL,
            success      INTEGER DEFAULT 0
        );
    """)
    conn.commit()

init_db()

# ─────────────────────────────────────────────
# AUTO BACKUP
# Runs once per session (stored in session_state). Copies the live DB to
# backups/worldcup_YYYYMMDD_HHMMSS.db — keeps the 10 most recent files
# so the folder never grows unbounded.
# ─────────────────────────────────────────────
import shutil, glob, os

BACKUP_DIR      = "backups"
BACKUP_KEEP     = 10          # number of recent backups to retain
BACKUP_INTERVAL = 3600        # seconds between backups within the same session

def run_backup():
    """Write a timestamped copy of the DB, then prune old ones."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts       = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dst      = os.path.join(BACKUP_DIR, f"worldcup_{ts}.db")
        shutil.copy2(DB_PATH, dst)

        # Prune: keep only the N most recent backups
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "worldcup_*.db")))
        for old in backups[:-BACKUP_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass   # never crash the app over a backup failure

def maybe_backup():
    """
    Called once per page load. Throttled by BACKUP_INTERVAL so we don't
    hammer the disk on every Streamlit rerun.
    """
    now = datetime.utcnow().timestamp()
    last = st.session_state.get("last_backup_ts", 0)
    if now - last >= BACKUP_INTERVAL:
        run_backup()
        st.session_state["last_backup_ts"] = now

# ─────────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────────
def get_or_create_user(name: str, pin: str):
    conn       = get_db()
    clean_name = name.strip().lower()
    existing   = conn.execute("SELECT * FROM users WHERE name=?", (clean_name,)).fetchone()
    if existing:
        if existing["pin"] == pin:
            return dict(existing)
        return None
    conn.execute("INSERT INTO users (name,pin) VALUES (?,?)", (clean_name, pin))
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE name=?", (clean_name,)).fetchone()
    return dict(user)

def verify_user(name: str, pin: str):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE name=? AND pin=?", (name, pin)
    ).fetchone()
    return dict(user) if user else None

def generate_invite_links(names: list):
    links = {}
    conn  = get_db()
    for name in names:
        pin = str(random.randint(1000, 9999))
        conn.execute("INSERT OR IGNORE INTO users (name,pin) VALUES (?,?)", (name, pin))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        links[name] = {"pin": user["pin"], "url": f"?user={name}&pin={user['pin']}"}
    return links

# ─────────────────────────────────────────────
# FOOTBALL-DATA.ORG HELPERS
# ─────────────────────────────────────────────
def _normalise_match_fd(m: dict) -> dict:
    score      = m.get("score", {})
    ft         = score.get("fullTime", {}) or {}
    ht         = score.get("halfTime", {}) or {}
    status_raw = (m.get("status") or "").upper()

    if status_raw in ("IN_PLAY", "PAUSED", "LIVE"):
        status = "live"
    elif status_raw == "FINISHED":
        status = "finished"
    else:
        status = "scheduled"

    stage_raw = (m.get("stage") or "GROUP_STAGE").upper()
    stage = "group" if stage_raw in ("GROUP_STAGE", "GROUP STAGE", "") else stage_raw.lower()

    return {
        "id":            str(m["id"]),
        "home_team":     m["homeTeam"]["name"],
        "away_team":     m["awayTeam"]["name"],
        "home_team_id":  m["homeTeam"]["id"],
        "away_team_id":  m["awayTeam"]["id"],
        "home_crest":    m["homeTeam"].get("crest", ""),
        "away_crest":    m["awayTeam"].get("crest", ""),
        "date":          m.get("utcDate", ""),
        "home_score":    ft.get("home"),
        "away_score":    ft.get("away"),
        "home_ht":       ht.get("home"),
        "away_ht":       ht.get("away"),
        "status":        status,
        "stage":         stage,
        "matchday":      str(m.get("matchday") or m.get("group") or "Group stage"),
        "group":         m.get("group") or "",
        "venue":         m.get("venue") or "",
        "referee":       (m.get("referees") or [{}])[0].get("name", "") if m.get("referees") else "",
    }

@st.cache_data(ttl=60)
def fetch_matches_fd() -> list:
    if not API_TOKEN_FD or API_TOKEN_FD == "YOUR_FOOTBALL_DATA_API_TOKEN_HERE":
        st.warning("⚠️ No football-data.org API token set.")
        return []
    try:
        r = requests.get(
            f"{API_BASE_FD}/competitions/{COMPETITION}/matches",
            headers=HEADERS_FD,
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json().get("matches", [])
        return [_normalise_match_fd(m) for m in raw]
    except Exception as e:
        st.error(f"Could not fetch matches: {e}")
        return []

@st.cache_data(ttl=300)
def fetch_standings_fd() -> list:
    """Fetch group-stage standings table."""
    try:
        r = requests.get(
            f"{API_BASE_FD}/competitions/{COMPETITION}/standings",
            headers=HEADERS_FD,
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("standings", [])
    except Exception:
        return []

# ─────────────────────────────────────────────
# SQUAD FETCHING
# ─────────────────────────────────────────────
@st.cache_data(ttl=3600)
def fetch_team_squad(team_id: int) -> list:
    if not team_id:
        return []
    try:
        url = f"{API_BASE_FD}/teams/{team_id}"
        r   = requests.get(url, headers=HEADERS_FD, timeout=10)
        r.raise_for_status()
        squad = r.json().get("squad", [])
        return [p.get("name") for p in squad if p.get("name")]
    except Exception:
        return []

# ─────────────────────────────────────────────
# SOFASCORE HELPERS
# ─────────────────────────────────────────────
def _sofa_get(url: str, timeout: int = 10):
    # SofaScore is an unofficial API — failures are expected and handled
    # silently so users never see confusing 403/timeout messages.
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        pass
    return None

@st.cache_data(ttl=3600)
def get_latest_world_cup_season_id() -> int | None:
    data = _sofa_get(
        f"{API_BASE_SOFA}/unique-tournament/{SOFA_WORLD_CUP_TOURNAMENT_ID}/seasons"
    )
    if not data:
        return None
    seasons = data.get("seasons", [])
    if not seasons:
        return None
    wc_2026 = [s for s in seasons if "2026" in str(s.get("name", ""))]
    if wc_2026:
        return int(wc_2026[-1]["id"])
    seasons.sort(key=lambda s: s.get("id", 0))
    return int(seasons[-1]["id"])

@st.cache_data(ttl=3600)
def fetch_sofascore_events_for_world_cup() -> list:
    season_id = get_latest_world_cup_season_id()
    if not season_id:
        return []
    data = _sofa_get(
        f"{API_BASE_SOFA}/unique-tournament/{SOFA_WORLD_CUP_TOURNAMENT_ID}"
        f"/season/{season_id}/events"
    )
    if not data:
        return []
    out = []
    for ev in data.get("events", []):
        out.append({
            "id":        ev.get("id"),
            "home_team": ev.get("homeTeam", {}).get("name", ""),
            "away_team": ev.get("awayTeam", {}).get("name", ""),
            "start_ts":  ev.get("startTimestamp"),
        })
    return out

def get_sofascore_id_cached(match_id_fd: str) -> int | None:
    conn = get_db()
    row  = conn.execute(
        "SELECT sofascore_id FROM match_mapping WHERE match_id_fd=?",
        (match_id_fd,)
    ).fetchone()
    return row["sofascore_id"] if row else None

def cache_sofascore_id(match_id_fd: str, sofascore_id: int):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO match_mapping (match_id_fd, sofascore_id) VALUES (?,?)",
        (match_id_fd, sofascore_id)
    )
    conn.commit()

def parse_kickoff(raw) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt
    except Exception:
        return datetime.now(ZoneInfo("UTC")) + timedelta(days=365)

def _best_match_sofascore_event(fd_match: dict, ss_events: list) -> int | None:
    home_fd = fd_match["home_team"]
    away_fd = fd_match["away_team"]
    ko_fd   = parse_kickoff(fd_match["date"])

    def name_match(a, b):
        a0 = a.split()[0].lower()
        b0 = b.split()[0].lower()
        return a0 in b.lower() or b0 in a.lower()

    candidates = []
    for ev in ss_events:
        ts = ev["start_ts"]
        if not ts:
            continue
        ko_ss = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))
        if not name_match(home_fd, ev["home_team"]):
            continue
        if not name_match(away_fd, ev["away_team"]):
            continue
        if abs((ko_ss - ko_fd).total_seconds()) > 3 * 3600:
            continue
        candidates.append((ev, abs((ko_ss - ko_fd).total_seconds())))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return int(candidates[0][0]["id"])

def get_sofascore_id_for_match(fd_match: dict, ss_events: list) -> int | None:
    match_id_fd = fd_match["id"]
    cached = get_sofascore_id_cached(match_id_fd)
    if cached:
        return cached
    ss_id = _best_match_sofascore_event(fd_match, ss_events)
    if ss_id:
        cache_sofascore_id(match_id_fd, ss_id)
    return ss_id

def fetch_sofascore_incidents(sofascore_id: int) -> list:
    data = _sofa_get(f"{API_BASE_SOFA}/event/{sofascore_id}/incidents")
    return data.get("incidents", []) if data else []

@st.cache_data(ttl=60)
def fetch_sofascore_live_details(sofascore_id: int) -> dict:
    """
    Fetch live match details from SofaScore: current minute, incidents
    (goals, cards, substitutions). Returns a dict with keys:
        minute, period, incidents
    Returns empty dict on failure — callers must handle gracefully.
    """
    data = _sofa_get(f"{API_BASE_SOFA}/event/{sofascore_id}")
    if not data:
        return {}
    ev = data.get("event", {})
    status  = ev.get("status", {})
    minute  = status.get("description", "")   # e.g. "45+2", "67"
    period  = status.get("type", "")          # "inprogress", "finished" etc.
    incidents_data = _sofa_get(f"{API_BASE_SOFA}/event/{sofascore_id}/incidents") or {}
    incidents = incidents_data.get("incidents", [])
    return {"minute": minute, "period": period, "incidents": incidents}

def extract_first_goal_from_incidents(incidents: list):
    goals = []
    for inc in incidents:
        if inc.get("type") != "goal":
            continue
        team   = inc.get("team", {}).get("name")
        player = inc.get("player", {}).get("name")
        minute = inc.get("time", {}).get("minute", 0)
        if team and player:
            goals.append((minute, player, team))
    if not goals:
        return None, None
    goals.sort(key=lambda x: x[0])
    _, player, team = goals[0]
    return player, team

def parse_incidents(incidents: list, home_team: str, away_team: str) -> dict:
    """
    Split raw SofaScore incidents into structured lists for the match centre.
    Returns:
        goals        — list of {minute, player, team, is_penalty, is_own_goal}
        yellow_cards — list of {minute, player, team}
        red_cards    — list of {minute, player, team}
        subs         — list of {minute, player_in, player_out, team}
    """
    goals = []
    yellow_cards = []
    red_cards = []
    subs = []

    for inc in incidents:
        t        = inc.get("type", "")
        minute   = inc.get("time", {}).get("minute", 0)
        added    = inc.get("time", {}).get("injuryTime", 0)
        min_str  = f"{minute}+{added}'" if added else f"{minute}'"
        team     = inc.get("team", {}).get("name", "")
        player   = inc.get("player", {}).get("name", "")
        player2  = inc.get("playerIn", {}).get("name", "") or inc.get("assist1", {}).get("name", "")

        if t == "goal":
            goals.append({
                "minute":      min_str,
                "player":      player,
                "team":        team,
                "is_penalty":  inc.get("goalType") == "penalty",
                "is_own_goal": inc.get("goalType") == "ownGoal",
            })
        elif t == "card":
            card_colour = inc.get("cardType", "yellow").lower()
            if "red" in card_colour:
                red_cards.append({"minute": min_str, "player": player, "team": team})
            else:
                yellow_cards.append({"minute": min_str, "player": player, "team": team})
        elif t == "substitution":
            player_out = inc.get("playerOut", {}).get("name", player)
            player_in  = inc.get("playerIn",  {}).get("name", player2)
            subs.append({"minute": min_str, "player_in": player_in, "player_out": player_out, "team": team})

    return {"goals": goals, "yellow_cards": yellow_cards, "red_cards": red_cards, "subs": subs}

# ─────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────
def display_time(kickoff_utc: datetime) -> str:
    return kickoff_utc.astimezone(UK_TZ).strftime("%a %d %b %Y, %H:%M")

def is_locked(kickoff_utc: datetime) -> bool:
    return datetime.now(ZoneInfo("UTC")) > kickoff_utc - timedelta(hours=1)

# ─────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────
def outcome(home, away):
    if home > away: return "H"
    if away > home: return "A"
    return "D"

def score_prediction(pred, result, is_knockout):
    points = 0
    ph = pred.get("home_goals")
    pa = pred.get("away_goals")
    rh = result.get("home_goals")
    ra = result.get("away_goals")

    if ph is None or pa is None or rh is None or ra is None:
        return 0

    if outcome(ph, pa) == outcome(rh, ra):
        points += 3
    if ph == rh: points += 1
    if pa == ra: points += 1
    if (ph - pa) == (rh - ra):
        points += 1

    if is_knockout:
        if pred.get("first_scorer") and pred["first_scorer"] == result.get("first_scorer"):
            points += 2
        if pred.get("first_team") and pred["first_team"] == result.get("first_team"):
            points += 1

    return points

def settle_match(match_id, result, is_knockout):
    conn    = get_db()
    already = conn.execute(
        "SELECT id FROM predictions WHERE match_id=? AND settled=1 LIMIT 1",
        (match_id,)
    ).fetchone()
    if already:
        return

    preds = [dict(p) for p in conn.execute(
        "SELECT * FROM predictions WHERE match_id=?", (match_id,)
    ).fetchall()]

    total = len(preds)
    if total > 0:
        count = Counter((p["home_goals"], p["away_goals"]) for p in preds)
        def underdog(p):
            return count[(p["home_goals"], p["away_goals"])] / total < 0.10
    else:
        def underdog(_): return False

    for pred in preds:
        pts = score_prediction(pred, result, is_knockout)
        if underdog(pred):
            pts += 2
        if pred["booster_used"]:
            pts *= 2
        conn.execute(
            "UPDATE predictions SET points=?, settled=1 WHERE id=?",
            (pts, pred["id"])
        )
        conn.execute(
            "UPDATE users SET total_pts = total_pts + ? WHERE id=?",
            (pts, pred["user_id"])
        )

    conn.commit()

# ─────────────────────────────────────────────
# SETTLE LOG HELPERS
# ─────────────────────────────────────────────
def settle_already_attempted(match_id: str) -> bool:
    conn = get_db()
    row  = conn.execute(
        "SELECT success FROM settle_log WHERE match_id=?", (match_id,)
    ).fetchone()
    return row is not None

def mark_settle_attempted(match_id: str, success: bool):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settle_log (match_id, attempted_at, success) VALUES (?,?,?)",
        (match_id, datetime.utcnow().isoformat(), 1 if success else 0)
    )
    conn.commit()

# ─────────────────────────────────────────────
# PREDICTION DB HELPERS
# ─────────────────────────────────────────────
def save_prediction(user_id, match_id, home_goals, away_goals,
                    booster_used=False, first_scorer=None, first_team=None):
    conn = get_db()
    conn.execute("""
        INSERT INTO predictions
            (user_id, match_id, home_goals, away_goals,
             booster_used, first_scorer, first_team)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(user_id, match_id) DO UPDATE SET
            home_goals=excluded.home_goals,
            away_goals=excluded.away_goals,
            booster_used=excluded.booster_used,
            first_scorer=excluded.first_scorer,
            first_team=excluded.first_team
    """, (user_id, match_id, home_goals, away_goals,
          1 if booster_used else 0, first_scorer, first_team))
    conn.commit()

def get_prediction(user_id, match_id):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM predictions WHERE user_id=? AND match_id=?",
        (user_id, match_id)
    ).fetchone()
    return dict(row) if row else None

def booster_used_this_matchday(user_id, matchday_match_ids, exclude_match_id=None):
    """Legacy alias kept for backward compatibility."""
    return booster_used_this_stage(user_id, matchday_match_ids, exclude_match_id)

def booster_used_this_stage(user_id: int, stage_match_ids: list, exclude_match_id=None) -> bool:
    """
    Returns True if the user has already used their booster on any match
    in the same stage bucket (excluding the current match being rendered).
    One booster is allowed per stage bucket:
        group_round_1, group_round_2, group_round_3,
        round_of_32, round_of_16, quarterfinal, semifinal, final
    """
    conn = get_db()
    for mid in stage_match_ids:
        if mid == exclude_match_id:
            continue
        row = conn.execute(
            "SELECT booster_used FROM predictions WHERE user_id=? AND match_id=?",
            (user_id, mid)
        ).fetchone()
        if row and row["booster_used"]:
            return True
    return False

def get_leaderboard():
    conn = get_db()
    rows = conn.execute(
        "SELECT name, total_pts FROM users ORDER BY total_pts DESC"
    ).fetchall()
    return [dict(r) for r in rows]

def delete_user(user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM predictions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()

def get_prediction_history(user_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT match_id, home_goals, away_goals, booster_used,
                  first_scorer, first_team, points, settled
           FROM predictions
           WHERE user_id=? AND settled=1
           ORDER BY match_id DESC""",
        (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────
# AUTO SETTLE
# ─────────────────────────────────────────────
def auto_settle(matches):
    ss_events = fetch_sofascore_events_for_world_cup()

    for match in matches:
        if match["status"] != "finished":
            continue
        if settle_already_attempted(match["id"]):
            continue

        is_ko        = match["stage"] not in GROUP_STAGES
        first_scorer = None
        first_team   = None
        sofa_success = False

        try:
            ss_id = get_sofascore_id_for_match(match, ss_events)
            if ss_id:
                incidents = fetch_sofascore_incidents(ss_id)
                first_scorer, first_team = extract_first_goal_from_incidents(incidents)
                sofa_success = True
        except Exception:
            pass

        result = {
            "home_goals":   match["home_score"],
            "away_goals":   match["away_score"],
            "first_scorer": first_scorer,
            "first_team":   first_team,
        }

        settle_match(match["id"], result, is_ko)
        mark_settle_attempted(match["id"], success=sofa_success)

# ─────────────────────────────────────────────
# UI — LOGIN
# ─────────────────────────────────────────────
def login_page():
    st.title("⚽ World Cup 2026 Predictor")

    params       = st.query_params
    default_name = params.get("user", "")
    default_pin  = params.get("pin", "")

    name   = st.text_input("Your name", value=default_name)
    pin    = st.text_input("PIN (4 digits)", value=default_pin, max_chars=4, type="password")
    league = st.text_input("League code", type="password")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Login / Register"):
            if not name or not pin.isdigit() or len(pin) != 4:
                st.error("Enter a valid name and 4-digit PIN.")
            elif league != LEAGUE_CODE:
                st.error("Incorrect league code.")
            else:
                user = get_or_create_user(name, pin)
                if user:
                    st.session_state["user"] = user
                    st.session_state["page"] = "predictions"
                    st.rerun()
                else:
                    st.error("Wrong PIN.")

    with col2:
        if st.button("Admin panel"):
            st.session_state["awaiting_admin_pin"] = True
            st.rerun()

        if st.session_state.get("awaiting_admin_pin"):
            admin_pin = st.text_input("Enter admin PIN", type="password", key="admin_pin_input")
            if st.button("Submit PIN"):
                if admin_pin == ADMIN_PIN:
                    st.session_state["admin"] = True
                    st.session_state.pop("awaiting_admin_pin", None)
                    st.rerun()
                else:
                    st.error("Incorrect admin PIN.")
                    st.session_state.pop("awaiting_admin_pin", None)
                    st.rerun()

# ─────────────────────────────────────────────
# UI — ADMIN
# ─────────────────────────────────────────────
def admin_delete_users():
    st.title("❌ Delete User Accounts")
    conn  = get_db()
    users = conn.execute(
        "SELECT id, name, total_pts FROM users ORDER BY name ASC"
    ).fetchall()

    if not users:
        st.info("No users found.")
        return

    user_names  = [f"{u['name']} (ID {u['id']}, {u['total_pts']} pts)" for u in users]
    selected    = st.selectbox("Select a user to delete", user_names)
    selected_id = int(selected.split("ID ")[1].split(",")[0])

    if st.button("Delete User"):
        delete_user(selected_id)
        st.success("User deleted successfully.")
        st.rerun()

    if st.button("← Back"):
        st.session_state["admin_page"] = "main"
        st.rerun()


def admin_restore_backup():
    st.title("♻️ Restore from Backup")
    st.warning(
        "⚠️ Restoring a backup will **overwrite all current data** "
        "(predictions, users, scores) with the selected snapshot. "
        "This cannot be undone. A safety backup of the current database "
        "is taken automatically before the restore proceeds."
    )

    backup_files = sorted(
        glob.glob(os.path.join(BACKUP_DIR, "worldcup_*.db")),
        reverse=True   # newest first
    )

    if not backup_files:
        st.info("No backups found in the backups/ folder.")
        if st.button("← Back"):
            st.session_state["admin_page"] = "main"
            st.rerun()
        return

    # Build display labels from filenames
    def _label(path):
        fname = os.path.basename(path)            # worldcup_20250610_143022.db
        try:
            ts_part = fname.replace("worldcup_", "").replace(".db", "")
            dt = datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
            return dt.strftime("%-d %b %Y  %H:%M:%S UTC") + f"  ({fname})"
        except Exception:
            return fname

    labels      = [_label(p) for p in backup_files]
    selected_lbl = st.selectbox("Select a backup to restore", labels)
    selected_path = backup_files[labels.index(selected_lbl)]

    # Show a basic summary of what's inside the chosen backup
    try:
        tmp_conn = sqlite3.connect(selected_path)
        tmp_conn.row_factory = sqlite3.Row
        user_count = tmp_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pred_count = tmp_conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        tmp_conn.close()
        st.info(f"**Snapshot contains:** {user_count} user(s) · {pred_count} prediction row(s)")
    except Exception as e:
        st.error(f"Could not read backup file: {e}")
        if st.button("← Back"):
            st.session_state["admin_page"] = "main"
            st.rerun()
        return

    # Two-step confirmation to avoid accidental restores
    confirmed = st.checkbox("I understand this will overwrite the live database")

    if confirmed:
        if st.button("🔄 Restore now", type="primary"):
            try:
                # Safety-backup current DB first
                run_backup()

                # Close the shared connection before overwriting
                # (SQLite on disk — shutil.copy2 is safe here)
                shutil.copy2(selected_path, DB_PATH)

                # Clear any cached connection so the app re-opens the restored DB
                st.cache_resource.clear()

                st.success(
                    "✅ Restore complete! The database has been replaced with the "
                    f"selected snapshot. Reload the app to see the restored data."
                )
                st.info("A safety backup of the previous live database was saved before overwriting.")
            except Exception as e:
                st.error(f"Restore failed: {e}")

    if st.button("← Back"):
        st.session_state["admin_page"] = "main"
        st.rerun()

def admin_page():
    st.title("🔧 Admin Control Panel")

    if "admin_page" not in st.session_state:
        st.session_state["admin_page"] = "main"

    page = st.session_state["admin_page"]

    if page == "main":
        st.subheader("Admin Tools")
        if st.button("❌ Delete User Accounts"):
            st.session_state["admin_page"] = "delete"
            st.rerun()
        if st.button("📄 View User Predictions"):
            st.session_state["admin_page"] = "view_preds"
            st.rerun()
        if st.button("♻️ Restore from Backup"):
            st.session_state["admin_page"] = "restore"
            st.rerun()

        st.divider()
        st.subheader("💾 Manual Backup")
        st.caption("Download a copy of the live database to your device.")
        try:
            with open(DB_PATH, "rb") as _f:
                _db_bytes = _f.read()
            _ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                label="⬇️ Download worldcup.db",
                data=_db_bytes,
                file_name=f"worldcup_{_ts}.db",
                mime="application/octet-stream",
            )
        except FileNotFoundError:
            st.error("Database file not found — has the app written any data yet?")

        st.divider()
        if st.button("← Back to login"):
            st.session_state.pop("admin", None)
            st.rerun()
    elif page == "delete":
        admin_delete_users()
    elif page == "view_preds":
        admin_view_user_predictions()
    elif page == "restore":
        admin_restore_backup()

def admin_view_user_predictions():
    st.title("📄 View User Predictions")
    conn  = get_db()
    users = conn.execute("SELECT id, name FROM users ORDER BY name ASC").fetchall()

    if not users:
        st.info("No users found.")
        return

    user_map      = {u["name"]: u["id"] for u in users}
    selected_name = st.selectbox("Select a user", list(user_map.keys()))
    user_id       = user_map[selected_name]

    preds = conn.execute(
        "SELECT * FROM predictions WHERE user_id=? ORDER BY match_id",
        (user_id,)
    ).fetchall()

    if not preds:
        st.info("This user has no predictions.")
        return

    matches   = fetch_matches_fd()
    match_map = {m["id"]: m for m in matches}

    rows = []
    for p in preds:
        m = match_map.get(p["match_id"])
        if not m:
            continue
        rows.append({
            "Match":        f"{m['home_team']} vs {m['away_team']}",
            "Prediction":   f"{p['home_goals']} – {p['away_goals']}",
            "Booster":      "Yes" if p["booster_used"] else "No",
            "First Scorer": p["first_scorer"] or "-",
            "First Team":   p["first_team"] or "-",
            "Actual Score": (
                f"{m['home_score']} – {m['away_score']}"
                if m["home_score"] is not None else "-"
            ),
            "Points":  p["points"],
            "Settled": "Yes" if p["settled"] else "No",
        })

    st.table(rows)

    if st.button("← Back"):
        st.session_state["admin_page"] = "main"
        st.rerun()

# ─────────────────────────────────────────────
# UI — LEADERBOARD
# ─────────────────────────────────────────────
def leaderboard_page():
    st.title("🏆 Leaderboard")
    board = get_leaderboard()
    if not board:
        st.info("No scores yet.")
        return

    ranked = [
        {"🏅 Rank": i, "👤 Name": r["name"], "⭐ Points": r["total_pts"]}
        for i, r in enumerate(board, start=1)
    ]
    st.table(ranked)

    if st.button("← Back"):
        st.session_state["page"] = "predictions"
        st.rerun()

# ─────────────────────────────────────────────
# UI — PREDICTION HISTORY
# ─────────────────────────────────────────────
def history_page(user: dict, matches: list):
    st.title("📜 My Prediction History")

    history   = get_prediction_history(user["id"])
    match_map = {m["id"]: m for m in matches}

    if not history:
        st.info("No settled predictions yet — check back after matches finish.")
        if st.button("← Back"):
            st.session_state["page"] = "predictions"
            st.rerun()
        return

    total_points = sum(h["points"] for h in history)
    st.metric("Total points from settled matches", total_points)
    st.divider()

    for h in history:
        m = match_map.get(h["match_id"])
        if not m:
            continue

        actual_h = m.get("home_score")
        actual_a = m.get("away_score")

        col1, col2, col3 = st.columns([4, 3, 2])
        with col1:
            st.markdown(f"**{m['home_team']} vs {m['away_team']}**")
            st.caption(display_time(parse_kickoff(m["date"])))
        with col2:
            booster_tag = " ⚡2x" if h["booster_used"] else ""
            st.markdown(f"Your prediction: **{h['home_goals']} – {h['away_goals']}**{booster_tag}")
            st.markdown(
                f"Actual: **{actual_h} – {actual_a}**"
                if actual_h is not None else "Actual: TBC"
            )
            if h["first_scorer"]:
                st.caption(f"First scorer: {h['first_scorer']} ({h['first_team']})")
        with col3:
            colour = "green" if h["points"] > 0 else "grey"
            st.markdown(
                f"<span style='font-size:1.4rem;color:{colour};font-weight:bold'>"
                f"+{h['points']} pts</span>",
                unsafe_allow_html=True,
            )

        st.divider()

    if st.button("← Back"):
        st.session_state["page"] = "predictions"
        st.rerun()

# ─────────────────────────────────────────────
# UI — MATCH CENTRE
# ─────────────────────────────────────────────
def match_centre_page(matches: list, user: dict):
    st.title("🏟️ Match Centre")

    now_utc = datetime.now(ZoneInfo("UTC"))

    # ── tab layout ───────────────────────────
    tab_live, tab_today, tab_results, tab_fixtures, tab_standings = st.tabs([
        "🔴 Live", "📅 Today", "✅ Results", "🗓️ Fixtures", "📊 Standings"
    ])

    # ── helper: render one match card ────────
    def _match_card(m: dict, show_incidents: bool = False):
        kickoff  = parse_kickoff(m["date"])
        is_ko    = m["stage"] not in GROUP_STAGES
        stage_lbl = m["stage"].replace("_", " ").title()
        if m.get("group"):
            stage_lbl = m["group"]

        # outer container
        with st.container():
            # header row: stage label + venue
            meta_parts = [stage_lbl]
            if m.get("venue"):
                meta_parts.append(m["venue"])
            st.caption(" · ".join(meta_parts))

            # score / time row
            c_home, c_score, c_away = st.columns([4, 3, 4])

            with c_home:
                crest = m.get("home_crest", "")
                if crest:
                    st.image(crest, width=40)
                st.markdown(f"**{m['home_team']}**")

            with c_score:
                if m["status"] == "finished":
                    ht_line = ""
                    if m.get("home_ht") is not None:
                        ht_line = f"<br><small>(HT {m['home_ht']}–{m['away_ht']})</small>"
                    st.markdown(
                        f"<div style='text-align:center;font-size:1.8rem;font-weight:bold'>"
                        f"{m['home_score']} – {m['away_score']}"
                        f"</div>{ht_line}",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        "<div style='text-align:center;color:grey;font-size:0.8rem'>FT</div>",
                        unsafe_allow_html=True,
                    )
                elif m["status"] == "live":
                    st.markdown(
                        f"<div style='text-align:center;font-size:1.8rem;font-weight:bold'>"
                        f"{m['home_score'] or 0} – {m['away_score'] or 0}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        "<div style='text-align:center;color:red;font-weight:bold'>"
                        "🔴 LIVE</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='text-align:center;font-size:1.1rem;color:grey'>"
                        f"{display_time(kickoff)}</div>",
                        unsafe_allow_html=True,
                    )

            with c_away:
                crest = m.get("away_crest", "")
                if crest:
                    st.image(crest, width=40)
                st.markdown(f"**{m['away_team']}**")

            # referee
            if m.get("referee"):
                st.caption(f"🟨 Referee: {m['referee']}")

            # user's prediction badge
            if user:
                pred = get_prediction(user["id"], m["id"])
                if pred and pred["home_goals"] is not None:
                    boost = " ⚡" if pred["booster_used"] else ""
                    pts_txt = f"  •  **{pred['points']} pts**" if pred["settled"] else ""
                    st.info(
                        f"Your pick: {pred['home_goals']} – {pred['away_goals']}"
                        f"{boost}{pts_txt}"
                    )
                else:
                    if is_locked(kickoff):
                        st.warning("No prediction submitted.")

            # ── incidents (goals / cards / subs) ──
            if show_incidents and m["status"] in ("live", "finished"):
                ss_events = fetch_sofascore_events_for_world_cup()
                ss_id     = get_sofascore_id_for_match(m, ss_events)
                if ss_id:
                    raw_inc = fetch_sofascore_incidents(ss_id)
                    inc     = parse_incidents(raw_inc, m["home_team"], m["away_team"])

                    if inc["goals"]:
                        st.markdown("**⚽ Goals**")
                        for g in inc["goals"]:
                            suffix = " (pen)" if g["is_penalty"] else " (og)" if g["is_own_goal"] else ""
                            st.markdown(
                                f"&nbsp;&nbsp;`{g['minute']}` {g['player']}{suffix} — *{g['team']}*"
                            )

                    if inc["yellow_cards"]:
                        st.markdown("**🟨 Yellow Cards**")
                        for c in inc["yellow_cards"]:
                            st.markdown(f"&nbsp;&nbsp;`{c['minute']}` {c['player']} — *{c['team']}*")

                    if inc["red_cards"]:
                        st.markdown("**🟥 Red Cards**")
                        for c in inc["red_cards"]:
                            st.markdown(f"&nbsp;&nbsp;`{c['minute']}` {c['player']} — *{c['team']}*")

                    if inc["subs"]:
                        with st.expander("🔄 Substitutions"):
                            for s in inc["subs"]:
                                st.markdown(
                                    f"`{s['minute']}` ↑ {s['player_in']} / "
                                    f"↓ {s['player_out']} — *{s['team']}*"
                                )
                else:
                    st.caption("Incident data unavailable (SofaScore).")

            st.divider()

    # ── LIVE TAB ─────────────────────────────
    with tab_live:
        live = [m for m in matches if m["status"] == "live"]
        if not live:
            st.info("No matches currently live.")
        else:
            st.caption("🔄 Scores refresh every 60 seconds — press F5 to update.")
            for m in live:
                _match_card(m, show_incidents=True)

    # ── TODAY TAB ────────────────────────────
    with tab_today:
        today_uk = datetime.now(UK_TZ).date()
        today_matches = [
            m for m in matches
            if parse_kickoff(m["date"]).astimezone(UK_TZ).date() == today_uk
        ]
        if not today_matches:
            st.info("No matches today.")
        else:
            for m in sorted(today_matches, key=lambda x: x["date"]):
                _match_card(m, show_incidents=(m["status"] in ("live", "finished")))

    # ── RESULTS TAB ──────────────────────────
    with tab_results:
        finished = [m for m in matches if m["status"] == "finished"]
        if not finished:
            st.info("No results yet.")
        else:
            # Group by date (UK)
            by_date: dict = {}
            for m in finished:
                d = parse_kickoff(m["date"]).astimezone(UK_TZ).strftime("%A %d %b %Y")
                by_date.setdefault(d, []).append(m)

            for date_label in sorted(by_date.keys(), reverse=True):
                with st.expander(f"📅 {date_label}", expanded=(date_label == list(by_date.keys())[0])):
                    for m in sorted(by_date[date_label], key=lambda x: x["date"]):
                        _match_card(m, show_incidents=True)

    # ── FIXTURES TAB ─────────────────────────
    with tab_fixtures:
        upcoming = [m for m in matches if m["status"] == "scheduled"]
        if not upcoming:
            st.info("No upcoming fixtures.")
        else:
            # Key by ISO date string (YYYY-MM-DD) so sorting is chronological,
            # then display the human-readable label separately.
            by_date: dict = {}   # "2026-06-12" -> list of matches
            for m in upcoming:
                iso_day = parse_kickoff(m["date"]).astimezone(UK_TZ).strftime("%Y-%m-%d")
                by_date.setdefault(iso_day, []).append(m)

            sorted_iso_dates = sorted(by_date.keys())          # correct chrono order
            near_dates       = set(sorted_iso_dates[:7])       # first 7 days expanded

            for iso_day in sorted_iso_dates:
                display_label = datetime.strptime(iso_day, "%Y-%m-%d").strftime("%A %d %b %Y")
                expanded = iso_day in near_dates
                with st.expander(f"📅 {display_label}", expanded=expanded):
                    for m in sorted(by_date[iso_day], key=lambda x: x["date"]):
                        _match_card(m, show_incidents=False)

    # ── STANDINGS TAB ────────────────────────
    with tab_standings:
        standings = fetch_standings_fd()
        if not standings:
            st.info("Standings not available yet.")
        else:
            # football-data returns one entry per group
            for group in standings:
                group_name = group.get("group") or group.get("stage") or "Standings"
                table      = group.get("table", [])
                if not table:
                    continue

                with st.expander(f"🏴 {group_name}", expanded=False):
                    rows = []
                    for entry in table:
                        rows.append({
                            "Pos":  entry.get("position", ""),
                            "Team": entry.get("team", {}).get("name", ""),
                            "P":    entry.get("playedGames", 0),
                            "W":    entry.get("won", 0),
                            "D":    entry.get("draw", 0),
                            "L":    entry.get("lost", 0),
                            "GF":   entry.get("goalsFor", 0),
                            "GA":   entry.get("goalsAgainst", 0),
                            "GD":   entry.get("goalDifference", 0),
                            "Pts":  entry.get("points", 0),
                        })
                    st.table(rows)

    if st.button("← Back"):
        st.session_state["page"] = "predictions"
        st.rerun()

# ─────────────────────────────────────────────
# UI — PREDICTIONS
# ─────────────────────────────────────────────

# Ordered list of (bucket_key, display_label, emoji) for section headers.
STAGE_ORDER = [
    ("group_round_1",  "Group Stage — Round 1",  "🌍"),
    ("group_round_2",  "Group Stage — Round 2",  "🌍"),
    ("group_round_3",  "Group Stage — Round 3",  "🌍"),
    ("round_of_32",    "Round of 32",             "⚔️"),
    ("round_of_16",    "Round of 16",             "⚔️"),
    ("quarterfinal",   "Quarter Finals",          "🏅"),
    ("semifinal",      "Semi Finals",             "🏅"),
    ("final",          "Final",                   "🏆"),
]

def predictions_page(user: dict, matches: list):
    st.header("📋 Your predictions")

    now_utc = datetime.now(ZoneInfo("UTC"))

    if not matches:
        st.info("No matches found. Check your API token or try again shortly.")
        return

    # Build a lookup: stage_bucket -> list of ALL match IDs (for booster scoping)
    bucket_ids: dict = {}
    for m in matches:
        b = stage_bucket(m)
        bucket_ids.setdefault(b, []).append(m["id"])

    # Group every match by its stage bucket
    bucket_matches: dict = {}
    for m in matches:
        b = stage_bucket(m)
        bucket_matches.setdefault(b, []).append(m)

    # Sort matches within each bucket chronologically
    for b in bucket_matches:
        bucket_matches[b].sort(key=lambda x: x["date"])

    any_section_shown = False

    for bucket_key, section_label, emoji in STAGE_ORDER:
        section_matches = bucket_matches.get(bucket_key, [])
        if not section_matches:
            continue

        any_section_shown = True
        upcoming_in_section  = [m for m in section_matches if parse_kickoff(m["date"]) > now_utc]
        finished_in_section  = [m for m in section_matches if parse_kickoff(m["date"]) <= now_utc]

        st.markdown(f"### {emoji} {section_label}")

        if upcoming_in_section:
            with st.expander(f"📝 Upcoming ({len(upcoming_in_section)} match{'es' if len(upcoming_in_section)!=1 else ''})", expanded=True):
                stage_ids = bucket_ids.get(bucket_key, [])
                for match in upcoming_in_section:
                    _render_prediction_form(user, match, stage_ids)

        if finished_in_section:
            with st.expander(f"✅ Played ({len(finished_in_section)} match{'es' if len(finished_in_section)!=1 else ''})", expanded=False):
                for match in finished_in_section:
                    _render_locked_match(user, match)

        st.divider()

    if not any_section_shown:
        st.info("No matches found. Check your API token or try again shortly.")

def _render_prediction_form(user: dict, match: dict, matchday_ids: list):
    match_id  = match["id"]
    home_team = match["home_team"]
    away_team = match["away_team"]
    kickoff   = parse_kickoff(match["date"])
    locked    = is_locked(kickoff)
    is_ko     = match["stage"] not in GROUP_STAGES
    saved     = get_prediction(user["id"], match_id)

    st.markdown(f"**{home_team} vs {away_team}**")
    st.caption(f"🕐 {display_time(kickoff)}")

    mins_to_lock = int(
        (kickoff - timedelta(hours=1) - datetime.now(ZoneInfo("UTC"))).total_seconds() / 60
    )
    if not locked and mins_to_lock <= 120:
        st.caption(f"⏳ Locks in ~{mins_to_lock} min")

    if locked:
        if saved:
            st.info(
                f"🔒 Locked — your prediction: "
                f"{saved['home_goals']} – {saved['away_goals']}"
                + (" ⚡2x" if saved["booster_used"] else "")
            )
        else:
            st.warning("🔒 Locked — no prediction submitted.")
        st.divider()
        return

    c1, c2, c3 = st.columns([3, 1, 3])
    with c1:
        st.write(home_team)
        home_g = st.number_input(
            "Home goals", min_value=0, max_value=20,
            value=int(saved["home_goals"]) if saved and saved["home_goals"] is not None else 0,
            key=f"h_{match_id}"
        )
    with c2:
        st.markdown("<br><br>**–**", unsafe_allow_html=True)
    with c3:
        st.write(away_team)
        away_g = st.number_input(
            "Away goals", min_value=0, max_value=20,
            value=int(saved["away_goals"]) if saved and saved["away_goals"] is not None else 0,
            key=f"a_{match_id}"
        )

    booster_already_used = booster_used_this_stage(
        user["id"], matchday_ids, exclude_match_id=match_id
    )
    current_boost = bool(saved and saved["booster_used"])
    if booster_already_used and not current_boost:
        st.caption("⚡ 2x booster already used for this stage round.")
        booster = False
    else:
        booster = st.checkbox(
            "⚡ Use 2x booster on this match",
            value=current_boost,
            key=f"b_{match_id}"
        )

    first_scorer = None
    first_team   = None
    if is_ko:
        st.caption("Knockout match — extra points for first scorer & first team.")
        home_squad     = fetch_team_squad(match["home_team_id"])
        away_squad     = fetch_team_squad(match["away_team_id"])
        combined_squad = [""] + home_squad + away_squad

        first_scorer = st.selectbox(
            "First goalscorer (optional)",
            options=combined_squad,
            index=combined_squad.index(saved["first_scorer"])
                  if saved and saved["first_scorer"] in combined_squad else 0,
            key=f"fs_{match_id}"
        )
        first_team = st.selectbox(
            "Team to score first (optional)",
            options=["", home_team, away_team],
            index=(["", home_team, away_team].index(saved["first_team"])
                   if saved and saved["first_team"] in ["", home_team, away_team] else 0),
            key=f"ft_{match_id}"
        )

    if st.button("Save prediction", key=f"save_{match_id}"):
        save_prediction(
            user["id"], match_id,
            int(home_g), int(away_g),
            booster_used=booster,
            first_scorer=first_scorer or None,
            first_team=first_team or None,
        )
        st.success("Prediction saved.")
        st.rerun()

    st.divider()

def _render_locked_match(user: dict, match: dict):
    match_id  = match["id"]
    home_team = match["home_team"]
    away_team = match["away_team"]
    kickoff   = parse_kickoff(match["date"])
    saved     = get_prediction(user["id"], match_id)

    st.markdown(f"**{home_team} vs {away_team}**")
    st.caption(f"🕐 {display_time(kickoff)}")

    if match["status"] == "finished":
        st.write(f"Final score: {match['home_score']} – {match['away_score']}")
    elif match["status"] == "live":
        st.write("Match in progress.")
    else:
        st.write("Match scheduled / not started.")

    if saved:
        st.info(
            f"Your prediction: {saved['home_goals']} – {saved['away_goals']}"
            + (" ⚡2x" if saved["booster_used"] else "")
        )
    else:
        st.warning("No prediction submitted for this match.")

    st.divider()

# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────
def main():
    maybe_backup()

    matches = fetch_matches_fd()
    if matches:
        try:
            auto_settle(matches)
        except Exception:
            pass

    if st.session_state.get("admin"):
        admin_page()
        return

    user = st.session_state.get("user")
    if not user:
        login_page()
        return

    # Refresh user's points from DB on each load
    conn       = get_db()
    fresh_user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if fresh_user:
        st.session_state["user"] = dict(fresh_user)
        user = st.session_state["user"]

    st.sidebar.title("👤 Your profile")
    st.sidebar.write(f"Name: {user['name']}")
    st.sidebar.write(f"Total points: {user['total_pts']}")

    if st.sidebar.button("📋 Predictions"):
        st.session_state["page"] = "predictions"
        st.rerun()

    if st.sidebar.button("🏟️ Match Centre"):
        st.session_state["page"] = "match_centre"
        st.rerun()

    if st.sidebar.button("🏆 Leaderboard"):
        st.session_state["page"] = "leaderboard"
        st.rerun()

    if st.sidebar.button("📜 My History"):
        st.session_state["page"] = "history"
        st.rerun()

    if st.sidebar.button("Log out"):
        st.session_state.pop("user", None)
        st.session_state["page"] = "predictions"
        st.rerun()

    page = st.session_state.get("page", "predictions")

    if page == "leaderboard":
        leaderboard_page()
    elif page == "history":
        history_page(user, matches)
    elif page == "match_centre":
        match_centre_page(matches, user)
    else:
        predictions_page(user, matches)

if __name__ == "__main__":
    main()

# ─────────────────────────────────────────────
