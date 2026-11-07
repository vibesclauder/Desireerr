import http.client
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desireerr as d  # noqa: E402

# Never read the real torrent-box's ntfy topic (or push to it) from a test.
d.TORRENT_BOX_CONF = Path(tempfile.gettempdir()) / "desireerr-tests-no-torrent-box"
d.WHISPARR_CONFIG = d.TORRENT_BOX_CONF / "config.xml"


class FakeWhisparr(d.Whisparr):
    """Whisparr's API as a dict of (method, path) -> handler; records every call."""

    def __init__(self, db, routes):
        super().__init__(db)
        self.routes = routes
        self.calls = []

    def creds(self):
        return "http://whisparr", "KEY"

    def call(self, method, path, body=None, params=None, timeout=30):
        self.calls.append((method, path, body, params))
        handler = self.routes.get((method, path))
        if handler is None:
            raise d.ApiError(502, f"unexpected {method} {path}")
        return handler(body, params) if callable(handler) else handler


class FakeStash:
    def __init__(self, scenes):
        self.scenes = scenes
        self.listed = []         # what query_scenes answers: scene cards, newest change first
        self.queries = []

    def find_scene(self, sid):
        return self.scenes.get(sid)

    def query_scenes(self, inp, ttl=300):
        self.queries.append(dict(inp))
        start = (inp["page"] - 1) * inp["per_page"]
        return {"count": len(self.listed), "scenes": self.listed[start:start + inp["per_page"]]}

    def find_many(self, field, ids, selection, fragments="", ttl=300):
        return [{"id": i, "tags": []} for i in ids]   # no scene has a left-out tag


SCENE = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


def scene(sid=SCENE):
    return {"id": sid, "title": "A scene", "release_date": "2026-11-01",
            "studio": {"id": "s1", "name": "Studio"},
            "images": [{"url": "https://cdn.stashdb.org/a", "width": 1280, "height": 720},
                       {"url": "https://cdn.stashdb.org/b", "width": 600, "height": 900}]}


def make_app(tmp, routes=None):
    app = d.App(Path(tmp) / "data", Path(tmp) / "cache")
    app.whisparr = FakeWhisparr(app.db, routes or {})
    app.whisparr.index_time = app.whisparr.queue_time = 9e18  # never refresh on its own
    app.stash = FakeStash({SCENE: scene(), OTHER: scene(OTHER)})
    app.notify.background = False
    app.pushes = []
    app.notify.post = lambda server, body: app.pushes.append((server, body)) or True
    return app


class Passwords(unittest.TestCase):
    def test_round_trip(self):
        salt, digest = d.hash_password("correct horse")
        self.assertTrue(d.check_password("correct horse", salt, digest))
        self.assertFalse(d.check_password("wrong horse", salt, digest))

    def test_salted(self):
        self.assertNotEqual(d.hash_password("same")[1], d.hash_password("same")[1])


class SceneQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        inp = self.app.scene_input({})
        self.assertEqual(inp, {"page": 1, "per_page": 40, "sort": "DATE", "direction": "DESC"})

    def test_tags_all_and_any(self):
        inp = self.app.scene_input({"tags": f"{SCENE},{OTHER}"})
        self.assertEqual(inp["tags"], {"value": [SCENE, OTHER], "modifier": "INCLUDES_ALL"})
        inp = self.app.scene_input({"tags": SCENE, "tag_mode": "any"})
        self.assertEqual(inp["tags"]["modifier"], "INCLUDES")

    def test_excluded_tags_alone_go_to_stashdb(self):
        inp = self.app.scene_input({"not_tags": SCENE})
        self.assertEqual(inp["tags"], {"value": [SCENE], "modifier": "EXCLUDES"})

    def test_included_tags_win_the_one_criterion(self):
        inp = self.app.scene_input({"tags": SCENE, "not_tags": OTHER})
        self.assertEqual(inp["tags"], {"value": [SCENE], "modifier": "INCLUDES_ALL"})

    def test_bad_ids_and_values_are_dropped(self):
        inp = self.app.scene_input({"tags": "x\" } evil,", "sort": "DROP", "per_page": "9999",
                                    "after": "yesterday", "favorites": "NOPE"})
        self.assertNotIn("tags", inp)
        self.assertEqual(inp["sort"], "DATE")
        self.assertEqual(inp["per_page"], d.PER_PAGE_MAX)
        self.assertNotIn("date", inp)
        self.assertNotIn("favorites", inp)

    def test_one_studio_with_sub_studios(self):
        self.assertEqual(self.app.scene_input({"studios": SCENE, "sub_studios": "1"})["parentStudio"], SCENE)
        self.assertEqual(self.app.scene_input({"studios": SCENE})["studios"],
                         {"value": [SCENE], "modifier": "INCLUDES"})

    def test_dates(self):
        self.assertEqual(self.app.scene_input({"after": "2026-01-01"})["date"],
                         {"value": "2026-01-01", "modifier": "GREATER_THAN"})

    def test_find_many_ignores_odd_ids(self):
        stash = d.StashDB(lambda: "KEY")
        seen = []
        stash.query = lambda q, v=None, ttl=300, key=None: seen.append(q) or {"a0": {"id": SCENE, "name": "x"}}
        self.assertEqual(stash.names("tags", [SCENE, '") { evil }']), {SCENE: "x"})
        self.assertEqual(seen[0].count("findTag("), 1)


