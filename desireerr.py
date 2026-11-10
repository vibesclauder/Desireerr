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
import errno
import hashlib
import itertools
import hmac
import json
import math
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

VERSION = "1.0"
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
NFO_BATCH = 50               # .nfo files written per background round at most
NFO_RETRY = 86400            # a scene StashDB didn't have is tried again after this long
NFO_MARK = "Desireerr"       # in the comment at the top of the .nfo files we write
SIMILAR_MAX = 24             # scenes per reel under a scene (see App.similar)
SIMILAR_FETCH = 30           # candidates per StashDB query for them
SIMILAR_PERFORMER = 5        # one of this scene's performers, in another scene
SIMILAR_FAV_PERFORMER = 10   # a favourite performer
SIMILAR_FAV_HERE = 25        # a favourite performer who's in this scene too
SIMILAR_FAVS = 200           # favourite performers asked about at most
SIMILAR_CAST = 6             # Together again: pairs from this much of the cast (15 pairs)
FEELINGS = {"love": 2, "like": 1, "dislike": -1, "hate": -2}  # a user's for a tag; else neutral
# Under a scene, what a tag of another scene adds: (as it is, if this scene has it too).
FEELING_SCORE = {2: (4, 10), 1: (2, 5), 0: (0, 1), -1: (-5, -5)}  # hate: never recommended
SIMILAR_TTL = 3600
DISCOVER_ROW = 20            # scenes in a Discover row
DISCOVER_TTL = 3600          # Discover's StashDB rows are kept this long (Reload: anew)
DISCOVER_POOL = 100          # "New with tags you love or like" picks from this many newest
# A tag's rarity scales the points it adds (see rarity): one on this many of StashDB's scenes
# counts as it is, a common one less (Blowjob, on a third of them, about a third), a rare one
# more, within these bounds. Each tag's scenes are counted every TAG_COUNT_TTL.
RARITY_TYPICAL = 30000
RARITY_MIN, RARITY_MAX = 0.25, 3.0
TAG_COUNT_TTL = 7 * 86400
# Performers in the taste score, by the user's feeling for them (FEELINGS, as for tags; a hated
# one: never recommended). Without one, a followed performer counts as liked, and one whose
# scenes they requested up to that, by how many and how lately (half after
# AFFINITY_HALF_LIFE). Several add up, unless the video is cut together from several scenes
# (MULTI_SCENE_TAGS): then only the best and the worst.
PERFORMER_SCORE = {2: 8, 1: 4, -1: -4}
PERFORMER_POINTS = PERFORMER_SCORE[1]
AFFINITY_HALF_LIFE = 365 * 86400
MULTI_SCENE_TAGS = {"3da5244d-b0f9-48af-b5e1-d46ecedb4308",   # Compilation
                    "d9ea7d4e-57ea-4315-9f21-57e98bf42457"}   # Multiple Scenes
# Similar performers (see App.similar_performers): how alike two profiles are, out of 100, by
# these weights (only what both have counts; a sparse profile loses some). A co-star of the
# performer gets COSTAR_BONUS more. The best LOOKALIKE_TASTED get their newest TASTE_SCENES
# scored by the user's taste too, which adds its average times the weight: a little on a
# performer's page, more on Discover's "Performers you might like".
LOOKALIKE = {"country": 25, "era": 20, "hair": 15, "age": 10, "ethnicity": 10, "height": 8,
             "breasts": 5, "cup": 4, "eyes": 3}
HAIR_FAMILY = {"BLACK": "dark", "BRUNETTE": "dark", "AUBURN": "red", "RED": "red"}
CUPS = {"AA": 0, "A": 1, "B": 2, "C": 3, "D": 4, "DD": 5, "E": 5, "DDD": 6, "F": 6, "FF": 7, "G": 7,
        "GG": 8, "H": 8, "HH": 9, "I": 9, "J": 10, "K": 11}
COSTAR_BONUS = 5
LOOKALIKE_PAGES = 3          # pages of 100 of the same country's (or ethnicity's) most popular
COSTAR_PAGES = 2             # pages of 100 of their co-stars
LOOKALIKE_TASTED = 30
TASTE_SCENES = 12
SIMILAR_PERFORMERS = 24
SIMILAR_TASTE_WEIGHT = 0.3   # on a performer's page: looks and era first
MIGHT_LIKE_TASTE_WEIGHT = 3.0  # an average taste is small (a few points a scene): this makes it count
MIGHT_LIKE_SEEDS = 10        # the performers loved most lately
MIGHT_LIKE_EACH = 3          # suggestions like any one of them, at most
TASTE_POOL = 500             # sorted by taste: this many of the newest matching scenes
TASTE_TTL = 600

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
    "write_nfo": "1",            # downloaded scenes' .nfo from StashDB, not from Whisparr
    "whisparr_nfo_off": "0",     # we turned Whisparr's .nfo writing off (and turn it back on)
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
CREATE TABLE IF NOT EXISTS tag_feelings (   -- StashDB has no tag favourites: ours, per user
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL,
    name TEXT NOT NULL,
    feeling INTEGER NOT NULL,                -- FEELINGS: 2 love, 1 like, -1 dislike, -2 hate
    created INTEGER NOT NULL,
    PRIMARY KEY (user_id, tag_id)
);
CREATE TABLE IF NOT EXISTS performer_feelings (  -- per user, as tag_feelings
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    performer_id TEXT NOT NULL,
    name TEXT NOT NULL,
    feeling INTEGER NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (user_id, performer_id)
);
CREATE TABLE IF NOT EXISTS tag_scenes (     -- how many StashDB scenes have each tag
    tag_id TEXT PRIMARY KEY,                 -- '': all of StashDB's scenes
    scenes INTEGER NOT NULL,
    checked INTEGER NOT NULL
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
    ("watches", "follow", "TEXT NOT NULL DEFAULT ''"),      # "performer:<id>": a follow
    ("users", "performer_sort", "TEXT NOT NULL DEFAULT 'TASTE'"),  # a performer's scenes: TASTE, DATE
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
        # Before the five feelings: favourite tags (now liked ones) and, briefly, disliked ones.
        for old, feeling in (("favorite_tags", 1), ("disliked_tags", -1)):
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                                 (old,)).fetchone():
                with self.conn:
                    self.conn.execute("BEGIN")
                    self.conn.execute(
                        "INSERT OR IGNORE INTO tag_feelings(user_id, tag_id, name, feeling, created) "
                        f"SELECT user_id, tag_id, name, ?, created FROM {old}", (feeling,))
                    self.conn.execute(f"DROP TABLE {old}")

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
  tags { id }
}"""

SCENE_FULL = SCENE_CARD + """
fragment SceneFull on Scene {
  ...SceneCard
  details code director production_date
  urls { url site { name } }
  studio { id name parent { id name } }
  tags { id name description category { id name group } }
  performers { as performer { id name disambiguation gender birth_date death_date
    images { url width height } } }
}"""

PERFORMER_CARD = """
fragment PerformerCard on Performer {
  id name disambiguation gender scene_count is_favorite
  images { url width height }
}"""

PERFORMER_FULL = PERFORMER_CARD + """
fragment PerformerFull on Performer {
  ...PerformerCard
  aliases birth_date death_date age country ethnicity hair_color eye_color height
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

PERFORMER_PROFILE = """
fragment PerformerProfile on Performer {
  id name disambiguation gender scene_count
  images { url width height }
  country ethnicity hair_color eye_color height cup_size breast_type career_start_year birth_date
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

    def query_scenes_many(self, inputs, ttl=300):
        """Several scene queries in one request, aliased; their scene lists in order."""
        if not inputs:
            return []
        args = ", ".join(f"$i{n}: SceneQueryInput!" for n in range(len(inputs)))
        body = "\n".join(f"a{n}: queryScenes(input: $i{n}) {{ scenes {{ ...SceneCard }} }}"
                         for n in range(len(inputs)))
        data = self.query(SCENE_CARD + f"\nquery ({args}) {{\n{body}\n}}",
                          {f"i{n}": inp for n, inp in enumerate(inputs)}, ttl)
        return [data[f"a{n}"]["scenes"] for n in range(len(inputs))]

    def scene_counts(self, tag_ids, ttl=0):
        """How many scenes have each tag ({tag ID: count}); with None, all of them ({None: count})."""
        found = {}
        for start in range(0, len(tag_ids), 50):
            chunk = tag_ids[start:start + 50]
            args = ", ".join(f"$i{n}: SceneQueryInput!" for n in range(len(chunk)))
            body = "\n".join(f"a{n}: queryScenes(input: $i{n}) {{ count }}" for n in range(len(chunk)))
            inputs = {f"i{n}": dict({"page": 1, "per_page": 1}, **(
                {"tags": {"value": [t], "modifier": "INCLUDES"}} if t else {}))
                for n, t in enumerate(chunk)}
            data = self.query(f"query ({args}) {{\n{body}\n}}", inputs, ttl)
            found.update((t, data[f"a{n}"]["count"]) for n, t in enumerate(chunk))
        return found

    def tag_ids(self):
        """Every tag's ID."""
        ids, page = [], 1
        while True:
            q = "query ($input: TagQueryInput!) { queryTags(input: $input) { count tags { id } } }"
            got = self.query(q, {"input": {"page": page, "per_page": 100, "sort": "NAME"}}, 0)["queryTags"]
            ids += [t["id"] for t in got["tags"]]
            if not got["tags"] or len(ids) >= got["count"]:
                return ids
            page += 1

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

    def performer_profiles(self, perf_input, ttl=3600):
        q = PERFORMER_PROFILE + """
query ($input: PerformerQueryInput!) {
  queryPerformers(input: $input) { count performers { ...PerformerProfile } }
}"""
        return self.query(q, {"input": perf_input}, ttl)["queryPerformers"]["performers"]

    def performer_profile(self, pid, ttl=3600):
        found = self.find_many("findPerformer", [pid], "{ ...PerformerProfile }", PERFORMER_PROFILE, ttl)
        return found[0] if found else None

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
        if kind == "studio":     # its page and the lists show is_favorite; a performer's, nowhere
            with self.lock:
                self.cache.clear()


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

    def followed_here(self):
        """The performers and studios Whisparr itself follows (monitors for new scenes):
        [(kind, item)]. Desireerr's follows are saved searches; see App.adopt_whisparr_follows."""
        return [(kind, item) for kind in ("performer", "studio")
                for item in self.call("GET", kind, timeout=60) or [] if item.get("monitored")]

    def stop_following(self, kind, item):
        self.call("PUT", f"{kind}/{item['id']}", dict(item, monitored=False))

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

    def set_metadata(self, on):
        """Whisparr's Kodi (XBMC) / Emby metadata (its .nfo and screenshot.jpg) off, or back
        on with both. True if it changed anything; one that was off stays off."""
        changed = False
        for m in self.call("GET", "metadata") or []:
            if m.get("implementation") != "XbmcMetadata":
                continue
            fields = [f for f in m.get("fields", []) if f.get("name") in ("movieMetadata", "movieImages")]
            if not on and m.get("enable"):
                m["enable"] = False
            elif on and not (m.get("enable") and all(f.get("value") for f in fields)):
                m["enable"] = True
                for f in fields:
                    f["value"] = True
            else:
                continue
            self.call("PUT", f"metadata/{m['id']}", body=m)
            changed = True
        return changed

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

    def call(self, path, timeout=20, method="GET"):
        url, key = self.creds()
        req = urllib.request.Request(f"{url}/{path}", method=method,
                                     data=b"" if method == "POST" else None, headers={
            "Authorization": f'MediaBrowser Token="{key}"', "User-Agent": USER_AGENT})
        try:
            with timed("jellyfin"), urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw) if raw.strip() else None
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

    def refresh_item(self, item_id):
        """Jellyfin looks at the scene's folder again (its cover; a screenshot.jpg gone), then
        reads the item's .nfo again and fetches the cast's photos it names."""
        found = self.call(f"Items?Ids={item_id}&Fields=ParentId") or {}
        parent = ((found.get("Items") or [{}])[0]).get("ParentId")
        if parent:
            self.call(f"Items/{parent}/Refresh?Recursive=true&metadataRefreshMode=Default"
                      "&imageRefreshMode=Default", method="POST")
        self.call(f"Items/{item_id}/Refresh?metadataRefreshMode=FullRefresh"
                  "&imageRefreshMode=FullRefresh&replaceAllMetadata=true&replaceAllImages=false",
                  method="POST")


