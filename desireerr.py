#!/usr/bin/env python3
"""Desireerr: a Seerr-style request app for StashDB scenes, on top of Whisparr v3.

Browse and search StashDB (scenes, performers, studios, tags), request scenes and follow
performers or studios. Approved requests go to Whisparr, which searches Prowlarr and
downloads; the app shows each scene's state (pending, processing, downloading, available)
from Whisparr's library and queue. Saved searches request new matching scenes on their own,
ntfy pushes tell about requests, and downloaded scenes open in Jellyfin.

One file, Python 3 standard library only. Settings, users, sessions and requests live in
one SQLite file; cover images are fetched through the server and cached on disk, so the
browser only talks to Desireerr and nothing loads before you sign in.
"""

import argparse
import base64
import configparser
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.0"
USER_AGENT = f"Desireerr/{VERSION}"
STASHDB_URL = "https://stashdb.org/graphql"
STASHDB_WEB = "https://stashdb.org"
DEFAULT_WHISPARR = "http://localhost:6969"
# torrent-box keeps Whisparr's config here; its API key is read from it when none is set.
WHISPARR_CONFIG = Path.home() / ".local/share/torrent-box/arr/whisparr/config.xml"
# torrent-box's settings: its ntfy server (config.ini) and topic (secrets.ini) are used when
# none are set, so the phone that gets torrent-box's pushes gets Desireerr's too.
TORRENT_BOX_CONF = Path.home() / ".config/rss2transmission"
DEFAULT_JELLYFIN = "http://localhost:8096"
WEB_DIR = Path(__file__).resolve().parent / "web"

SESSION_COOKIE = "desireerr_session"
SESSION_DAYS = 30
INDEX_MAX_AGE = 120          # seconds between Whisparr library refreshes
QUEUE_MAX_AGE = 15
IMAGE_HOSTS = re.compile(r"^(?:[a-z0-9-]+\.)*stashdb\.org$")
IMAGE_CACHE_BYTES = 2 * 1024**3
PER_PAGE_MAX = 100
SLOW_REQUEST = 0.5           # seconds; slower API requests are logged with where the time went
WATCH_EVERY = 3600           # seconds between a saved search's checks
WATCH_PAGES = 5              # at most this many pages of 100 scenes per check
WATCH_MAX_REQUESTS = 25      # requests one check makes at most; the rest wait for the next
WATCH_RETRY_DAYS = 14        # how long a scene Whisparr doesn't know yet is tried again
AVAILABLE_WINDOW = 2 * 86400  # "now available" only for files this new (not a backlog)
JELLYFIN_MAX_AGE = 600       # seconds between Jellyfin library refreshes

DEFAULT_SETTINGS = {
    "stashdb_key": "",
    "whisparr_url": DEFAULT_WHISPARR,
    "whisparr_key": "",          # empty: read from torrent-box's Whisparr config.xml
    "quality_profile_id": "",    # empty: the one most studios use
    "root_folder": "",           # empty: Whisparr's first root folder
    "search_on_request": "1",
    "stall_days": "7",           # approved, no release this many days: stalled
    "tab_title": "Desireerr",
    "notify_server": "",         # empty: torrent-box's ntfy server
    "notify_topic": "",          # empty: torrent-box's ntfy topic
    "notify_events": "pending,watch,available,failed,stalled",
    "notify_discreet": "1",      # no titles, names or pictures in pushes
    "app_url": "",               # for links in pushes; empty: where the app was last opened
    "jellyfin_url": DEFAULT_JELLYFIN,
    "jellyfin_key": "",
    "jellyfin_link": "",         # Jellyfin's address for the browser; empty: our host, :8096
}


def now():
    return int(time.time())


def today():
    return date.today().isoformat()


class ApiError(Exception):
    """An error shown to the user: status code plus a plain message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class NotInWhisparr(ApiError):
    """Whisparr's metadata doesn't have the scene yet (new StashDB scenes take a while)."""


# ---------------------------------------------------------------------------------------
# Timing: where a request's time goes
# ---------------------------------------------------------------------------------------

class Timing:
    """Time a request spent waiting for StashDB, Whisparr and images, with how many calls.

    One per request, found through a thread-local; threads working for the request (like
    Discover's rows) are given the same one."""

    local = threading.local()

    def __init__(self):
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.spent = {}  # name -> [seconds, calls]

    @classmethod
    def begin(cls, timing=None):
        cls.local.timing = timing or Timing()
        return cls.local.timing

    @classmethod
    def current(cls):
        return getattr(cls.local, "timing", None)

    def add(self, name, seconds):
        with self.lock:
            spent = self.spent.setdefault(name, [0.0, 0])
            spent[0] += seconds
            spent[1] += 1

    def total(self):
        return time.monotonic() - self.start

    def summary(self):
        with self.lock:
            return ", ".join(f"{name} {sec:.2f}s in {calls}" for name, (sec, calls) in self.spent.items())

    def header(self):
        """For the browser's developer tools (Network, Timing)."""
        with self.lock:
            parts = [f'{name};dur={sec * 1000:.0f};desc="{calls} calls"'
                     for name, (sec, calls) in self.spent.items()]
        return ", ".join(parts + [f"total;dur={self.total() * 1000:.0f}"])


@contextlib.contextmanager
def timed(name):
    t0 = time.monotonic()
    try:
        yield
    finally:
        timing = Timing.current()
        if timing:
            timing.add(name, time.monotonic() - t0)


# ---------------------------------------------------------------------------------------
# Passwords and storage
# ---------------------------------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def check_password(password, salt_b64, hash_b64):
    _, digest = hash_password(password, base64.b64decode(salt_b64))
    return hmac.compare_digest(digest, hash_b64)


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    auto_approve INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created INTEGER NOT NULL,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    scene_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    studio TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL,            -- pending, approved, declined, failed, abandoned
    whisparr_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    decided_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at INTEGER
);
CREATE INDEX IF NOT EXISTS requests_scene ON requests(scene_id);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    query TEXT NOT NULL,             -- the scene filters, as the Scenes page's URL query
    summary TEXT NOT NULL DEFAULT '',
    since TEXT NOT NULL,             -- only scenes released on or after this date
    ask_first INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created INTEGER NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    cursor TEXT NOT NULL DEFAULT '', -- the newest StashDB "updated" time seen
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS favorite_tags (   -- StashDB has no tag favourites: ours, per user
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (user_id, tag_id)
);
CREATE TABLE IF NOT EXISTS watch_scenes (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    scene_id TEXT NOT NULL,
    card TEXT NOT NULL,              -- the scene as StashDB listed it (JSON)
    seen INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, -- 0: to request, 1: done, 2: given up
    PRIMARY KEY (watch_id, scene_id)
);
"""

# Columns added after the first release: (table, column, definition).
COLUMNS = [
    ("requests", "via", "TEXT NOT NULL DEFAULT ''"),        # the saved search that made it
    ("requests", "watch_id", "INTEGER"),
    ("requests", "notified", "TEXT NOT NULL DEFAULT ''"),   # pushes sent once: available, stalled
    ("requests", "searched_at", "INTEGER"),                 # last "search again"
]


class DB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        for table, column, definition in COLUMNS:
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    # settings
    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        values.update({r["key"]: r["value"] for r in self.all("SELECT key, value FROM settings")})
        return values

    def setting(self, key):
        return self.settings().get(key, "")

    def set_settings(self, values):
        with self.lock:
            for key, value in values.items():
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ---------------------------------------------------------------------------------------
# StashDB (stash-box GraphQL)
# ---------------------------------------------------------------------------------------

SCENE_CARD = """
fragment SceneCard on Scene {
  id title release_date duration updated
  studio { id name }
  images { url width height }
  performers { as performer { id name gender } }
}"""

SCENE_FULL = SCENE_CARD + """
fragment SceneFull on Scene {
  ...SceneCard
  details code director production_date
  urls { url site { name } }
  studio { id name parent { id name } }
  tags { id name description category { id name group } }
  performers { as performer { id name disambiguation gender images { url width height } } }
}"""

PERFORMER_CARD = """
fragment PerformerCard on Performer {
  id name disambiguation gender scene_count is_favorite
  images { url width height }
}"""

PERFORMER_FULL = PERFORMER_CARD + """
fragment PerformerFull on Performer {
  ...PerformerCard
  aliases birth_date age country ethnicity hair_color eye_color height
  cup_size band_size waist_size hip_size breast_type career_start_year career_end_year
  urls { url site { name } }
  tattoos { location description }
  piercings { location description }
}"""

STUDIO_CARD = """
fragment StudioCard on Studio {
  id name is_favorite
  images { url width height }
  parent { id name }
}"""

TAG_CARD = """
fragment TagCard on Tag {
  id name description aliases
  category { id name group }
}"""

SCENE_SORTS = {"TRENDING", "DATE", "POPULARITY", "TITLE", "DURATION", "CREATED_AT", "UPDATED_AT"}
PERFORMER_SORTS = {"POPULARITY", "SCENE_COUNT", "LAST_SCENE", "DEBUT", "NAME", "CREATED_AT",
                   "CAREER_START_YEAR", "BIRTHDATE"}
STUDIO_SORTS = {"NAME", "CREATED_AT", "UPDATED_AT"}
TAG_MODES = {"all": "INCLUDES_ALL", "any": "INCLUDES"}
GENDERS = {"MALE", "FEMALE", "TRANSGENDER_MALE", "TRANSGENDER_FEMALE", "INTERSEX", "NON_BINARY",
           "UNKNOWN"}


class StashDB:
    def __init__(self, key_fn):
        self.key_fn = key_fn
        self.cache = {}
        self.lock = threading.Lock()

    def query(self, query, variables=None, ttl=300, key=None):
        key = key or self.key_fn()
        if not key:
            raise ApiError(503, "No StashDB API key yet. An admin can add it in Settings.")
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        cache_key = hashlib.sha256(body).hexdigest()
        if ttl:
            with self.lock:
                hit = self.cache.get(cache_key)
                if hit and hit[0] > time.time():
                    return hit[1]
        req = urllib.request.Request(STASHDB_URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "ApiKey": key, "User-Agent": USER_AGENT})
        try:
            with timed("stashdb"), urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError(502, "StashDB refused the API key. Check it in Settings.")
            raise ApiError(502, f"StashDB answered {e.code}. Try again in a minute.")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(502, f"Can't reach StashDB ({getattr(e, 'reason', e)}).")
        if payload.get("errors"):
            message = "; ".join(err.get("message", "?") for err in payload["errors"])
            if "not authorized" in message.lower() or "unauthorized" in message.lower():
                raise ApiError(502, "StashDB refused the API key. Check it in Settings.")
            raise ApiError(502, f"StashDB: {message}")
        data = payload.get("data") or {}
        if ttl:
            with self.lock:
                if len(self.cache) > 2000:
                    self.cache.clear()
                self.cache[cache_key] = (time.time() + ttl, data)
        return data

    # scenes
    def query_scenes(self, scene_input, ttl=300):
        q = SCENE_CARD + """
