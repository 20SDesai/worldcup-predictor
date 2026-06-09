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
API_BASE_FD = "https://api.football-data.org/v4"
API_TOKEN_FD = "3cc2efd31a07449aa2f36539e9cda614"  # <- put your real token here

API_BASE_SOFA = "https://api.sofascore.com/api/v1"
SOFA_WORLD_CUP_TOURNAMENT_ID = 16  # World Cup unique-tournament ID on SofaScore

COMPETITION = "WC"          # World Cup on football-data.org
DB_PATH     = "worldcup.db"
UK_TZ       = ZoneInfo("Europe/London")

HEADERS_FD = {"X-Auth-Token": API_TOKEN_FD}
LEAGUE_CODE = "WC2026" 
ADMIN_PIN = "9999"   # change this to whatever you want
  # ← change this to whatever you want

st.set_page_config(
    page_title="World Cup 2026 Predictor",
    page_icon="⚽",
    layout="wide",
)

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

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
            match_id_fd   TEXT PRIMARY KEY,
            sofascore_id  INTEGER
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────────
def get_or_create_user(name: str, pin: str):
    conn = get_db()

    # NORMALISE NAME
    clean_name = name.strip().lower()

    # Check if name already exists
    existing = conn.execute("SELECT * FROM users WHERE name=?", (clean_name,)).fetchone()
    if existing:
        # If PIN matches → login
        if existing["pin"] == pin:
            conn.close()
            return dict(existing)
        else:
            conn.close()
            return None  # Wrong PIN

    # Otherwise create new user
    conn.execute("INSERT INTO users (name,pin) VALUES (?,?)", (clean_name, pin))
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE name=?", (clean_name,)).fetchone()
    conn.close()
    return dict(user)


def verify_user(name: str, pin: str):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE name=? AND pin=?", (name, pin)
    ).fetchone()
    conn.close()
    return dict(user) if user else None