# ---------------------------------------------------------------------------------------
# .nfo files for Jellyfin: a downloaded scene's details from StashDB, the performers as its
# cast with their photos. Whisparr's own lack the photos and list the performers twice (also
# as writers), so with write_nfo Whisparr's metadata is turned off and ours replaces it. The
# cover is folder.jpg: Jellyfin takes it as the folder's and the video's picture, where
# Whisparr's screenshot.jpg shows up as a photo of its own beside the video.
# ---------------------------------------------------------------------------------------

COVER_NAMES = ("folder.jpg", "folder.jpeg", "folder.png", "folder.webp")
COVER_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def has_cover(folder):
    return any(os.path.exists(os.path.join(folder, n)) for n in COVER_NAMES)


def scene_cover(scene):
    """The scene's picture for its cover: its biggest landscape one, as StashDB's cards."""
    images = scene.get("images") or []
    if not images:
        return ""
    best = max(images, key=lambda i: ((i.get("width") or 0) >= (i.get("height") or 0),
                                      (i.get("width") or 0) * (i.get("height") or 0)))
    return best["url"]

def nfo_path(video):
    return os.path.splitext(video)[0] + ".nfo"


def is_ours(nfo):
    try:
        with open(nfo, encoding="utf-8", errors="replace") as f:
            return f"<!-- {NFO_MARK}" in f.read(400)
    except OSError:
        return False


def scene_nfo(scene):
    """The .nfo Kodi and Jellyfin read for a movie, for a StashDB scene."""
    root = ET.Element("movie")

    def add(tag, text, **attrs):
        if text not in (None, ""):
            ET.SubElement(root, tag, attrs).text = str(text)

    add("title", scene.get("title") or "Untitled")
    add("plot", scene.get("details"))
    released = scene.get("release_date") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", released):
        add("premiered", released)
    add("year", released[:4] if re.match(r"\d{4}", released) else "")
    if scene.get("duration"):
        add("runtime", round(scene["duration"] / 60))
    add("studio", (scene.get("studio") or {}).get("name"))
    add("director", scene.get("director"))
    for tag in scene.get("tags") or []:
        add("genre", tag.get("name"))
    add("uniqueid", scene.get("id"), type="stashdb", default="true")
    for order, appearance in enumerate(scene.get("performers") or []):
        performer = appearance.get("performer") or {}
        if not performer.get("name"):
            continue
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = performer["name"]
        if appearance.get("as"):
            ET.SubElement(actor, "role").text = appearance["as"]
        images = performer.get("images") or []
        if images:  # StashDB lists a performer's main photo first
            ET.SubElement(actor, "thumb").text = images[0]["url"] + "?size=600"
        ET.SubElement(actor, "order").text = str(order)
    ET.indent(root)
    return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
            f"<!-- {NFO_MARK} wrote this from StashDB ({STASHDB_WEB}/scenes/{scene.get('id')}) "
            "and rewrites it; edits here don't last. -->\n"
            + ET.tostring(root, encoding="unicode") + "\n")


def write_cover(folder, fetch, url):
    """folder.jpg: Whisparr's screenshot.jpg renamed, or else the scene's picture from StashDB
    (fetch(url) -> (bytes, content type)). A screenshot.jpg beside a cover goes."""
    shot = os.path.join(folder, "screenshot.jpg")
    if not has_cover(folder):
        if os.path.exists(shot):
            os.replace(shot, os.path.join(folder, "folder.jpg"))
        elif url:
            data, ctype = fetch(url + "?size=1280")
            ext = COVER_TYPES.get(ctype)
            if ext:
                tmp = os.path.join(folder, f"folder.{ext}.desireerr-tmp")
                with open(tmp, "wb") as f:
                    f.write(data)
                os.chmod(tmp, 0o664)
                os.replace(tmp, os.path.join(folder, f"folder.{ext}"))
    if has_cover(folder) and os.path.exists(shot):
        os.remove(shot)


def write_nfo(video, text):
    """Write the .nfo beside the video (atomically). Ours left from the scene's earlier file,
    e.g. before an upgrade, go."""
    nfo = nfo_path(video)
    tmp = f"{nfo}.desireerr-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o664)
    os.replace(tmp, nfo)
    folder = os.path.dirname(video)
    with contextlib.suppress(OSError):
        for name in os.listdir(folder):
            other = os.path.join(folder, name)
            if name.endswith(".nfo") and other != nfo and is_ours(other):
                stem = os.path.splitext(name)[0]
                if not any(n != name and os.path.splitext(n)[0] == stem for n in os.listdir(folder)):
                    os.remove(other)


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


def rarity(scenes, total):
    """How much a tag on SCENES of StashDB's TOTAL scenes weighs: 1 for one on RARITY_TYPICAL
    of them, by the log of how rare it is, within RARITY_MIN and RARITY_MAX."""
    if total <= RARITY_TYPICAL:
        return 1.0
    factor = math.log(total / max(scenes, 1)) / math.log(total / RARITY_TYPICAL)
    return min(RARITY_MAX, max(RARITY_MIN, factor))


