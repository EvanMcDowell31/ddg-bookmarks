---
name: bookmarks
description: Read, add or remove bookmarks and folders in DuckDuckGo for Mac from the shell while the browser runs, with a backup first. Use for "add to my DuckDuckGo bookmarks", "bookmarks bar folder", "what's in my bookmarks".
argument-hint: "[what to do with the bookmarks]"
---

# DuckDuckGo for Mac bookmarks

`scripts/ddg_bookmarks.py`, beside this file, reads and changes DuckDuckGo for Mac's bookmarks while the browser runs. It needs no window and no restart, only Python 3 from macOS's command line tools. With this plugin enabled in Claude Code, `ddg-bookmarks` on the PATH runs the same script.

```
ddg-bookmarks tree                                  # the bookmarks bar
ddg-bookmarks tree "Projects > GitHub"              # one folder
ddg-bookmarks tree "Projects" --json                # the same, as a spec
ddg-bookmarks find example.com                      # titles and links that hold the text
ddg-bookmarks add --title T --url U --into "Projects" --merge
ddg-bookmarks add spec.json --into "Projects" --after "Docs" --merge --dry-run
ddg-bookmarks remove "Projects > Old link"
ddg-bookmarks where                                 # which store it uses
```

Without the plugin's PATH entry, run `python3 -I <this skill's folder>/scripts/ddg_bookmarks.py` with the same words.

## Doing it well

1. **Check each link loads** before adding it (`curl -sL -o /dev/null -w '%{http_code}' URL`). A link to something running on this Mac, such as `127.0.0.1:8080`, loads only while it runs; say so in its title, as "App (local)".
2. **Look before you add.** `find` says whether a link is already there, and `tree PATH --json` shows a folder in the shape `add` takes.
3. **Use `--merge`** whenever the folder may already hold some of the items: a folder already there takes only the children it lacks, and a bookmark already there by title or by link is skipped. Without it, a title already in the folder refuses the whole command.
4. **Try it first** with `--dry-run` when a change is more than a bookmark or two, or try it on a copy with `--db COPY`. Copy the store with `sqlite3 "file:STORE?mode=ro" ".backup 'COPY'"`, then turn on WAL in the copy with `sqlite3 COPY "PRAGMA journal_mode=WAL;"`. `ddg-bookmarks where` gives STORE.
5. **Tell the person when it shows.** DuckDuckGo reads new rows at its next bookmarks load: when anything is bookmarked, edited or moved in the app, or at its next launch. It does not reload by itself. Say "it shows the next time DuckDuckGo starts, or when you next add or edit a bookmark". Don't restart the browser for them: it may not restore their tabs, and a relaunch puts a window in front of them.

## Specs and paths

- A **spec** is a JSON list: `{"title": T, "url": U}` for a bookmark, `{"title": T, "children": [...]}` for a folder (which may be empty). Give `-` instead of a file to read it from standard input.
- A **path** is titles from the bar down, joined by ` > `: `"Projects > GitHub"`. No path, or `--into ""`, is the bar itself. A title can't hold ` > `, and two items with one title in a folder can't be told apart: rename one in the app.
- `--after TITLE` puts new items after that item; without it, they go at the end.
- Only `http://` and `https://` links pass. DuckDuckGo itself refuses links that run code, and drops untitled rows at launch.

## What each write does

- First it saves a one-file backup beside the store, `Bookmarks.sqlite.bak-<UTC time>`, and prints its path. A refused command, a dry run and an add with nothing to add write nothing and leave no backup.
- It writes the rows the way the app's own BookmarkEntity does: new rows get `Z_OPT` 1, an upper-case UUID and `modifiedAt` now; the folder that gains or loses children gets its `Z_OPT` bumped and `modifiedAt` set; `Z_PRIMARYKEY.Z_MAX` moves on. All of it happens in one `BEGIN IMMEDIATE` transaction that ends with an integrity check, so a failure leaves the store as it was.
- `remove` does what the app's own removal does (`markPendingDeletion`): the item and everything inside it lose their title and link and are marked pending deletion. DuckDuckGo hides them at once and clears them itself, after Sync has sent the deletion when Sync is on. Favorites links stay until then, as the app leaves them.

To undo an add, `remove` what it added. Don't copy a backup over the live store while DuckDuckGo runs.

## Where the store is

The direct download keeps it in `~/Library/Containers/com.duckduckgo.macos.browser/Data/Library/Application Support/Bookmarks.sqlite`, and the Mac App Store build in the same place under `com.duckduckgo.mobile.ios`. When both exist, pass `--db` or set `DDG_BOOKMARKS_DB`. On macOS 14 and later, the first read of another app's container asks the person to let their terminal "access data from other apps"; a terminal with Full Disk Access isn't asked.

## Limits

- macOS only, and only DuckDuckGo's own store: it doesn't import from or export to other browsers.
- With DuckDuckGo Sync on, adds and removals are written as the app writes its own, but that path is untested. The script prints a note when the store shows Sync has run.
- A later DuckDuckGo version could change the store. The script checks its tables and refuses a store that isn't the one it knows.