def generate_invite_links(names: list):
    links = {}
    conn = get_db()
    for name in names:
        pin = str(random.randint(1000, 9999))
        conn.execute(
            "INSERT OR IGNORE INTO users (name,pin) VALUES (?,?)", (name, pin)
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        links[name] = {"pin": user["pin"], "url": f"?user={name}&pin={user['pin']}"}
    conn.close()
    return links

# ─────────────────────────────────────────────
# FOOTBALL-DATA.ORG HELPERS
# ─────────────────────────────────────────────
def _normalise_match_fd(m: dict) -> dict:
    score = m.get("score", {})
    ft    = score.get("fullTime", {}) or {}

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
        "id":           str(m["id"]),
        "home_team":    m["homeTeam"]["name"],
        "away_team":    m["awayTeam"]["name"],
        "home_team_id": m["homeTeam"]["id"],
        "away_team_id": m["awayTeam"]["id"],
        "date":         m.get("utcDate", ""),
        "home_score":   ft.get("home"),
        "away_score":   ft.get("away"),
        "status":       status,
        "stage":        stage,
        "matchday":     str(m.get("matchday") or m.get("group") or "Group stage"),
        "group":        m.get("group") or "",
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

# ─────────────────────────────────────────────
# SQUAD FETCHING (Option A)
# ─────────────────────────────────────────────
@st.cache_data(ttl=3600)
def fetch_team_squad(team_id: int) -> list:
    if not team_id:
        return []
    try:
        url = f"{API_BASE_FD}/teams/{team_id}"
        r = requests.get(url, headers=HEADERS_FD, timeout=10)
        r.raise_for_status()
        data = r.json()
        squad = data.get("squad", [])
        return [p.get("name") for p in squad if p.get("name")]
    except Exception:
        return []

# ─────────────────────────────────────────────
# SOFASCORE HELPERS
# ─────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_latest_world_cup_season_id() -> int | None:
    try:
        url = f"{API_BASE_SOFA}/unique-tournament/{SOFA_WORLD_CUP_TOURNAMENT_ID}/seasons"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        seasons = r.json().get("seasons", [])
        if not seasons:
            return None

        wc_2026 = [s for s in seasons if "2026" in str(s.get("name", ""))]
        if wc_2026:
            return int(wc_2026[-1]["id"])

        seasons.sort(key=lambda s: s.get("id", 0))
        return int(seasons[-1]["id"])
    except Exception:
        return None

@st.cache_data(ttl=3600)
def fetch_sofascore_events_for_world_cup() -> list:
    season_id = get_latest_world_cup_season_id()
    if not season_id:
        return []

    try:
        url = f"{API_BASE_SOFA}/unique-tournament/{SOFA_WORLD_CUP_TOURNAMENT_ID}/season/{season_id}/events"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        events = r.json().get("events", [])
        out = []
        for ev in events:
            out.append({
                "id": ev.get("id"),
                "home_team": ev.get("homeTeam", {}).get("name", ""),
                "away_team": ev.get("awayTeam", {}).get("name", ""),
                "start_ts": ev.get("startTimestamp"),
                "round_name": ev.get("tournamentRound", {}).get("name", ""),  # NEW
            })


        return out
    except Exception:
        return []

def get_sofascore_id_cached(match_id_fd: str) -> int | None:
    conn = get_db()
    row = conn.execute(
        "SELECT sofascore_id FROM match_mapping WHERE match_id_fd=?",
        (match_id_fd,)
    ).fetchone()
    conn.close()
    return row["sofascore_id"] if row else None

def cache_sofascore_id(match_id_fd: str, sofascore_id: int):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO match_mapping (match_id_fd, sofascore_id) VALUES (?,?)",
        (match_id_fd, sofascore_id)
    )
    conn.commit()
    conn.close()

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

    candidates = []
    for ev in ss_events:
        home_ss = ev["home_team"]
        away_ss = ev["away_team"]
        ts      = ev["start_ts"]
        if not ts:
            continue
        ko_ss = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))

        def name_match(a, b):
            a0 = a.split()[0].lower()
            b0 = b.split()[0].lower()
            return a0 in b.lower() or b0 in a.lower()

        if not name_match(home_fd, home_ss):
            continue
        if not name_match(away_fd, away_ss):
            continue

        if abs((ko_ss - ko_fd).total_seconds()) > 3 * 3600:
            continue

        candidates.append((ev, abs((ko_ss - ko_fd).total_seconds())))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1])
    best = candidates[0][0]
    fd_match["round_name"] = best.get("tournamentRound", {}).get("name", "")
    return int(best["id"])


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
    try:
        url = f"{API_BASE_SOFA}/event/{sofascore_id}/incidents"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json().get("incidents", [])
    except Exception:
        return []

def extract_first_goal_from_incidents(incidents: list):
    goals = []
    for inc in incidents:
        if inc.get("type") != "goal":
            continue
        team = inc.get("team", {}).get("name")
        player = inc.get("player", {}).get("name")
        minute = inc.get("time", {}).get("minute", 0)
        if team and player:
            goals.append((minute, player, team))

    if not goals:
        return None, None

    goals.sort(key=lambda x: x[0])
    _, player, team = goals[0]
    return player, team

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
    ph, pa = pred["home_goals"], pred["away_goals"]
    rh, ra = result["home_goals"], result["away_goals"]

    if ph is None or pa is None:
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
    conn = get_db()
    already = conn.execute(
        "SELECT id FROM predictions WHERE match_id=? AND settled=1 LIMIT 1",
        (match_id,)
    ).fetchone()
    if already:
        conn.close()
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
    conn.close()

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
    conn.close()