query ($input: SceneQueryInput!) {
  queryScenes(input: $input) { count scenes { ...SceneCard } }
}"""
        return self.query(q, {"input": scene_input}, ttl)["queryScenes"]

    def find_scene(self, scene_id):
        q = SCENE_FULL + "\nquery ($id: ID!) { findScene(id: $id) { ...SceneFull } }"
        return self.query(q, {"id": scene_id})["findScene"]

    def find_many(self, field, ids, selection, fragments="", ttl=300):
        """Look up several objects by ID in one query, one aliased find per ID.

        Aliases rather than findScenes(ids:) and friends, which older stash-box versions
        lack. IDs are checked against id_list's pattern before they get here.
        """
        ids = [i for i in ids if re.fullmatch(r"[0-9a-fA-F-]{8,40}", i)]
        found = []
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            body = "\n".join(f'a{n}: {field}(id: "{i}") {selection}' for n, i in enumerate(chunk))
            data = self.query(fragments + "\nquery {\n" + body + "\n}", ttl=ttl)
            found += [data.get(f"a{n}") for n in range(len(chunk))]
        return [x for x in found if x]

    def find_scenes(self, ids):
        return self.find_many("findScene", ids, "{ ...SceneCard }", SCENE_CARD)

    def names(self, kind, ids):
        field = {"tags": "findTag", "performers": "findPerformer", "studios": "findStudio"}[kind]
        return {x["id"]: x["name"] for x in self.find_many(field, ids[:50], "{ id name }", ttl=3600)}

    def search(self, term):
        q = SCENE_CARD + PERFORMER_CARD + STUDIO_CARD + TAG_CARD + """
query ($term: String!) {
  searchScene(term: $term, limit: 40) { ...SceneCard }
  searchPerformer(term: $term, limit: 24) { ...PerformerCard }
  searchStudio(term: $term, limit: 24) { ...StudioCard }
  searchTag(term: $term, limit: 24) { ...TagCard }
}"""
        data = self.query(q, {"term": term}, ttl=120)
        return {"scenes": data["searchScene"], "performers": data["searchPerformer"],
                "studios": data["searchStudio"], "tags": data["searchTag"]}

    # performers
    def find_performer(self, pid):
        q = PERFORMER_FULL + "\nquery ($id: ID!) { findPerformer(id: $id) { ...PerformerFull } }"
        return self.query(q, {"id": pid})["findPerformer"]

    def query_performers(self, perf_input):
        q = PERFORMER_CARD + """
query ($input: PerformerQueryInput!) {
  queryPerformers(input: $input) { count performers { ...PerformerCard } }
}"""
        return self.query(q, {"input": perf_input})["queryPerformers"]

    # studios
    def find_studio(self, sid):
        q = STUDIO_CARD + """
query ($id: ID!) {
  findStudio(id: $id) {
    ...StudioCard aliases
    urls { url site { name } }
    child_studios { ...StudioCard }
  }
}"""
        return self.query(q, {"id": sid})["findStudio"]

    def query_studios(self, studio_input):
        q = STUDIO_CARD + """
query ($input: StudioQueryInput!) {
  queryStudios(input: $input) { count studios { ...StudioCard } }
}"""
        return self.query(q, {"input": studio_input})["queryStudios"]

    # tags
    def find_tag(self, tid):
        q = TAG_CARD + "\nquery ($id: ID!) { findTag(id: $id) { ...TagCard } }"
        return self.query(q, {"id": tid}, ttl=3600)["findTag"]

    def find_tags(self, ids):
        return self.find_many("findTag", ids, "{ ...TagCard }", TAG_CARD, ttl=3600)

    def search_tags(self, term):
        q = TAG_CARD + "\nquery ($term: String!) { searchTag(term: $term, limit: 15) { ...TagCard } }"
        return self.query(q, {"term": term}, ttl=600)["searchTag"]

    def query_tags(self, tag_input):
        q = TAG_CARD + """
query ($input: TagQueryInput!) {
  queryTags(input: $input) { count tags { ...TagCard } }
}"""
        return self.query(q, {"input": tag_input}, ttl=3600)["queryTags"]

    def tag_categories(self):
        q = "{ queryTagCategories { count tag_categories { id name group description } } }"
        return self.query(q, ttl=6 * 3600)["queryTagCategories"]["tag_categories"]

    def me(self, key=None):
        return self.query("{ me { name } }", ttl=0, key=key)["me"]

    def set_favorite(self, kind, sid, favorite):
        """Favourite (or not) a performer or studio on the key owner's StashDB account."""
        field = {"performer": "favoritePerformer", "studio": "favoriteStudio"}[kind]
        q = f"mutation ($id: ID!, $fav: Boolean!) {{ {field}(id: $id, favorite: $fav) }}"
        self.query(q, {"id": sid, "fav": bool(favorite)}, ttl=0)
        with self.lock:
            self.cache.clear()   # pages and lists show is_favorite


# ---------------------------------------------------------------------------------------
# Whisparr v3
# ---------------------------------------------------------------------------------------

def whisparr_config_key(path=WHISPARR_CONFIG):
    try:
        return ET.parse(path).getroot().findtext("ApiKey") or ""
    except (OSError, ET.ParseError):
        return ""