def weigh_tags(tags, feel, mine=frozenset(), rare=None):
    """What a scene's TAGS add by the user's feelings for them (FEEL: tag ID -> feeling), as
    FEELING_SCORE has it, MINE being the tags of the scene it's compared with, the loved, liked
    and neutral ones by their rarity (RARE: tag ID -> rarity, 1 if not there; a disliked one
    weighs the same however common): (what its loved and liked tags add, what its neutral ones
    add, what its disliked ones take off, whether it has a hated one)."""
    rare = rare or {}
    liked = neutral = disliked = 0
    for t in set(tags):
        f = feel.get(t, 0)
        if f == FEELINGS["hate"]:
            return 0, 0, 0, True
        points = FEELING_SCORE[f][t in mine]
        if f >= 0:
            points *= rare.get(t, 1.0)
        if f > 0:
            liked += points
        elif f < 0:
            disliked -= points
        else:
            neutral += points
    return liked, neutral, disliked, False


def recommendable(weights, outweigh=True):
    """Never with a hated tag; with a disliked one only if OUTWEIGH and the loved and liked ones
    outweigh it."""
    liked, _, disliked, hated = weights
    return not hated and (not disliked or outweigh and liked > disliked)


def taste(weights, performers=0):
    """A scene's taste points, from weigh_tags: what its loved and liked tags add less what its
    disliked ones take off, plus what its PERFORMERS add (see performer_points); None with a
    hated tag or performer."""
    liked, _, disliked, hated = weights
    return None if hated or performers is None else round(liked - disliked + performers)


def performer_points(people, tags, perf):
    """What a scene's PEOPLE (performer IDs) add by PERF (performer ID -> (points, why), points
    None for a hated one): all of them, or in a video of several scenes (TAGS has one of
    MULTI_SCENE_TAGS) the best one and the worst one. None with a hated one."""
    values = [perf[p][0] for p in set(people) if p in perf]
    if None in values:
        return None
    if not MULTI_SCENE_TAGS & set(tags):
        return sum(values)
    return max([v for v in values if v > 0], default=0) + min([v for v in values if v < 0], default=0)


def lookalike(a, b):
    """How alike performers A and B look and when they worked, 0 to 100 (see LOOKALIKE)."""
    def year(p, key):
        v = p.get(key)
        return int(str(v)[:4]) if v and str(v)[:4].isdigit() else None

    def near(x, y, span):
        return None if x is None or y is None else max(0.0, 1 - abs(x - y) / span)

    def same(x, y):
        return None if not x or not y else float(x == y)

    def hair(x, y):
        if not x or not y or "VARIOUS" in (x, y) or "OTHER" in (x, y):
            return None
        if x == y:
            return 1.0
        fam = {HAIR_FAMILY.get(x), HAIR_FAMILY.get(y)}
        return 0.7 if len(fam) == 1 and None not in fam else 0.4 if fam == {"dark", "red"} else 0.0
    parts = {
        "country": same(a.get("country"), b.get("country")),
        "era": near(year(a, "career_start_year"), year(b, "career_start_year"), 6),
        "hair": hair(a.get("hair_color"), b.get("hair_color")),
        "age": near(year(a, "birth_date"), year(b, "birth_date"), 6),
        "ethnicity": same(a.get("ethnicity"), b.get("ethnicity")),
        "height": near(a.get("height"), b.get("height"), 12),
        "breasts": same(a.get("breast_type"), b.get("breast_type")),
        "cup": near(CUPS.get((a.get("cup_size") or "").upper()), CUPS.get((b.get("cup_size") or "").upper()), 3),
        "eyes": same(a.get("eye_color"), b.get("eye_color")),
    }
    known = sum(LOOKALIKE[k] for k, v in parts.items() if v is not None)
    if known < 40:
        return 0.0
    matched = sum(LOOKALIKE[k] * v for k, v in parts.items() if v is not None)
    return 100 * matched / known * (0.6 + 0.4 * known / sum(LOOKALIKE.values()))