def get_prediction(user_id, match_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM predictions WHERE user_id=? AND match_id=?",
        (user_id, match_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def booster_used_this_round(user_id, round_match_ids, exclude_match_id=None):
    conn = get_db()
    for mid in matchday_match_ids:
        if mid == exclude_match_id:
            continue
        row = conn.execute(
            "SELECT booster_used FROM predictions WHERE user_id=? AND match_id=?",
            (user_id, mid)
        ).fetchone()
        if row and row["booster_used"]:
            conn.close()
            return True
    conn.close()
    return False

def get_leaderboard():
    conn = get_db()
    rows = conn.execute(
        "SELECT name, total_pts FROM users ORDER BY total_pts DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_user(user_id: int):
    conn = get_db()
    # Delete predictions first (foreign key cleanup)
    conn.execute("DELETE FROM predictions WHERE user_id=?", (user_id,))
    # Delete the user
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# AUTO SETTLE FINISHED MATCHES
# ─────────────────────────────────────────────
def auto_settle(matches):
    ss_events = fetch_sofascore_events_for_world_cup()

    for match in matches:
        if match["status"] != "finished":
            continue

        is_ko = match["stage"] not in ("group", "group stage", "GROUP_STAGE", "")

        first_scorer = None
        first_team   = None

        try:
            ss_id = get_sofascore_id_for_match(match, ss_events)
            if ss_id:
                incidents = fetch_sofascore_incidents(ss_id)
                fs_name, fs_team = extract_first_goal_from_incidents(incidents)
                first_scorer = fs_name
                first_team   = fs_team
        except Exception:
            pass

        result = {
            "home_goals":   match["home_score"],
            "away_goals":   match["away_score"],
            "first_scorer": first_scorer,
            "first_team":   first_team,
        }

        settle_match(match["id"], result, is_ko)
# ─────────────────────────────────────────────
# UI — LOGIN
# ─────────────────────────────────────────────
def login_page():
    st.title("⚽ World Cup 2026 Predictor")

    params = st.query_params
    default_name = params.get("user", "")
    default_pin  = params.get("pin", "")

    name = st.text_input("Your name", value=default_name)
    pin  = st.text_input("PIN (4 digits)", value=default_pin, max_chars=4, type="password")
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
        # Admin panel access
        if st.button("Admin panel"):
            st.session_state["awaiting_admin_pin"] = True
            st.rerun()

# Ask for PIN
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

    conn = get_db()
    users = conn.execute("SELECT id, name, total_pts FROM users ORDER BY name ASC").fetchall()
    conn.close()

    if not users:
        st.info("No users found.")
        return

    user_names = [f"{u['name']} (ID {u['id']}, {u['total_pts']} pts)" for u in users]
    selected = st.selectbox("Select a user to delete", user_names)

    selected_id = int(selected.split("ID ")[1].split(",")[0])

    if st.button("Delete User"):
        delete_user(selected_id)
        st.success("User deleted successfully.")
        st.rerun()

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

        if st.button("← Back to login"):
            st.session_state.pop("admin", None)
            st.rerun()

    elif page == "delete":
        admin_delete_users()

    elif page == "view_preds":
        admin_view_user_predictions()


def admin_view_user_predictions():
    st.title("📄 View User Predictions")

    # Load users
    conn = get_db()
    users = conn.execute("SELECT id, name FROM users ORDER BY name ASC").fetchall()
    conn.close()

    if not users:
        st.info("No users found.")
        return

    # Select user
    user_map = {u["name"]: u["id"] for u in users}
    selected_name = st.selectbox("Select a user", list(user_map.keys()))
    user_id = user_map[selected_name]

    # Load predictions for this user
    conn = get_db()
    preds = conn.execute(
        "SELECT * FROM predictions WHERE user_id=? ORDER BY match_id",
        (user_id,)
    ).fetchall()
    conn.close()

    if not preds:
        st.info("This user has no predictions.")
        return

    # Load matches from API
    matches = fetch_matches_fd()
    match_map = {m["id"]: m for m in matches}

    # Build table rows
    rows = []
    for p in preds:
        mid = p["match_id"]
        m = match_map.get(mid)

        if not m:
            continue

        rows.append({
            "Match": f"{m['home_team']} vs {m['away_team']}",
            "Prediction": f"{p['home_goals']} – {p['away_goals']}",
            "Booster": "Yes" if p["booster_used"] else "No",
            "First Scorer": p["first_scorer"] or "-",
            "First Team": p["first_team"] or "-",
            "Actual Score": (
                f"{m['home_score']} – {m['away_score']}"
                if m["home_score"] is not None else "-"
            ),
            "Points": p["points"],
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

    st.table(board)

    if st.button("← Back"):
        st.session_state["page"] = "predictions"
        st.rerun()

# ─────────────────────────────────────────────
# UI — PREDICTIONS
# ─────────────────────────────────────────────
def predictions_page(user: dict, matches: list):
    st.header("📋 Your predictions")

    now_utc = datetime.now(ZoneInfo("UTC"))
    upcoming = [m for m in matches if parse_kickoff(m["date"]) > now_utc]
    live_finished = [m for m in matches if parse_kickoff(m["date"]) <= now_utc]

    if not upcoming and not live_finished:
        st.info("No matches found. Check your API token or try again shortly.")
        return

    if upcoming:
        st.subheader("Upcoming matches")
        matchday_groups: dict = {}
        for m in upcoming:
            round_name = m.get("round_name", "")

            if "Round" in round_name:
                label = round_name  # e.g. "Round 1", "Round of 16"
            else:
                label = m.get("matchday", "Unknown Round")


            matchday_groups.setdefault(label, []).append(m)

        for md_label, md_matches in matchday_groups.items():
            with st.expander(f"📅 {md_label}", expanded=True):
                md_ids = [m["id"] for m in md_matches]
                for match in md_matches:
                    _render_prediction_form(user, match, md_ids)

    if live_finished:
        st.subheader("Live & finished matches")
        for match in live_finished[-10:]:
            _render_locked_match(user, match)

def _render_prediction_form(user: dict, match: dict, matchday_ids: list):
    match_id  = match["id"]
    # Extract matchday number from SofaScore round name
    round_name = match.get("round_name", "")
    try:
        matchday_number = int(round_name.split()[-1])  # "Round 1" → 1
    except:
        matchday_number = 1

    home_team = match["home_team"]
    away_team = match["away_team"]
    kickoff   = parse_kickoff(match["date"])
    locked    = is_locked(kickoff)
    is_ko     = match["stage"] not in ("group", "group stage", "GROUP_STAGE", "")
    saved     = get_prediction(user["id"], match_id)

    st.markdown(f"**{home_team} vs {away_team}**")
    st.caption(f"🕐 {display_time(kickoff)}")

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

    booster_already_used = booster_used_this_matchday(user["id"], round_ids, exclude_match_id=match_id)

    current_boost = bool(saved and saved["booster_used"])
    if booster_already_used and not current_boost:
        st.caption("⚡ 2x booster already used this matchday.")
        booster = False
    else:
        booster = st.checkbox(
            f"⚡ Use Booster for {round_name}",
            value=current_boost,
            key=f"b_{match_id}"
        )


    first_scorer = None
    first_team   = None
    if is_ko:
        st.caption("Knockout match — extra points for first scorer & first team.")

        home_squad = fetch_team_squad(match["home_team_id"])
        away_squad = fetch_team_squad(match["away_team_id"])
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
        score_line = f"{match['home_score']} – {match['away_score']}"
        st.write(f"Final score: {score_line}")
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

    st.sidebar.title("👤 Your profile")
    st.sidebar.write(f"Name: {user['name']}")
    st.sidebar.write(f"Total points: {user['total_pts']}")

    if st.sidebar.button("🏆 Leaderboard"):
        st.session_state["page"] = "leaderboard"
        st.rerun()

    if st.sidebar.button("Log out"):
        st.session_state.pop("user", None)
        st.session_state["page"] = "predictions"
        st.rerun()

    page = st.session_state.get("page", "predictions")
    if page == "leaderboard":
        leaderboard_page()
    else:
        predictions_page(user, matches)

if __name__ == "__main__":
    main()

# ─────────────────────────────────────────────
