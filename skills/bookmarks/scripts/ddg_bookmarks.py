#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read and change DuckDuckGo for Mac's bookmarks while the browser runs.

DuckDuckGo keeps bookmarks in a Core Data SQLite store (WAL mode) and reads
it through fresh contexts every time, so rows written here show in a running
browser after its next bookmarks load: any bookmark added, edited or moved in
the app, or its next launch. Writes copy what the app's BookmarkEntity does:
new rows get Z_OPT 1 and modifiedAt now, the parent's Z_OPT is bumped and its
modifiedAt set, Z_PRIMARYKEY moves on, and a removal marks rows for deletion
as the app's own removal does. Each write is one IMMEDIATE transaction, with
a one-file backup beside the store first.

usage:
  ddg-bookmarks tree [PATH] [--json]       the bar, or one folder
  ddg-bookmarks find TEXT [--json]         items whose title or link holds TEXT
  ddg-bookmarks add SPEC.json|- [--into PATH] [--after TITLE] [--merge] [--dry-run]
  ddg-bookmarks add --title T --url U [--into PATH] [--after TITLE] [--merge] [--dry-run]
  ddg-bookmarks remove PATH [--dry-run]    an item, with all it holds
  ddg-bookmarks where                      which store it uses
  (every command takes --db FILE, e.g. to work on a copy)

PATH is titles from the bar down, joined by " > ", e.g. "Projects > GitHub".
SPEC is a JSON list of items: {"title": T, "url": U} for a bookmark, or
{"title": T, "children": [items]} for a folder. `tree --json` prints the same
shape. --merge makes add safe to repeat: a folder already there takes only the
children it lacks, and a bookmark already there by title or link is skipped.
"""
import argparse
import contextlib
import json
import os
import sqlite3
import stat
import sys
import time
import urllib.parse
import uuid

__version__ = "0.1.0"

# The direct download (Developer ID) build and the Mac App Store build keep
# their data in different sandbox containers (DuckDuckGo's
# macOS/Configuration/DeveloperID.xcconfig and AppStore.xcconfig).
CONTAINERS = ("com.duckduckgo.macos.browser", "com.duckduckgo.mobile.ios")
STORE = os.path.join("Data", "Library", "Application Support", "Bookmarks.sqlite")
CORE_DATA_EPOCH = 978307200  # 2001-01-01 in Unix time
STEP = 1024  # spacing of Z_FOK_PARENT, as Core Data leaves it
# What the app counts as a folder's children (BookmarkEntity.childrenArray):
# neither a Sync stub nor pending deletion. The app leaves ZISSTUB NULL.
LIVE = "ZISPENDINGDELETION = 0 AND (ZISSTUB IS NULL OR ZISSTUB = 0)"
ACCESS = ("macOS didn't let this terminal read another app's data: give the terminal Full Disk "
          "Access (System Settings › Privacy & Security), or allow it when macOS asks about "
          "data from other apps")


class Refused(Exception):
    """A command that can't go ahead; nothing was written."""


class Nothing(Refused):
    """Nothing to do after all; nothing was written, and that's no error."""


# ---------------------------------------------------------------- the store

def find_store(explicit=None):
    """The store to use: --db, else $DDG_BOOKMARKS_DB, else the one container
    that holds a store. An empty value is refused rather than skipped, so a
    variable that isn't set never sends a write to the browser's own store,
    and two stores are a question for the person, never a guess."""
    if explicit is not None:
        if not explicit.strip():
            raise Refused("--db is empty: give it the path of a Bookmarks.sqlite")
        return explicit
    if "DDG_BOOKMARKS_DB" in os.environ:
        env = os.environ["DDG_BOOKMARKS_DB"]
        if not env.strip():
            raise Refused("DDG_BOOKMARKS_DB is set but empty: unset it, or give it a path")
        return env
    home = os.path.expanduser("~")
    found, denied = [], []
    for container in CONTAINERS:
        path = os.path.join(home, "Library", "Containers", container, STORE)
        try:
            if stat.S_ISREG(os.stat(path).st_mode):
                found.append(path)
        except PermissionError:
            denied.append(path)
        except OSError:
            pass
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise Refused("found more than one DuckDuckGo bookmarks store; pass --db with the one "
                      "to use:\n  " + "\n  ".join(found))
    if denied:
        raise Refused(ACCESS)
    raise Refused("no DuckDuckGo for Mac bookmarks store found; "
                  "pass --db with the path to Bookmarks.sqlite")


