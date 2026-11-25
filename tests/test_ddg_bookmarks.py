# SPDX-License-Identifier: Apache-2.0
"""Tests for ddg_bookmarks.py, run against stores built here with the schema
DuckDuckGo for Mac's Core Data model gives Bookmarks.sqlite.

    python3 -m unittest discover -s tests
"""
import glob
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "skills", "bookmarks", "scripts", "ddg_bookmarks.py")
WRAPPER = os.path.join(HERE, "..", "bin", "ddg-bookmarks")
EPOCH = 978307200

SCHEMA = """
CREATE TABLE ZBOOKMARKENTITY ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZISFOLDER INTEGER, ZISPENDINGDELETION INTEGER, ZISSTUB INTEGER, ZPARENT INTEGER,
  Z_FOK_PARENT INTEGER, ZMODIFIEDAT TIMESTAMP, ZLASTCHILDRENPAYLOADRECEIVEDFROMSYNC VARCHAR,
  ZTITLE VARCHAR, ZURL VARCHAR, ZUUID VARCHAR );
CREATE TABLE Z_1FAVORITES ( Z_1FAVORITEFOLDERS INTEGER, Z_1FAVORITES INTEGER,
  Z_FOK_1FAVORITES INTEGER, PRIMARY KEY (Z_1FAVORITEFOLDERS, Z_1FAVORITES) );
CREATE INDEX ZBOOKMARKENTITY_ZPARENT_INDEX ON ZBOOKMARKENTITY (ZPARENT);
CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB);
"""

ROOTS = ["favorites_root", "bookmarks_root", "mobile_favorites_root", "desktop_favorites_root"]

TREE = [
    {"title": "GitHub", "url": "https://github.com/"},
    {"title": "Projects", "children": [
        {"title": "Alpha", "url": "https://alpha.example.com/"},
        {"title": "Beta", "children": [
            {"title": "Beta site", "url": "https://beta.example.com/"},
        ]},
    ]},
    {"title": "News", "url": "https://news.example.com/"},
]


def make_store(path, tree=TREE, favorite="News", journal="WAL", synced=False):
    """A store like DuckDuckGo's: four roots, TREE under bookmarks_root, and
    FAVORITE in the favorites and desktop favorites folders."""
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA journal_mode={journal}")
    con.executescript(SCHEMA)
    then = time.time() - EPOCH - 86400
    pk = 0
    ids = {}

    def row(parent, key, title, url, is_folder, uuid_):
        nonlocal pk
        pk += 1
        con.execute("INSERT INTO ZBOOKMARKENTITY VALUES (?, 1, 3, ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (pk, is_folder, parent, key, then, "[]" if synced and is_folder else None,
                     title, url, uuid_))
        return pk

    for r in ROOTS:
        ids[r] = row(None, None, r, None, 1, r)

    def put(parent, items):
        for n, item in enumerate(items, start=1):
            is_folder = "children" in item
            p = row(parent, n * 1024, item["title"], item.get("url"), int(is_folder),
                    f"UUID-{pk + 1:04d}")
            ids[item["title"]] = p
            if is_folder:
                put(p, item["children"])

    put(ids["bookmarks_root"], tree)
    if favorite:
        for folder in ("favorites_root", "desktop_favorites_root"):
            con.execute("INSERT INTO Z_1FAVORITES VALUES (?, ?, 1024)", (ids[folder], ids[favorite]))
    con.execute("INSERT INTO Z_PRIMARYKEY VALUES (1, 'BookmarkEntity', 0, ?)", (pk,))
    con.commit()
    con.close()
    return ids


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "Bookmarks.sqlite")
        self.ids = make_store(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None, env=None, db=True):
        argv = [sys.executable, "-I", SCRIPT] + list(args) + (["--db", self.db] if db else [])
        return subprocess.run(argv, input=stdin, capture_output=True, text=True,
                              env=env or dict(os.environ))

    def ok(self, *args, **kw):
        p = self.run_cli(*args, **kw)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p.stdout

    def refused(self, *args, says="", **kw):
        p = self.run_cli(*args, **kw)
        self.assertEqual(p.returncode, 2, p.stderr + p.stdout)
        self.assertIn(says, p.stderr)
        return p.stderr

    def spec(self, items):
        path = os.path.join(self.tmp.name, f"spec{time.monotonic_ns()}.json")
        with open(path, "w") as f:
            json.dump(items, f)
        return path

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def row(self, title):
        rows = self.q("SELECT Z_PK, Z_OPT, ZISFOLDER, ZPARENT, Z_FOK_PARENT, ZMODIFIEDAT, ZURL, ZUUID,"
                      " ZISPENDINGDELETION, ZISSTUB FROM ZBOOKMARKENTITY WHERE ZTITLE = ?", title)
        self.assertEqual(len(rows), 1, f"{len(rows)} rows titled {title!r}")
        keys = ["pk", "opt", "folder", "parent", "fok", "modified", "url", "uuid", "pending", "stub"]
        return dict(zip(keys, rows[0]))

    def titles(self, parent_title):
        pk = self.row(parent_title)["pk"]
        return [r[0] for r in self.q("SELECT ZTITLE FROM ZBOOKMARKENTITY WHERE ZPARENT = ?"
                                     " AND ZISPENDINGDELETION = 0 ORDER BY Z_FOK_PARENT, Z_PK", pk)]

    def backups(self):
        return glob.glob(self.db + ".bak-*")

    def snapshot(self):
        return self.q("SELECT * FROM ZBOOKMARKENTITY ORDER BY Z_PK"), \
            self.q("SELECT * FROM Z_PRIMARYKEY"), self.q("SELECT * FROM Z_1FAVORITES ORDER BY 1, 2")


