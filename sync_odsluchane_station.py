#!/usr/bin/env python3
"""
Radio -> Spotify v1.0 — automatyczne playlisty z odsluchane.eu

Obsługiwane stacje:
- Radio Nowy Świat
- Chillizet
- Radio ZET
- Trójka
- Czwórka

Tryby:
  SYNC_MODE=live
  SYNC_MODE=backfill

STATION_KEY wybiera stację.
Każda stacja ma osobną playlistę i osobny plik stanu.
"""

import json
import os
import re
import sys
import time
import random
import unicodedata
from datetime import datetime, date, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ODSLUCHANE_URL = "https://www.odsluchane.eu/szukaj.php"

STATIONS = {
    "nowyswiat": {
        "id": "105",
        "playlist": "Radio Nowy Świat",
        "display": "Radio Nowy Świat",
    },
    "chillizet": {
        "id": "40",
        "playlist": "Chillizet",
        "display": "Chillizet",
    },
    "radiozet": {
        "id": "1",
        "playlist": "Radio ZET",
        "display": "Radio ZET",
    },
    "trojka": {
        "id": "48",
        "playlist": "Trójka",
        "display": "Trójka",
    },
    "czworka": {
        "id": "49",
        "playlist": "Czwórka",
        "display": "Czwórka",
    },
}

STATION_KEY = os.getenv("STATION_KEY", "").strip().lower()
if STATION_KEY not in STATIONS:
    raise SystemExit(
        "Ustaw STATION_KEY na: " + ", ".join(STATIONS.keys())
    )

STATION = STATIONS[STATION_KEY]
ODSLUCHANE_STATION_ID = STATION["id"]
STATION_DISPLAY = STATION["display"]
SOURCE_GENERATION = f"odsluchane-multi-v1:{ODSLUCHANE_STATION_ID}"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
WARSAW = ZoneInfo("Europe/Warsaw")

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

SYNC_MODE = os.getenv("SYNC_MODE", "live").strip().lower()
if SYNC_MODE not in {"live", "backfill"}:
    raise SystemExit("SYNC_MODE musi mieć wartość live albo backfill")

PLAYLIST_NAME = os.getenv("PLAYLIST_NAME", STATION["playlist"]).strip() or STATION["playlist"]
PLAYLIST_ID_ENV = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()
MAX_TRACKS = int(os.getenv("MAX_TRACKS", "5000"))
MAX_SEARCH_REQUESTS = int(os.getenv("MAX_SEARCH_REQUESTS", "30"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "900"))
MAX_INLINE_RETRY_SECONDS = int(os.getenv("MAX_INLINE_RETRY_SECONDS", "15"))
MISS_RETRY_DAYS = int(os.getenv("MISS_RETRY_DAYS", "7"))
LIVE_OVERLAP_MINUTES = int(os.getenv("LIVE_OVERLAP_MINUTES", "90"))
LIVE_FUTURE_TOLERANCE_MINUTES = int(os.getenv("LIVE_FUTURE_TOLERANCE_MINUTES", "15"))
BACKFILL_DAYS_PER_RUN = int(os.getenv("BACKFILL_DAYS_PER_RUN", "1"))
STATE_PATH = Path(os.getenv("STATE_PATH", f".radio_state_{STATION_KEY}.json"))

BACKFILL_RE = re.compile(r"\[radio-backfill:(\d{4}-\d{2}-\d{2})\]")
LIVE_RE = re.compile(r"\[radio-live:(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})\]")
STARTED = time.monotonic()

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

http = requests.Session()
http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; RadioSpotifyMulti/1.0)"})
ACCESS_TOKEN = None


class SearchBudgetStop(Exception):
    pass


class RuntimeStop(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after=60, reason=None, message=None):
        super().__init__(message or "Spotify rate limit")
        self.retry_after = max(1, int(retry_after or 60))
        self.reason = reason or ""
        self.message = message or ""