def connect(path):
    """Open an existing store read-write, never creating one."""
    try:
        st = os.stat(path)
    except PermissionError:
        raise Refused(f"can't read {path}: {ACCESS}")
    except OSError:
        raise Refused(f"no such store: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"not a file: {path}")
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=rw"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            raise Refused("the store is busy, as DuckDuckGo may be saving: try again in a moment")
        raise Refused(f"can't open {path}: {e}")
    except sqlite3.DatabaseError as e:
        raise Refused(f"{path} isn't a SQLite store: {e}")
    if not {"ZBOOKMARKENTITY", "Z_PRIMARYKEY", "Z_1FAVORITES"} <= names:
        raise Refused(f"{path} isn't DuckDuckGo's Bookmarks.sqlite (its tables differ)")
    if mode != "wal":
        raise Refused("expected a WAL store, as DuckDuckGo keeps it: is this its Bookmarks.sqlite?")
    return con


@contextlib.contextmanager
def snapshot(con):
    """One read transaction, so what's read is the store as of one moment,
    even while the app saves."""
    con.execute("BEGIN")
    try:
        yield
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")


def synced(con):
    """Whether DuckDuckGo Sync has delivered rows into this store."""
    return con.execute("SELECT COUNT(*) FROM ZBOOKMARKENTITY"
                       " WHERE ZLASTCHILDRENPAYLOADRECEIVEDFROMSYNC IS NOT NULL").fetchone()[0] > 0


def root_pk(con):
    row = con.execute("SELECT Z_PK FROM ZBOOKMARKENTITY WHERE ZUUID = 'bookmarks_root'"
                      " AND ZPARENT IS NULL").fetchone()
    if row is None:
        raise Refused("no bookmarks_root in this store")
    return row[0]


def children(con, parent):
    """(pk, order key, is folder, title, url) for each live child, in order.
    A missing title or link reads as ''."""
    return con.execute(
        "SELECT Z_PK, Z_FOK_PARENT, ZISFOLDER, IFNULL(ZTITLE, ''), IFNULL(ZURL, '')"
        f" FROM ZBOOKMARKENTITY WHERE ZPARENT = ? AND {LIVE} ORDER BY Z_FOK_PARENT, Z_PK",
        (parent,)).fetchall()


def split_path(path):
    return [t.strip() for t in path.split(" > ")] if path.strip() else []


def resolve(con, path):
    """The row id for PATH, from the bar down; the bar itself for ''."""
    pk = root_pk(con)
    for title in split_path(path):
        hits = [c for c in children(con, pk) if c[3] == title]
        if not hits:
            raise Refused(f"nothing titled {title!r} on the way to {path!r}")
        if len(hits) > 1:
            raise Refused(f"{len(hits)} items titled {title!r} on the way to {path!r}: "
                          "rename one in the app first")
        pk = hits[0][0]
    return pk


def folder(con, path):
    """The row id of the folder at PATH; the bar for ''."""
    pk = resolve(con, path)
    if not con.execute("SELECT ZISFOLDER FROM ZBOOKMARKENTITY WHERE Z_PK = ?", (pk,)).fetchone()[0]:
        raise Refused(f"{path!r} is a bookmark, not a folder")
    return pk


def where_label(path):
    return path if path.strip() else "the bar"


def join(path, title):
    return f"{path} > {title}" if path.strip() else title


# ------------------------------------------------------------------ reading

def as_spec(con, pk):
    out = []
    for cpk, _, is_folder, title, url in children(con, pk):
        if is_folder:
            out.append({"title": title, "children": as_spec(con, cpk)})
        else:
            out.append({"title": title, "url": url})
    return out


def show(items, depth=0):
    for item in items:
        pad = "   " * depth
        title = item["title"] or "(untitled)"
        if "children" in item:
            print(f"{pad}{title} ▸")
            show(item["children"], depth + 1)
        else:
            print(f"{pad}{title}  {item['url']}")


def walk(con, pk, trail):
    for cpk, _, is_folder, title, url in children(con, pk):
        path = trail + [title]
        yield path, bool(is_folder), title, url
        if is_folder:
            for hit in walk(con, cpk, path):
                yield hit


def find(con, text):
    needle = text.casefold()
    return [{"path": " > ".join(path), "title": title, "url": url or None, "folder": is_folder}
            for path, is_folder, title, url in walk(con, root_pk(con), [])
            if needle in title.casefold() or needle in url.casefold()]


# ------------------------------------------------------------------ writing

def check_spec(items, where="spec"):
    """A spec is a non-empty list; a folder in it may be empty."""
    if not isinstance(items, list) or (where == "spec" and not items):
        raise Refused(f"{where}: expected a non-empty list of items")
    titles = set()
    for item in items:
        title = item.get("title") if isinstance(item, dict) else None
        if not isinstance(title, str) or not title.strip():
            raise Refused(f"{where}: every item needs a title")
        # Padded, so a title that starts with "> " or ends with " >" is
        # caught too: either would break the paths that name it.
        if title != title.strip() or " > " in f" {title} ":
            raise Refused(f"{where} > {title}: a title can't start or end with spaces, "
                          "or hold \" > \", which separates a path")
        if title in titles:
            raise Refused(f"{where}: two items titled {title!r}")
        titles.add(title)
        if ("url" in item) == ("children" in item):
            raise Refused(f"{where} > {title}: give a url (bookmark) or children (folder)")
        if "url" in item:
            url = item["url"]
            if not isinstance(url, str) or not url.startswith(("https://", "http://")) \
                    or any(c.isspace() for c in url):
                raise Refused(f"{where} > {title}: only http(s) links, with no spaces")
        else:
            check_spec(item["children"], f"{where} > {title}")


def backup(con, path):
    """A one-file copy beside the store, made only once the checks pass, so a
    refused command leaves nothing behind. Returns its path."""
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d}Z"
    dst = path + ".bak-" + stamp
    out = sqlite3.connect(dst)
    con.backup(out)
    # Rollback-journal mode, so the copy is one file with no -wal or -shm
    # beside it (Apple's SQLite keeps WAL files after close).
    out.execute("PRAGMA journal_mode=DELETE")
    out.close()
    for extra in ("-wal", "-shm"):
        if os.path.exists(dst + extra):
            os.remove(dst + extra)
    return dst