class Whisparr:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.index = {}            # stash id -> {"id", "monitored", "has_file", "file_added"}
        self.index_time = 0
        self.queue = {}            # whisparr id -> {"progress", "status", "timeleft"}
        self.queue_time = 0
        self.followed = {"performers": [], "studios": []}
        self.error = ""

    def creds(self):
        s = self.db.settings()
        url = (s.get("whisparr_url") or DEFAULT_WHISPARR).rstrip("/")
        key = s.get("whisparr_key") or whisparr_config_key()
        return url, key

    def call(self, method, path, body=None, params=None, timeout=30):
        url, key = self.creds()
        if not key:
            raise ApiError(503, "No Whisparr API key. Add it in Settings.")
        full = f"{url}/api/v3/{path}"
        if params:
            full += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(full, data=data, method=method, headers={
            "X-Api-Key": key, "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with timed("whisparr"), urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read() or b"null")
                if isinstance(err, list):
                    detail = "; ".join(x.get("errorMessage", "") for x in err if isinstance(x, dict))
                elif isinstance(err, dict):
                    detail = err.get("message") or err.get("title") or ""
            except ValueError:
                pass
            if e.code == 401:
                raise ApiError(502, "Whisparr refused the API key. Check it in Settings.")
            raise ApiError(502, f"Whisparr answered {e.code}" + (f": {detail}" if detail else "."))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(502, f"Can't reach Whisparr at {url} ({getattr(e, 'reason', e)}).")
        return json.loads(raw) if raw.strip() else None

    # library index
    def refresh_index(self):
        movies = self.call("GET", "movie", params={"excludeLocalCovers": "true"}, timeout=120)
        index = {}
        for m in movies:
            sid = m.get("stashId") or m.get("foreignId")
            if not sid:
                continue
            index[sid] = {
                "id": m["id"],
                "monitored": bool(m.get("monitored")),
                "has_file": bool(m.get("hasFile")),
                "file_added": (m.get("movieFile") or {}).get("dateAdded", ""),
                "path": (m.get("movieFile") or {}).get("path", ""),
                "item_type": m.get("itemType", ""),
            }
        with self.lock:
            self.index, self.index_time, self.error = index, time.time(), ""

    def refresh_queue(self):
        page = self.call("GET", "queue", params={"pageSize": 500, "includeUnknownMovieItems": "false"})
        queue = {}
        for r in page.get("records", []):
            mid = r.get("movieId")
            if not mid:
                continue
            size = r.get("size") or 0
            left = r.get("sizeleft") or 0
            queue[mid] = {
                "progress": round(100 * (size - left) / size) if size else 0,
                "status": r.get("trackedDownloadState") or r.get("status") or "",
                "timeleft": r.get("timeleft") or "",
            }
        with self.lock:
            self.queue, self.queue_time = queue, time.time()

    def refresh_followed(self):
        """The performers and studios Whisparr monitors, for Discover's rows."""
        def image(item):
            urls = [i.get("remoteUrl") for i in item.get("images") or [] if i.get("remoteUrl")]
            return urls[0] if urls else ""

        followed = {}
        for kind, name_key in (("performer", "name"), ("studio", "title")):
            items = self.call("GET", kind, timeout=60) or []
            followed[kind + "s"] = sorted(
                ({"id": i["foreignId"], "name": i.get(name_key) or "", "image": image(i),
                  "since": i.get("afterDate")} for i in items if i.get("monitored") and i.get("foreignId")),
                key=lambda x: x["name"].lower())
        with self.lock:
            self.followed = followed

    def ensure_fresh(self):
        try:
            if time.time() - self.index_time > INDEX_MAX_AGE * 3:
                self.refresh_index()
            if time.time() - self.queue_time > QUEUE_MAX_AGE:
                self.refresh_queue()
        except ApiError as e:
            with self.lock:
                self.error = e.message

    def lookup(self, stash_id):
        with self.lock:
            return self.index.get(stash_id)

    def recently_added(self, limit=20):
        with self.lock:
            items = [(v["file_added"], k) for k, v in self.index.items()
                     if v["has_file"] and v["item_type"] == "scene"]
        items.sort(reverse=True)
        return [k for _, k in items[:limit]]

    def options(self):
        profiles = self.call("GET", "qualityprofile")
        roots = self.call("GET", "rootfolder")
        return ([{"id": p["id"], "name": p["name"]} for p in profiles],
                [{"path": r["path"], "free": r.get("freeSpace")} for r in roots])

    def defaults(self):
        s = self.db.settings()
        profile = s.get("quality_profile_id")
        root = s.get("root_folder")
        if not profile or not root:
            profiles, roots = self.options()
            if not root and roots:
                root = roots[0]["path"]
            if not profile:
                studios = self.call("GET", "studio")
                counts = {}
                for st in studios:
                    counts[st.get("qualityProfileId")] = counts.get(st.get("qualityProfileId"), 0) + 1
                profile = max(counts, key=counts.get) if counts else (profiles[0]["id"] if profiles else 1)
        return int(profile), root

    def send_scene(self, stash_id, search=True):
        """Monitor the scene in Whisparr (adding it first if needed) and start a search."""
        existing = self.call("GET", "movie", params={"stashId": stash_id})
        existing = [m for m in existing or [] if (m.get("stashId") or m.get("foreignId")) == stash_id]
        if existing:
            movie = self.call("GET", f"movie/{existing[0]['id']}")
            if not movie.get("monitored"):
                movie["monitored"] = True
                self.call("PUT", f"movie/{movie['id']}", movie)
            movie_id = movie["id"]
            if search and not movie.get("hasFile"):
                self.call("POST", "command", {"name": "MoviesSearch", "movieIds": [movie_id]})
        else:
            found = self.call("GET", "lookup/scene", params={"term": f"stash:{stash_id}"}) or []
            match = next((f for f in found if f.get("foreignId") == stash_id), None)
            if not match:
                raise NotInWhisparr(502, "Whisparr couldn't find this scene in its metadata yet. "
                                         "New StashDB scenes can take a day to show up there.")
            profile, root = self.defaults()
            movie = dict(match["movie"])
            movie.pop("id", None)
            movie.update({
                "qualityProfileId": profile, "rootFolderPath": root, "monitored": True,
                "addOptions": {"searchForMovie": bool(search), "monitor": "movieOnly",
                               "addMethod": "manual"},
            })
            created = self.call("POST", "movie", movie)
            movie_id = created["id"]
        with self.lock:
            entry = self.index.setdefault(stash_id, {"has_file": False, "file_added": "",
                                                     "item_type": "scene"})
            entry.update({"id": movie_id, "monitored": True})
        return movie_id

    def search(self, movie_ids):
        """Ask Whisparr to search its indexers for these scenes now."""
        self.call("POST", "command", {"name": "MoviesSearch", "movieIds": list(movie_ids)})

    def unmonitor(self, movie_id):
        movie = self.call("GET", f"movie/{movie_id}")
        if movie.get("monitored"):
            movie["monitored"] = False
            self.call("PUT", f"movie/{movie_id}", movie)
        with self.lock:
            for entry in self.index.values():
                if entry.get("id") == movie_id:
                    entry["monitored"] = False

    def follow_state(self, kind, stash_id):
        items = self.call("GET", kind, params={"stashId": stash_id}) or []
        items = [i for i in items if i.get("foreignId") == stash_id]
        if not items:
            return {"followed": False, "since": None}
        item = items[0]
        return {"followed": bool(item.get("monitored")), "since": item.get("afterDate")}

    def set_follow(self, kind, stash_id, follow):
        """Follow (monitor) a performer or studio for new scenes only: after date today.

        With no after date Whisparr would add and want the whole back catalogue, and the
        after date also puts the older scenes on its exclusion list, so it is always set
        when following. Single scenes from the back catalogue can still be requested.
        """
        items = self.call("GET", kind, params={"stashId": stash_id}) or []
        items = [i for i in items if i.get("foreignId") == stash_id]
        if items:
            item = items[0]
            if follow and not item.get("afterDate"):
                item["afterDate"] = today()
            item["monitored"] = bool(follow)
            self.call("PUT", f"{kind}/{item['id']}", item)
        elif follow:
            found = self.call("GET", f"lookup/{kind}", params={"term": f"stash:{stash_id}"}) or []
            match = next((f for f in found if f.get("foreignId") == stash_id), None)
            if not match:
                raise ApiError(502, f"Whisparr couldn't find this {kind} in its metadata.")
            profile, root = self.defaults()
            item = dict(match[kind])
            item.pop("id", None)
            item.update({"monitored": True, "qualityProfileId": profile, "rootFolderPath": root,
                         "searchOnAdd": False, "afterDate": today()})
            self.call("POST", kind, item)
        try:
            self.refresh_followed()
        except ApiError:
            pass
        return self.follow_state(kind, stash_id)

    def web_url(self, whisparr_id=None):
        url, _ = self.creds()
        return f"{url}/movie/{whisparr_id}" if whisparr_id else url


# ---------------------------------------------------------------------------------------
# Notifications through ntfy
# ---------------------------------------------------------------------------------------

# event -> (title as Seerr words it, the discreet message)
NOTIFY_EVENTS = {
    "pending": ("New Scene Request", "A request is waiting for approval."),
    "auto": ("Scene Request Automatically Approved", "A request was approved."),
    "watch": ("Scene Requested by Saved Search", "A saved search made a request."),
    "approved": ("Scene Request Approved", "A request was approved."),
    "declined": ("Scene Request Declined", "A request was declined."),
    "available": ("Scene Request Now Available", "A request is ready."),
    "failed": ("Scene Request Failed", "A request failed."),
    "stalled": ("Scene Request Still Waiting", "A request has no release yet."),
}


def torrent_box_ntfy(conf_dir=None):
    """torrent-box's ntfy server and topic, or empty strings."""
    conf_dir = Path(conf_dir or TORRENT_BOX_CONF)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read([conf_dir / "config.ini", conf_dir / "secrets.ini"], encoding="utf-8")
    except configparser.Error:
        return "", ""
    topic = parser.get("secrets", "ntfy_topic", fallback="").strip()
    server = parser.get("ntfy", "server", fallback="").strip() or "https://ntfy.sh"
    return (server, topic) if topic else ("", "")


class Notifier:
    """Pushes about requests to an ntfy topic, worded like Seerr's (title, scene, who asked,
    a cover and a link), or discreetly: the tab title and a line that names nothing."""

    def __init__(self, db, conf_dir=None):
        self.db = db
        self.conf_dir = conf_dir
        self.error = ""          # the last push's trouble, for Settings
        self.background = True   # tests push in the same thread

    def target(self, s=None):
        s = s or self.db.settings()
        if s["notify_server"] or s["notify_topic"]:
            return (s["notify_server"] or "https://ntfy.sh").rstrip("/"), s["notify_topic"]
        server, topic = torrent_box_ntfy(self.conf_dir)
        return server.rstrip("/"), topic

    @staticmethod
    def app_url(s):
        return (s.get("app_url") or s.get("seen_origin") or "").rstrip("/")

    def payload(self, event, req, s, extra=""):
        title, quiet = NOTIFY_EVENTS[event]
        base = self.app_url(s)
        if s["notify_discreet"] == "1":
            out = {"title": s["tab_title"] or "Desireerr", "message": quiet}
            if base:
                out["click"] = base + "/requests"
            return out
        lines = [req.get("title") or "Untitled"]
        meta = " · ".join(x for x in (req.get("studio"), req.get("release_date")) if x)
        if meta:
            lines.append(meta)
        lines.append("")
        if req.get("requested_by"):
            lines.append(f"Requested by: {req['requested_by']}")
        if req.get("via"):
            lines.append(f"Saved search: {req['via']}")
        if event in ("approved", "declined") and req.get("decided_by_name"):
            lines.append(f"{event.title()} by: {req['decided_by_name']}")
        if event == "failed" and req.get("error"):
            lines.append(f"Reason: {req['error']}")
        if extra:
            lines.append(extra)
        out = {"title": title, "message": "\n".join(lines).strip()}
        image = req.get("image") or ""
        if image:
            out["attach"] = image if "?" in image else image + "?size=600"
        if base:
            out["click"] = f"{base}/scene/{req['scene_id']}"
        return out

    def send(self, event, req, extra=""):
        """Push in the background, if this event is switched on and there's a topic."""
        s = self.db.settings()
        if event not in s["notify_events"].split(","):
            return
        server, topic = self.target(s)
        if not topic:
            return
        body = dict(self.payload(event, req, s, extra), topic=topic)
        if self.background:
            threading.Thread(target=self.post, args=(server, body), daemon=True).start()
        else:
            self.post(server, body)

    def test(self):
        s = self.db.settings()
        server, topic = self.target(s)
        if not topic:
            raise ApiError(400, "No ntfy topic yet. Add one above.")
        if s["notify_discreet"] == "1":
            body = {"title": s["tab_title"] or "Desireerr", "message": "This is a test."}
        else:
            body = {"title": "Test Notification", "message": "Desireerr can reach this phone."}
        if self.app_url(s):
            body["click"] = self.app_url(s) + "/settings"
        if not self.post(server, dict(body, topic=topic)):
            raise ApiError(502, self.error)

    def post(self, server, body):
        req = urllib.request.Request(server + "/", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            self.error = f"ntfy at {server} answered {e.code}."
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.error = f"Can't reach ntfy at {server} ({getattr(e, 'reason', e)})."
        else:
            self.error = ""
            return True
        print(f"notify: {self.error}", file=sys.stderr, flush=True)
        return False


# ---------------------------------------------------------------------------------------
# Jellyfin: where a downloaded scene is, for the Watch button
# ---------------------------------------------------------------------------------------

class Jellyfin:
    """Jellyfin's videos by file path (and by file name, should the paths differ), from its
    library list, refreshed every few minutes and soon after a miss."""

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.paths, self.names = {}, {}
        self.server_id = ""
        self.time = 0            # last refresh
        self.tried = 0           # last attempt, so a down Jellyfin isn't asked every time
        self.error = ""

    def creds(self):
        s = self.db.settings()
        return (s.get("jellyfin_url") or DEFAULT_JELLYFIN).rstrip("/"), s.get("jellyfin_key", "")

    def call(self, path, timeout=20):
        url, key = self.creds()
        req = urllib.request.Request(f"{url}/{path}", headers={
            "Authorization": f'MediaBrowser Token="{key}"', "User-Agent": USER_AGENT})
        try:
            with timed("jellyfin"), urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError(502, "Jellyfin refused the API key. Check it in Settings.")
            raise ApiError(502, f"Jellyfin answered {e.code}.")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ApiError(502, f"Can't reach Jellyfin at {url} ({getattr(e, 'reason', e)}).")

    def refresh(self):
        self.tried = time.time()
        info = self.call("System/Info/Public")
        items = self.call("Items?Recursive=true&IncludeItemTypes=Movie,Video,MusicVideo"
                          "&Fields=Path&EnableImages=false&EnableUserData=false"
                          "&EnableTotalRecordCount=false", timeout=60).get("Items") or []
        paths = {i["Path"]: i["Id"] for i in items if i.get("Path") and i.get("Id")}
        names = {os.path.basename(p): i for p, i in paths.items()}
        with self.lock:
            self.paths, self.names = paths, names
            self.server_id, self.time, self.error = info.get("Id", ""), time.time(), ""

    def ensure(self):
        """Refreshed once, so the Watch button's server ID is known (tried once a minute)."""
        if self.creds()[1] and not self.time and time.time() - self.tried > 60:
            try:
                self.refresh()
            except ApiError as e:
                self.error = e.message

    def find(self, path):
        """Jellyfin's item ID for a file, or None."""
        if not path or not self.creds()[1]:
            return None
        stale = time.time() - self.time > JELLYFIN_MAX_AGE
        with self.lock:
            hit = self.paths.get(path) or self.names.get(os.path.basename(path))
        if (stale or not hit) and time.time() - self.tried > 60:
            try:
                self.refresh()
            except ApiError as e:
                self.error = e.message
            with self.lock:
                hit = self.paths.get(path) or self.names.get(os.path.basename(path))
        return hit


# ---------------------------------------------------------------------------------------
# Image proxy with a disk cache
# ---------------------------------------------------------------------------------------

class Images:
    def __init__(self, cache_dir):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.last_prune = 0

    @staticmethod
    def allowed(url):
        p = urllib.parse.urlsplit(url)
        return p.scheme == "https" and bool(IMAGE_HOSTS.match(p.hostname or ""))

    def get(self, url):
        if not self.allowed(url):
            raise ApiError(400, "Only StashDB images are served.")
        name = hashlib.sha256(url.encode()).hexdigest()
        path = self.dir / name[:2] / name
        meta = path.with_suffix(".type")
        if path.exists() and meta.exists():
            os.utime(path)
            return path.read_bytes(), meta.read_text()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with timed("image"), urllib.request.urlopen(req, timeout=30) as resp:
                ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                data = resp.read(25 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            raise ApiError(404 if e.code == 404 else 502, "Image not available.")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError(502, "Image not available.")
        if not ctype.startswith("image/"):
            raise ApiError(502, "Not an image.")
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        meta.write_text(ctype)
        tmp.replace(path)
        self.maybe_prune()
        return data, ctype

    def maybe_prune(self):
        if time.time() - self.last_prune < 3600:
            return
        self.last_prune = time.time()
        files = []
        for p in self.dir.glob("*/*"):
            if p.suffix:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _, size, p in sorted(files):
            if total <= IMAGE_CACHE_BYTES * 0.8:
                break
            for q in (p, p.with_suffix(".type")):
                try:
                    q.unlink()
                except OSError:
                    pass
            total -= size


# ---------------------------------------------------------------------------------------
# The app: API handlers
# ---------------------------------------------------------------------------------------

def clamp_int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def id_list(value):
    return [v for v in (value or "").split(",") if re.fullmatch(r"[0-9a-fA-F-]{8,40}", v)]


class App:
    def __init__(self, data_dir, cache_dir, setup_code=True):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data_dir / "desireerr.db")
        self.stash = StashDB(lambda: self.db.setting("stashdb_key") or self.file_key())
        self.whisparr = Whisparr(self.db)
        self.notify = Notifier(self.db)
        self.jellyfin = Jellyfin(self.db)
        self.images = Images(cache_dir) if cache_dir else None
        self.watch_lock = threading.Lock()
        self.failures = {}       # ip -> (count, last time), for login throttling
        self.setup_code = self.load_setup_code() if setup_code else ""

    def load_setup_code(self):
        """Until the admin exists, setup asks for a code only someone on the server can read,
        so nobody else on the network can claim the app first."""
        path = self.data_dir / "setup-code"
        if not self.setup_needed():
            path.unlink(missing_ok=True)
            return ""
        if not path.exists():
            path.write_text(secrets.token_hex(4) + "\n")
            path.chmod(0o600)
        code = path.read_text().strip()
        print(f"First run: the setup code is {code} (also in {path})", flush=True)
        return code

    @staticmethod
    def file_key():
        path = Path.home() / ".config/desireerr/stashdb_api_key"
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    # -- sessions -----------------------------------------------------------------------
    def setup_needed(self):
        return self.db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0

    def new_session(self, user_id):
        token = secrets.token_urlsafe(32)
        self.db.run("INSERT INTO sessions(token_hash, user_id, created, expires) VALUES (?,?,?,?)",
                    (hashlib.sha256(token.encode()).hexdigest(), user_id, now(),
                     now() + SESSION_DAYS * 86400))
        self.db.run("DELETE FROM sessions WHERE expires < ?", (now(),))
        return token

    def session_user(self, token):
        if not token:
            return None
        return self.db.one(
            "SELECT u.id, u.username, u.is_admin, u.auto_approve FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires > ?",
            (hashlib.sha256(token.encode()).hexdigest(), now()))

    def end_session(self, token):
        if token:
            self.db.run("DELETE FROM sessions WHERE token_hash = ?",
                        (hashlib.sha256(token.encode()).hexdigest(),))

    def throttle(self, ip):
        count, last = self.failures.get(ip, (0, 0))
        if time.time() - last > 900:
            count = 0
        if count >= 5:
            wait = min(300, 2 ** (count - 4))
            if time.time() - last < wait:
                raise ApiError(429, f"Too many wrong passwords. Try again in {int(wait)} seconds.")
        return count

    def login(self, ip, username, password):
        count = self.throttle(ip)
        user = self.db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if not user or not check_password(password, user["pw_salt"], user["pw_hash"]):
            self.failures[ip] = (count + 1, time.time())
            raise ApiError(401, "Wrong username or password.")
        self.failures.pop(ip, None)
        return self.new_session(user["id"])

    # -- users --------------------------------------------------------------------------
    def create_user(self, username, password, is_admin=False, auto_approve=False, min_password=8):
        username = (username or "").strip()
        if not re.fullmatch(r"[^\s:]{1,64}", username):
            raise ApiError(400, "Usernames are up to 64 characters, without spaces or colons.")
        if len(password or "") < max(1, min_password):
            raise ApiError(400, f"Passwords need at least {min_password} characters.")
        if self.db.one("SELECT id FROM users WHERE username = ?", (username,)):
            raise ApiError(409, f"There is already a user called {username}.")
        salt, digest = hash_password(password)
        return self.db.run(
            "INSERT INTO users(username, pw_salt, pw_hash, is_admin, auto_approve, created) "
            "VALUES (?,?,?,?,?,?)", (username, salt, digest, int(is_admin), int(auto_approve), now()))

    def set_password(self, user_id, password, min_password=8):
        if len(password or "") < max(1, min_password):
            raise ApiError(400, f"Passwords need at least {min_password} characters.")
        salt, digest = hash_password(password)
        self.db.run("UPDATE users SET pw_salt = ?, pw_hash = ? WHERE id = ?", (salt, digest, user_id))
        self.db.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def users(self):
        return self.db.all(
            "SELECT u.id, u.username, u.is_admin, u.auto_approve, u.created, "
            "(SELECT COUNT(*) FROM requests r WHERE r.user_id = u.id) AS requests "
            "FROM users u ORDER BY u.username")

    # -- status -------------------------------------------------------------------------
    def annotate(self, scenes):
        """Add each scene's request state, from Whisparr and our own requests."""
        scenes = [s for s in scenes if s]
        if not scenes:
            return scenes
        self.whisparr.ensure_fresh()
        ids = [s["id"] for s in scenes]
        marks = ",".join("?" * len(ids))
        reqs = {}
        for r in self.db.all(f"SELECT scene_id, status, id FROM requests WHERE scene_id IN ({marks}) "
                             "ORDER BY id", ids):
            reqs[r["scene_id"]] = r
        for s in scenes:
            s["state"] = self.state(s["id"], reqs.get(s["id"]))
        return scenes

    def state(self, scene_id, req=None):
        info = self.whisparr.lookup(scene_id)
        st = {"status": "none", "request_id": req["id"] if req else None}
        if info:
            st["whisparr_id"] = info["id"]
        if info and info["has_file"]:
            st["status"] = "available"
            jid = self.jellyfin.find(info.get("path"))
            if jid:
                st["jellyfin_id"] = jid
        elif info and info["id"] in self.whisparr.queue:
            q = self.whisparr.queue[info["id"]]
            st.update(status="downloading", progress=q["progress"], timeleft=q["timeleft"])
        elif info and info["monitored"]:
            st["status"] = "processing"
        elif req and req["status"] == "pending":
            st["status"] = "pending"
        elif req and req["status"] == "declined":
            st["status"] = "declined"
        elif req and req["status"] == "failed":
            st["status"] = "failed"
        elif req and req["status"] == "abandoned":
            st["status"] = "abandoned"
        return st

    def stalled_days(self, row, st):
        """Days an approved request has waited for a release, once that's too long; else 0."""
        if row.get("status") != "approved" or st["status"] != "processing":
            return 0
        start = max(row.get("decided_at") or row["created"], row.get("searched_at") or 0)
        days = (now() - start) // 86400
        return days if days >= clamp_int(self.db.setting("stall_days"), 7, 1, 365) else 0

    def decorate(self, row):
        row["state"] = self.state(row["scene_id"], row)
        row["stalled_days"] = self.stalled_days(row, row["state"])
        return row

    # -- requests -----------------------------------------------------------------------
    def request_scene(self, user, scene_id):
        scene = self.stash.find_scene(scene_id)
        if not scene:
            raise ApiError(404, "StashDB has no scene with that ID.")
        self.whisparr.ensure_fresh()
        current = self.state(scene_id)
        if current["status"] in ("available", "downloading", "processing"):
            raise ApiError(409, "This scene is already wanted or in the library.")
        pending = self.db.one("SELECT id FROM requests WHERE scene_id = ? AND status = 'pending'",
                              (scene_id,))
        if pending:
            raise ApiError(409, "This scene is already waiting for approval.")
        return self.create_request(user, scene, approve=bool(user["is_admin"] or user["auto_approve"]))

    def create_request(self, user, scene, approve=False, watch=None):
        """A request for a StashDB scene: approved and sent to Whisparr, or pending.

        From a saved search (watch), a request Whisparr can't take is removed again
        (the search tries later) and nobody is told it failed."""
        rid = self.db.run(
            "INSERT INTO requests(scene_id, title, studio, release_date, image, user_id, status, "
            "created, via, watch_id) VALUES (?,?,?,?,?,?, 'pending', ?,?,?)",
            (scene["id"], scene.get("title") or "", (scene.get("studio") or {}).get("name", ""),
             scene.get("release_date") or "", pick_image(scene.get("images"), landscape=True),
             user["id"], now(), watch["name"] if watch else "", watch["id"] if watch else None))
        if not approve:
            self.notify.send("pending", self.request_row(rid))
            return self.request_row(rid)
        try:
            return self.approve(user, rid, event="watch" if watch else "auto", quiet=bool(watch))
        except ApiError:
            if watch:
                self.db.run("DELETE FROM requests WHERE id = ?", (rid,))
            raise

    def approve(self, user, rid, event="approved", quiet=False):
        req = self.db.one("SELECT * FROM requests WHERE id = ?", (rid,))
        if not req:
            raise ApiError(404, "No such request.")
        search = self.db.setting("search_on_request") == "1"
        try:
            wid = self.whisparr.send_scene(req["scene_id"], search=search)
        except ApiError as e:
            self.db.run("UPDATE requests SET status = 'failed', error = ?, decided_by = ?, decided_at = ? "
                        "WHERE id = ?", (e.message, user["id"], now(), rid))
            if not quiet:
                self.notify.send("failed", self.request_row(rid))
            raise
        self.db.run("UPDATE requests SET status = 'approved', error = '', whisparr_id = ?, decided_by = ?, "
                    "decided_at = ?, notified = '', searched_at = NULL WHERE id = ?",
                    (wid, user["id"], now(), rid))
        row = self.request_row(rid)
        self.notify.send(event, row)
        return row

    def decline(self, user, rid):
        self.db.run("UPDATE requests SET status = 'declined', decided_by = ?, decided_at = ? "
                    "WHERE id = ? AND status IN ('pending', 'failed')", (user["id"], now(), rid))
        row = self.request_row(rid)
        if row and row["status"] == "declined":
            self.notify.send("declined", row)
        return row

    def search_again(self, rows):
        """Search Whisparr's indexers again for approved requests still waiting; the
        stalled clock starts over."""
        self.whisparr.ensure_fresh()
        rows = [r for r in rows if self.decorate(r)["state"]["status"] == "processing"]
        if not rows:
            raise ApiError(409, "Nothing to search for: these aren't waiting for a release.")
        self.whisparr.search([r["state"]["whisparr_id"] for r in rows])
        for r in rows:
            notified = ",".join(x for x in r["notified"].split(",") if x and x != "stalled")
            self.db.run("UPDATE requests SET searched_at = ?, notified = ? WHERE id = ?",
                        (now(), notified, r["id"]))
        return len(rows)

    def give_up(self, user, rid):
        """Stop waiting for a release: Whisparr no longer wants the scene."""
        row = self.request_row(rid)
        if row["status"] != "approved" or row["state"]["status"] not in ("processing", "downloading"):
            raise ApiError(409, "Only requests still waiting for a release can be given up.")
        self.whisparr.unmonitor(row["state"]["whisparr_id"])
        self.db.run("UPDATE requests SET status = 'abandoned', decided_by = ?, decided_at = ? "
                    "WHERE id = ?", (user["id"], now(), rid))
        return self.request_row(rid)

    REQUEST_SQL = ("SELECT r.*, u.username AS requested_by, d.username AS decided_by_name "
                   "FROM requests r LEFT JOIN users u ON u.id = r.user_id "
                   "LEFT JOIN users d ON d.id = r.decided_by")

    def request_row(self, rid):
        row = self.db.one(self.REQUEST_SQL + " WHERE r.id = ?", (rid,))
        if row:
            self.whisparr.ensure_fresh()
            self.decorate(row)
        return row

    def list_requests(self, user, which, page, per_page=30):
        where, args = [], []
        if not user["is_admin"]:
            where.append("r.user_id = ?")
            args.append(user["id"])
        if which in ("pending", "approved", "declined", "failed", "abandoned"):
            where.append("r.status = ?")
            args.append(which)
        elif which in ("available", "stalled"):
            where.append("r.status = 'approved'")
        sql = self.REQUEST_SQL + (" WHERE " + " AND ".join(where) if where else "")
        self.whisparr.ensure_fresh()
        if which in ("available", "stalled"):
            # Whisparr's state, not ours: filter them all, then take the page.
            rows = [self.decorate(r) for r in self.db.all(sql + " ORDER BY r.id DESC", args)]
            rows = [r for r in rows if (r["state"]["status"] == "available" if which == "available"
                                        else r["stalled_days"])]
            start = (page - 1) * per_page
            return {"count": len(rows), "requests": rows[start:start + per_page]}
        total = self.db.one(f"SELECT COUNT(*) AS n FROM ({sql})", args)["n"]
        rows = self.db.all(sql + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
                           args + [per_page, (page - 1) * per_page])
        return {"count": total, "requests": [self.decorate(r) for r in rows]}

    def check_requests(self):
        """Push once when an approved request's scene arrives, or when it has waited too
        long for a release. Files older than AVAILABLE_WINDOW (say, from before the update
        that brought pushes) are only marked."""
        for row in self.db.all(self.REQUEST_SQL + " WHERE r.status = 'approved'"):
            notified = set(filter(None, row["notified"].split(",")))
            self.decorate(row)
            event = extra = None
            if row["state"]["status"] == "available" and "available" not in notified:
                info = self.whisparr.lookup(row["scene_id"]) or {}
                if now() - iso_time(info.get("file_added")) < AVAILABLE_WINDOW:
                    event = "available"
                notified.add("available")
            elif row["stalled_days"] and "stalled" not in notified:
                event, extra = "stalled", f"No release found in {row['stalled_days']} days."
                notified.add("stalled")
            else:
                continue
            self.db.run("UPDATE requests SET notified = ? WHERE id = ?",
                        (",".join(sorted(notified)), row["id"]))
            if event:
                self.notify.send(event, row, extra)

    def pending_count(self):
        return self.db.one("SELECT COUNT(*) AS n FROM requests WHERE status = 'pending'")["n"]

    # -- browsing -----------------------------------------------------------------------
    @staticmethod
    def tag_plan(q, favs=()):
        """How a query's tags are matched: (the one tags criterion StashDB takes, tags of which
        a scene needs one, tags it needs all of, tags it mustn't have), the last three checked
        here. fav_tags=1 means any of the user's favourite tags (favs).

        Favourites go to StashDB (likely the narrower part) and picked tags are checked here,
        except with one favourite and all picked tags wanted: one exact criterion then."""
        tags, not_tags = id_list(q.get("tags")), id_list(q.get("not_tags"))
        mode = TAG_MODES.get(q.get("tag_mode"), "INCLUDES_ALL")
        favs = list(favs) if q.get("fav_tags") == "1" else []
        if favs and tags:
            if len(favs) == 1 and mode == "INCLUDES_ALL":
                return {"value": list(dict.fromkeys(tags + favs)), "modifier": mode}, [], [], not_tags
            any_, all_ = (tags, []) if mode == "INCLUDES" else ([], tags)
            return {"value": favs, "modifier": "INCLUDES"}, any_, all_, not_tags
        if tags or favs:
            return ({"value": tags, "modifier": mode} if tags else
                    {"value": favs, "modifier": "INCLUDES"}), [], [], not_tags
        if not_tags:
            return {"value": not_tags, "modifier": "EXCLUDES"}, [], [], []
        return None, [], [], []

    def scene_input(self, q, favs=()):
        """Build a SceneQueryInput from URL parameters (the Scenes page's filters)."""
        inp = {
            "page": clamp_int(q.get("page"), 1, 1, 10000),
            "per_page": clamp_int(q.get("per_page"), 40, 1, PER_PAGE_MAX),
            "sort": q.get("sort") if q.get("sort") in SCENE_SORTS else "DATE",
            "direction": "ASC" if q.get("dir") == "asc" else "DESC",
        }
        if q.get("q"):
            inp["title"] = q["q"][:200]
        # StashDB takes one tags criterion per query; filtered_scenes checks the rest.
        criterion = self.tag_plan(q, favs)[0]
        if criterion:
            inp["tags"] = criterion
        performers = id_list(q.get("performers"))
        if performers:
            inp["performers"] = {"value": performers,
                                 "modifier": "INCLUDES" if q.get("performer_mode") == "any" else "INCLUDES_ALL"}
        studios = id_list(q.get("studios"))
        if studios:
            if q.get("sub_studios") == "1" and len(studios) == 1:
                inp["parentStudio"] = studios[0]
            else:
                inp["studios"] = {"value": studios, "modifier": "INCLUDES"}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", q.get("after", "")):
            inp["date"] = {"value": q["after"], "modifier": "GREATER_THAN"}
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", q.get("before", "")):
            inp["date"] = {"value": q["before"], "modifier": "LESS_THAN"}
        if q.get("favorites") in ("PERFORMER", "STUDIO", "ALL"):
            inp["favorites"] = q["favorites"]
        return inp

    def browse_scenes(self, q, user=None):
        favs = self.fav_tag_ids(user) if user else []
        inp = self.scene_input(q, favs)
        if q.get("fav_tags") == "1" and not favs:
            return {"count": 0, "scenes": [], "page": inp["page"], "per_page": inp["per_page"]}
        result, scenes = self.filtered_scenes(q, inp, favs)
        return {"count": result["count"], "scenes": self.annotate(scenes), "page": inp["page"],
                "per_page": inp["per_page"]}

    def filtered_scenes(self, q, inp, favs=(), ttl=300):
        """StashDB's answer to a scene query, and its scenes that pass the tag checks StashDB
        couldn't take (see tag_plan)."""
        result = self.stash.query_scenes(inp, ttl)
        scenes = result["scenes"]
        _, need_one, need_all, not_tags = self.tag_plan(q, favs)
        if need_one or need_all or not_tags:
            # So a page can come back short. The scene cards carry no tags: fetch them.
            full = {sid: set(t) for sid, t in self.scene_tags([s["id"] for s in scenes]).items()}
            need_one, need_all, not_tags = set(need_one), set(need_all), set(not_tags)

            def ok(sc):
                have = full.get(sc["id"], set())
                return ((not need_one or have & need_one) and need_all <= have
                        and not have & not_tags)
            scenes = [sc for sc in scenes if ok(sc)]
        return result, scenes

    # -- favourite tags (Desireerr's own, per user) ------------------------------------
    def fav_tags(self, user):
        return self.db.all("SELECT tag_id AS id, name FROM favorite_tags WHERE user_id = ? "
                           "ORDER BY name COLLATE NOCASE", (user["id"],))

    def fav_tag_ids(self, user):
        return [t["id"] for t in self.fav_tags(user)]

    def set_fav_tag(self, user, tag_id, favorite):
        if favorite:
            tag = self.stash.find_tag(tag_id)
            if not tag:
                raise ApiError(404, "StashDB has no tag with that ID.")
            self.db.run("INSERT OR IGNORE INTO favorite_tags(user_id, tag_id, name, created) "
                        "VALUES (?,?,?,?)", (user["id"], tag_id, tag["name"], now()))
        else:
            self.db.run("DELETE FROM favorite_tags WHERE user_id = ? AND tag_id = ?",
                        (user["id"], tag_id))
        # Their saved searches with favourite tags look through the matches again.
        self.db.run("UPDATE watches SET cursor = '' WHERE user_id = ? AND query LIKE '%fav_tags=1%'",
                    (user["id"],))
        return self.fav_tags(user)

    def scene_tags(self, ids):
        found = self.stash.find_many("findScene", ids, "{ id tags { id } }")
        return {s["id"]: [t["id"] for t in s["tags"]] for s in found}

    def names(self, q):
        out = {}
        for kind in ("tags", "performers", "studios"):
            ids = id_list(q.get(kind))
            out[kind] = self.stash.names(kind, ids) if ids else {}
        return out

    def discover(self, user):
        favs = self.fav_tag_ids(user)
        specs = [
            ("trending", "Trending", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": 20, "sort": "TRENDING", "direction": "DESC"})["scenes"],
             "/scenes?sort=TRENDING"),
            ("favorites", "New from your StashDB favourites", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": 20, "sort": "DATE", "direction": "DESC", "favorites": "ALL",
                 "date": {"value": tomorrow(), "modifier": "LESS_THAN"}})["scenes"],
             "/scenes?favorites=ALL"),
            ("favtags", "New with your favourite tags", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": 20, "sort": "DATE", "direction": "DESC",
                 "tags": {"value": favs, "modifier": "INCLUDES"},
                 "date": {"value": tomorrow(), "modifier": "LESS_THAN"}})["scenes"] if favs else [],
             "/scenes?fav_tags=1"),
            ("added", "Recently added", self.recent_library, None),
            ("new", "New releases", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": 20, "sort": "DATE", "direction": "DESC",
                 "date": {"value": tomorrow(), "modifier": "LESS_THAN"}})["scenes"],
             "/scenes?sort=DATE"),
            ("popular", "Popular", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": 20, "sort": "POPULARITY", "direction": "DESC"})["scenes"],
             "/scenes?sort=POPULARITY"),
        ]
        timing = Timing.current()

        def row(spec):
            key, title, fn, link = spec
            Timing.begin(timing)  # this thread works for the same request
            try:
                return {"key": key, "title": title, "link": link, "scenes": self.annotate(fn())}
            except ApiError as e:
                return {"key": key, "title": title, "link": link, "scenes": [], "error": e.message}

        # The rows wait on StashDB, not on us: ask for all of them at once. Whisparr's
        # state is brought up to date first, so the rows don't each refresh it.
        self.whisparr.ensure_fresh()
        with ThreadPoolExecutor(len(specs)) as pool:
            rows = list(pool.map(row, specs))
        mine = self.db.all("SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT 12",
                           (user["id"],))
        for r in mine:
            self.decorate(r)
        return {"rows": rows, "requests": mine, "followed": self.whisparr.followed,
                "whisparr_error": self.whisparr.error}

    def recent_library(self):
        self.whisparr.ensure_fresh()
        ids = self.whisparr.recently_added(20)
        order = {sid: i for i, sid in enumerate(ids)}
        return sorted(self.stash.find_scenes(ids), key=lambda s: order.get(s["id"], 99))

    def scene(self, scene_id):
        scene = self.stash.find_scene(scene_id)
        if not scene:
            raise ApiError(404, "StashDB has no scene with that ID.")
        req = self.db.one("SELECT * FROM requests WHERE scene_id = ? ORDER BY id DESC LIMIT 1", (scene_id,))
        self.whisparr.ensure_fresh()
        scene["state"] = self.state(scene_id, req)
        scene["request"] = self.request_row(req["id"]) if req else None
        if scene["request"]:
            scene["state"] = scene["request"]["state"]
        scene["stashdb_url"] = f"{STASHDB_WEB}/scenes/{scene_id}"
        return scene

    def performer(self, pid):
        perf = self.stash.find_performer(pid)
        if not perf:
            raise ApiError(404, "StashDB has no performer with that ID.")
        perf["stashdb_url"] = f"{STASHDB_WEB}/performers/{pid}"
        try:
            perf["follow"] = self.whisparr.follow_state("performer", pid)
        except ApiError as e:
            perf["follow"] = {"followed": False, "error": e.message}
        return perf

    def studio(self, sid):
        studio = self.stash.find_studio(sid)
        if not studio:
            raise ApiError(404, "StashDB has no studio with that ID.")
        studio["stashdb_url"] = f"{STASHDB_WEB}/studios/{sid}"
        try:
            studio["follow"] = self.whisparr.follow_state("studio", sid)
        except ApiError as e:
            studio["follow"] = {"followed": False, "error": e.message}
        return studio

    def browse_performers(self, q):
        inp = {
            "page": clamp_int(q.get("page"), 1, 1, 10000),
            "per_page": clamp_int(q.get("per_page"), 40, 1, PER_PAGE_MAX),
            "sort": q.get("sort") if q.get("sort") in PERFORMER_SORTS else "POPULARITY",
            "direction": "ASC" if q.get("dir") == "asc" else "DESC",
        }
        if q.get("q"):
            inp["names"] = q["q"][:100]
        if q.get("gender") in GENDERS:
            inp["gender"] = q["gender"]
        if q.get("favorites") == "1":
            inp["is_favorite"] = True
        return self.stash.query_performers(inp)

    def browse_studios(self, q):
        inp = {
            "page": clamp_int(q.get("page"), 1, 1, 10000),
            "per_page": clamp_int(q.get("per_page"), 40, 1, PER_PAGE_MAX),
            "sort": q.get("sort") if q.get("sort") in STUDIO_SORTS else "NAME",
            "direction": "DESC" if q.get("dir") == "desc" else "ASC",
        }
        if q.get("q"):
            inp["names"] = q["q"][:100]
        if q.get("favorites") == "1":
            inp["is_favorite"] = True
        if q.get("top") == "1":
            inp["has_parent"] = False
        return self.stash.query_studios(inp)

    def tags(self, q):
        if q.get("q"):
            return {"tags": self.stash.search_tags(q["q"][:100])}
        if q.get("ids"):
            return {"tags": self.stash.find_tags(id_list(q["ids"])[:50])}
        if q.get("category"):
            inp = {"category_id": q["category"], "page": clamp_int(q.get("page"), 1, 1, 1000),
                   "per_page": 100, "sort": "NAME", "direction": "ASC"}
            return self.stash.query_tags(inp)
        return {"categories": self.stash.tag_categories()}

    def search(self, term):
        term = (term or "").strip()[:200]
        if not term:
            return {"scenes": [], "performers": [], "studios": [], "tags": []}
        found = self.stash.search(term)
        self.annotate(found["scenes"])
        return found

    def set_favorite(self, kind, sid, favorite):
        """Favourite (or not) on StashDB only: a filter for Discover, Scenes and the lists.
        Whisparr isn't touched, so nothing is downloaded for it (Follow does that). Should
        Whisparr's StashDB import list be on, it adds favourites by itself."""
        self.stash.set_favorite(kind, sid, favorite)
        return {"is_favorite": bool(favorite)}

    # -- saved searches ---------------------------------------------------------------
    # A saved search is the Scenes page's filters plus a date: every hour, scenes released
    # since then that match (also older ones newly tagged to match) are requested as their
    # owner, so the usual approval applies, or always left pending with "ask first".

    WATCH_FILTERS = ("q", "tags", "tag_mode", "not_tags", "fav_tags", "performers",
                     "performer_mode", "studios", "sub_studios", "favorites")

    def watch_query(self, query):
        q = {k: v for k, v in urllib.parse.parse_qsl(str(query or "")) if k in self.WATCH_FILTERS}
        if not (q.get("q") or id_list(q.get("tags")) or q.get("fav_tags") == "1" or id_list(q.get("performers"))
                or id_list(q.get("studios")) or q.get("favorites") in ("PERFORMER", "STUDIO", "ALL")):
            raise ApiError(400, "Pick a tag, performer, studio, favourites or title words first; "
                                "without one, a saved search would request every new scene.")
        return q

    def watches(self, user):
        sql = ("SELECT w.*, u.username AS owner, "
               "(SELECT COUNT(*) FROM requests r WHERE r.watch_id = w.id) AS requests, "
               "(SELECT COUNT(*) FROM watch_scenes x WHERE x.watch_id = w.id AND x.done = 0) AS waiting "
               "FROM watches w JOIN users u ON u.id = w.user_id")
        if user["is_admin"]:
            return self.db.all(sql + " ORDER BY w.name COLLATE NOCASE")
        return self.db.all(sql + " WHERE w.user_id = ? ORDER BY w.name COLLATE NOCASE", (user["id"],))

    def watch(self, user, wid):
        w = self.db.one("SELECT * FROM watches WHERE id = ?", (wid,))
        if not w or not (user["is_admin"] or w["user_id"] == user["id"]):
            raise ApiError(404, "No such saved search.")
        return w

    def save_watch(self, user, body, wid=None):
        values = {}
        if wid is None or "query" in body:
            q = self.watch_query(body.get("query"))
            values["query"] = urllib.parse.urlencode(q)
            values["summary"] = str(body.get("summary") or "")[:300]
        if wid is None or "name" in body:
            name = str(body.get("name") or "").strip()[:80]
            if not name:
                raise ApiError(400, "Give the saved search a name.")
            values["name"] = name
        if wid is None or "since" in body:
            since = str(body.get("since") or today())
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
                raise ApiError(400, "The date looks like 2026-11-07.")
            values["since"] = since
        for key in ("ask_first", "enabled"):
            if key in body:
                values[key] = int(bool(body[key]))
        if wid is None:
            values.update(user_id=user["id"], created=now())
            cols = ", ".join(values)
            wid = self.db.run(f"INSERT INTO watches({cols}) VALUES ({','.join('?' * len(values))})",
                              list(values.values()))
        elif values:
            if {"query", "since"} & values.keys():
                values["cursor"] = ""     # look through the matches again
            sets = ", ".join(f"{k} = ?" for k in values)
            self.db.run(f"UPDATE watches SET {sets} WHERE id = ?", list(values.values()) + [wid])
        return self.watch(user, wid)

    def check_watches(self):
        """The saved searches due for a check (from the background refresher)."""
        due = self.db.all("SELECT id FROM watches WHERE enabled = 1 AND checked < ? ORDER BY checked",
                          (now() - WATCH_EVERY,))
        for w in due:
            try:
                self.check_watch(w["id"])
            except ApiError:
                pass  # kept with the saved search, shown on its page

    def check_watch(self, wid):
        """Look for new matches and request them. Returns what was done, for "Check now"."""
        with self.watch_lock:
            w = self.db.one("SELECT * FROM watches WHERE id = ?", (wid,))
            if not w:
                return {}
            try:
                found = self.watch_matches(w)
                done = self.watch_requests(w)
            except ApiError as e:
                self.db.run("UPDATE watches SET checked = ?, error = ? WHERE id = ?",
                            (now(), e.message, wid))
                raise
            self.db.run("UPDATE watches SET checked = ?, error = '' WHERE id = ?", (now(), wid))
            return dict(done, found=found)

    def watch_matches(self, w):
        """Note the scenes matching since the last check: newest changes first, until the
        ones seen last time. Returns how many were new."""
        q = self.watch_query(w["query"])
        favs = self.fav_tag_ids({"id": w["user_id"]})   # the owner's, as they are now
        if q.get("fav_tags") == "1" and not favs:
            return 0
        inp = self.scene_input(q, favs)
        inp.update(per_page=100, sort="UPDATED_AT", direction="DESC",
                   date={"value": day_before(w["since"]), "modifier": "GREATER_THAN"})
        newest, found = w["cursor"], 0
        for page in range(1, WATCH_PAGES + 1):
            inp["page"] = page
            result, scenes = self.filtered_scenes(q, inp, favs, ttl=0)
            for sc in scenes:
                if not self.db.one("SELECT 1 AS x FROM watch_scenes WHERE watch_id = ? AND scene_id = ?",
                                   (w["id"], sc["id"])):
                    self.db.run("INSERT INTO watch_scenes(watch_id, scene_id, card, seen) VALUES (?,?,?,?)",
                                (w["id"], sc["id"], json.dumps(sc), now()))
                    found += 1
            stamps = [sc.get("updated") or "" for sc in result["scenes"]]
            newest = max([newest] + stamps)
            if len(result["scenes"]) < inp["per_page"] or (w["cursor"] and min(stamps) <= w["cursor"]):
                break
        self.db.run("UPDATE watches SET cursor = ? WHERE id = ?", (newest, w["id"]))
        return found

    def watch_requests(self, w):
        """Request the noted scenes not wanted or asked for yet."""
        owner = self.db.one("SELECT id, username, is_admin, auto_approve FROM users WHERE id = ?",
                            (w["user_id"],))
        approve = bool(owner["is_admin"] or owner["auto_approve"]) and not w["ask_first"]
        todo = self.db.all("SELECT * FROM watch_scenes WHERE watch_id = ? AND done = 0 ORDER BY seen",
                           (w["id"],))
        self.whisparr.ensure_fresh()
        requested = waiting = 0

        def mark(row, done):
            self.db.run("UPDATE watch_scenes SET done = ? WHERE watch_id = ? AND scene_id = ?",
                        (done, w["id"], row["scene_id"]))

        for row in todo:
            asked = self.db.one("SELECT id FROM requests WHERE scene_id = ?", (row["scene_id"],))
            if asked or self.state(row["scene_id"])["status"] != "none":
                mark(row, 1)
                continue
            if requested >= WATCH_MAX_REQUESTS:
                waiting += 1
                continue
            try:
                self.create_request(owner, json.loads(row["card"]), approve=approve, watch=w)
            except NotInWhisparr:
                if now() - row["seen"] > WATCH_RETRY_DAYS * 86400:
                    mark(row, 2)
                else:
                    waiting += 1
                continue
            requested += 1
            mark(row, 1)
        return {"requested": requested, "waiting": waiting}

    # -- settings -----------------------------------------------------------------------
    def public_settings(self):
        s = self.db.settings()
        file_key = whisparr_config_key()
        out = {
            "stashdb_key_set": bool(s["stashdb_key"] or self.file_key()),
            "whisparr_url": s["whisparr_url"],
            "whisparr_key_set": bool(s["whisparr_key"]),
            "whisparr_key_from_config": bool(not s["whisparr_key"] and file_key),
            "quality_profile_id": s["quality_profile_id"],
            "root_folder": s["root_folder"],
            "search_on_request": s["search_on_request"] == "1",
            "stall_days": clamp_int(s["stall_days"], 7, 1, 365),
            "tab_title": s["tab_title"],
            "notify_server": s["notify_server"],
            "notify_topic": s["notify_topic"],
            "notify_events": [e for e in s["notify_events"].split(",") if e in NOTIFY_EVENTS],
            "notify_discreet": s["notify_discreet"] == "1",
            "notify_error": self.notify.error,
            "app_url": s["app_url"],
            "seen_origin": s.get("seen_origin", ""),
            "jellyfin_url": s["jellyfin_url"],
            "jellyfin_key_set": bool(s["jellyfin_key"]),
            "jellyfin_link": s["jellyfin_link"],
        }
        tb_server, tb_topic = torrent_box_ntfy(self.notify.conf_dir)
        out["torrent_box_ntfy"] = tb_server if tb_topic else ""
        try:
            profiles, roots = self.whisparr.options()
            out["profiles"], out["roots"] = profiles, roots
        except ApiError as e:
            out["whisparr_error"] = e.message
        if s["jellyfin_key"]:
            try:
                self.jellyfin.refresh()
                out["jellyfin_videos"] = len(self.jellyfin.paths)
            except ApiError as e:
                out["jellyfin_error"] = e.message
        return out

    def save_settings(self, body):
        allowed = {}
        if "stashdb_key" in body and body["stashdb_key"] is not None:
            key = str(body["stashdb_key"]).strip()
            if key:
                self.stash.me(key=key)          # raises if StashDB refuses it
            allowed["stashdb_key"] = key
            self.stash.cache.clear()
        if "whisparr_url" in body:
            url = str(body["whisparr_url"]).strip().rstrip("/") or DEFAULT_WHISPARR
            if not re.match(r"^https?://", url):
                raise ApiError(400, "Whisparr's address starts with http:// or https://")
            allowed["whisparr_url"] = url
        if "whisparr_key" in body and body["whisparr_key"] is not None:
            allowed["whisparr_key"] = str(body["whisparr_key"]).strip()
        if "quality_profile_id" in body:
            allowed["quality_profile_id"] = str(body["quality_profile_id"] or "")
        if "root_folder" in body:
            allowed["root_folder"] = str(body["root_folder"] or "")
        if "search_on_request" in body:
            allowed["search_on_request"] = "1" if body["search_on_request"] else "0"
        if "stall_days" in body:
            allowed["stall_days"] = str(clamp_int(body["stall_days"], 7, 1, 365))
        if "tab_title" in body:
            allowed["tab_title"] = (str(body["tab_title"]).strip() or "Desireerr")[:40]
        for key in ("notify_server", "app_url", "jellyfin_link"):
            if key in body:
                allowed[key] = web_address(body[key])
        if "jellyfin_url" in body:
            allowed["jellyfin_url"] = web_address(body["jellyfin_url"]) or DEFAULT_JELLYFIN
        if "notify_topic" in body:
            topic = str(body["notify_topic"] or "").strip()
            if topic and not re.fullmatch(r"[\w-]{1,64}", topic):
                raise ApiError(400, "ntfy topics are letters, digits, - and _ (up to 64).")
            allowed["notify_topic"] = topic
        if isinstance(body.get("notify_events"), list):
            allowed["notify_events"] = ",".join(e for e in NOTIFY_EVENTS if e in body["notify_events"])
        if "notify_discreet" in body:
            allowed["notify_discreet"] = "1" if body["notify_discreet"] else "0"
        if "jellyfin_key" in body and body["jellyfin_key"] is not None:
            allowed["jellyfin_key"] = str(body["jellyfin_key"]).strip()
        self.db.set_settings(allowed)
        if {"whisparr_url", "whisparr_key"} & allowed.keys():
            self.whisparr.index_time = 0
            self.whisparr.queue_time = 0
        if {"jellyfin_url", "jellyfin_key"} & allowed.keys():
            self.jellyfin.time = self.jellyfin.tried = 0
        return self.public_settings()

    def remember_origin(self, origin):
        """Where the app is opened, for links in pushes while no address is set."""
        if origin and self.db.setting("seen_origin") != origin:
            self.db.set_settings({"seen_origin": origin})