def now_pl():
    return datetime.now(WARSAW)


def now_utc():
    return datetime.now(timezone.utc)


def iso_utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def default_state():
    return {
        "version": "1.0",
        "playlist_id": "",
        "playlist_name": PLAYLIST_NAME,
        "uris": [],
        "keys": {},
        "misses": {},
        "pending_live": [],
        "pending_backfill": [],
        "backfill_last_completed": None,
        "live_day": None,
        "live_time": None,
        "blocked_until": None,
        "last_429": None,
        "source_generation": None,
    }


def load_state():
    state = default_state()
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as exc:
            print(f"UWAGA: nie mogę odczytać {STATE_PATH}: {exc}")

    state["version"] = "1.0"
    state["playlist_name"] = PLAYLIST_NAME
    state["uris"] = list(dict.fromkeys(state.get("uris") or []))
    state["keys"] = dict(state.get("keys") or {})
    state["misses"] = dict(state.get("misses") or {})
    state["pending_live"] = list(dict.fromkeys(state.get("pending_live") or []))
    state["pending_backfill"] = list(dict.fromkeys(state.get("pending_backfill") or []))

    if state.get("source_generation") != SOURCE_GENERATION:
        print(
            f"Migracja źródła {STATION_DISPLAY} -> odsluchane.eu: resetuję markery LIVE/BACKFILL "
            "bez kasowania playlisty."
        )
        state["source_generation"] = SOURCE_GENERATION
        state["backfill_last_completed"] = None
        state["live_day"] = None
        state["live_time"] = None

    return state


def save_state(state):
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def runtime_guard():
    if time.monotonic() - STARTED >= MAX_RUNTIME_SECONDS:
        raise RuntimeStop(f"Osiągnięto limit czasu {MAX_RUNTIME_SECONDS}s dla tego runu.")


def blocked_by_previous_429(state):
    until = parse_iso_datetime(state.get("blocked_until"))
    if not until:
        return False
    if now_utc() < until:
        left = int((until - now_utc()).total_seconds())
        print(f"Spotify nadal w okresie blokady po 429. Pozostało około {max(0, left)}s.")
        return True
    state["blocked_until"] = None
    save_state(state)
    return False


def remember_rate_limit(state, exc):
    blocked_until = now_utc() + timedelta(seconds=exc.retry_after + 5)
    state["blocked_until"] = iso_utc(blocked_until)
    state["last_429"] = {
        "at": iso_utc(now_utc()),
        "retry_after": exc.retry_after,
        "reason": exc.reason or None,
        "message": exc.message or None,
        "mode": SYNC_MODE,
    }
    save_state(state)
    print(
        f"429 zapisany: Retry-After={exc.retry_after}s, "
        f"reason={exc.reason or 'brak'}, blocked_until={state['blocked_until']}"
    )


def ensure_access_token():
    global ACCESS_TOKEN
    if ACCESS_TOKEN:
        return
    r = http.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    ACCESS_TOKEN = r.json()["access_token"]
    http.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})


def parse_spotify_error(response):
    reason = ""
    message = ""
    try:
        payload = response.json()
    except Exception:
        payload = {}

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            reason = error.get("reason") or error.get("code") or ""
            message = error.get("message") or error.get("description") or ""
        elif isinstance(error, str):
            message = error
        reason = reason or payload.get("reason") or ""
        message = message or payload.get("message") or ""

    reason = reason or response.headers.get("X-RateLimit-Reason", "")
    return str(reason), str(message)