def touch(con, pk, now):
    """What a save does to a folder whose children change."""
    con.execute("UPDATE ZBOOKMARKENTITY SET Z_OPT = Z_OPT + 1, ZMODIFIEDAT = ? WHERE Z_PK = ?",
                (now, pk))


def finish(con):
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("integrity check failed")
    con.execute("COMMIT")


@contextlib.contextmanager
def writing(con, bak):
    """One IMMEDIATE transaction. A refusal inside it rolls back and takes
    the fresh backup away, as a refusal before it would have made none; any
    other error rolls back and keeps the backup."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException as e:
        if con.in_transaction:
            con.execute("ROLLBACK")
        if isinstance(e, Refused):
            if os.path.exists(bak):
                os.remove(bak)
        else:
            print(f"backup kept: {bak}", file=sys.stderr)
        raise


def count(items):
    return sum(1 + count(i.get("children", [])) for i in items)


def check_after(con, parent, after, where):
    if after is None:
        return
    n = sum(1 for c in children(con, parent) if c[3] == after)
    if n == 0:
        raise Refused(f"nothing titled {after!r} in {where}")
    if n > 1:
        raise Refused(f"{n} items titled {after!r} in {where}: rename one in the app first")


def plan(con, parent, items, merge, path, nested, notes):
    """The ITEMS that PARENT, the folder at PATH, lacks. With merge, a folder
    already there takes the children it lacks (appended to NESTED as (pk,
    items, path)) and a bookmark already there, by title or by link, is
    skipped with a note; without it, any title already there refuses the
    command. A title held twice is refused either way, as a path to it is."""
    where = where_label(path)
    held, times, links = {}, {}, set()
    for pk, _, is_folder, title, url in children(con, parent):
        held.setdefault(title, (pk, is_folder, url))
        times[title] = times.get(title, 0) + 1
        if url:
            links.add(url)
    new = []
    for item in items:
        title = item["title"]
        if title in held:
            pk, is_folder, url = held[title]
            if times[title] > 1:
                raise Refused(f"{where} holds {times[title]} items titled {title!r}: "
                              "rename one in the app first")
            if not merge:
                raise Refused(f"{where} already holds {title!r} (--merge adds only what's missing)")
            if "children" in item:
                if not is_folder:
                    raise Refused(f"{where} holds a bookmark titled {title!r}, not a folder")
                sub = join(path, title)
                lacking = plan(con, pk, item["children"], merge, sub, nested, notes)
                if lacking:
                    nested.append((pk, lacking, sub))
            elif is_folder:
                raise Refused(f"{where} holds a folder titled {title!r}, not a bookmark")
            elif url == item["url"]:
                notes.append(f"already there: {join(path, title)}")
            else:
                notes.append(f"skipped: {where} already holds {title!r}, with another link ({url})")
            continue
        if merge and "url" in item and item["url"] in links:
            notes.append(f"skipped: {where} already holds {item['url']} under another title")
            continue
        new.append(item)
    return new


def order_keys(con, siblings, n, after, where):
    """Order keys for N new items among SIBLINGS: after the sibling titled
    AFTER when there is room in the gap, else renumbering the folder; at the
    end when AFTER is None."""
    foks = [s[1] or 0 for s in siblings]
    if after is None:
        start = (max(foks) if foks else 0) + STEP
        return [start + i * STEP for i in range(n)]
    idx = [i for i, s in enumerate(siblings) if s[3] == after]
    if len(idx) != 1:
        raise Refused(f"expected one item titled {after!r} in {where}")
    i = idx[0]
    lo = foks[i]
    hi = foks[i + 1] if i + 1 < len(foks) else lo + STEP * (n + 1)
    gap = (hi - lo) // (n + 1)
    if gap >= 1:
        return [lo + gap * (k + 1) for k in range(n)]
    order = [s[0] for s in siblings[:i + 1]] + [None] * n + [s[0] for s in siblings[i + 1:]]
    keys = []
    for k, pk in enumerate(order, start=1):
        if pk is None:
            keys.append(k * STEP)
        else:
            con.execute("UPDATE ZBOOKMARKENTITY SET Z_FOK_PARENT = ? WHERE Z_PK = ?", (k * STEP, pk))
    return keys


def sync_note(con):
    if synced(con):
        print("note: DuckDuckGo Sync has run on this store; changes made outside the app "
              "haven't been tested with Sync", file=sys.stderr)


def add(con, path, items, into, after, merge=False, dry_run=False):
    check_spec(items)
    where = where_label(into)
    with snapshot(con):
        parent = folder(con, into)
        check_after(con, parent, after, where)
        nested, notes = [], []
        top = plan(con, parent, items, merge, into, nested, notes)
    for note in notes:
        print(note)
    rows = count(top) + sum(count(n[1]) for n in nested)
    if not rows:
        print(f"nothing to add to {where}")
        return 0
    if dry_run:
        if top:
            print(f"would add to {where}:")
            show(top, 1)
        for _, lacking, label in nested:
            print(f"would add to {label}:")
            show(lacking, 1)
        print(f"dry run: {rows} row(s) would be added; nothing was written")
        return rows
    sync_note(con)
    bak = backup(con, path)
    with writing(con, bak):
        # Plan again inside the transaction, in case the app changed the
        # store since the first look.
        parent = folder(con, into)
        check_after(con, parent, after, where)
        nested, notes = [], []
        top = plan(con, parent, items, merge, into, nested, notes)
        if not top and not nested:
            raise Nothing(f"nothing to add to {where}: it was added while this ran")
        now = time.time() - CORE_DATA_EPOCH
        ent, z_max = con.execute(
            "SELECT Z_ENT, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'BookmarkEntity'").fetchone()
        next_pk = max(z_max, con.execute("SELECT MAX(Z_PK) FROM ZBOOKMARKENTITY").fetchone()[0]) + 1
        first_pk = next_pk

        def insert(parent_pk, key, item):
            nonlocal next_pk
            pk = next_pk
            next_pk += 1
            url = item.get("url")
            con.execute(
                "INSERT INTO ZBOOKMARKENTITY (Z_PK, Z_ENT, Z_OPT, ZISFOLDER,"
                " ZISPENDINGDELETION, ZISSTUB, ZPARENT, Z_FOK_PARENT, ZMODIFIEDAT,"
                " ZLASTCHILDRENPAYLOADRECEIVEDFROMSYNC, ZTITLE, ZURL, ZUUID)"
                " VALUES (?, ?, 1, ?, 0, NULL, ?, ?, ?, NULL, ?, ?, ?)",
                (pk, ent, 0 if url else 1, parent_pk, key, now, item["title"], url,
                 str(uuid.uuid4()).upper()))
            for n, child in enumerate(item.get("children", []), start=1):
                insert(pk, n * STEP, child)

        if top:
            keys = order_keys(con, children(con, parent), len(top), after, where)
            for key, item in zip(keys, top):
                insert(parent, key, item)
            touch(con, parent, now)
        for pk, lacking, label in nested:
            for key, item in zip(order_keys(con, children(con, pk), len(lacking), None, label),
                                 lacking):
                insert(pk, key, item)
            touch(con, pk, now)
        con.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = 'BookmarkEntity'",
                    (next_pk - 1,))
        finish(con)
    added = next_pk - first_pk
    places = ([where] if top else []) + [label for _, _, label in nested]
    print(f"backup: {bak}")
    print(f"added {added} row(s) to {', '.join(places)}; DuckDuckGo shows them after its "
          "next bookmark change or launch")
    return added


def marked(con, pk):
    """PK and every live item inside it, in the order the app's
    markPendingDeletion walks them."""
    out, queue = [], [pk]
    while queue:
        item = queue.pop(0)
        out.append(item)
        queue.extend(c[0] for c in children(con, item))
    return out


def remove(con, path, target, dry_run=False):
    """Mark TARGET, and all it holds, for deletion as the app's own removal
    does (BookmarkEntity.markPendingDeletion): no title, no link, pending
    deletion, saved with a new modifiedAt, and its folder dated too. The app
    hides such rows at once and clears them itself, after Sync has sent the
    deletion when Sync is on. Favorites links stay, as the app leaves them."""
    if not target.strip():
        raise Refused("give the path of the item to remove")
    with snapshot(con):
        items = len(marked(con, resolve(con, target)))
    if dry_run:
        print(f"dry run: removing {target!r} would remove {items} item(s); nothing was written")
        return items
    sync_note(con)
    bak = backup(con, path)
    with writing(con, bak):
        pk = resolve(con, target)
        parent = con.execute("SELECT ZPARENT FROM ZBOOKMARKENTITY WHERE Z_PK = ?", (pk,)).fetchone()[0]
        doomed = marked(con, pk)
        now = time.time() - CORE_DATA_EPOCH
        marks = ",".join("?" * len(doomed))
        con.execute("UPDATE ZBOOKMARKENTITY SET ZTITLE = NULL, ZURL = NULL, ZISPENDINGDELETION = 1,"
                    f" Z_OPT = Z_OPT + 1, ZMODIFIEDAT = ? WHERE Z_PK IN ({marks})", [now] + doomed)
        touch(con, parent, now)
        finish(con)
    print(f"backup: {bak}")
    print(f"removed {target!r} ({len(doomed)} item(s)), as DuckDuckGo removes its own: "
          "it hides them now and clears them itself")
    return len(doomed)


# --------------------------------------------------------------- the command

def read_spec(args):
    if args.spec is not None and (args.title or args.url):
        raise Refused("give a SPEC or --title and --url, not both")
    if args.spec is None:
        if not (args.title and args.url):
            raise Refused("give a SPEC file (or - for standard input), or --title and --url")
        return [{"title": args.title, "url": args.url}]
    try:
        if args.spec == "-":
            return json.load(sys.stdin)
        with open(args.spec, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"can't read the spec: {e}")


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="the Bookmarks.sqlite to use (default: DuckDuckGo's own)")
    ap = argparse.ArgumentParser(prog="ddg-bookmarks", parents=[common],
                                 description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n\n".join(__doc__.split("\n\n")[2:]))
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tree", parents=[common], help="print the bar, or one folder")
    t.add_argument("path", nargs="?", default="")
    t.add_argument("--json", action="store_true", help="print it as a SPEC list")
    f = sub.add_parser("find", parents=[common], help="items whose title or link holds TEXT")
    f.add_argument("text")
    f.add_argument("--json", action="store_true")
    a = sub.add_parser("add", parents=[common], help="add bookmarks and folders")
    a.add_argument("spec", nargs="?", help="a JSON list of items, or - for standard input")
    a.add_argument("--title", help="one bookmark's title, with --url")
    a.add_argument("--url", help="one bookmark's link, with --title")
    a.add_argument("--into", default="", help="the folder to add to (default: the bar)")
    a.add_argument("--after", help="put the new items after the item with this title")
    a.add_argument("--merge", action="store_true", help="add only what's missing")
    a.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    r = sub.add_parser("remove", parents=[common], help="remove an item and all it holds")
    r.add_argument("path")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("where", parents=[common], help="print which store it uses")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        path = find_store(getattr(args, "db", None))
        if args.cmd == "where":
            print(path)
            return 0
        con = connect(path)
        if args.cmd == "tree":
            with snapshot(con):
                items = as_spec(con, folder(con, args.path))
            if args.json:
                print(json.dumps(items, indent=2, ensure_ascii=False))
            else:
                show(items)
        elif args.cmd == "find":
            with snapshot(con):
                hits = find(con, args.text)
            if args.json:
                print(json.dumps(hits, indent=2, ensure_ascii=False))
            else:
                for h in hits:
                    print(f"{h['path']} ▸" if h["folder"] else f"{h['path']}  {h['url']}")
            return 0 if hits else 1
        elif args.cmd == "add":
            add(con, path, read_spec(args), args.into, args.after, args.merge, args.dry_run)
        else:
            remove(con, path, args.path, args.dry_run)
    except Nothing as e:
        print(e)
    except Refused as e:
        print(f"ddg-bookmarks: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