def tomorrow():
    return date.fromordinal(date.today().toordinal() + 1).isoformat()


def day_before(day):
    return date.fromordinal(date.fromisoformat(day).toordinal() - 1).isoformat()


def iso_time(value):
    """Whisparr's and StashDB's ISO times ("2026-11-05T10:22:33.123Z", UTC) -> Unix time; 0
    if there's none."""
    try:
        return int(datetime.fromisoformat(str(value)[:19]).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def pick_image(images, landscape=True):
    if not images:
        return ""
    def score(img):
        w, h = img.get("width") or 0, img.get("height") or 0
        wide = (w >= h) == landscape
        return (wide, w * h)
    return max(images, key=score)["url"]


# ---------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"Desireerr/{VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        if os.environ.get("DESIREERR_ACCESS_LOG"):
            super().log_message(fmt, *args)

    # -- helpers ------------------------------------------------------------------------
    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def send(self, status, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json", "application/javascript")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        timing = Timing.current()
        if timing:
            self.send_header("Server-Timing", timing.header())
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ApiError(413, "Request too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Body is not JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Body must be a JSON object.")
        return data

    def session_cookie(self, token, clear=False):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        if clear:
            return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
        return (f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
                f"HttpOnly; SameSite=Lax{secure}")

    def client_ip(self):
        return self.client_address[0]

    def origin(self):
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        proto = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        return f"{proto}://{host}" if re.fullmatch(r"[\w.-]+(:\d+)?|\[[\w:.]+\](:\d+)?", host) else ""

    # -- dispatch -----------------------------------------------------------------------
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        timing = Timing.begin()
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if path.startswith("/api/"):
                if method != "GET" and self.headers.get("X-Requested-With") != "desireerr":
                    raise ApiError(403, "Missing request header.")
                status, body, headers = self.api(method, path[5:], query)
                self.send(status, body, headers=headers)
            elif path == "/img":
                self.image(query)
            else:
                self.static(path)
        except ApiError as e:
            self.send(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep serving; report what broke
            print(f"error: {method} {path}: {e!r}", file=sys.stderr, flush=True)
            try:
                self.send(500, {"error": "Something went wrong on the server. The log has details."})
            except OSError:
                pass
        finally:
            # Only the path: queries can hold search terms.
            if path.startswith("/api/") and timing.total() > SLOW_REQUEST:
                print(f"slow: {method} {path} {timing.total():.2f}s"
                      f" ({timing.summary() or 'all in Desireerr'})", flush=True)

    def static(self, path):
        if path in ("/favicon.ico",):
            path = "/static/favicon.svg"
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR not in target.parents or not target.is_file():
                raise ApiError(404, "Not found.")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".webmanifest":
                ctype = "application/manifest+json"
            self.send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})
            return
        # Every other path is a page of the single-page app.
        title = self.app.db.setting("tab_title") or "Desireerr"
        page = (WEB_DIR / "index.html").read_text().replace("{{TITLE}}", html_escape(title))
        self.send(200, page, "text/html", {
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                       "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        })

    def image(self, query):
        user = self.app.session_user(self.cookie(SESSION_COOKIE))
        if not user:
            raise ApiError(401, "Sign in first.")
        data, ctype = self.app.images.get(query.get("u", ""))
        self.send(200, data, ctype, {"Cache-Control": "private, max-age=604800"})

    def api(self, method, route, q):
        app = self.app
        token = self.cookie(SESSION_COOKIE)
        user = app.session_user(token)
        parts = [p for p in route.split("/") if p]
        key = (method, parts[0] if parts else "")

        # -- open routes --
        if key == ("GET", "status"):
            admin_user = bool(user and user["is_admin"])
            out = {"setup_needed": app.setup_needed(), "user": user, "version": VERSION,
                   "pending": app.pending_count() if admin_user else 0,
                   "whisparr_url": app.whisparr.web_url() if admin_user else None}
            if user:
                out["fav_tags"] = app.fav_tags(user)
                app.remember_origin(self.origin())
                app.jellyfin.ensure()
                if app.jellyfin.creds()[1] and app.jellyfin.server_id:
                    out["jellyfin"] = {"server_id": app.jellyfin.server_id,
                                       "link": app.db.setting("jellyfin_link")}
            return 200, out, None
        if key == ("POST", "setup"):
            if not app.setup_needed():
                raise ApiError(409, "Setup is done already. Sign in instead.")
            body = self.body_json()
            app.throttle(self.client_ip())
            if not hmac.compare_digest(str(body.get("setup_code", "")).strip().lower(), app.setup_code):
                count, _ = app.failures.get(self.client_ip(), (0, 0))
                app.failures[self.client_ip()] = (count + 1, time.time())
                raise ApiError(403, "Wrong setup code. It's in the server's log and in "
                                    f"{app.data_dir / 'setup-code'}.")
            if body.get("stashdb_key"):
                app.save_settings({"stashdb_key": body["stashdb_key"]})
            uid = app.create_user(body.get("username"), body.get("password"), is_admin=True,
                                  auto_approve=True)
            (app.data_dir / "setup-code").unlink(missing_ok=True)
            app.setup_code = ""
            return 200, {"ok": True}, {"Set-Cookie": self.session_cookie(app.new_session(uid))}
        if key == ("POST", "login"):
            body = self.body_json()
            token = app.login(self.client_ip(), body.get("username", ""), body.get("password", ""))
            return 200, {"ok": True}, {"Set-Cookie": self.session_cookie(token)}
        if key == ("POST", "logout"):
            app.end_session(token)
            return 200, {"ok": True}, {"Set-Cookie": self.session_cookie("", clear=True)}

        if not user:
            raise ApiError(401, "Sign in first.")

        def admin():
            if not user["is_admin"]:
                raise ApiError(403, "Only admins can do that.")

        # -- browsing --
        if key == ("GET", "discover"):
            return 200, app.discover(user), None
        if key == ("GET", "scenes"):
            return 200, app.browse_scenes(q, user), None
        if key == ("GET", "scene") and len(parts) == 2:
            return 200, app.scene(parts[1]), None
        if key == ("GET", "search"):
            return 200, app.search(q.get("q")), None
        if key == ("GET", "performers"):
            return 200, app.browse_performers(q), None
        if key == ("GET", "performer") and len(parts) == 2:
            return 200, app.performer(parts[1]), None
        if key == ("GET", "studios"):
            return 200, app.browse_studios(q), None
        if key == ("GET", "studio") and len(parts) == 2:
            return 200, app.studio(parts[1]), None
        if key == ("GET", "tags"):
            return 200, app.tags(q), None
        if key == ("GET", "names"):
            return 200, app.names(q), None
        if key == ("GET", "tag") and len(parts) == 2:
            tag = app.stash.find_tag(parts[1])
            if not tag:
                raise ApiError(404, "StashDB has no tag with that ID.")
            return 200, dict(tag, is_favorite=tag["id"] in app.fav_tag_ids(user)), None
        if key == ("POST", "favorite-tags"):
            body = self.body_json()
            if not id_list(body.get("id")):
                raise ApiError(400, "Favourite takes a tag ID.")
            return 200, {"tags": app.set_fav_tag(user, body["id"], bool(body.get("favorite")))}, None

        # -- favourites (admins: they're the StashDB account's) --
        if key == ("POST", "favorite"):
            admin()
            body = self.body_json()
            kind = body.get("kind")
            if kind not in ("performer", "studio") or not id_list(body.get("id")):
                raise ApiError(400, "Favourite takes a performer or studio ID.")
            return 200, app.set_favorite(kind, body["id"], bool(body.get("favorite"))), None

        # -- following (admins: it changes what Whisparr wants from now on) --
        if key == ("POST", "follow"):
            admin()
            body = self.body_json()
            kind = body.get("kind")
            if kind not in ("performer", "studio") or not id_list(body.get("id")):
                raise ApiError(400, "Follow takes a performer or studio ID.")
            return 200, app.whisparr.set_follow(kind, body["id"], bool(body.get("follow"))), None

        # -- requests --
        if key == ("POST", "requests") and len(parts) == 1:
            body = self.body_json()
            if not id_list(body.get("scene_id")):
                raise ApiError(400, "Request takes a scene ID.")
            return 200, app.request_scene(user, body["scene_id"]), None
        if key == ("GET", "requests"):
            return 200, app.list_requests(user, q.get("filter", "all"),
                                          clamp_int(q.get("page"), 1, 1, 10000)), None
        if key == ("POST", "requests") and parts[1:] == ["search-stalled"]:
            admin()
            rows = app.list_requests(user, "stalled", 1, per_page=10000)["requests"]
            return 200, {"searched": app.search_again(rows)}, None
        if parts[:1] == ["requests"] and len(parts) >= 2 and parts[1].isdigit():
            rid = int(parts[1])
            row = app.db.one("SELECT * FROM requests WHERE id = ?", (rid,))
            if not row:
                raise ApiError(404, "No such request.")
            own = row["user_id"] == user["id"]
            if method == "POST" and parts[2:] == ["approve"]:
                admin()
                return 200, app.approve(user, rid), None
            if method == "POST" and parts[2:] == ["decline"]:
                admin()
                return 200, app.decline(user, rid), None
            if method == "POST" and parts[2:] in (["search"], ["give-up"]):
                if not (user["is_admin"] or own):
                    raise ApiError(403, "Only admins can do that to someone else's request.")
                if parts[2] == "search":
                    app.search_again([app.request_row(rid)])
                    return 200, app.request_row(rid), None
                return 200, app.give_up(user, rid), None
            if method == "DELETE" and len(parts) == 2:
                if not user["is_admin"] and not (row["user_id"] == user["id"] and row["status"] == "pending"):
                    raise ApiError(403, "You can only cancel your own pending requests.")
                app.db.run("DELETE FROM requests WHERE id = ?", (rid,))
                return 200, {"ok": True}, None

        # -- saved searches (each user their own; admins see all) --
        if key == ("GET", "watches"):
            return 200, {"watches": app.watches(user)}, None
        if key == ("POST", "watches") and len(parts) == 1:
            return 200, app.save_watch(user, self.body_json()), None
        if parts[:1] == ["watches"] and len(parts) >= 2 and parts[1].isdigit():
            wid = app.watch(user, int(parts[1]))["id"]
            if method == "PUT" and len(parts) == 2:
                return 200, app.save_watch(user, self.body_json(), wid), None
            if method == "DELETE" and len(parts) == 2:
                app.db.run("DELETE FROM watches WHERE id = ?", (wid,))
                return 200, {"ok": True}, None
            if method == "POST" and parts[2:] == ["check"]:
                return 200, app.check_watch(wid), None

        # -- account --
        if key == ("POST", "password"):
            body = self.body_json()
            full = app.db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
            if not check_password(body.get("current", ""), full["pw_salt"], full["pw_hash"]):
                raise ApiError(400, "Your current password is wrong.")
            app.set_password(user["id"], body.get("new", ""))
            return 200, {"ok": True}, {"Set-Cookie": self.session_cookie(app.new_session(user["id"]))}

        # -- admin: users and settings --
        if key == ("GET", "users"):
            admin()
            return 200, {"users": app.users()}, None
        if key == ("POST", "users") and len(parts) == 1:
            admin()
            body = self.body_json()
            app.create_user(body.get("username"), body.get("password"), bool(body.get("is_admin")),
                            bool(body.get("auto_approve")))
            return 200, {"users": app.users()}, None
        if parts[:1] == ["users"] and len(parts) == 2 and parts[1].isdigit():
            admin()
            uid = int(parts[1])
            target = app.db.one("SELECT * FROM users WHERE id = ?", (uid,))
            if not target:
                raise ApiError(404, "No such user.")
            admins = app.db.one("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1")["n"]
            if method == "PUT":
                body = self.body_json()
                if "is_admin" in body:
                    if target["is_admin"] and not body["is_admin"] and admins <= 1:
                        raise ApiError(400, "Keep at least one admin.")
                    app.db.run("UPDATE users SET is_admin = ? WHERE id = ?", (int(bool(body["is_admin"])), uid))
                if "auto_approve" in body:
                    app.db.run("UPDATE users SET auto_approve = ? WHERE id = ?",
                               (int(bool(body["auto_approve"])), uid))
                if body.get("password"):
                    app.set_password(uid, body["password"])
                return 200, {"users": app.users()}, None
            if method == "DELETE":
                if uid == user["id"]:
                    raise ApiError(400, "You can't delete yourself.")
                if target["is_admin"] and admins <= 1:
                    raise ApiError(400, "Keep at least one admin.")
                app.db.run("DELETE FROM users WHERE id = ?", (uid,))
                return 200, {"users": app.users()}, None
        if key == ("GET", "settings"):
            admin()
            return 200, app.public_settings(), None
        if key == ("PUT", "settings"):
            admin()
            return 200, app.save_settings(self.body_json()), None
        if key == ("POST", "notify-test"):
            admin()
            app.notify.test()
            return 200, {"ok": True}, None

        raise ApiError(404, "No such API route.")


def web_address(value):
    url = str(value or "").strip().rstrip("/")
    if url and not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", url):
        raise ApiError(400, f"{url} isn't a web address (http://... or https://...).")
    return url


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, app):
        self.app = app
        super().__init__(addr, handler)


class DualStackServer(Server):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def refresher(app):
    """Keep Whisparr's library index and queue fresh in the background, push about requests
    that arrived or wait too long, and run the saved searches that are due."""
    while True:
        try:
            app.whisparr.refresh_index()
            app.whisparr.refresh_queue()
            app.whisparr.refresh_followed()
            app.check_requests()
        except ApiError as e:
            app.whisparr.error = e.message
        except Exception as e:
            print(f"refresh failed: {e!r}", file=sys.stderr, flush=True)
        try:
            app.check_watches()
        except ApiError as e:
            print(f"saved search: {e.message}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"saved search failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(INDEX_MAX_AGE)


def init(app, user="", password="", stashdb_key="", jellyfin_key=""):
    """Set up the admin, the StashDB key and a Jellyfin API key from outside, e.g. from
    torrent-box's setup.

    The admin gets a login shared with other apps; a salted hash of it is kept, so a later
    run can tell whether that login changed (then the admin follows it) without storing it.
    An admin made on the first visit and a key set in Settings are never overwritten; a
    Jellyfin key given here before is replaced by a new one.
    Returns what was done, one line each.
    """
    db, done = app.db, []

    def remember():
        salt, digest = hash_password(f"{user}:{password}")
        db.set_settings({"managed_login": json.dumps({"user": user, "salt": salt, "hash": digest})})

    if user and password:
        managed = json.loads(db.setting("managed_login") or "null")
        own = db.one("SELECT * FROM users WHERE username = ?", (user,))
        if app.setup_needed():
            app.create_user(user, password, is_admin=True, auto_approve=True, min_password=1)
            remember()
            done.append(f"created the admin '{user}'")
        elif managed and managed["user"] == user and check_password(
                f"{user}:{password}", managed["salt"], managed["hash"]):
            done.append(f"admin '{user}': up to date")
        elif managed:
            # The shared login changed since the last run: the admin follows it.
            admin = db.one("SELECT * FROM users WHERE username = ?", (managed["user"],))
            if not admin:
                done.append(f"its admin '{managed['user']}' was deleted in Desireerr; not making it again")
            elif own and own["id"] != admin["id"]:
                done.append(f"can't rename its admin to '{user}': there's another user by that name")
            else:
                db.run("UPDATE users SET username = ?, is_admin = 1 WHERE id = ?", (user, admin["id"]))
                app.set_password(admin["id"], password, min_password=1)
                remember()
                done.append(f"admin login updated (user '{user}')")
        elif own and check_password(password, own["pw_salt"], own["pw_hash"]):
            db.run("UPDATE users SET is_admin = 1 WHERE id = ?", (own["id"],))
            remember()
            done.append(f"admin '{user}': up to date")
        else:
            done.append("has its own admin, made on its first visit; not changing it")
    saved = db.setting("stashdb_key")
    if stashdb_key and not saved:
        db.set_settings({"stashdb_key": stashdb_key})
        done.append("StashDB key: set")
    elif stashdb_key and saved != stashdb_key:
        done.append("StashDB key: keeps the one set in Desireerr")
    elif saved:
        done.append("StashDB key: up to date")
    else:
        done.append("no StashDB key yet: add it in Desireerr's Settings")
    if jellyfin_key:
        saved, given = db.setting("jellyfin_key"), db.setting("jellyfin_key_managed")
        if saved == jellyfin_key:
            done.append("Jellyfin key: up to date")
        elif not saved or saved == given:
            db.set_settings({"jellyfin_key": jellyfin_key, "jellyfin_key_managed": jellyfin_key})
            done.append("Jellyfin key: set (available scenes get a Watch button)")
        else:
            done.append("Jellyfin key: keeps the one set in Desireerr")
    if not app.setup_needed():
        (app.data_dir / "setup-code").unlink(missing_ok=True)
    return done


def cmd_init(argv):
    p = argparse.ArgumentParser(prog="desireerr.py init", description=(
        "Set up the admin and the StashDB key without the first-visit page. The secrets come "
        "from the environment: DESIREERR_ADMIN_USER, DESIREERR_ADMIN_PASSWORD, "
        "DESIREERR_STASHDB_KEY, DESIREERR_JELLYFIN_KEY (each optional)."))
    p.add_argument("--data", default=os.environ.get(
        "DESIREERR_DATA", Path.home() / ".local/state/desireerr"))
    args = p.parse_args(argv)
    app = App(args.data, None, setup_code=False)
    try:
        done = init(app, os.environ.get("DESIREERR_ADMIN_USER", "").strip(),
                    os.environ.get("DESIREERR_ADMIN_PASSWORD", ""),
                    os.environ.get("DESIREERR_STASHDB_KEY", "").strip(),
                    os.environ.get("DESIREERR_JELLYFIN_KEY", "").strip())
    except ApiError as e:
        print(f"desireerr init: {e.message}", file=sys.stderr)
        return 1
    print("\n".join(done))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:
        sys.exit(cmd_init(argv[1:]))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=int(os.environ.get("DESIREERR_PORT", 5056)))
    p.add_argument("--bind", default=os.environ.get("DESIREERR_BIND", "::"))
    p.add_argument("--data", default=os.environ.get(
        "DESIREERR_DATA", Path.home() / ".local/state/desireerr"))
    p.add_argument("--cache", default=os.environ.get(
        "DESIREERR_CACHE", Path.home() / ".cache/desireerr/images"))
    p.add_argument("--no-refresh", action="store_true", help="don't poll Whisparr in the background")
    args = p.parse_args(argv)

    app = App(args.data, args.cache)
    if not args.no_refresh:
        threading.Thread(target=refresher, args=(app,), daemon=True).start()
    cls = DualStackServer if ":" in args.bind else Server
    server = cls((args.bind, args.port), Handler, app)
    print(f"Desireerr {VERSION} on port {args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
