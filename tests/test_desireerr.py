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

    def find_scene(self, sid):
        return self.scenes.get(sid)


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

    def test_new_favourite_is_followed_from_today(self):
        out = self.app.set_favorite("performer", SCENE, True)
        self.assertEqual(self.favs, [("performer", SCENE, True)])
        self.assertTrue(out["is_favorite"])
        self.assertEqual((self.posted[0]["afterDate"], self.posted[0]["monitored"]), (d.today(), True))

    def test_already_followed_is_left_alone(self):
        self.app.whisparr.routes[("GET", "performer")] = [{"id": 3, "foreignId": SCENE, "monitored": True,
                                                           "afterDate": "2026-10-30"}]
        out = self.app.set_favorite("performer", SCENE, True)
        self.assertEqual(out["follow"], {"followed": True, "since": "2026-10-30"})
        self.assertEqual(self.posted, [])

    def test_unfavourite_leaves_whisparr(self):
        self.app.set_favorite("performer", SCENE, False)
        self.assertEqual(self.favs, [("performer", SCENE, False)])
        self.assertEqual(self.app.whisparr.calls, [])

    def test_whisparr_trouble_does_not_undo_the_favourite(self):
        self.app.whisparr.routes[("GET", "performer")] = lambda b, p: (_ for _ in ()).throw(d.ApiError(502, "down"))
        out = self.app.set_favorite("performer", SCENE, True)
        self.assertEqual((out["is_favorite"], out["follow_error"]), (True, "down"))


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


if __name__ == "__main__":
    unittest.main()