def spotify(method, path, **kwargs):
    ensure_access_token()
    url = SPOTIFY_API + path

    for attempt in range(1, 6):
        runtime_guard()
        r = http.request(method, url, timeout=30, **kwargs)

        if r.status_code == 429:
            raw_retry = r.headers.get("Retry-After", "60")
            try:
                retry_after = max(1, int(float(raw_retry)))
            except Exception:
                retry_after = 60
            reason, message = parse_spotify_error(r)
            print(
                f"Spotify 429 — Retry-After={retry_after}s, "
                f"reason={reason or 'brak'}, message={message or 'brak'}"
            )
            is_quota = reason.upper() == "QUOTA_EXCEEDED"
            if not is_quota and retry_after <= MAX_INLINE_RETRY_SECONDS and attempt < 3:
                print(f"Czekam {retry_after}s i ponawiam...")
                time.sleep(retry_after)
                continue
            raise RateLimited(retry_after, reason, message)

        if r.status_code in (500, 502, 503, 504):
            if attempt == 5:
                print(f"Spotify {r.status_code}: {r.text[:800]}", file=sys.stderr)
                r.raise_for_status()
            delay = min(20, (2 ** (attempt - 1)) + random.random())
            print(f"Spotify {r.status_code} — próba {attempt}/5, retry za {delay:.1f}s")
            time.sleep(delay)
            continue

        if r.status_code >= 400:
            print(f"Spotify {r.status_code}: {r.text[:1000]}", file=sys.stderr)
            r.raise_for_status()

        return r

    raise RuntimeError("Spotify API: przekroczono liczbę prób")


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(feat|ft)\.?\b.*$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title_for_search(text):
    text = (text or "").strip()
    return re.sub(
        r"\s*\((?:remaster(?:ed)?(?:\s+\d{4})?|radio edit|edit)\)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()


def title_key(title):
    return normalize(clean_title_for_search(title))


def source_key(artist, title):
    return f"{normalize(artist)}|||{title_key(title)}"


def _dedupe_rows(rows):
    unique = []
    seen = set()
    for hhmm, artist, title in rows:
        key = (hhmm, normalize(artist), title_key(title))
        if key in seen:
            continue
        seen.add(key)
        unique.append((hhmm, artist, title))
    return unique


def _trim_live_rows_to_today(rows):
    """
    Odcina wpisy z przyszłą godziną i starsze wpisy po przejściu przez północ.
    Źródła LIVE zwykle pokazują wpisy od najnowszego do najstarszego.
    """
    if not rows:
        return []

    now_dt = now_pl()
    now_minutes = now_dt.hour * 60 + now_dt.minute
    latest_allowed = min(23 * 60 + 59, now_minutes + LIVE_FUTURE_TOLERANCE_MINUTES)

    output = []
    previous_minutes = None

    for hhmm, artist, title in rows:
        minutes = minutes_from_hhmm(hhmm)

        # Typowy rollover: 00:xx -> 23:xx = zaczyna się poprzedni dzień.
        if previous_minutes is not None and minutes > previous_minutes + 12 * 60:
            print(
                f"LIVE: wykryto granicę dnia przy {hhmm}; "
                "starsze wpisy z poprzedniego dnia pomijam."
            )
            break

        previous_minutes = minutes

        if minutes > latest_allowed:
            print(
                f"LIVE: pomijam przyszłą godzinę {hhmm} "
                f"(teraz {now_dt.strftime('%H:%M')})."
            )
            continue

        output.append((hhmm, artist, title))

    return _dedupe_rows(output)


def _split_artist_title(text):
    text = " ".join((text or "").split()).strip()
    if " - " not in text:
        return None
    artist, title = text.split(" - ", 1)
    artist = artist.strip()
    title = title.strip()
    if not artist or not title:
        return None
    return artist, title


def parse_odsluchane_html(html):
    """Parsuje playlistę odsluchane.eu: HH:MM | Wykonawca - Tytuł."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        hhmm = " ".join(cells[0].stripped_strings).strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
            continue

        song = " ".join(cells[1].stripped_strings).strip()
        parsed = _split_artist_title(song)
        if not parsed:
            continue

        hh, mm = map(int, hhmm.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue

        artist, title = parsed
        rows.append((f"{hh:02d}:{mm:02d}", artist, title))

    if rows:
        return _dedupe_rows(rows)

    # Fallback tekstowy, gdyby HTML serwisu się zmienił.
    strings = [" ".join(x.split()) for x in soup.stripped_strings]
    for i, text in enumerate(strings):
        if not re.fullmatch(r"\d{1,2}:\d{2}", text):
            continue
        if i + 1 >= len(strings):
            continue

        parsed = _split_artist_title(strings[i + 1])
        if not parsed:
            continue

        artist, title = parsed
        rows.append((text.zfill(5), artist, title))

    return _dedupe_rows(rows)


def fetch_odsluchane_window(day, time_from, time_to):
    runtime_guard()

    params = {
        "date": day.strftime("%d-%m-%Y"),
        "r": ODSLUCHANE_STATION_ID,
        "time_from": int(time_from),
        "time_to": int(time_to),
    }

    r = requests.get(
        ODSLUCHANE_URL,
        params=params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RadioSpotifyMulti/1.0)"},
        timeout=30,
    )
    r.raise_for_status()

    rows = parse_odsluchane_html(r.text)

    print(
        f"odsluchane.eu {params['date']} "
        f"{int(time_from):02d}:00-{int(time_to):02d}:00 -> {len(rows)} pozycji"
    )

    return rows


def get_odsluchane_live_rows(state):
    today = now_pl().date()
    now_hour = now_pl().hour

    marker_hour = None
    if state.get("live_day") == today.isoformat() and state.get("live_time"):
        try:
            marker_hour = int(state["live_time"].split(":", 1)[0])
        except Exception:
            marker_hour = None

    # Pierwszy run dnia: od północy. Kolejne: ok. 3h przed markerem.
    start_hour = 0 if marker_hour is None else max(0, marker_hour - 3)
    end_hour = min(24, now_hour + 1)
    if end_hour <= start_hour:
        end_hour = min(24, start_hour + 1)

    return _trim_live_rows_to_today(
        fetch_odsluchane_window(today, start_hour, end_hour)
    )


def get_odsluchane_day_rows(day):
    """
    Archiwum pełnego dnia w 6 oknach po 4h.
    Zwraca (rows, complete). Marker nie przesunie się po niepełnym dniu.
    """
    all_rows = []
    complete = True

    for start_hour in range(0, 24, 4):
        end_hour = min(24, start_hour + 4)
        try:
            all_rows.extend(
                fetch_odsluchane_window(day, start_hour, end_hour)
            )
        except Exception as exc:
            complete = False
            print(
                f"odsluchane.eu ERROR {day} "
                f"{start_hour:02d}:00-{end_hour:02d}:00: {exc}"
            )

    return _dedupe_rows(all_rows), complete


def get_live_rows(state):
    """LIVE dla wybranej stacji wyłącznie z odsluchane.eu."""
    try:
        rows = get_odsluchane_live_rows(state)
    except Exception as exc:
        print(f"LIVE source: odsluchane.eu ERROR: {exc}")
        rows = []

    if rows:
        print(
            f"LIVE source: odsluchane.eu / {STATION_DISPLAY} "
            f"({len(rows)} pozycji)"
        )
        return rows, "odsluchane.eu"

    print(
        f"LIVE source: odsluchane.eu zwróciło 0 utworów "
        f"dla {STATION_DISPLAY}."
    )
    return [], "none"


def score_candidate(target_artist, target_title, item):
    spotify_artists = item.get("artists", [])
    first_artist = spotify_artists[0]["name"] if spotify_artists else ""
    all_artists = ", ".join(a["name"] for a in spotify_artists)
    spotify_title = item.get("name", "")

    a1 = normalize(target_artist)
    t1 = title_key(target_title)
    a2_first = normalize(first_artist)
    a2_all = normalize(all_artists)
    t2 = title_key(spotify_title)

    artist_score = max(
        SequenceMatcher(None, a1, a2_first).ratio(),
        SequenceMatcher(None, a1, a2_all).ratio(),
    )
    title_score = SequenceMatcher(None, t1, t2).ratio()
    score = 0.32 * artist_score + 0.68 * title_score

    if a1 and (a1 in a2_first or a2_first in a1 or a1 in a2_all):
        score += 0.06
    if t1 and (t1 in t2 or t2 in t1):
        score += 0.08
    return min(1.0, score)


class SearchBudget:
    def __init__(self, maximum):
        self.maximum = max(0, maximum)
        self.used = 0

    def take(self):
        if self.used >= self.maximum:
            raise SearchBudgetStop(f"Osiągnięto limit {self.maximum} zapytań Spotify Search.")
        self.used += 1


def search_track(artist, title, budget):
    simple_title = clean_title_for_search(title)
    queries = [f'track:"{simple_title}" artist:"{artist}"', f"{artist} {simple_title}"]
    best = None
    best_score = 0.0

    for index, query in enumerate(dict.fromkeys(queries)):
        budget.take()
        data = spotify("GET", f"/search?q={quote(query)}&type=track&limit=10").json()
        for item in data.get("tracks", {}).get("items", []):
            score = score_candidate(artist, title, item)
            if score > best_score:
                best = item
                best_score = score
        if best is not None and best_score >= 0.82:
            break
        if index >= 1:
            break

    if best is not None and best_score >= 0.76:
        return best, best_score
    return None, best_score


def find_playlist_id():
    if PLAYLIST_ID_ENV:
        return PLAYLIST_ID_ENV

    offset = 0
    while True:
        data = spotify("GET", f"/me/playlists?limit=50&offset={offset}").json()
        items = data.get("items", [])
        for playlist in items:
            if playlist and playlist.get("name") == PLAYLIST_NAME:
                return playlist["id"]
        if not data.get("next"):
            break
        offset += len(items)

    data = spotify(
        "POST",
        "/me/playlists",
        json={"name": PLAYLIST_NAME, "public": False, "description": f"Automatyczna playlista utworów granych w {STATION_DISPLAY}."},
    ).json()
    return data["id"]


def get_playlist_meta(pid):
    return spotify("GET", f"/playlists/{pid}").json()


def fetch_playlist_items(pid):
    output = []
    offset = 0
    while True:
        runtime_guard()
        data = spotify("GET", f"/playlists/{pid}/items?limit=100&offset={offset}").json()
        batch = data.get("items", [])
        for row in batch:
            item = row.get("item") or row.get("track") or {}
            uri = item.get("uri")
            if not uri:
                continue
            artists = [a.get("name", "") for a in item.get("artists", []) if a.get("name")]
            output.append({"uri": uri, "artists": artists, "title": item.get("name", "")})
        if not data.get("next"):
            break
        offset += len(batch)
    return output


def add_bootstrap_keys(state, uri, artists, title):
    if not title_key(title) or not artists:
        return
    for artist in artists:
        state["keys"][source_key(artist, title)] = uri
    state["keys"][source_key(", ".join(artists), title)] = uri


def migrate_markers_from_description(state, description):
    m = BACKFILL_RE.search(description or "")
    if m and not state.get("backfill_last_completed"):
        state["backfill_last_completed"] = m.group(1)
    m = LIVE_RE.search(description or "")
    if m and not state.get("live_day"):
        state["live_day"] = m.group(1)
        state["live_time"] = m.group(2)


def bootstrap_state_if_needed(state):
    if state.get("playlist_id") and isinstance(state.get("uris"), list) and state.get("uris"):
        return state["playlist_id"]

    print(f"Pierwszy run {STATION_DISPLAY}: buduję stan z istniejącej playlisty Spotify.")
    pid = find_playlist_id()
    meta = get_playlist_meta(pid)
    items = fetch_playlist_items(pid)

    state["playlist_id"] = pid
    state["playlist_name"] = PLAYLIST_NAME
    state["uris"] = []
    state["keys"] = state.get("keys") or {}

    seen = set()
    for item in items:
        uri = item["uri"]
        if uri not in seen:
            seen.add(uri)
            state["uris"].append(uri)
        add_bootstrap_keys(state, uri, item.get("artists") or [], item.get("title") or "")

    migrate_markers_from_description(state, meta.get("description") or "")
    save_state(state)
    print(f"Bootstrap gotowy: {len(state['uris'])} pozycji, {len(state['keys'])} kluczy.")
    return pid


def miss_is_active(state, key):
    value = state.get("misses", {}).get(key)
    if not value:
        return False
    try:
        when = date.fromisoformat(value)
    except Exception:
        return False
    return (now_pl().date() - when).days < MISS_RETRY_DAYS


def mark_miss(state, key):
    state.setdefault("misses", {})[key] = now_pl().date().isoformat()
    save_state(state)


def all_pending_uris(state):
    return set((state.get("pending_live") or []) + (state.get("pending_backfill") or []))


def queue_uri(state, queue_name, uri):
    if uri in set(state.get("uris") or []) or uri in all_pending_uris(state):
        return False
    state.setdefault(queue_name, []).append(uri)
    save_state(state)
    return True


def add_items(pid, uris, position=None):
    inserted = 0
    for i in range(0, len(uris), 100):
        runtime_guard()
        batch = uris[i:i + 100]
        body = {"uris": batch}
        if position is not None:
            body["position"] = position + inserted
        spotify("POST", f"/playlists/{pid}/items", json=body)
        inserted += len(batch)


def remove_items(pid, uris):
    for i in range(0, len(uris), 100):
        runtime_guard()
        batch = uris[i:i + 100]
        spotify("DELETE", f"/playlists/{pid}/items", json={"items": [{"uri": uri} for uri in batch]})


def flush_pending(pid, state):
    live = list(dict.fromkeys(state.get("pending_live") or []))
    if live:
        print(f"Pending LIVE: dodaję {len(live)} pozycji.")
        add_items(pid, live, position=0)
        live_set = set(live)
        state["uris"] = live + [uri for uri in state["uris"] if uri not in live_set]
        state["pending_live"] = []
        save_state(state)

    backfill = list(dict.fromkeys(state.get("pending_backfill") or []))
    if backfill:
        print(f"Pending BACKFILL: dodaję {len(backfill)} pozycji.")
        add_items(pid, backfill)
        existing = set(state["uris"])
        for uri in backfill:
            if uri not in existing:
                state["uris"].append(uri)
                existing.add(uri)
        state["pending_backfill"] = []
        save_state(state)


def enforce_max_tracks(pid, state):
    if len(state["uris"]) <= MAX_TRACKS:
        return
    to_remove = state["uris"][MAX_TRACKS:]
    print(f"Limit {MAX_TRACKS}: usuwam {len(to_remove)} najstarszych pozycji.")
    remove_items(pid, to_remove)
    state["uris"] = state["uris"][:MAX_TRACKS]
    save_state(state)


def process_rows(rows, state, queue_name, budget):
    stats = {"rows": 0, "known": 0, "miss_cache": 0, "matched": 0, "miss": 0, "queued": 0}
    start_budget = budget.used

    for hhmm, artist, title in rows:
        runtime_guard()
        stats["rows"] += 1
        key = source_key(artist, title)

        if key in state["keys"]:
            stats["known"] += 1
            if queue_uri(state, queue_name, state["keys"][key]):
                stats["queued"] += 1
            continue

        if miss_is_active(state, key):
            stats["miss_cache"] += 1
            continue

        try:
            track, score = search_track(artist, title, budget)
        except (SearchBudgetStop, RateLimited, RuntimeStop) as exc:
            stats["search_requests"] = budget.used - start_budget
            return False, exc, stats

        if not track:
            stats["miss"] += 1
            print(f"MISS {hhmm} | {artist} - {title} ({score:.2f})")
            mark_miss(state, key)
            continue

        uri = track["uri"]
        state["keys"][key] = uri
        save_state(state)
        stats["matched"] += 1

        if queue_uri(state, queue_name, uri):
            stats["queued"] += 1

        matched_artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
        print(f"MATCH {hhmm} | {artist} - {title} -> {matched_artists} - {track.get('name', '')} ({score:.2f})")

    stats["search_requests"] = budget.used - start_budget
    return True, None, stats


def print_stats(label, stats, budget):
    print(
        f"{label}: rows={stats.get('rows',0)}, search={stats.get('search_requests',0)}, "
        f"known={stats.get('known',0)}, miss_cache={stats.get('miss_cache',0)}, "
        f"matched={stats.get('matched',0)}, miss={stats.get('miss',0)}, "
        f"queued={stats.get('queued',0)}, search_total={budget.used}/{budget.maximum}"
    )


def minutes_from_hhmm(hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def newest_time(rows):
    if not rows:
        return None

    now_dt = now_pl()
    latest_allowed = min(
        23 * 60 + 59,
        now_dt.hour * 60 + now_dt.minute + LIVE_FUTURE_TOLERANCE_MINUTES,
    )

    candidates = [
        row[0]
        for row in rows
        if minutes_from_hhmm(row[0]) <= latest_allowed
    ]

    return max(candidates, key=minutes_from_hhmm) if candidates else None


def sanitize_live_marker(state):
    """Resetuje LIVE marker, jeśli wskazuje godzinę z przyszłości."""
    today = now_pl().date().isoformat()

    if state.get("live_day") != today:
        return

    marker = state.get("live_time")
    if not marker:
        return

    try:
        marker_minutes = minutes_from_hhmm(marker)
    except Exception:
        print(f"LIVE: nieprawidłowy marker {marker!r}; resetuję.")
        state["live_time"] = None
        save_state(state)
        return

    now_dt = now_pl()
    now_minutes = now_dt.hour * 60 + now_dt.minute

    if marker_minutes > now_minutes + LIVE_FUTURE_TOLERANCE_MINUTES:
        print(
            f"LIVE: marker {marker} jest z przyszłości "
            f"(teraz {now_dt.strftime('%H:%M')}); resetuję marker."
        )
        state["live_time"] = None
        save_state(state)


def filter_live_rows(rows, state):
    sanitize_live_marker(state)

    today = now_pl().date().isoformat()
    if state.get("live_day") != today or not state.get("live_time"):
        return rows

    threshold = max(
        0,
        minutes_from_hhmm(state["live_time"]) - LIVE_OVERLAP_MINUTES,
    )

    return [
        row
        for row in rows
        if minutes_from_hhmm(row[0]) >= threshold
    ]


def run_live(pid, state, budget):
    rows_all, live_source = get_live_rows(state)
    rows = filter_live_rows(rows_all, state)

    print(
        f"LIVE: source={live_source}, strona={len(rows_all)}, "
        f"do_sprawdzenia={len(rows)}, "
        f"marker={state.get('live_day')} {state.get('live_time')}"
    )

    if not rows_all:
        print(
            "LIVE: brak danych źródłowych. Marker pozostaje bez zmian; "
            "następny run spróbuje ponownie."
        )
        return True, None

    completed, stop_exc, stats = process_rows(
        rows,
        state,
        "pending_live",
        budget,
    )
    print_stats("LIVE", stats, budget)

    if not isinstance(stop_exc, RateLimited):
        flush_pending(pid, state)
        enforce_max_tracks(pid, state)

    if completed:
        latest = newest_time(rows_all)
        if latest:
            state["live_day"] = now_pl().date().isoformat()
            state["live_time"] = latest
            save_state(state)
            print(
                f"LIVE marker zapisany: "
                f"{state['live_day']} {state['live_time']} "
                f"(source={live_source})"
            )

    return completed, stop_exc


def next_backfill_day(state):
    value = state.get("backfill_last_completed")
    if value:
        return date.fromisoformat(value) - timedelta(days=1)
    return now_pl().date() - timedelta(days=1)


def run_backfill(pid, state, budget):
    if len(state["uris"]) >= MAX_TRACKS:
        print(f"BACKFILL: playlista ma już {len(state['uris'])} pozycji — nic do zrobienia.")
        return True, None

    day = next_backfill_day(state)

    for _ in range(BACKFILL_DAYS_PER_RUN):
        runtime_guard()
        if len(state["uris"]) >= MAX_TRACKS:
            break

        rows, complete = get_odsluchane_day_rows(day)
        source_name = "odsluchane.eu"

        print(
            f"BACKFILL {day}: station={STATION_DISPLAY}, "
            f"source={source_name}, odczytano {len(rows)} pozycji. "
            f"Marker={state.get('backfill_last_completed')}"
        )

        if not rows or not complete:
            print(
                f"BACKFILL {day}: źródło puste lub niekompletne. "
                "Nie przesuwam markera."
            )
            return False, None

        completed, stop_exc, stats = process_rows(rows, state, "pending_backfill", budget)
        print_stats(f"BACKFILL {day}", stats, budget)

        if not isinstance(stop_exc, RateLimited):
            flush_pending(pid, state)
            enforce_max_tracks(pid, state)

        if not completed:
            print(f"BACKFILL {day}: dzień nieukończony — następny run wróci do tego samego dnia.")
            return False, stop_exc

        state["backfill_last_completed"] = day.isoformat()
        save_state(state)
        print(f"BACKFILL marker zapisany: {day}")
        day -= timedelta(days=1)

    return True, None


def main():
    print(f"=== Radio -> Spotify Multi v1.0 | {STATION_DISPLAY} ===")
    print(f"Tryb: {SYNC_MODE.upper()}")
    print(f"Czas PL: {now_pl().isoformat(timespec='seconds')}")
    print(f"Search budget: {MAX_SEARCH_REQUESTS}, runtime: {MAX_RUNTIME_SECONDS}s")

    state = load_state()

    if blocked_by_previous_429(state):
        print("GOTOWE — run pominięty przez aktywny blocked_until.")
        return

    budget = SearchBudget(MAX_SEARCH_REQUESTS)

    try:
        pid = bootstrap_state_if_needed(state)
        print(
            f"Stan {STATION_DISPLAY}: playlist={len(state['uris'])}, keys={len(state['keys'])}, "
            f"misses={len(state['misses'])}, pending_live={len(state['pending_live'])}, "
            f"pending_backfill={len(state['pending_backfill'])}"
        )

        flush_pending(pid, state)
        enforce_max_tracks(pid, state)

        if SYNC_MODE == "live":
            _, stop_exc = run_live(pid, state, budget)
        else:
            _, stop_exc = run_backfill(pid, state, budget)

        if isinstance(stop_exc, RateLimited):
            remember_rate_limit(state, stop_exc)
            print("STOP KONTROLOWANY: Spotify 429. Stan i pending zostały zachowane.")
        elif isinstance(stop_exc, SearchBudgetStop):
            print(f"STOP KONTROLOWANY: {stop_exc} Następny run będzie kontynuował.")
        elif isinstance(stop_exc, RuntimeStop):
            print(f"STOP KONTROLOWANY: {stop_exc} Następny run będzie kontynuował.")

    except RateLimited as exc:
        remember_rate_limit(state, exc)
        print("STOP KONTROLOWANY: Spotify 429 podczas operacji playlisty.")
    except RuntimeStop as exc:
        print(f"STOP KONTROLOWANY: {exc} Stan został zachowany.")

    elapsed = int(time.monotonic() - STARTED)
    print(
        f"GOTOWE. Tryb={SYNC_MODE}, playlist={len(state.get('uris') or [])}, "
        f"search={budget.used}/{budget.maximum}, czas={elapsed}s."
    )


if __name__ == "__main__":
    main()