def card_people(sc):
    return [p["performer"]["id"] for p in sc.get("performers") or [] if p.get("performer")]


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
        self.nfo_missing = {}    # scene id -> when StashDB didn't have it
        self.discover_cache = {}  # user id -> (when made, {row key: row}), see discover
        self.discover_lock = threading.Lock()
        self.rare = None         # tag id -> rarity, from tag_scenes (see count_tags)
        self.nfo_error = ""
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
            "SELECT u.id, u.username, u.is_admin, u.auto_approve, u.performer_sort FROM sessions s "
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
    def annotate(self, scenes, user=None):
        """Copies of SCENES with their request state, from Whisparr and our own requests, and
        with a USER who has tag or performer feelings, their "taste" (see taste; "hated" if
        None). Without their tags: the page doesn't need them."""
        scenes = [dict(s) for s in scenes if s]
        if not scenes:
            return scenes
        feel, perf = self.feelings(user), self.performer_taste(user)
        if feel or perf:
            rare, tags = self.rarities(), self.card_tags(scenes)
            for s in scenes:
                have = tags.get(s["id"], ())
                s["taste"] = taste(weigh_tags(have, feel, rare=rare),
                                   performer_points(card_people(s), have, perf))
                s["hated"] = s["taste"] is None
        for s in scenes:
            s.pop("tags", None)
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
        if q.get("sort") == "TASTE":
            return self.by_taste(q, inp, favs, user)
        result, scenes = self.filtered_scenes(q, inp, favs)
        return {"count": result["count"], "scenes": self.annotate(scenes, user), "page": inp["page"],
                "per_page": inp["per_page"]}

    def by_taste(self, q, inp, favs, user):
        """A page of the newest TASTE_POOL scenes that match, by the user's taste for them (see
        taste), the newest first among equals, those with a hated tag last."""
        pool, page, count = [], 1, 0
        while len(pool) < TASTE_POOL:
            got = self.stash.query_scenes(dict(inp, page=page, per_page=PER_PAGE_MAX, sort="DATE",
                                               direction="DESC"), TASTE_TTL)
            pool += got["scenes"]
            count = got["count"]
            if len(got["scenes"]) < PER_PAGE_MAX:
                break
            page += 1
        scenes = self.annotate(self.check_tags(q, pool[:TASTE_POOL], favs), user)
        scenes.sort(key=lambda sc: (sc.get("hated", False), -(sc.get("taste") or 0)))
        start = (inp["page"] - 1) * inp["per_page"]
        return {"count": len(scenes), "scenes": scenes[start:start + inp["per_page"]],
                "page": inp["page"], "per_page": inp["per_page"], "newest": count > TASTE_POOL and TASTE_POOL}

    def filtered_scenes(self, q, inp, favs=(), ttl=300):
        """StashDB's answer to a scene query, and its scenes that pass the tag checks StashDB
        couldn't take (see tag_plan)."""
        result = self.stash.query_scenes(inp, ttl)
        return result, self.check_tags(q, result["scenes"], favs)

    def check_tags(self, q, scenes, favs=()):
        """SCENES that pass the tag checks StashDB couldn't take (see tag_plan)."""
        _, need_one, need_all, not_tags = self.tag_plan(q, favs)
        if need_one or need_all or not_tags:
            # So a page can come back short.
            full = {sid: set(t) for sid, t in self.card_tags(scenes).items()}
            need_one, need_all, not_tags = set(need_one), set(need_all), set(not_tags)

            def ok(sc):
                have = full.get(sc["id"], set())
                return ((not need_one or have & need_one) and need_all <= have
                        and not have & not_tags)
            scenes = [sc for sc in scenes if ok(sc)]
        return scenes

    # -- tag feelings (Desireerr's own, per user): love, like, dislike, hate ----------------
    def tag_feelings(self, user):
        """The user's tags with a feeling (none is neutral), with the feeling's name."""
        names = {v: k for k, v in FEELINGS.items()}
        rows = self.db.all("SELECT tag_id AS id, name, feeling FROM tag_feelings WHERE user_id = ? "
                           "ORDER BY name COLLATE NOCASE", (user["id"],))
        return [dict(r, feeling=names[r["feeling"]]) for r in rows]

    def feelings(self, user):
        """tag ID -> feeling (2 love ... -2 hate); {} without a user."""
        if not user:
            return {}
        return {r["tag_id"]: r["feeling"] for r in self.db.all(
            "SELECT tag_id, feeling FROM tag_feelings WHERE user_id = ?", (user["id"],))}

    def fav_tags(self, user):
        """The favourite tags (a filter on Discover and Scenes): the loved and liked ones."""
        return [{"id": t["id"], "name": t["name"]} for t in self.tag_feelings(user)
                if FEELINGS[t["feeling"]] > 0]

    def fav_tag_ids(self, user):
        return [t["id"] for t in self.fav_tags(user)]

    def set_tag_feeling(self, user, tag_id, feeling):
        """FEELING: a FEELINGS name, or "neutral" for none."""
        if feeling == "neutral":
            self.db.run("DELETE FROM tag_feelings WHERE user_id = ? AND tag_id = ?", (user["id"], tag_id))
        else:
            tag = self.stash.find_tag(tag_id)
            if not tag:
                raise ApiError(404, "StashDB has no tag with that ID.")
            self.db.run("INSERT INTO tag_feelings(user_id, tag_id, name, feeling, created) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(user_id, tag_id) DO UPDATE SET feeling = excluded.feeling",
                        (user["id"], tag_id, tag["name"], FEELINGS[feeling], now()))
        # Their saved searches with favourite tags look through the matches again.
        self.db.run("UPDATE watches SET cursor = '' WHERE user_id = ? AND query LIKE '%fav_tags=1%'",
                    (user["id"],))
        self.forget_discover(user)
        return self.tag_feelings(user)

    def rarities(self):
        """Each tag's rarity (tag ID -> rarity), as counted by count_tags; until then none."""
        if self.rare is None:
            rows = self.db.all("SELECT tag_id, scenes FROM tag_scenes")
            counts = {r["tag_id"]: r["scenes"] for r in rows}
            total = counts.pop("", 0)
            self.rare = {t: rarity(n, total) for t, n in counts.items()} if total else {}
        return self.rare

    def count_tags(self, now=None):
        """Count how many StashDB scenes have each tag, all of them anew every TAG_COUNT_TTL
        (a tag made since weighs 1 until then). Returns how many were counted."""
        now = int(now or time.time())
        last = self.db.one("SELECT checked FROM tag_scenes WHERE tag_id = ''")
        if last and last["checked"] > now - TAG_COUNT_TTL:
            return 0
        ids = self.stash.tag_ids()
        counts = self.stash.scene_counts([None] + ids)
        with self.db.lock, self.db.conn:
            self.db.conn.execute("BEGIN")
            self.db.conn.execute("DELETE FROM tag_scenes")
            self.db.conn.executemany(
                "INSERT INTO tag_scenes (tag_id, scenes, checked) VALUES (?, ?, ?)",
                [(t or "", counts[t], now) for t in [None] + ids])
        self.rare = None
        return len(ids)

    def card_tags(self, scenes):
        """Scene ID -> its tag IDs: from the cards (SceneCard has them), else asked for. If
        StashDB can't say, none."""
        found = {sc["id"]: [t["id"] for t in sc["tags"]] for sc in scenes if "tags" in sc}
        rest = [sc["id"] for sc in scenes if sc["id"] not in found]
        if rest:
            try:
                found.update(self.scene_tags(rest))
            except ApiError:
                pass
        return found

    def scene_tags(self, ids):
        found = self.stash.find_many("findScene", ids, "{ id tags { id } }")
        return {s["id"]: [t["id"] for t in s["tags"]] for s in found}

    def names(self, q):
        out = {}
        for kind in ("tags", "performers", "studios"):
            ids = id_list(q.get(kind))
            out[kind] = self.stash.names(kind, ids) if ids else {}
        return out

    def discover(self, user, fresh=False):
        """Discover's rows. StashDB's are kept for DISCOVER_TTL per user (until their tag
        feelings, the favourites or the follows change); FRESH makes them again, StashDB asked
        anew. The scenes' states, the user's requests and Recently added are always current."""
        ttl = 0 if fresh else 300
        specs = [
            ("trending", "Trending", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": DISCOVER_ROW, "sort": "TRENDING", "direction": "DESC"}, ttl)["scenes"],
             "/scenes?sort=TRENDING"),
            ("favorites", "New scenes with favourite performers", lambda: self.new_with_favourites(user, ttl),
             None),
            ("favtags", "New with tags you love or like", lambda: self.new_with_loved_tags(user, ttl),
             "/scenes?fav_tags=1"),
            ("performers", "Performers you might like", lambda: self.might_like(user, ttl), None),
            ("added", "Recently added", self.recent_library, None),
            ("new", "New releases", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": DISCOVER_ROW, "sort": "DATE", "direction": "DESC",
                 "date": {"value": tomorrow(), "modifier": "LESS_THAN"}}, ttl)["scenes"],
             "/scenes?sort=DATE"),
            ("popular", "Popular", lambda: self.stash.query_scenes(
                {"page": 1, "per_page": DISCOVER_ROW, "sort": "POPULARITY", "direction": "DESC"}, ttl)["scenes"],
             "/scenes?sort=POPULARITY"),
        ]
        with self.discover_lock:
            made, kept = self.discover_cache.get(user["id"], (0, {}))
            if fresh or time.time() - made > DISCOVER_TTL:
                made, kept = time.time(), {}
        timing = Timing.current()

        def row(spec):
            key, title, fn, link = spec
            Timing.begin(timing)  # this thread works for the same request
            try:
                return {"key": key, "title": title, "link": link, "scenes": fn()}
            except ApiError as e:
                return {"key": key, "title": title, "link": link, "scenes": [], "error": e.message}

        # The rows wait on StashDB, not on us: ask for all of them at once. Whisparr's
        # state is brought up to date first, so the rows don't each refresh it.
        self.whisparr.ensure_fresh()
        todo = [spec for spec in specs if spec[0] not in kept]
        with ThreadPoolExecutor(max(1, len(todo))) as pool:
            rows = {r["key"]: r for r in pool.map(row, todo)}
        keep = {k: r for k, r in rows.items() if k != "added" and not r.get("error")}
        with self.discover_lock:
            self.discover_cache[user["id"]] = (made, {**kept, **keep})
        rows.update(kept)
        out = [dict(rows[key], scenes=self.annotate(rows[key]["scenes"], user)) if key != "performers"
               else dict(rows[key], performers=rows[key]["scenes"], scenes=[])
               for key, *_ in specs]
        mine = self.db.all("SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT 12",
                           (user["id"],))
        for r in mine:
            self.decorate(r)
        return {"rows": out, "requests": mine, "followed": self.followed(user),
                "whisparr_error": self.whisparr.error, "updated": made}

    def forget_discover(self, user=None):
        """Discover's rows are made again next time: the user's, or (no USER) everyone's."""
        with self.discover_lock:
            if user:
                self.discover_cache.pop(user["id"], None)
            else:
                self.discover_cache.clear()

    def new_with_favourites(self, user, ttl=300):
        """Discover's row: the newest scenes with the favourite performers (see
        favourite_performers), none with a tag the user hates or dislikes."""
        favs = self.favourite_performers(user)
        if not favs:
            return []
        scenes = self.stash.query_scenes(
            {"page": 1, "per_page": DISCOVER_ROW + 10, "sort": "DATE", "direction": "DESC",
             "performers": {"value": sorted(favs)[:SIMILAR_FAVS], "modifier": "INCLUDES"},
             "date": {"value": tomorrow(), "modifier": "LESS_THAN"}}, ttl)["scenes"]
        return self.by_feelings(scenes, user, rank=False)[:DISCOVER_ROW]

    def new_with_loved_tags(self, user, ttl=300):
        """Discover's row: among the newest DISCOVER_POOL scenes with any tag the user loves or
        likes, the ones those weigh most in (as under a scene: a loved tag 4, a liked one 2, a
        disliked one -5; one with a hated tag, or more disliked than loved and liked, not at
        all), the newest first among equals."""
        favs = self.fav_tag_ids(user)
        if not favs:
            return []
        scenes = self.stash.query_scenes(
            {"page": 1, "per_page": DISCOVER_POOL, "sort": "DATE", "direction": "DESC",
             "tags": {"value": favs, "modifier": "INCLUDES"},
             "date": {"value": tomorrow(), "modifier": "LESS_THAN"}}, ttl)["scenes"]
        return self.by_feelings(scenes, user)[:DISCOVER_ROW]

    def by_feelings(self, scenes, user, rank=True):
        """SCENES without those the user's tag and performer feelings rule out (with RANK, a disliked tag can be
        outweighed), best first if RANK (copies, with their "points"), else in their order. If
        StashDB can't say their tags, as they are."""
        feel, perf = self.feelings(user), self.performer_taste(user)
        hated = {p for p, (points, _) in perf.items() if points is None}
        scenes = [sc for sc in scenes if not hated & set(card_people(sc))]
        if not feel:
            return scenes
        tags = self.card_tags(scenes)
        rare = self.rarities()
        weights = {sc["id"]: weigh_tags(tags.get(sc["id"], ()), feel, rare=rare) for sc in scenes}
        kept = [sc for sc in scenes if recommendable(weights[sc["id"]], outweigh=rank)]
        if rank:  # sorted is stable: the newest first among equals
            points = {sc["id"]: taste(weights[sc["id"]], performer_points(
                card_people(sc), tags.get(sc["id"], ()), perf)) for sc in kept}
            kept = sorted(kept, key=lambda sc: -points[sc["id"]])
        return kept

    def recent_library(self):
        self.whisparr.ensure_fresh()
        ids = self.whisparr.recently_added(20)
        order = {sid: i for i, sid in enumerate(ids)}
        return sorted(self.stash.find_scenes(ids), key=lambda s: order.get(s["id"], 99))

    def scene(self, scene_id, user=None):
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
        feel, perf = self.feelings(user), self.performer_taste(user)
        if feel or perf:
            scene = dict(scene)  # StashDB's answer is cached
            rare = self.rarities()
            tags = [t["id"] for t in scene.get("tags") or []]
            parts, names = [], {v: k for k, v in FEELINGS.items()}
            for t in scene.get("tags") or []:
                points = weigh_tags([t["id"]], feel, rare=rare)
                if feel.get(t["id"], 0):
                    parts.append({"kind": "tag", "id": t["id"], "name": t["name"], "feeling": names[feel[t["id"]]],
                                  "points": None if points[3] else round(points[0] - points[2], 1)})
            for p in scene.get("performers") or []:
                pid = p["performer"]["id"]
                if pid in perf and pid not in {x["id"] for x in parts}:
                    points, why = perf[pid]
                    parts.append({"kind": "performer", "id": pid, "name": p.get("as") or p["performer"]["name"],
                                  "why": why, "points": points})
            scene["taste"] = taste(weigh_tags(tags, feel, rare=rare), performer_points(card_people(scene), tags, perf))
            scene["taste_parts"] = sorted(parts, key=lambda t: -abs(t["points"] if t["points"] is not None else 99))
            scene["taste_several"] = bool(MULTI_SCENE_TAGS & set(tags))  # performers: best and worst only
        return scene

    def similar(self, scene_id, user=None):
        """The reels under a scene.

        With favourite performers: scenes with any of them (StashDB favourites and those
        the user follows): SIMILAR_FAV_HERE for each who's in this scene too,
        SIMILAR_FAV_PERFORMER for the others, plus the tag score. Discover: scenes with none
        of them (so never the same scene twice): SIMILAR_PERFORMER for each of this scene's
        performers, plus the tag score: what the other scene's tags add by the user's
        feeling for them, FEELING_SCORE (a loved one 4, 10 if this scene has it too; liked 2
        or 5; neutral 0 or 1; disliked -5), times the tag's rarity unless disliked (so the
        common ones don't drown the rest). A scene with a hated tag is never recommended;
        one with a disliked tag never with the favourite performers, and in Discover only if
        its loved and liked tags outweigh its disliked ones. Both look among StashDB's most
        popular scenes with this one's tags (its ACTION and SCENE tags, loved, then liked
        ones first, the rarest first among them, never disliked or hated ones; all of the first
        three, fewer if that finds too little), and among those of the performers that count.
        Together again: other scenes with two or more of this cast, the most of them first,
        then the newest; they're left out of the other two. Popularity breaks ties.
        """
        scene = self.stash.find_scene(scene_id)
        if not scene:
            raise ApiError(404, "StashDB has no scene with that ID.")
        tags = scene.get("tags") or []
        mine = {t["id"] for t in tags}
        feel = self.feelings(user)
        action = [t["id"] for t in tags if (t.get("category") or {}).get("group") in ("ACTION", "SCENE")]
        action = [t for t in action or [t["id"] for t in tags] if feel.get(t, 0) >= 0]
        rare = self.rarities()
        action.sort(key=lambda t: (-feel.get(t, 0), -rare.get(t, 1.0)))
        cast = list(dict.fromkeys(p["performer"]["id"] for p in scene.get("performers") or []))
        perf = self.performer_taste(user)
        hated = {p for p, (points, _) in perf.items() if points is None}
        less = {p: points for p, (points, _) in perf.items() if points is not None and points < 0}
        favs = self.favourite_performers(user)

        def people(sc):
            return {p["performer"]["id"] for p in sc.get("performers") or []}

        def gather(criteria, keep):
            """Candidates: the most popular scenes with this one's first tags (and criteria)."""
            found = {}
            steps = [action[:n] for n in (3, 2, 1) if len(action) >= n]
            if criteria:
                steps.append([])  # then any of theirs
            for step in steps:
                inp = dict({"page": 1, "per_page": SIMILAR_FETCH, "sort": "POPULARITY",
                            "direction": "DESC"}, **criteria)
                if step:
                    inp["tags"] = {"value": step, "modifier": "INCLUDES_ALL"}
                try:
                    scenes = self.stash.query_scenes(inp, SIMILAR_TTL)["scenes"]
                except ApiError as e:
                    print(f"similar scenes for {scene_id}: {e.message}", file=sys.stderr, flush=True)
                    continue
                for sc in scenes:
                    if sc["id"] != scene_id and keep(sc):
                        found.setdefault(sc["id"], sc)
                if len(found) >= SIMILAR_MAX:
                    break
            return found

        def together():
            pairs = list(itertools.combinations(cast[:SIMILAR_CAST], 2))
            try:
                answers = self.stash.query_scenes_many(
                    [{"page": 1, "per_page": SIMILAR_FETCH, "sort": "DATE", "direction": "DESC",
                      "performers": {"value": list(pair), "modifier": "INCLUDES_ALL"}} for pair in pairs],
                    SIMILAR_TTL)
            except ApiError as e:
                print(f"together again for {scene_id}: {e.message}", file=sys.stderr, flush=True)
                return []
            found = {sc["id"]: sc for scenes in answers for sc in scenes
                     if sc["id"] != scene_id and not people(sc) & hated}
            newest = sorted(found.values(), key=lambda sc: sc.get("release_date") or "", reverse=True)
            return sorted(newest, key=lambda sc: -len(people(sc) & set(cast)))

        timing = Timing.current()

        def run(fn):
            Timing.begin(timing)
            return fn()

        def with_(ids):
            return {"performers": {"value": sorted(ids)[:SIMILAR_FAVS], "modifier": "INCLUDES"}}

        def fav(sc):
            return people(sc) & favs

        def no_fav(sc):
            return not people(sc) & favs
        favs_here, others_here = set(cast) & favs, set(cast) - favs
        jobs = [lambda: gather(with_(favs_here), fav) if favs_here else {},
                lambda: gather(with_(favs), fav) if favs else {},
                lambda: gather(with_(others_here), no_fav) if others_here else {},
                lambda: gather({}, no_fav),
                together]
        with ThreadPoolExecutor(len(jobs)) as pool:
            inner_here, inner, outer_here, outer, again = pool.map(run, jobs)
        inner, outer = {**inner_here, **inner}, {**outer_here, **outer}
        again = again[:SIMILAR_MAX]
        for sid in {sc["id"] for sc in again}:
            inner.pop(sid, None)
            outer.pop(sid, None)
        their_tags = self.card_tags(list({**inner, **outer}.values()))

        weights = {sid: weigh_tags(their_tags.get(sid, ()), feel, mine, rare) for sid in {**inner, **outer}}

        def tag_score(sc):
            liked, neutral, disliked, _ = weights[sc["id"]]
            return liked + neutral - disliked
        inner = {sid: sc for sid, sc in inner.items()
                 if recommendable(weights[sid], outweigh=False) and not people(sc) & hated}
        outer = {sid: sc for sid, sc in outer.items() if recommendable(weights[sid]) and not people(sc) & hated}

        def ranked(found, score):
            """Best first, copies with their "points" (StashDB's cards are cached, shared)."""
            order = {sid: n for n, sid in enumerate(found)}
            best = sorted(found.values(), key=lambda sc: (-score(sc), order[sc["id"]]))[:SIMILAR_MAX]
            return [dict(sc, points=round(score(sc))) for sc in best]

        def inner_score(sc):
            return (SIMILAR_FAV_HERE * len(people(sc) & favs_here)
                    + SIMILAR_FAV_PERFORMER * len(people(sc) & favs - favs_here) + tag_score(sc)
                    + sum(less[p] for p in people(sc) & set(less)))

        def outer_score(sc):
            return (SIMILAR_PERFORMER * len(people(sc) & others_here) + tag_score(sc)
                    + sum(less[p] for p in people(sc) & set(less)))
        rows = [("inner", "Similar scenes with favourite performers", "", ranked(inner, inner_score)),
                ("discover", "Discover similar scenes", "", ranked(outer, outer_score)),
                ("together", "Together again", "Two or more of this cast in another scene", again)]
        return {"rows": [{"key": key, "title": title, "sub": sub, "scenes": self.annotate(scenes, user)}
                         for key, title, sub, scenes in rows if scenes]}

    # -- similar performers -------------------------------------------------------------
    def lookalikes(self, seed, ttl=3600):
        """Performers like SEED (a PerformerProfile): {ID: (lookalike score, with COSTAR_BONUS
        for a co-star, their profile)}, from the same gender's most popular of their country
        (else ethnicity) and their co-stars."""
        base = {"sort": "POPULARITY", "direction": "DESC", "per_page": 100}
        if seed.get("gender"):
            base["gender"] = seed["gender"]
        if seed.get("country"):
            near = {"country": {"value": seed["country"], "modifier": "EQUALS"}}
        elif seed.get("ethnicity"):
            near = {"ethnicity": seed["ethnicity"]}
        else:
            near = None
        inputs = [dict(base, page=n, **near) for n in range(1, LOOKALIKE_PAGES + 1)] if near else []
        costar = [dict(base, page=n, performed_with=seed["id"]) for n in range(1, COSTAR_PAGES + 1)]

        def ask(inp):
            try:
                return self.stash.performer_profiles(inp, ttl)
            except ApiError as e:
                print(f"similar performers to {seed['id']}: {e.message}", file=sys.stderr, flush=True)
                return []
        with ThreadPoolExecutor(len(inputs) + len(costar)) as pool:
            answers = list(pool.map(ask, inputs + costar))
        costars = {p["id"] for got in answers[len(inputs):] for p in got}
        found = {}
        for got in answers:
            for p in got:
                if p["id"] != seed["id"] and p["id"] not in found:
                    found[p["id"]] = (lookalike(seed, p) + (COSTAR_BONUS if p["id"] in costars else 0), p)
        return found

    def performer_tastes(self, ids, user, ttl=3600):
        """Performer ID -> the user's average taste for their newest TASTE_SCENES scenes (a
        hated one -10); none without feelings, or without scenes."""
        feel, perf = self.feelings(user), self.performer_taste(user)
        if not ids or not (feel or perf):
            return {}
        rare, ids = self.rarities(), list(ids)
        chunks = [ids[i:i + 15] for i in range(0, len(ids), 15)]

        def ask(chunk):
            try:
                return self.stash.query_scenes_many(
                    [{"page": 1, "per_page": TASTE_SCENES, "sort": "DATE", "direction": "DESC",
                      "performers": {"value": [pid], "modifier": "INCLUDES"},
                      "date": {"value": tomorrow(), "modifier": "LESS_THAN"}} for pid in chunk], ttl)
            except ApiError as e:
                print(f"performers' scenes: {e.message}", file=sys.stderr, flush=True)
                return [[] for _ in chunk]
        with ThreadPoolExecutor(len(chunks)) as pool:
            answers = [scenes for got in pool.map(ask, chunks) for scenes in got]
        out = {}
        for pid, scenes in zip(ids, answers):
            if scenes:
                tags = self.card_tags(scenes)
                values = [taste(weigh_tags(tags.get(sc["id"], ()), feel, rare=rare),
                                performer_points(card_people(sc), tags.get(sc["id"], ()), perf))
                          for sc in scenes]
                out[pid] = sum(-10 if v is None else v for v in values) / len(values)
        return out

    def ranked_performers(self, scored, user, weight, size, ttl=3600, tasted=LOOKALIKE_TASTED, each=None):
        """The best SIZE of SCORED ({ID: (score, profile, extra)}), the best TASTED of them also
        by the user's taste times WEIGHT (with EACH, at most that many with the same "like"):
        cards with "similarity" and "taste"."""
        best = sorted(scored, key=lambda pid: -scored[pid][0])[:tasted]
        tastes = self.performer_tastes(best, user, ttl)
        best.sort(key=lambda pid: -(scored[pid][0] + weight * tastes.get(pid, 0)))
        if each:
            count, kept = {}, []
            for pid in best:
                seed = scored[pid][2].get("like")
                if count.get(seed, 0) < each:
                    count[seed] = count.get(seed, 0) + 1
                    kept.append(pid)
            best = kept
        out = []
        for pid in best[:size]:
            score, prof, extra = scored[pid]
            card = {k: prof.get(k) for k in ("id", "name", "disambiguation", "gender", "scene_count", "images")}
            card.update(extra, similarity=round(score))
            if pid in tastes:
                card["taste"] = round(tastes[pid])
            out.append(card)
        return out

    def similar_performers(self, pid, user):
        """Performers like this one: by looks, country and era (lookalikes), the user's taste
        only nudging (SIMILAR_TASTE_WEIGHT); none they dislike or hate."""
        seed = self.stash.performer_profile(pid)
        if not seed:
            raise ApiError(404, "StashDB has no performer with that ID.")
        feel = self.performer_feelings(user)
        scored = {p: (score, prof, {}) for p, (score, prof) in self.lookalikes(seed).items()
                  if feel.get(p, 0) >= 0}
        return {"performers": self.ranked_performers(scored, user, SIMILAR_TASTE_WEIGHT, SIMILAR_PERFORMERS)}

    def might_like(self, user, ttl=3600):
        """Discover's performers: like the ones the user loved lately (MIGHT_LIKE_SEEDS; else
        liked), by looks, country and era and their taste for their scenes
        (MIGHT_LIKE_TASTE_WEIGHT); none they have a feeling for or follow already."""
        rows = self.db.all("SELECT performer_id, name, feeling FROM performer_feelings WHERE user_id = ? "
                           "AND feeling > 0 ORDER BY feeling DESC, created DESC LIMIT ?",
                           (user["id"], MIGHT_LIKE_SEEDS))
        if not rows:
            return []
        known = set(self.performer_feelings(user)) | {f["id"] for f in self.follows(user, "performer")}
        timing = Timing.current()

        def around(row):
            Timing.begin(timing)
            seed = self.stash.performer_profile(row["performer_id"], ttl or 3600)
            return (row["name"], self.lookalikes(seed, ttl or 3600)) if seed else (row["name"], {})
        with ThreadPoolExecutor(len(rows)) as pool:
            found = list(pool.map(around, rows))
        scored = {}
        for name, alike in found:
            for pid, (score, prof) in alike.items():
                if pid not in known and score > scored.get(pid, (-1,))[0]:
                    scored[pid] = (score, prof, {"like": name})
        return self.ranked_performers(scored, user, MIGHT_LIKE_TASTE_WEIGHT, DISCOVER_ROW, ttl or 3600,
                                      tasted=LOOKALIKE_TASTED * 2, each=MIGHT_LIKE_EACH)

    def favourite_performers(self, user=None):
        """The inner circle: the performers the user loves, likes or follows, but not the ones
        they dislike or hate. (StashDB's favourites aren't read: loving or liking makes them,
        see set_performer_feeling.)"""
        if not user:
            return set()
        feel = self.performer_feelings(user)
        ids = {f["id"] for f in self.follows(user, "performer")}
        return (ids | {p for p, f in feel.items() if f > 0}) - {p for p, f in feel.items() if f < 0}

    def stash_favourite_ids(self):
        """The StashDB account's favourite performers: {ID: name}."""
        found = {}
        for page in range(1, 11):
            res = self.stash.query_performers({"page": page, "per_page": 100, "sort": "NAME",
                                               "direction": "ASC", "is_favorite": True})
            found.update((p["id"], p.get("name") or p["id"]) for p in res["performers"])
            if page * 100 >= res["count"]:
                break
        return found

    def sync_stash_favourites(self):
        """Once: make the StashDB account's favourite performers the ones an admin loves or likes
        (as set_performer_feeling keeps them from then on). Returns how many changed."""
        if self.db.setting("favourites_synced") or not self.db.setting("favourites_imported"):
            return 0
        have = set(self.stash_favourite_ids())
        want = {r["performer_id"] for r in self.db.all(
            "SELECT DISTINCT f.performer_id FROM performer_feelings f JOIN users u ON u.id = f.user_id "
            "WHERE f.feeling > 0 AND u.is_admin = 1")}
        for pid in sorted(want - have):
            self.stash.set_favorite("performer", pid, True)
        for pid in sorted(have - want):
            self.stash.set_favorite("performer", pid, False)
        self.db.set_settings({"favourites_synced": "1"})
        return len(want ^ have)

    def import_stash_favourites(self):
        """Once: StashDB's favourite performers become the first admin's liked ones (from then
        on, an admin loving or liking a performer makes them one there, and Desireerr reads them
        no more). Returns how many."""
        if self.db.setting("favourites_imported"):
            return 0
        admin = self.db.one("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1")
        if not admin:
            return 0
        found = self.stash_favourite_ids()
        for pid, name in found.items():
            self.db.run("INSERT OR IGNORE INTO performer_feelings(user_id, performer_id, name, feeling, created) "
                        "VALUES (?, ?, ?, ?, ?)", (admin["id"], pid, name, FEELINGS["like"], now()))
        self.db.set_settings({"favourites_imported": "1"})
        self.forget_discover()
        return len(found)

    def performer(self, pid, user):
        perf = self.stash.find_performer(pid)
        if not perf:
            raise ApiError(404, "StashDB has no performer with that ID.")
        perf["stashdb_url"] = f"{STASHDB_WEB}/performers/{pid}"
        perf["follow"] = self.follow_state(user, "performer", pid)
        names = {v: k for k, v in FEELINGS.items()}
        perf["feeling"] = names.get(self.performer_feelings(user).get(pid), "neutral") if user else "neutral"
        return perf

    def performer_feelings(self, user, named=False):
        """Performer ID -> the user's feeling for them (2 love ... -2 hate); NAMED: a list of
        {"id", "name", "feeling"} with the feelings' names."""
        if not user:
            return [] if named else {}
        rows = self.db.all("SELECT performer_id, name, feeling FROM performer_feelings WHERE user_id = ? "
                           "ORDER BY name COLLATE NOCASE", (user["id"],))
        if named:
            names = {v: k for k, v in FEELINGS.items()}
            return [{"id": r["performer_id"], "name": r["name"], "feeling": names[r["feeling"]]} for r in rows]
        return {r["performer_id"]: r["feeling"] for r in rows}

    def set_performer_feeling(self, user, pid, feeling):
        """FEELING: a FEELINGS name, or "neutral" for none. An admin's also makes the performer a
        favourite on the StashDB account, or not, for other tools that read it: they are one while
        any admin loves or likes them. Returns the feeling, and "stashdb": what went wrong there."""
        if feeling == "neutral":
            self.db.run("DELETE FROM performer_feelings WHERE user_id = ? AND performer_id = ?", (user["id"], pid))
        else:
            found = self.stash.find_performer(pid)
            if not found:
                raise ApiError(404, "StashDB has no performer with that ID.")
            self.db.run("INSERT OR REPLACE INTO performer_feelings(user_id, performer_id, name, feeling, created) "
                        "VALUES (?, ?, ?, ?, ?)", (user["id"], pid, found["name"], FEELINGS[feeling], now()))
        self.forget_discover(user)
        out = {"feeling": feeling}
        if user.get("is_admin"):
            fav = bool(self.db.one(
                "SELECT 1 FROM performer_feelings f JOIN users u ON u.id = f.user_id "
                "WHERE f.performer_id = ? AND f.feeling > 0 AND u.is_admin = 1", (pid,)))
            try:
                self.stash.set_favorite("performer", pid, fav)
            except ApiError as e:
                out["stashdb"] = e.message
        return out

    def learned_performers(self, user, at=None):
        """Performer ID -> how much the user likes them by the scenes they requested themselves
        (not those saved searches made, nor declined ones, nor videos of several scenes): each
        counts 1, halved every AFFINITY_HALF_LIFE; 1 - 0.5 ** (sum / 2), so 0.29 after one, 0.65
        after three, 0.88 after six."""
        at = at or time.time()
        rows = self.db.all("SELECT scene_id, created FROM requests WHERE user_id = ? AND status != 'declined' "
                           "AND via = '' AND watch_id IS NULL", (user["id"],))
        if not rows:
            return {}
        try:
            cards = {sc["id"]: sc for sc in self.stash.find_scenes(sorted({r["scene_id"] for r in rows}))}
        except ApiError:
            return {}
        sums = {}
        for r in rows:
            sc = cards.get(r["scene_id"])
            if not sc or MULTI_SCENE_TAGS & {t["id"] for t in sc.get("tags") or []}:
                continue
            for p in set(card_people(sc)):
                sums[p] = sums.get(p, 0) + 0.5 ** (max(0, at - r["created"]) / AFFINITY_HALF_LIFE)
        return {p: 1 - 0.5 ** (n / 2) for p, n in sums.items()}

    def performer_taste(self, user):
        """Performer ID -> (points, why) for the user's taste: by their feeling for them,
        PERFORMER_SCORE (a hated one None); without one, followed ones as liked
        (PERFORMER_POINTS), the ones they requested scenes of up to that (learned_performers)."""
        if not user:
            return {}
        out = {p: (round(PERFORMER_POINTS * a, 1), "requested") for p, a in self.learned_performers(user).items()}
        out.update((f["id"], (PERFORMER_POINTS, "followed")) for f in self.follows(user, "performer"))
        names = {v: k for k, v in FEELINGS.items()}
        out.update((p, (PERFORMER_SCORE.get(f), names[f])) for p, f in self.performer_feelings(user).items())
        return out

    def studio(self, sid, user):
        studio = self.stash.find_studio(sid)
        if not studio:
            raise ApiError(404, "StashDB has no studio with that ID.")
        studio["stashdb_url"] = f"{STASHDB_WEB}/studios/{sid}"
        studio["follow"] = self.follow_state(user, "studio", sid)
        return studio

    # -- following: a saved search for a performer's or studio's new scenes ------------------
    # So its new scenes are requested in Desireerr like any other, as the follower; Whisparr
    # itself follows nothing.

    def follows(self, user, kind=None):
        """The user's follows: [{"kind", "id", "name", "since", "watch_id"}]."""
        rows = self.db.all("SELECT id, name, since, follow FROM watches WHERE user_id = ? AND follow != ''",
                           (user["id"],))
        out = [{"kind": r["follow"].split(":")[0], "id": r["follow"].split(":", 1)[1],
                "name": r["name"].removeprefix("Following "), "since": r["since"], "watch_id": r["id"]}
               for r in rows]
        return [f for f in out if kind in (None, f["kind"])]

    def follow_state(self, user, kind, sid):
        w = self.db.one("SELECT since FROM watches WHERE user_id = ? AND follow = ?", (user["id"], f"{kind}:{sid}"))
        return {"followed": bool(w), "since": w["since"] if w else None}

    def set_follow(self, user, kind, sid, follow, name=None, since=None):
        """Follow: a saved search for the performer's (studio's) scenes released from today
        (SINCE) on. Unfollow: that saved search goes; the requests it made stay."""
        key = f"{kind}:{sid}"
        if not follow:
            self.db.run("DELETE FROM watches WHERE user_id = ? AND follow = ?", (user["id"], key))
        elif not self.follow_state(user, kind, sid)["followed"]:
            name = name or self.stash.names(kind + "s", [sid]).get(sid)
            if not name:
                raise ApiError(404, f"StashDB has no {kind} with that ID.")
            w = self.save_watch(user, {"name": f"Following {name}"[:80], "query": f"{kind}s={sid}",
                                       "summary": f"New scenes {'with' if kind == 'performer' else 'from'} {name}",
                                       "since": since or today()})
            self.db.run("UPDATE watches SET follow = ? WHERE id = ?", (key, w["id"]))
        self.forget_discover(user)
        return self.follow_state(user, kind, sid)

    def followed(self, user):
        """Discover's "you follow" rows: the user's follows as StashDB's cards, with since."""
        out = {}
        for kind, field, card, fragment in (("performer", "findPerformer", "PerformerCard", PERFORMER_CARD),
                                            ("studio", "findStudio", "StudioCard", STUDIO_CARD)):
            mine = {f["id"]: f for f in self.follows(user, kind)}
            try:
                cards = self.stash.find_many(field, list(mine), f"{{ ...{card} }}", fragment, ttl=3600) if mine else []
            except ApiError:
                cards = [{"id": i, "name": f["name"]} for i, f in mine.items()]
            out[kind + "s"] = sorted((dict(c, name=c.get("name") or mine[c["id"]]["name"], since=mine[c["id"]]["since"])
                                      for c in cards if c["id"] in mine), key=lambda c: c["name"].lower())
        return out

    def adopt_whisparr_follows(self):
        """What Whisparr follows itself (from before follows were Desireerr's, or set up in
        Whisparr): followed in Desireerr instead, by the first admin, from the same date, and
        no longer in Whisparr, so its new scenes come as requests."""
        admin = self.db.one("SELECT * FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1")
        if not admin:
            return []
        done = []
        for kind, item in self.whisparr.followed_here():
            sid, name = item.get("foreignId"), item.get("name") or item.get("title") or ""
            if not sid:
                continue
            since = (item.get("afterDate") or "")[:10]
            self.set_follow(admin, kind, sid, True, name=name or None,
                            since=since if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) else None)
            self.whisparr.stop_following(kind, item)
            done.append(f"{kind} {name}")
        return done

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

    def search(self, term, user=None):
        term = (term or "").strip()[:200]
        if not term:
            return {"scenes": [], "performers": [], "studios": [], "tags": []}
        found = self.stash.search(term)
        return dict(found, scenes=self.annotate(found["scenes"], user))

    def set_favorite(self, kind, sid, favorite):
        """Favourite (or not) on StashDB only: a filter for Discover, Scenes and the lists.
        Whisparr isn't touched, so nothing is downloaded for it (Follow does that). Should
        Whisparr's StashDB import list be on, it adds favourites by itself."""
        self.stash.set_favorite(kind, sid, favorite)
        self.forget_discover()
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
            if "query" in values:
                values["follow"] = ""     # other filters: a plain saved search now
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
            "write_nfo": s["write_nfo"] == "1",
            "nfo_error": self.nfo_error,
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
            self.forget_discover()
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
        if "write_nfo" in body:
            allowed["write_nfo"] = "1" if body["write_nfo"] else "0"
        self.db.set_settings(allowed)
        if {"whisparr_url", "whisparr_key"} & allowed.keys():
            self.whisparr.index_time = 0
            self.whisparr.queue_time = 0
        if {"jellyfin_url", "jellyfin_key"} & allowed.keys():
            self.jellyfin.time = self.jellyfin.tried = 0
        return self.public_settings()

    def fetch_image(self, url):
        if self.images:
            return self.images.get(url)
        raise ApiError(502, "No image cache.")

    def sync_nfos(self):
        """With write_nfo: Whisparr's metadata off, and our .nfo and folder.jpg for the
        downloaded scenes that lack one (NFO_BATCH a round); Jellyfin then reads each again.
        Without it: Whisparr's back on, if we turned it off. Returns how many were written."""
        s = self.db.settings()
        if s["write_nfo"] != "1":
            if s["whisparr_nfo_off"] == "1":
                self.whisparr.set_metadata(True)
                self.db.set_settings({"whisparr_nfo_off": "0"})
            return 0
        if self.whisparr.set_metadata(False):
            self.db.set_settings({"whisparr_nfo_off": "1"})
        with self.whisparr.lock:
            files = {sid: i["path"] for sid, i in self.whisparr.index.items()
                     if i["has_file"] and i["path"] and i.get("item_type", "scene") == "scene"}
        now = time.time()
        todo = [sid for sid, path in files.items()
                if not (is_ours(nfo_path(path)) and has_cover(os.path.dirname(path))
                        and not os.path.exists(os.path.join(os.path.dirname(path), "screenshot.jpg")))
                and now - self.nfo_missing.get(sid, 0) > NFO_RETRY]
        batch = todo[:NFO_BATCH]
        scenes = self.stash.find_many("findScene", batch, "{ ...SceneFull }", SCENE_FULL,
                                      ttl=0) if batch else []
        found = {sc["id"] for sc in scenes}
        for sid in batch:
            if sid not in found:  # StashDB doesn't have it (any more)
                self.nfo_missing[sid] = now
        written, errors = [], []
        for scene in scenes:
            path = files.get(scene["id"])
            if not path:
                continue
            try:
                write_nfo(path, scene_nfo(scene))
                written.append(path)
                with contextlib.suppress(ApiError):
                    write_cover(os.path.dirname(path), self.fetch_image, scene_cover(scene))
                if not has_cover(os.path.dirname(path)):  # no picture: tried again later
                    self.nfo_missing[scene["id"]] = now
            except OSError as e:
                if e.errno == errno.EROFS:  # the service's sandbox: see README, Jellyfin
                    self.nfo_error = (f"{os.path.dirname(path)} is read-only for Desireerr; its "
                                      "service needs ReadWritePaths= for Whisparr's root folders")
                    print(f"nfo: {self.nfo_error}", file=sys.stderr, flush=True)
                    return len(written)
                errors.append(f"{nfo_path(path)}: {e.strerror or e}")
        self.nfo_error = "; ".join(errors[:3]) + (f" (and {len(errors) - 3} more)" if len(errors) > 3 else "")
        if self.nfo_error:
            print(f"nfo: {self.nfo_error}", file=sys.stderr, flush=True)
        if written and self.jellyfin.creds()[1]:
            for path in written:
                item = self.jellyfin.find(path)
                if item:  # not yet in Jellyfin: its next scan reads the .nfo anyway
                    with contextlib.suppress(ApiError):
                        self.jellyfin.refresh_item(item)
        return len(written)

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
                out["tag_feelings"] = app.tag_feelings(user)
                out["performer_feelings"] = app.performer_feelings(user, named=True)
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
            return 200, app.discover(user, fresh=q.get("fresh") == "1"), None
        if key == ("GET", "scenes"):
            return 200, app.browse_scenes(q, user), None
        if key == ("GET", "scene") and len(parts) == 2:
            return 200, app.scene(parts[1], user), None
        if key == ("GET", "scene") and len(parts) == 3 and parts[2] == "similar":
            return 200, app.similar(parts[1], user), None
        if key == ("GET", "search"):
            return 200, app.search(q.get("q"), user), None
        if key == ("GET", "performers"):
            return 200, app.browse_performers(q), None
        if key == ("GET", "performer") and len(parts) == 3 and parts[2] == "similar":
            return 200, app.similar_performers(parts[1], user), None
        if key == ("GET", "performer") and len(parts) == 2:
            return 200, app.performer(parts[1], user), None
        if key == ("GET", "studios"):
            return 200, app.browse_studios(q), None
        if key == ("GET", "studio") and len(parts) == 2:
            return 200, app.studio(parts[1], user), None
        if key == ("GET", "tags"):
            return 200, app.tags(q), None
        if key == ("GET", "names"):
            return 200, app.names(q), None
        if key == ("GET", "tag") and len(parts) == 2:
            tag = app.stash.find_tag(parts[1])
            if not tag:
                raise ApiError(404, "StashDB has no tag with that ID.")
            feeling = {v: k for k, v in FEELINGS.items()}.get(app.feelings(user).get(tag["id"]), "neutral")
            return 200, dict(tag, feeling=feeling), None
        if key == ("POST", "tag-feelings"):
            body = self.body_json()
            if not id_list(body.get("id")) or body.get("feeling") not in (*FEELINGS, "neutral"):
                raise ApiError(400, "A feeling takes a tag ID and love, like, neutral, dislike or hate.")
            return 200, {"tags": app.set_tag_feeling(user, body["id"], body["feeling"])}, None

        # -- favourites (admins: they're the StashDB account's) --
        if key == ("POST", "favorite"):
            admin()
            body = self.body_json()
            kind = body.get("kind")
            if kind not in ("performer", "studio") or not id_list(body.get("id")):
                raise ApiError(400, "Favourite takes a performer or studio ID.")
            return 200, app.set_favorite(kind, body["id"], bool(body.get("favorite"))), None

        # -- following (anyone: a saved search for the new scenes, requested as them) --
        if key == ("POST", "follow"):
            body = self.body_json()
            kind = body.get("kind")
            if kind not in ("performer", "studio") or not id_list(body.get("id")):
                raise ApiError(400, "Follow takes a performer or studio ID.")
            out = app.set_follow(user, kind, body["id"], bool(body.get("follow")))
            app.forget_discover()  # favourite performers are everyone's StashDB ones plus follows
            return 200, out, None

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
        if key == ("POST", "performer-feelings"):
            body = self.body_json()
            pid, feeling = str(body.get("id", "")), body.get("feeling")
            if not id_list(pid) or feeling not in (*FEELINGS, "neutral"):
                raise ApiError(400, "Which performer, and love, like, neutral, dislike or hate?")
            return 200, app.set_performer_feeling(user, pid, feeling), None
        if key == ("POST", "prefs"):
            body = self.body_json()
            if body.get("performer_sort") not in ("TASTE", "DATE"):
                raise ApiError(400, "Sort a performer's scenes by TASTE or DATE.")
            app.db.run("UPDATE users SET performer_sort = ? WHERE id = ?", (body["performer_sort"], user["id"]))
            return 200, {"ok": True}, None
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
            app.adopt_whisparr_follows()
            app.check_requests()
        except ApiError as e:
            app.whisparr.error = e.message
        except Exception as e:
            print(f"refresh failed: {e!r}", file=sys.stderr, flush=True)
        try:
            if app.import_stash_favourites():
                print("StashDB's favourite performers are liked now", file=sys.stderr, flush=True)
            changed = app.sync_stash_favourites()
            if changed:
                print(f"StashDB's favourite performers follow the admins' feelings: {changed} changed",
                      file=sys.stderr, flush=True)
        except ApiError as e:
            print(f"favourites: {e.message}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"favourites failed: {e!r}", file=sys.stderr, flush=True)
        try:
            app.sync_nfos()
        except ApiError as e:
            app.nfo_error = e.message
            print(f"nfo: {e.message}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"nfo failed: {e!r}", file=sys.stderr, flush=True)
        try:
            app.check_watches()
        except ApiError as e:
            print(f"saved search: {e.message}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"saved search failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(INDEX_MAX_AGE)


def tag_counter(app):
    """Count each tag's scenes (see App.count_tags), checked hourly."""
    while True:
        try:
            counted = app.count_tags()
            if counted:
                print(f"counted the scenes of {counted} tags", file=sys.stderr, flush=True)
        except ApiError as e:
            print(f"tag counts: {e.message}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"tag counts failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(3600)


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
    finally:
        app.db.conn.close()
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
        threading.Thread(target=tag_counter, args=(app,), daemon=True).start()
    cls = DualStackServer if ":" in args.bind else Server
    server = cls((args.bind, args.port), Handler, app)
    print(f"Desireerr {VERSION} on port {args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