class Reading(Store):
    def test_tree_prints_the_bar(self):
        out = self.ok("tree")
        self.assertEqual(out.splitlines(), [
            "GitHub  https://github.com/",
            "Projects ▸",
            "   Alpha  https://alpha.example.com/",
            "   Beta ▸",
            "      Beta site  https://beta.example.com/",
            "News  https://news.example.com/",
        ])

    def test_tree_of_one_folder(self):
        self.assertEqual(self.ok("tree", "Projects > Beta").splitlines(),
                         ["Beta site  https://beta.example.com/"])

    def test_tree_json_is_a_spec(self):
        self.assertEqual(json.loads(self.ok("tree", "--json")), TREE)

    def test_tree_of_a_bookmark_is_refused(self):
        self.refused("tree", "GitHub", says="not a folder")

    def test_find_by_title_and_link(self):
        hits = json.loads(self.ok("find", "beta", "--json"))
        self.assertEqual([h["path"] for h in hits], ["Projects > Beta", "Projects > Beta > Beta site"])
        self.assertEqual(self.ok("find", "NEWS.EXAMPLE").strip(), "News  https://news.example.com/")

    def test_find_nothing_exits_1(self):
        self.assertEqual(self.run_cli("find", "nowhere").returncode, 1)

    def test_missing_path(self):
        self.refused("tree", "Projects > Gamma", says="nothing titled 'Gamma'")

    def test_two_items_with_one_title(self):
        make_store(self.db + "2", tree=[{"title": "A", "url": "https://a.example.com/"},
                                        {"title": "A", "children": []}], favorite=None)
        p = subprocess.run([sys.executable, "-I", SCRIPT, "tree", "A", "--db", self.db + "2"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("2 items titled 'A'", p.stderr)

    def test_db_before_the_command(self):
        p = subprocess.run([sys.executable, "-I", SCRIPT, "--db", self.db, "tree"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Projects ▸", p.stdout)

    def test_wrapper_runs_the_script(self):
        p = subprocess.run(["sh", WRAPPER, "tree", "Projects > Beta", "--db", self.db],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "Beta site  https://beta.example.com/")


class Adding(Store):
    def test_add_to_the_bar(self):
        before = self.row("bookmarks_root")
        out = self.ok("add", self.spec([{"title": "Docs", "url": "https://docs.example.com/"},
                                        {"title": "Tools", "children": [
                                            {"title": "One", "url": "https://one.example.com/"}]}]))
        self.assertIn("added 3 row(s) to the bar;", out)
        self.assertEqual(self.titles("bookmarks_root"), ["GitHub", "Projects", "News", "Docs", "Tools"])
        docs, tools, one = self.row("Docs"), self.row("Tools"), self.row("One")
        self.assertEqual((docs["opt"], docs["folder"], docs["pending"], docs["stub"]), (1, 0, 0, None))
        self.assertEqual(docs["fok"], 3 * 1024 + 1024)
        self.assertEqual(tools["fok"], 3 * 1024 + 2048)
        self.assertEqual((tools["folder"], tools["url"]), (1, None))
        self.assertEqual((one["parent"], one["fok"]), (tools["pk"], 1024))
        self.assertEqual(docs["uuid"], docs["uuid"].upper())
        self.assertAlmostEqual(docs["modified"], time.time() - EPOCH, delta=60)
        after = self.row("bookmarks_root")
        self.assertEqual(after["opt"], before["opt"] + 1)
        self.assertGreater(after["modified"], before["modified"])
        z_max = self.q("SELECT Z_MAX FROM Z_PRIMARYKEY")[0][0]
        self.assertEqual(z_max, self.q("SELECT MAX(Z_PK) FROM ZBOOKMARKENTITY")[0][0])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.q("PRAGMA integrity_check")[0][0], "ok")

    def test_backup_is_one_file_holding_the_old_rows(self):
        self.ok("add", "--title", "Docs", "--url", "https://docs.example.com/")
        (bak,) = self.backups()
        self.assertFalse(os.path.exists(bak + "-wal") or os.path.exists(bak + "-shm"))
        con = sqlite3.connect(bak)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM ZBOOKMARKENTITY WHERE ZTITLE = 'Docs'")
                         .fetchone()[0], 0)
        con.close()

    def test_add_into_a_folder_after_an_item(self):
        self.ok("add", "--title", "Middle", "--url", "https://middle.example.com/",
                "--into", "Projects", "--after", "Alpha")
        self.assertEqual(self.titles("Projects"), ["Alpha", "Middle", "Beta"])
        self.assertEqual(self.row("Middle")["fok"], 1024 + 512)

    def test_add_after_with_no_room_renumbers(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE ZBOOKMARKENTITY SET Z_FOK_PARENT = Z_FOK_PARENT / 1024"
                    " WHERE ZPARENT = ?", (self.ids["Projects"],))
        con.commit()
        con.close()
        self.ok("add", "--title", "Middle", "--url", "https://middle.example.com/",
                "--into", "Projects", "--after", "Alpha")
        self.assertEqual(self.titles("Projects"), ["Alpha", "Middle", "Beta"])
        self.assertEqual([self.row(t)["fok"] for t in ("Alpha", "Middle", "Beta")], [1024, 2048, 3072])

    def test_add_from_standard_input(self):
        self.ok("add", "-", "--into", "Projects > Beta",
                stdin=json.dumps([{"title": "Beta docs", "url": "https://beta.example.com/docs"}]))
        self.assertEqual(self.titles("Beta"), ["Beta site", "Beta docs"])

    def test_an_empty_folder(self):
        self.ok("add", self.spec([{"title": "Reading", "children": []}]))
        self.assertEqual(self.row("Reading")["folder"], 1)
        self.assertEqual(self.titles("Reading"), [])

    def test_a_title_already_there_is_refused(self):
        before = self.snapshot()
        self.refused("add", "--title", "News", "--url", "https://other.example.com/",
                     says="already holds 'News'")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_merge_adds_only_whats_missing(self):
        spec = self.spec([{"title": "Projects", "children": [
            {"title": "Alpha", "url": "https://alpha.example.com/"},
            {"title": "Beta", "children": [
                {"title": "Beta site", "url": "https://beta.example.com/"},
                {"title": "Beta repo", "url": "https://github.com/example/beta"}]},
            {"title": "Gamma", "url": "https://gamma.example.com/"}]}])
        out = self.ok("add", spec, "--merge")
        self.assertRegex(out, r"added 2 row\(s\) to (Projects, Projects > Beta|Projects > Beta, Projects);")
        self.assertEqual(self.titles("Projects"), ["Alpha", "Beta", "Gamma"])
        self.assertEqual(self.titles("Beta"), ["Beta site", "Beta repo"])
        self.assertEqual(self.row("Gamma")["fok"], 3 * 1024)
        self.assertEqual(self.row("Beta repo")["fok"], 2 * 1024)
        # Both folders that gained children were touched; the bar wasn't.
        self.assertEqual(self.row("Projects")["opt"], 4)
        self.assertEqual(self.row("Beta")["opt"], 4)
        self.assertEqual(self.row("bookmarks_root")["opt"], 3)
        # Again: nothing to add, so nothing is written and no backup is made.
        before = self.snapshot()
        out = self.ok("add", spec, "--merge")
        self.assertIn("nothing to add", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_merge_skips_a_link_already_there_under_another_title(self):
        out = self.ok("add", "--title", "Alpha home", "--url", "https://alpha.example.com/",
                      "--into", "Projects", "--merge")
        self.assertIn("already holds https://alpha.example.com/ under another title", out)
        self.assertEqual(self.titles("Projects"), ["Alpha", "Beta"])

    def test_merge_skips_a_title_with_another_link(self):
        out = self.ok("add", "--title", "Alpha", "--url", "https://alpha.example.org/",
                      "--into", "Projects", "--merge")
        self.assertIn("with another link", out)
        self.assertEqual(self.row("Alpha")["url"], "https://alpha.example.com/")

    def test_merge_wont_mix_folders_and_bookmarks(self):
        self.refused("add", self.spec([{"title": "GitHub", "children": []}]), "--merge",
                     says="not a folder")
        self.refused("add", "--title", "Projects", "--url", "https://p.example.com/", "--merge",
                     says="not a bookmark")

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        out = self.ok("add", self.spec([{"title": "Docs", "url": "https://docs.example.com/"}]),
                      "--into", "Projects", "--dry-run")
        self.assertIn("would add to Projects:", out)
        self.assertIn("   Docs  https://docs.example.com/", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_bad_specs_are_refused(self):
        cases = [
            ([{"title": "X", "url": "javascript:alert(1)"}], "only http(s) links"),
            ([{"title": "X", "url": "data:text/html,hi"}], "only http(s) links"),
            ([{"title": "X", "url": "https://x.example.com/", "children": []}], "url (bookmark) or children"),
            ([{"url": "https://x.example.com/"}], "needs a title"),
            ([{"title": "X", "url": "https://x.example.com/"},
              {"title": "X", "url": "https://y.example.com/"}], "two items titled 'X'"),
            ([{"title": "A > B", "url": "https://x.example.com/"}], "separates a path"),
            ([{"title": "Tools >", "children": []}], "separates a path"),
            ([{"title": "> Tools", "url": "https://x.example.com/"}], "separates a path"),
            ([], "non-empty list"),
            ({"title": "X"}, "non-empty list"),
        ]
        before = self.snapshot()
        for items, says in cases:
            with self.subTest(says=says):
                self.refused("add", self.spec(items), says=says)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_into_a_bookmark_is_refused(self):
        self.refused("add", "--title", "X", "--url", "https://x.example.com/", "--into", "GitHub",
                     says="not a folder")

    def test_after_an_item_not_there(self):
        self.refused("add", "--title", "X", "--url", "https://x.example.com/", "--after", "Nope",
                     says="nothing titled 'Nope'")

    def test_spec_and_title_together(self):
        self.refused("add", self.spec([{"title": "X", "url": "https://x.example.com/"}]),
                     "--title", "Y", says="not both")

    def test_a_synced_store_gets_a_note(self):
        make_store(self.db + "s", synced=True)
        p = subprocess.run([sys.executable, "-I", SCRIPT, "add", "--title", "X", "--url",
                            "https://x.example.com/", "--db", self.db + "s"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("DuckDuckGo Sync has run on this store", p.stderr)


class Removing(Store):
    def pending(self, pk):
        return self.q("SELECT ZISPENDINGDELETION, ZTITLE, ZURL, Z_OPT FROM ZBOOKMARKENTITY"
                      " WHERE Z_PK = ?", pk)[0]

    def test_remove_marks_a_folder_and_all_it_holds(self):
        before = self.row("bookmarks_root")
        out = self.ok("remove", "Projects")
        self.assertIn("removed 'Projects' (4 item(s))", out)
        for title in ("Projects", "Alpha", "Beta", "Beta site"):
            done, title_after, url_after, opt = self.pending(self.ids[title])
            self.assertEqual((done, title_after, url_after, opt), (1, None, None, 4), title)
        # The rows stay for the app to clear; the bar is dated and bumped.
        self.assertEqual(self.q("SELECT COUNT(*) FROM ZBOOKMARKENTITY")[0][0], 4 + 6)
        after = self.row("bookmarks_root")
        self.assertEqual(after["opt"], before["opt"] + 1)
        self.assertGreater(after["modified"], before["modified"])
        self.assertEqual(self.ok("tree").splitlines(),
                         ["GitHub  https://github.com/", "News  https://news.example.com/"])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.q("PRAGMA integrity_check")[0][0], "ok")

    def test_remove_leaves_favorites_links_as_the_app_does(self):
        self.ok("remove", "News")
        self.assertEqual(self.pending(self.ids["News"])[0], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM Z_1FAVORITES")[0][0], 2)
        self.assertEqual(self.row("favorites_root")["opt"], 3)

    def test_a_removed_title_can_be_added_again(self):
        self.ok("remove", "News")
        self.ok("add", "--title", "News", "--url", "https://news.example.org/")
        self.assertEqual(self.ok("find", "news").strip(), "News  https://news.example.org/")

    def test_remove_dry_run(self):
        before = self.snapshot()
        out = self.ok("remove", "Projects > Beta", "--dry-run")
        self.assertIn("would remove 2 item(s)", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_remove_needs_a_path(self):
        self.refused("remove", "", says="give the path")


class Review(Store):
    """Cases a review on 2026-11-23 found."""

    def test_an_item_titled_twice_is_never_merged_into(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE ZBOOKMARKENTITY SET ZTITLE = 'Projects', ZURL = NULL, ZISFOLDER = 1"
                    " WHERE ZTITLE = 'News'")
        con.commit()
        con.close()
        before = self.snapshot()
        self.refused("add", self.spec([{"title": "Projects", "children": [
            {"title": "New", "url": "https://new.example.com/"}]}]), "--merge",
            says="holds 2 items titled 'Projects'")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_after_an_item_titled_twice(self):
        self.ok("add", self.spec([{"title": "Docs", "children": []}]), "--into", "Projects")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE ZBOOKMARKENTITY SET ZTITLE = 'Docs', ZURL = NULL WHERE ZTITLE = 'Alpha'")
        con.commit()
        con.close()
        for dry in ([], ["--dry-run"]):
            with self.subTest(dry=dry):
                self.refused("add", "--title", "X", "--url", "https://x.example.com/",
                             "--into", "Projects", "--after", "Docs", *dry,
                             says="2 items titled 'Docs'")
        self.assertEqual(len(self.backups()), 1)

    def test_stubs_and_untitled_rows(self):
        con = sqlite3.connect(self.db)
        projects = self.ids["Projects"]
        con.execute("INSERT INTO ZBOOKMARKENTITY VALUES (90, 1, 1, 0, 0, 1, ?, 99999, 0, NULL,"
                    " NULL, NULL, 'STUB')", (projects,))
        con.execute("INSERT INTO ZBOOKMARKENTITY VALUES (91, 1, 1, 0, 0, 0, ?, 99998, 0, NULL,"
                    " NULL, 'https://untitled.example.com/', 'BARE')", (projects,))
        con.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = 91")
        con.commit()
        con.close()
        out = self.ok("tree", "Projects")
        self.assertNotIn("None", out)
        self.assertIn("(untitled)  https://untitled.example.com/", out)
        self.assertEqual(len(out.splitlines()), 4)
        hits = json.loads(self.ok("find", "", "--json"))
        self.assertEqual(len(hits), 7)

    def race(self, merge):
        """Run add in-process, with the app adding the same bookmark between
        the plan and the write; return the module and the backups made."""
        spec = importlib.util.spec_from_file_location("ddg_bookmarks", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real, made = mod.backup, []

        def backup_then_app_adds(con, path):
            made.append(real(con, path))
            app = sqlite3.connect(path)
            app.execute("INSERT INTO ZBOOKMARKENTITY VALUES (50, 1, 1, 0, 0, NULL, ?, 99999, 0, NULL,"
                        " 'Docs', 'https://docs.example.com/', 'APP-DOCS')", (self.ids["bookmarks_root"],))
            app.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = 50")
            app.commit()
            app.close()
            return made[-1]

        mod.backup = backup_then_app_adds
        con = mod.connect(self.db)
        self.addCleanup(con.close)
        return mod, con, made

    def test_a_merge_that_finds_its_work_done_leaves_nothing(self):
        mod, con, made = self.race(merge=True)
        with redirect_stdout(io.StringIO()), self.assertRaises(mod.Nothing):
            mod.add(con, self.db, [{"title": "Docs", "url": "https://docs.example.com/"}], "", None,
                    merge=True)
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))
        self.assertEqual(self.q("SELECT COUNT(*) FROM ZBOOKMARKENTITY WHERE ZTITLE = 'Docs'")[0][0], 1)
        self.assertEqual(self.q("PRAGMA integrity_check")[0][0], "ok")

    def test_a_refusal_inside_the_write_leaves_no_backup(self):
        mod, con, made = self.race(merge=False)
        with redirect_stdout(io.StringIO()), self.assertRaises(mod.Refused):
            mod.add(con, self.db, [{"title": "Docs", "url": "https://docs.example.com/"}], "", None)
        self.assertFalse(os.path.exists(made[0]))
        self.assertEqual(self.q("SELECT COUNT(*) FROM ZBOOKMARKENTITY WHERE ZTITLE = 'Docs'")[0][0], 1)

    def test_an_empty_db_never_falls_through(self):
        self.refused("tree", "--db", "", db=False, says="--db is empty")
        env = dict(os.environ, DDG_BOOKMARKS_DB="")
        self.refused("tree", db=False, env=env, says="DDG_BOOKMARKS_DB is set but empty")


class Finding(unittest.TestCase):
    """Which store is used when none is named."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = {k: v for k, v in os.environ.items() if k != "DDG_BOOKMARKS_DB"}
        self.env["HOME"] = self.home

    def tearDown(self):
        self.tmp.cleanup()

    def store_in(self, container):
        d = os.path.join(self.home, "Library", "Containers", container, "Data", "Library",
                         "Application Support")
        os.makedirs(d)
        make_store(os.path.join(d, "Bookmarks.sqlite"))
        return os.path.join(d, "Bookmarks.sqlite")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", SCRIPT] + list(args), capture_output=True,
                              text=True, env=self.env)

    def test_the_direct_download(self):
        path = self.store_in("com.duckduckgo.macos.browser")
        self.assertEqual(self.run_cli("where").stdout.strip(), path)
        self.assertIn("Projects ▸", self.run_cli("tree").stdout)

    def test_the_app_store_build(self):
        path = self.store_in("com.duckduckgo.mobile.ios")
        self.assertEqual(self.run_cli("where").stdout.strip(), path)

    def test_both_builds_is_a_question(self):
        self.store_in("com.duckduckgo.macos.browser")
        self.store_in("com.duckduckgo.mobile.ios")
        p = self.run_cli("tree")
        self.assertEqual(p.returncode, 2)
        self.assertIn("more than one", p.stderr)

    def test_none(self):
        p = self.run_cli("tree")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no DuckDuckGo for Mac bookmarks store found", p.stderr)

    def test_environment_names_one(self):
        self.store_in("com.duckduckgo.macos.browser")
        other = os.path.join(self.home, "copy.sqlite")
        make_store(other, tree=[{"title": "Only", "url": "https://only.example.com/"}], favorite=None)
        self.env["DDG_BOOKMARKS_DB"] = other
        self.assertEqual(self.run_cli("tree").stdout.strip(), "Only  https://only.example.com/")

    def test_a_store_macos_wont_let_us_read(self):
        path = self.store_in("com.duckduckgo.macos.browser")
        locked = os.path.dirname(path)
        os.chmod(locked, 0)
        try:
            if os.access(locked, os.X_OK):
                self.skipTest("running as a user that permissions don't stop")
            p = self.run_cli("tree")
            self.assertEqual(p.returncode, 2)
            self.assertIn("Full Disk Access", p.stderr)
            p = self.run_cli("tree", "--db", path)
            self.assertEqual(p.returncode, 2)
            self.assertIn("can't read", p.stderr)
        finally:
            os.chmod(locked, 0o755)

    def test_a_missing_db_is_not_created(self):
        missing = os.path.join(self.home, "nope.sqlite")
        p = self.run_cli("tree", "--db", missing)
        self.assertEqual(p.returncode, 2)
        self.assertIn("no such store", p.stderr)
        self.assertFalse(os.path.exists(missing))

    def test_other_databases_are_refused(self):
        other = os.path.join(self.home, "other.sqlite")
        con = sqlite3.connect(other)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (x)")
        con.close()
        p = self.run_cli("tree", "--db", other)
        self.assertEqual(p.returncode, 2)
        self.assertIn("isn't DuckDuckGo's Bookmarks.sqlite", p.stderr)

    def test_a_rollback_journal_store_is_refused(self):
        path = os.path.join(self.home, "delete.sqlite")
        make_store(path, journal="DELETE")
        p = self.run_cli("tree", "--db", path)
        self.assertEqual(p.returncode, 2)
        self.assertIn("expected a WAL store", p.stderr)


class IrieManifest(unittest.TestCase):
    """Irie gives sessions a manifest's skill only when the table's id is the
    name the skill's SKILL.md gives itself, and the word must name that id."""

    def test_names_agree(self):
        import re
        root = os.path.join(HERE, "..")
        manifest = open(os.path.join(root, "irie", "ddg-bookmarks.toml")).read()
        skills = re.findall(r"^\[skills\.([a-z0-9-]+)\]$", manifest, re.M)
        self.assertEqual(len(skills), 1, skills)
        path = re.search(r'^path = "~/[^"]*/(skills/[a-z0-9-]+)"$', manifest, re.M).group(1)
        skill_md = open(os.path.join(root, path, "SKILL.md")).read()
        name = re.search(r"^name: (\S+)$", skill_md, re.M).group(1)
        self.assertEqual(skills[0], name)
        self.assertEqual(re.search(r'^skill = "([^"]+)"$', manifest, re.M).group(1), name)


if __name__ == "__main__":
    unittest.main()