class States(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)
        self.w = self.app.whisparr

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, **kw):
        base = {"id": 7, "monitored": False, "has_file": False, "file_added": "", "item_type": "scene"}
        base.update(kw)
        self.w.index[SCENE] = base

    def test_not_in_whisparr(self):
        self.assertEqual(self.app.state(SCENE)["status"], "none")

    def test_unmonitored_is_not_requested(self):
        self.entry()
        self.assertEqual(self.app.state(SCENE)["status"], "none")

    def test_order(self):
        self.entry(monitored=True)
        self.assertEqual(self.app.state(SCENE)["status"], "processing")
        self.w.queue[7] = {"progress": 40, "status": "downloading", "timeleft": "00:10:00"}
        st = self.app.state(SCENE)
        self.assertEqual((st["status"], st["progress"]), ("downloading", 40))
        self.entry(monitored=True, has_file=True)
        self.assertEqual(self.app.state(SCENE)["status"], "available")

    def test_own_requests(self):
        self.assertEqual(self.app.state(SCENE, {"id": 1, "status": "pending"})["status"], "pending")
        self.assertEqual(self.app.state(SCENE, {"id": 1, "status": "declined"})["status"], "declined")


class Requests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.added = []
        routes = {
            ("GET", "movie"): [],
            ("GET", "lookup/scene"): lambda b, p: [{"foreignId": p["term"][6:], "movie": {
                "title": "A scene", "foreignId": p["term"][6:], "itemType": "scene", "id": 0}}],
            ("POST", "movie"): lambda b, p: self.added.append(b) or {"id": 42},
            ("GET", "qualityprofile"): [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD"}],
            ("GET", "rootfolder"): [{"path": "/x/HD"}, {"path": "/x/SD"}],
            ("GET", "studio"): [{"qualityProfileId": 4}, {"qualityProfileId": 4}, {"qualityProfileId": 1}],
        }
        self.app = make_app(self.tmp.name, routes)
        self.admin = self.app.db.one("SELECT * FROM users WHERE id = ?",
                                     (self.app.create_user("boss", "password1", is_admin=True),))
        self.user = self.app.db.one("SELECT * FROM users WHERE id = ?",
                                    (self.app.create_user("guest", "password1"),))

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_request_waits_for_approval(self):
        r = self.app.request_scene(self.user, SCENE)
        self.assertEqual((r["status"], r["state"]["status"]), ("pending", "pending"))
        self.assertEqual(self.added, [])
        with self.assertRaises(d.ApiError):
            self.app.request_scene(self.user, SCENE)

    def test_admin_request_adds_and_searches(self):
        r = self.app.request_scene(self.admin, SCENE)
        self.assertEqual((r["status"], r["whisparr_id"], r["state"]["status"]), ("approved", 42, "processing"))
        movie = self.added[0]
        self.assertNotIn("id", movie)
        self.assertEqual((movie["qualityProfileId"], movie["rootFolderPath"], movie["monitored"]), (4, "/x/HD", True))
        self.assertEqual(movie["addOptions"]["searchForMovie"], True)

    def test_existing_unmonitored_scene_is_monitored_and_searched(self):
        put = []
        self.app.whisparr.routes.update({
            ("GET", "movie"): [{"id": 5, "stashId": SCENE}],
            ("GET", "movie/5"): {"id": 5, "stashId": SCENE, "monitored": False, "hasFile": False},
            ("PUT", "movie/5"): lambda b, p: put.append(b) or b,
            ("POST", "command"): lambda b, p: put.append(b) or {},
        })
        self.app.request_scene(self.admin, SCENE)
        self.assertTrue(put[0]["monitored"])
        self.assertEqual(put[1], {"name": "MoviesSearch", "movieIds": [5]})

    def test_failed_send_is_kept_with_its_reason(self):
        self.app.whisparr.routes[("GET", "lookup/scene")] = []
        with self.assertRaises(d.ApiError):
            self.app.request_scene(self.admin, SCENE)
        row = self.app.db.one("SELECT * FROM requests")
        self.assertEqual(row["status"], "failed")
        self.assertIn("couldn't find", row["error"])

    def test_approve_and_decline(self):
        r = self.app.request_scene(self.user, SCENE)
        self.assertEqual(self.app.approve(self.admin, r["id"])["status"], "approved")
        r2 = self.app.request_scene(self.user, OTHER)
        self.assertEqual(self.app.decline(self.admin, r2["id"])["status"], "declined")

    def test_users_see_only_their_requests(self):
        self.app.request_scene(self.user, SCENE)
        self.app.request_scene(self.admin, OTHER)
        self.assertEqual(self.app.list_requests(self.user, "all", 1)["count"], 1)
        self.assertEqual(self.app.list_requests(self.admin, "all", 1)["count"], 2)


class Follow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.app = make_app(self.tmp.name, {
            ("GET", "performer"): lambda b, p: [],
            ("GET", "lookup/performer"): [{"foreignId": SCENE, "performer": {"foreignId": SCENE, "name": "P", "id": 0}}],
            ("POST", "performer"): lambda b, p: self.sent.append(b) or b,
            ("GET", "qualityprofile"): [{"id": 1, "name": "Any"}],
            ("GET", "rootfolder"): [{"path": "/x/HD"}],
            ("GET", "studio"): [],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_follow_sets_after_date_to_today(self):
        self.app.whisparr.set_follow("performer", SCENE, True)
        self.assertEqual(self.sent[0]["afterDate"], d.today())
        self.assertTrue(self.sent[0]["monitored"])
        self.assertFalse(self.sent[0]["searchOnAdd"])

    def test_refollow_keeps_existing_date(self):
        put = []
        self.app.whisparr.routes[("GET", "performer")] = [{"id": 3, "foreignId": SCENE, "monitored": False, "afterDate": "2026-10-30"}]
        self.app.whisparr.routes[("PUT", "performer/3")] = lambda b, p: put.append(b) or b
        self.app.whisparr.set_follow("performer", SCENE, True)
        self.assertEqual((put[0]["afterDate"], put[0]["monitored"]), ("2026-10-30", True))


class Init(unittest.TestCase):
    """desireerr.py init: the admin from a shared login, the StashDB key from Whisparr."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def run_init(self, user="alice", password="pw", key=""):
        app = d.App(self.data, None, setup_code=False)
        return app, d.init(app, user, password, key)

    def users(self, app):
        return [(u["username"], u["is_admin"]) for u in app.users()]

    def test_creates_the_admin_once(self):
        app, done = self.run_init(key="KEY1")
        self.assertEqual(done, ["created the admin 'alice'", "StashDB key: set"])
        self.assertEqual(self.users(app), [("alice", 1)])
        self.assertEqual(app.db.setting("stashdb_key"), "KEY1")
        app, done = self.run_init(key="KEY1")
        self.assertEqual(done, ["admin 'alice': up to date", "StashDB key: up to date"])
        self.assertEqual(self.users(app), [("alice", 1)])

    def test_short_shared_passwords_are_fine(self):
        app, _ = self.run_init(password="abc")
        user = app.db.one("SELECT * FROM users")
        self.assertTrue(d.check_password("abc", user["pw_salt"], user["pw_hash"]))

    def test_admin_follows_a_changed_login(self):
        self.run_init()
        app, done = self.run_init(password="new")
        self.assertEqual(done[0], "admin login updated (user 'alice')")
        app, done = self.run_init(user="bob", password="new2")
        self.assertEqual(self.users(app), [("bob", 1)])
        user = app.db.one("SELECT * FROM users")
        self.assertTrue(d.check_password("new2", user["pw_salt"], user["pw_hash"]))

    def test_an_admin_made_by_hand_is_left_alone(self):
        app = d.App(self.data, None, setup_code=False)
        app.create_user("carol", "handmade1", is_admin=True)
        app, done = self.run_init()
        self.assertEqual(done[0], "has its own admin, made on its first visit; not changing it")
        self.assertEqual(self.users(app), [("carol", 1)])

    def test_same_login_made_by_hand_is_adopted(self):
        app = d.App(self.data, None, setup_code=False)
        app.create_user("alice", "pw-by-hand", is_admin=True)
        _, done = self.run_init(password="pw-by-hand")
        self.assertEqual(done[0], "admin 'alice': up to date")
        _, done = self.run_init(password="changed")
        self.assertEqual(done[0], "admin login updated (user 'alice')")

    def test_a_key_set_in_settings_wins(self):
        app, _ = self.run_init(key="FROM-WHISPARR")
        app.db.set_settings({"stashdb_key": "MINE"})
        app, done = self.run_init(key="FROM-WHISPARR")
        self.assertEqual(done[1], "StashDB key: keeps the one set in Desireerr")
        self.assertEqual(app.db.setting("stashdb_key"), "MINE")

    def test_removes_the_setup_code(self):
        app = d.App(self.data, None)
        self.assertTrue((self.data / "setup-code").exists())
        self.run_init()
        self.assertFalse((self.data / "setup-code").exists())

    def test_command_reads_the_environment(self):
        env = {"DESIREERR_ADMIN_USER": "alice", "DESIREERR_ADMIN_PASSWORD": "pw",
               "DESIREERR_STASHDB_KEY": ""}
        with unittest.mock.patch.dict(d.os.environ, env):
            with unittest.mock.patch("sys.stdout") as out:
                self.assertEqual(d.cmd_init(["--data", str(self.data)]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("created the admin 'alice'", printed)
        self.assertIn("no StashDB key yet", printed)


class Favourites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.favs, self.posted = [], []
        self.app = make_app(self.tmp.name, {
            ("GET", "performer"): lambda b, p: [],
            ("GET", "lookup/performer"): [{"foreignId": SCENE, "performer": {"foreignId": SCENE, "name": "P"}}],
            ("POST", "performer"): lambda b, p: self.posted.append(b) or b,
            ("GET", "qualityprofile"): [{"id": 1, "name": "Any"}],
            ("GET", "rootfolder"): [{"path": "/x/HD"}],
            ("GET", "studio"): [],
        })
        self.app.whisparr.refresh_followed = lambda: None
        self.app.stash.set_favorite = lambda kind, sid, fav: self.favs.append((kind, sid, fav))

    def tearDown(self):
        self.tmp.cleanup()

    def test_favourites_only_touch_stashdb(self):
        self.assertEqual(self.app.set_favorite("performer", SCENE, True), {"is_favorite": True})
        self.app.set_favorite("studio", SCENE, False)
        self.assertEqual(self.favs, [("performer", SCENE, True), ("studio", SCENE, False)])
        self.assertEqual(self.app.whisparr.calls, [])     # nothing followed, nothing wanted


class Images(unittest.TestCase):
    def test_only_stashdb_hosts(self):
        ok = d.Images.allowed
        self.assertTrue(ok("https://stashdb.org/images/x"))
        self.assertTrue(ok("https://cdn.stashdb.org/images/x"))
        self.assertFalse(ok("http://stashdb.org/images/x"))
        self.assertFalse(ok("https://stashdb.org.evil.example/x"))
        self.assertFalse(ok("https://evilstashdb.org/x"))
        self.assertFalse(ok("file:///etc/passwd"))


class Http(unittest.TestCase):
    """The real handler on a local port: sign-in, sessions and the request header check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(self.tmp.name)
        self.server = d.Server(("127.0.0.1", 0), d.Handler, self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, header=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-Requested-With"] = "desireerr"
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.getheader("Set-Cookie"):
            self.cookie = resp.getheader("Set-Cookie").split(";")[0]
        conn.close()
        return resp.status, (json.loads(data) if data and resp.getheader("Content-Type", "").startswith("application/json") else data)

    def test_setup_once_then_login(self):
        self.assertTrue(self.req("GET", "/api/status")[1]["setup_needed"])
        self.assertEqual(self.req("GET", "/api/requests")[0], 401)
        status, _ = self.req("POST", "/api/setup", {"username": "boss", "password": "password1", "setup_code": self.app.setup_code})
        self.assertEqual(status, 200)
        self.assertEqual(self.req("GET", "/api/status")[1]["user"]["username"], "boss")
        self.assertEqual(self.req("GET", "/api/requests")[0], 200)
        self.assertEqual(self.req("POST", "/api/setup", {"username": "x2", "password": "password1"})[0], 409)
        self.req("POST", "/api/logout")
        self.assertEqual(self.req("GET", "/api/requests")[0], 401)
        self.assertEqual(self.req("POST", "/api/login", {"username": "boss", "password": "nope"})[0], 401)
        self.assertEqual(self.req("POST", "/api/login", {"username": "BOSS", "password": "password1"})[0], 200)

    def test_setup_needs_the_code(self):
        self.assertEqual(len(self.app.setup_code), 8)
        status, body = self.req("POST", "/api/setup", {"username": "boss", "password": "password1", "setup_code": "nope"})
        self.assertEqual(status, 403)
        self.assertTrue(self.req("GET", "/api/status")[1]["setup_needed"])
        self.assertEqual(self.req("POST", "/api/setup", {"username": "boss", "password": "password1",
                                                         "setup_code": self.app.setup_code.upper()})[0], 200)
        self.assertFalse((self.app.data_dir / "setup-code").exists())

    def test_writes_need_the_header(self):
        self.assertEqual(self.req("POST", "/api/setup", {"username": "boss", "password": "password1", "setup_code": self.app.setup_code}, header=False)[0], 403)

    def test_images_need_a_session(self):
        self.assertEqual(self.req("GET", "/img?u=https%3A%2F%2Fstashdb.org%2Fimages%2Fx")[0], 401)

    def test_pages_and_static_files(self):
        status, page = self.req("GET", "/scene/whatever")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Desireerr</title>", page)
        self.assertEqual(self.req("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.req("GET", "/static/../desireerr.py")[0], 404)

    def test_users_cannot_reach_admin_routes(self):
        self.req("POST", "/api/setup", {"username": "boss", "password": "password1", "setup_code": self.app.setup_code})
        self.assertEqual(self.req("POST", "/api/users", {"username": "guest", "password": "password1"})[0], 200)
        self.req("POST", "/api/logout")
        self.req("POST", "/api/login", {"username": "guest", "password": "password1"})
        self.assertEqual(self.req("GET", "/api/settings")[0], 403)
        self.assertEqual(self.req("POST", "/api/follow", {"kind": "performer", "id": SCENE, "follow": True})[0], 403)
        self.assertEqual(self.req("POST", "/api/favorite", {"kind": "performer", "id": SCENE, "favorite": True})[0], 403)
        self.assertEqual(self.req("POST", "/api/notify-test")[0], 403)
        self.assertEqual(self.req("POST", "/api/requests/search-stalled")[0], 403)

    def test_saved_searches_over_http(self):
        self.req("POST", "/api/setup", {"username": "boss", "password": "password1", "setup_code": self.app.setup_code})
        status, w = self.req("POST", "/api/watches", {"name": "Mine", "query": f"tags={TAG}"})
        self.assertEqual((status, w["since"]), (200, d.today()))
        self.assertEqual(self.req("PUT", f"/api/watches/{w['id']}", {"ask_first": True})[1]["ask_first"], 1)
        self.assertEqual(len(self.req("GET", "/api/watches")[1]["watches"]), 1)
        self.req("POST", "/api/users", {"username": "guest", "password": "password1"})
        self.req("POST", "/api/logout")
        self.req("POST", "/api/login", {"username": "guest", "password": "password1"})
        self.assertEqual(self.req("GET", "/api/watches")[1]["watches"], [])
        self.assertEqual(self.req("DELETE", f"/api/watches/{w['id']}")[0], 404)

    def test_status_remembers_where_the_app_is_opened(self):
        self.req("POST", "/api/setup", {"username": "boss", "password": "password1", "setup_code": self.app.setup_code})
        self.req("GET", "/api/status")
        self.assertEqual(self.app.db.setting("seen_origin"), f"http://127.0.0.1:{self.port}")


class Base(unittest.TestCase):
    """An app with a Whisparr that takes any scene, an admin, a user and an ntfy topic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.added, self.commands, self.puts = [], [], []
        self.movies = {}
        routes = {
            ("GET", "movie"): [],
            ("GET", "lookup/scene"): self.lookup,
            ("POST", "movie"): self.add_movie,
            ("POST", "command"): lambda b, p: self.commands.append(b) or {},
            ("GET", "qualityprofile"): [{"id": 1, "name": "Any"}],
            ("GET", "rootfolder"): [{"path": "/x/HD"}],
            ("GET", "studio"): [{"qualityProfileId": 1}],
        }
        self.unknown = set()     # scenes Whisparr's metadata doesn't have
        self.app = make_app(self.tmp.name, routes)
        self.app.db.set_settings({"notify_topic": "topic", "notify_server": "http://ntfy"})
        self.admin = self.user_row(self.app.create_user("boss", "password1", is_admin=True))
        self.user = self.user_row(self.app.create_user("guest", "password1"))

    def tearDown(self):
        self.tmp.cleanup()

    def user_row(self, uid):
        return self.app.db.one("SELECT id, username, is_admin, auto_approve FROM users WHERE id = ?", (uid,))

    def lookup(self, body, params):
        sid = params["term"][6:]
        if sid in self.unknown:
            return []
        return [{"foreignId": sid, "movie": {"title": "A scene", "foreignId": sid, "itemType": "scene"}}]

    def add_movie(self, body, params):
        self.added.append(body)
        mid = 40 + len(self.added)
        self.movies[mid] = dict(body, id=mid)
        self.app.whisparr.routes[("GET", f"movie/{mid}")] = lambda b, p: dict(self.movies[mid])
        self.app.whisparr.routes[("PUT", f"movie/{mid}")] = lambda b, p: self.puts.append(b) or b
        return {"id": mid}

    def events(self):
        return [b["title"] for _, b in self.app.pushes]

    def make_available(self, sid, added="2026-11-07T10:00:00Z", path="/mnt/storage/XXX/HD/a/a.mp4"):
        self.app.whisparr.index[sid].update(has_file=True, file_added=added, path=path)


class Notifications(Base):
    def test_discreet_by_default(self):
        self.app.db.set_settings({"seen_origin": "http://box:5056", "tab_title": "Notes"})
        self.app.request_scene(self.user, SCENE)
        (server, body), = self.app.pushes
        self.assertEqual(server, "http://ntfy")
        self.assertEqual(body, {"topic": "topic", "title": "Notes", "message": "A request is waiting for approval.",
                                "click": "http://box:5056/requests"})

    def test_like_seerr_when_not_discreet(self):
        self.app.db.set_settings({"notify_discreet": "0", "app_url": "https://desire.example/"})
        r = self.app.request_scene(self.user, SCENE)
        body = self.app.pushes[0][1]
        self.assertEqual(body["title"], "New Scene Request")
        self.assertEqual(body["message"], "A scene\nStudio · 2026-11-01\n\nRequested by: guest")
        self.assertEqual(body["attach"], "https://cdn.stashdb.org/a?size=600")
        self.assertEqual(body["click"], f"https://desire.example/scene/{SCENE}")
        self.app.db.set_settings({"notify_events": "declined"})
        self.app.decline(self.admin, r["id"])
        self.assertEqual(self.app.pushes[1][1]["title"], "Scene Request Declined")
        self.assertIn("Declined by: boss", self.app.pushes[1][1]["message"])

    def test_chosen_events_only(self):
        self.app.request_scene(self.admin, SCENE)          # "auto": off by default
        self.assertEqual(self.app.pushes, [])
        self.app.db.set_settings({"notify_events": "auto"})
        self.app.request_scene(self.admin, OTHER)
        self.assertEqual(len(self.app.pushes), 1)

    def test_failed_request(self):
        self.unknown.add(SCENE)
        with self.assertRaises(d.NotInWhisparr):
            self.app.request_scene(self.admin, SCENE)
        self.assertEqual(self.app.pushes[0][1]["message"], "A request failed.")

    def test_no_topic_no_push(self):
        self.app.db.set_settings({"notify_topic": "", "notify_server": ""})
        self.app.request_scene(self.user, SCENE)
        self.assertEqual(self.app.pushes, [])

    def test_torrent_box_topic_is_used(self):
        conf = Path(self.tmp.name) / "tb"
        conf.mkdir()
        (conf / "config.ini").write_text("[ntfy]\nserver = http://localhost:2586\n")
        (conf / "secrets.ini").write_text("[secrets]\nntfy_topic = torrent-box-abc\n")
        self.assertEqual(d.torrent_box_ntfy(conf), ("http://localhost:2586", "torrent-box-abc"))
        self.app.db.set_settings({"notify_topic": "", "notify_server": ""})
        self.app.notify.conf_dir = conf
        self.app.request_scene(self.user, SCENE)
        self.assertEqual(self.app.pushes[0][0], "http://localhost:2586")
        self.assertEqual(self.app.pushes[0][1]["topic"], "torrent-box-abc")
        (conf / "secrets.ini").write_text("[secrets]\n")
        self.assertEqual(d.torrent_box_ntfy(conf), ("", ""))

    def test_available_once_and_only_when_new(self):
        self.app.request_scene(self.admin, SCENE)
        self.app.request_scene(self.admin, OTHER)
        recent = time_iso(d.now() - 3600)
        self.make_available(SCENE, added=recent)
        self.make_available(OTHER, added="2025-01-01T00:00:00Z")   # from before pushes existed
        self.app.check_requests()
        self.app.check_requests()
        self.assertEqual(self.events(), ["Desireerr"])
        self.assertEqual(self.app.pushes[0][1]["message"], "A request is ready.")
        notified = {r["scene_id"]: r["notified"] for r in self.app.db.all("SELECT * FROM requests")}
        self.assertEqual(notified, {SCENE: "available", OTHER: "available"})


def time_iso(ts):
    return d.datetime.fromtimestamp(ts, d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")


class Stalled(Base):
    def setUp(self):
        super().setUp()
        self.app.db.set_settings({"notify_discreet": "0", "stall_days": "7"})
        self.rid = self.app.request_scene(self.admin, SCENE)["id"]
        self.age(8)

    def age(self, days):
        self.app.db.run("UPDATE requests SET created = ?, decided_at = ?", (d.now() - days * 86400,) * 2)

    def test_stalled_after_the_set_days(self):
        self.assertEqual(self.app.request_row(self.rid)["stalled_days"], 8)
        self.age(6)
        self.assertEqual(self.app.request_row(self.rid)["stalled_days"], 0)

    def test_downloading_is_not_stalled(self):
        self.app.whisparr.queue[self.app.whisparr.index[SCENE]["id"]] = {"progress": 5, "status": "", "timeleft": ""}
        self.assertEqual(self.app.request_row(self.rid)["stalled_days"], 0)

    def test_pushed_once_then_again_after_a_new_search(self):
        self.app.check_requests()
        self.app.check_requests()
        self.assertEqual(self.events(), ["Scene Request Still Waiting"])
        self.assertIn("No release found in 8 days.", self.app.pushes[0][1]["message"])
        self.app.search_again([self.app.request_row(self.rid)])
        self.assertEqual(self.commands[-1], {"name": "MoviesSearch", "movieIds": [41]})
        row = self.app.request_row(self.rid)
        self.assertEqual((row["stalled_days"], row["notified"]), (0, ""))
        self.app.db.run("UPDATE requests SET searched_at = ?", (d.now() - 8 * 86400,))
        self.app.check_requests()
        self.assertEqual(len(self.app.pushes), 2)

    def test_stalled_tab(self):
        self.app.request_scene(self.admin, OTHER)            # new: not stalled
        listed = self.app.list_requests(self.admin, "stalled", 1)
        self.assertEqual((listed["count"], listed["requests"][0]["id"]), (1, self.rid))
        self.assertEqual(self.app.list_requests(self.admin, "approved", 1)["count"], 2)

    def test_give_up(self):
        row = self.app.give_up(self.admin, self.rid)
        self.assertEqual((row["status"], row["state"]["status"]), ("abandoned", "abandoned"))
        self.assertFalse(self.puts[-1]["monitored"])
        self.assertFalse(self.app.whisparr.index[SCENE]["monitored"])
        with self.assertRaises(d.ApiError):
            self.app.give_up(self.admin, self.rid)
        # and it can be asked for again
        self.app.whisparr.routes[("GET", "movie")] = [{"id": 41, "stashId": SCENE}]
        self.movies[41]["monitored"] = False
        again = self.app.request_scene(self.admin, SCENE)
        self.assertEqual(again["state"]["status"], "processing")

    def test_search_again_needs_a_waiting_request(self):
        self.make_available(SCENE)
        with self.assertRaises(d.ApiError):
            self.app.search_again([self.app.request_row(self.rid)])


class Watches(Base):
    def setUp(self):
        super().setUp()
        self.app.stash.listed = [card(SCENE, "2026-11-07T10:00:00Z"), card(OTHER, "2026-11-06T10:00:00Z")]

    def save(self, user=None, **kw):
        body = {"name": "Mine", "query": f"tags={TAG}&not_tags={TAG2}&page=4&sort=TITLE", "since": "2026-11-01"}
        body.update(kw)
        return self.app.save_watch(user or self.admin, body)

    def test_needs_a_filter(self):
        with self.assertRaises(d.ApiError):
            self.save(query=f"not_tags={TAG}&sort=DATE")
        w = self.save()
        self.assertEqual(w["query"], f"tags={TAG}&not_tags={TAG2}")   # filters only

    def test_requests_matches_as_its_owner(self):
        self.app.scene_tags = lambda ids: {SCENE: [TAG], OTHER: [TAG, TAG2]}   # OTHER is left out
        w = self.save()
        out = self.app.check_watch(w["id"])
        self.assertEqual(out, {"found": 1, "requested": 1, "waiting": 0})
        q = self.app.stash.queries[0]
        self.assertEqual((q["sort"], q["per_page"], q["date"]), ("UPDATED_AT", 100,
                         {"value": "2026-10-31", "modifier": "GREATER_THAN"}))
        r = self.app.db.one("SELECT * FROM requests")
        self.assertEqual((r["scene_id"], r["status"], r["via"], r["watch_id"]), (SCENE, "approved", "Mine", w["id"]))
        self.assertEqual(self.app.pushes[0][1]["message"], "A saved search made a request.")
        # the next check finds nothing new and asks for nothing twice
        self.assertEqual(self.app.check_watch(w["id"]), {"found": 0, "requested": 0, "waiting": 0})
        self.assertEqual(self.app.db.one("SELECT cursor FROM watches")["cursor"], "2026-11-07T10:00:00Z")

    def test_skips_wanted_and_asked_for(self):
        self.app.request_scene(self.user, SCENE)            # already asked for
        self.app.whisparr.index[OTHER] = {"id": 9, "monitored": True, "has_file": False}
        self.app.check_watch(self.save()["id"])
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM requests")["n"], 1)

    def test_ask_first_and_plain_users_leave_them_pending(self):
        self.app.check_watch(self.save(ask_first=True)["id"])
        self.assertEqual({r["status"] for r in self.app.db.all("SELECT * FROM requests")}, {"pending"})
        self.app.db.run("DELETE FROM requests")
        self.app.check_watch(self.save(user=self.user, name="Theirs")["id"])
        rows = self.app.db.all("SELECT * FROM requests")
        self.assertEqual({(r["status"], r["user_id"]) for r in rows}, {("pending", self.user["id"])})
        self.assertEqual(self.added, [])

    def test_scene_unknown_to_whisparr_is_tried_again(self):
        self.unknown.add(SCENE)
        w = self.save()
        self.assertEqual(self.app.check_watch(w["id"])["waiting"], 1)
        self.assertIsNone(self.app.db.one("SELECT * FROM requests WHERE scene_id = ?", (SCENE,)))
        self.assertNotIn("Scene Request Failed", self.events())
        self.unknown.clear()
        self.assertEqual(self.app.check_watch(w["id"])["requested"], 1)

    def test_gives_up_on_a_scene_after_two_weeks(self):
        self.unknown.add(SCENE)
        w = self.save()
        self.app.check_watch(w["id"])
        self.app.db.run("UPDATE watch_scenes SET seen = ? WHERE scene_id = ?", (d.now() - 15 * 86400, SCENE))
        self.assertEqual(self.app.check_watch(w["id"])["waiting"], 0)
        self.assertEqual(self.app.db.one("SELECT done FROM watch_scenes WHERE scene_id = ?", (SCENE,))["done"], 2)

    def test_whisparr_down_is_kept_and_retried(self):
        w = self.save()
        self.app.whisparr.routes.pop(("POST", "movie"))
        with self.assertRaises(d.ApiError):
            self.app.check_watch(w["id"])
        self.assertIn("unexpected", self.app.db.one("SELECT error FROM watches")["error"])
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM requests")["n"], 0)
        self.app.whisparr.routes[("POST", "movie")] = self.add_movie
        self.assertEqual(self.app.check_watch(w["id"])["requested"], 2)
        self.assertEqual(self.app.db.one("SELECT error FROM watches")["error"], "")

    def test_pages_until_what_was_seen(self):
        self.app.stash.listed = [card(f"{n:08d}-0000-0000-0000-000000000000", f"2026-11-07T{23 - n // 10:02d}:00:00Z")
                                 for n in range(150)]
        w = self.save()
        self.assertEqual(self.app.check_watch(w["id"])["found"], 150)
        self.assertEqual(len(self.app.stash.queries), 2)
        self.app.stash.queries.clear()
        self.app.check_watch(w["id"])
        self.assertEqual(len(self.app.stash.queries), 1)     # the first page reaches the cursor

    def test_due_ones_only(self):
        w = self.save()
        self.app.check_watches()
        self.assertEqual(len(self.app.stash.queries), 1)
        self.app.check_watches()
        self.assertEqual(len(self.app.stash.queries), 1)      # checked within the hour
        self.app.save_watch(self.admin, {"enabled": False}, w["id"])
        self.app.db.run("UPDATE watches SET checked = 0")
        self.app.check_watches()
        self.assertEqual(len(self.app.stash.queries), 1)      # paused

    def test_only_owners_and_admins(self):
        w = self.save(user=self.user)
        other = self.user_row(self.app.create_user("other", "password1"))
        with self.assertRaises(d.ApiError):
            self.app.watch(other, w["id"])
        self.assertEqual(self.app.watch(self.admin, w["id"])["id"], w["id"])
        self.assertEqual([x["name"] for x in self.app.watches(other)], [])


TAG = "aaaaaaaa-0000-0000-0000-000000000001"
TAG2 = "aaaaaaaa-0000-0000-0000-000000000002"


def card(sid, updated):
    return dict(scene(sid), updated=updated)


class JellyfinLinks(Base):
    def setUp(self):
        super().setUp()
        jf = self.app.jellyfin
        self.calls = []

        def call(path, timeout=20):
            self.calls.append(path)
            if path.startswith("System"):
                return {"Id": "srv1"}
            return {"Items": [{"Id": "jf1", "Path": "/mnt/storage/XXX/HD/a/a.mp4"},
                              {"Id": "jf2", "Path": "/media/elsewhere/b.mp4"}]}
        jf.call = call
        self.app.db.set_settings({"jellyfin_key": "KEY"})

    def test_found_by_path_or_file_name(self):
        self.assertEqual(self.app.jellyfin.find("/mnt/storage/XXX/HD/a/a.mp4"), "jf1")
        self.assertEqual(self.app.jellyfin.find("/mnt/storage/XXX/SD/b/b.mp4"), "jf2")
        self.assertEqual(len(self.calls), 2)   # one refresh: info and items

    def test_available_scenes_carry_it(self):
        self.app.request_scene(self.admin, SCENE)
        self.make_available(SCENE)
        self.assertEqual(self.app.state(SCENE)["jellyfin_id"], "jf1")

    def test_a_miss_refreshes_at_most_once_a_minute(self):
        self.app.jellyfin.find("/x/a.mp4")
        self.app.jellyfin.find("/x/c.mp4")
        self.assertEqual(len(self.calls), 2)

    def test_without_a_key_nothing_is_asked(self):
        self.app.db.set_settings({"jellyfin_key": ""})
        self.assertIsNone(self.app.jellyfin.find("/mnt/storage/XXX/HD/a/a.mp4"))
        self.assertEqual(self.calls, [])

    def test_init_sets_the_key_and_follows_a_new_one(self):
        self.app.db.set_settings({"jellyfin_key": ""})
        self.assertIn("Jellyfin key: set (available scenes get a Watch button)", d.init(self.app, jellyfin_key="K1"))
        self.assertIn("Jellyfin key: up to date", d.init(self.app, jellyfin_key="K1"))
        d.init(self.app, jellyfin_key="K2")
        self.assertEqual(self.app.db.setting("jellyfin_key"), "K2")
        self.app.db.set_settings({"jellyfin_key": "MINE"})       # set in Settings by hand
        self.assertIn("Jellyfin key: keeps the one set in Desireerr", d.init(self.app, jellyfin_key="K3"))
        self.assertEqual(self.app.db.setting("jellyfin_key"), "MINE")


class SettingsChecks(Base):
    def test_addresses_and_topics_are_checked(self):
        self.app.whisparr.routes[("GET", "qualityprofile")] = []
        for body in ({"app_url": "javascript:alert(1)"}, {"notify_server": "ntfy.sh"},
                     {"notify_topic": "has spaces"}, {"jellyfin_link": "http://a b"}):
            with self.assertRaises(d.ApiError, msg=body):
                self.app.save_settings(body)
        out = self.app.save_settings({"app_url": "https://x.example/", "notify_events": ["available", "nope"],
                                      "notify_discreet": False, "stall_days": "500"})
        self.assertEqual((out["app_url"], out["notify_events"], out["notify_discreet"], out["stall_days"]),
                         ("https://x.example", ["available"], False, 365))

    def test_old_databases_get_the_new_columns(self):
        path = Path(self.tmp.name) / "old.db"
        import sqlite3
        conn = sqlite3.connect(path)
        conn.executescript(d.SCHEMA.split("CREATE TABLE IF NOT EXISTS watches")[0])
        conn.close()
        cols = {r["name"] for r in d.DB(path).all("PRAGMA table_info(requests)")}
        self.assertTrue({"via", "watch_id", "notified", "searched_at"} <= cols)


if __name__ == "__main__":
    unittest.main()
