---
name: add
description: Bookmark links in DuckDuckGo for Mac in one step. Checks each link loads, skips any already bookmarked, and files the rest into the folder named. Use for "bookmark this", "add these links to my DuckDuckGo bookmarks".
argument-hint: "<links, or what to bookmark> [into <folder>]"
---

# Bookmark it

Bookmark what the person asked for: $ARGUMENTS

Run `ddg-bookmarks` (this plugin puts it on the PATH). If the shell can't find it, run `python3 -I "${CLAUDE_PLUGIN_ROOT}/skills/bookmarks/scripts/ddg_bookmarks.py"` with the same words. The `bookmarks` skill of this plugin says how the store works, if you need more than these steps.

1. **Work out each bookmark:** its link, and a short title. That's the page's own name, or what the person called it. When they named a thing rather than a link ("my new site"), find its link first, and ask only when you can't.
2. **Check each link loads:** `curl -sL -o /dev/null -w '%{http_code}' URL` should give 2xx or 3xx. A private GitHub repository answers 404 to curl, so check one with `gh repo view OWNER/REPO` and add "(private)" to its title. Something served on this Mac (127.0.0.1, localhost) loads only while it runs: add "(local)" to its title. Leave out a link that doesn't load, and say why.
3. **Pick the folder:** the one they named, as a path from the bookmarks bar (`"Projects > GitHub"`), else the bar. `ddg-bookmarks tree` shows what's there. Group several links for one thing in a folder of their own, titled after the thing, when the folder they named already groups links that way.
4. **Add only what's missing:** write the items as a spec, `[{"title": T, "url": U}, {"title": F, "children": [...]}]`, then run `ddg-bookmarks add - --into "FOLDER" --merge --dry-run` with the spec on standard input. Read what it would do, then run it again without `--dry-run`. For one bookmark, `ddg-bookmarks add --title T --url U --into "FOLDER" --merge` does the same.
5. **Report back** in a line or two: what was added and where, what was skipped and why, and the backup's path. Then say "it shows the next time DuckDuckGo starts, or when you next add or edit a bookmark". Never restart DuckDuckGo for them.
