export const meta = {
  name: 'bookmark-projects',
  description: 'Find your recent GitHub repositories and the sites they publish (and, if asked, web apps running on this Mac), and bookmark the ones not yet in a DuckDuckGo for Mac folder',
  whenToUse: 'After you make new repositories, sites or local apps, to keep a DuckDuckGo bookmarks folder of your projects up to date. Args (all optional): into (folder path, default Projects), github (accounts), months (default 12) or since (a date), exclude (repository names or patterns), local (true to add web apps running on this Mac), dirs (folders of git repositories), apply (false to only propose).',
  phases: [
    { title: 'Find', detail: 'GitHub repositories and sites, local web apps, project folders' },
    { title: 'File', detail: 'check each link, skip what is bookmarked, merge the rest into the folder' },
  ],
}

// args: what the person typed after the command, or an object:
//   into     folder path from the bookmarks bar         (default "Projects")
//   github   account or organization names, or false    (default: the account gh is signed in as)
//   months   only repositories pushed in the last N months (default 12)
//   since    only repositories pushed since this date, YYYY-MM-DD (instead of months)
//   exclude  repository names or shell-style patterns to leave out, besides forks,
//            archived repositories, the profile repository and dotfiles
//   local    true to also bookmark web apps running on this Mac (default: no)
//   dirs     folders whose git repositories are projects (default: none)
//   apply    false to only propose                       (default: add)
//   db       a copy of the store to work on instead of DuckDuckGo's own
//   tool     the command that runs the tool              (default "ddg-bookmarks")
//   notes    anything else the person said
const o = args && typeof args === 'object' && !Array.isArray(args)
  ? args
  : { notes: typeof args === 'string' ? args : '' }
const list = v => (Array.isArray(v) ? v : typeof v === 'string' && v ? [v] : []).map(String)
const into = typeof o.into === 'string' && o.into.trim() ? o.into.trim() : 'Projects'
const owners = list(o.github)
const dirs = list(o.dirs)
const exclude = list(o.exclude)
const months = Number.isFinite(Number(o.months)) && Number(o.months) > 0 ? Math.round(Number(o.months)) : 12
const since = typeof o.since === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(o.since) ? o.since : ''
const recent = since
  ? `pushed on or after ${since}`
  : `pushed in the last ${months} months (on or after \`date -v-${months}m +%Y-%m-%d\` on macOS, \`date -d '${months} months ago' +%F\` elsewhere)`
const apply = o.apply !== false
const tool = typeof o.tool === 'string' && o.tool ? o.tool : 'ddg-bookmarks'
const db = typeof o.db === 'string' && o.db ? ` --db ${JSON.stringify(o.db)}` : ''
const notes = typeof o.notes === 'string' && o.notes.trim()
  ? `\n\nThe person also said, and this comes before the defaults above: ${o.notes.trim()}`
  : ''

const TOOL = `The bookmarks tool runs as \`${tool}\`, which the ddg-bookmarks plugin puts on the PATH. ` +
  `If the shell can't find it, run \`python3 -I\` on the newest skills/bookmarks/scripts/ddg_bookmarks.py ` +
  `under ~/.claude/plugins/ instead.` +
  (db ? ` Add${db} to every one of its commands: this run works on that copy of the store.` : '')

const CANDIDATES = {
  type: 'object',
  required: ['candidates', 'skipped'],
  properties: {
    candidates: {
      type: 'array',
      items: {
        type: 'object',
        required: ['project', 'title', 'url', 'kind'],
        properties: {
          project: { type: 'string', description: 'the project the link belongs to' },
          title: { type: 'string', description: 'a short bookmark title' },
          url: { type: 'string', description: 'an http(s) link' },
          kind: { type: 'string', enum: ['repo', 'site', 'app', 'page'] },
          private: { type: 'boolean' },
        },
      },
    },
    skipped: { type: 'array', items: { type: 'string' }, description: 'what was left out, each with why' },
  },
}

const RESULT = {
  type: 'object',
  required: ['added', 'skipped', 'output'],
  properties: {
    added: { type: 'array', items: { type: 'string' }, description: 'each new bookmark or folder, as "PATH  URL"' },
    skipped: { type: 'array', items: { type: 'string' }, description: 'candidates left out, each with why' },
    output: { type: 'string', description: 'what the last add command printed, its backup line included' },
  },
}

const READ_ONLY = 'Only read: change no file, push nothing, sign in nowhere, and post no form.'

const finders = []
if (o.github !== false) {
  finders.push(() => agent(
    `List the GitHub repositories of ${owners.length ? owners.join(', ') : 'the account the GitHub CLI is signed in as (`gh api user --jq .login`)'}, ` +
    `and the sites they publish, as bookmark candidates.\n\n` +
    `- With the GitHub CLI signed in, \`gh api --paginate 'user/repos?affiliation=owner&per_page=100'\` lists the signed-in account's own repositories, private ones too; ` +
    `\`gh api --paginate 'users/NAME/repos?per_page=100'\` lists another person's, and \`orgs/NAME/repos\` an organization's. ` +
    `Without gh, \`curl -s 'https://api.github.com/users/NAME/repos?per_page=100'\` lists the public ones.\n` +
    `- Keep only repositories ${recent}, by pushed_at.\n` +
    `- Leave out forks, archived repositories, the profile repository (named the same as its account), and dotfiles or settings repositories (names such as dotfiles, dots, .files or config)${exclude.length ? `, and any repository matching ${exclude.join(', ')}` : ''}. List each one left out in skipped, with why.\n` +
    `- Each repository is a candidate of kind "repo": project is its name; title is NAME for a person's repository and OWNER/NAME for an organization's; url is its html_url; private is its private flag.\n` +
    `- A repository with has_pages true publishes a site: \`gh api repos/OWNER/NAME/pages --jq .html_url\` gives its address. Add it as kind "site", titled after the project. A homepage field holding another https address is a site too.\n` +
    `- If no account can be found, return no candidates and say why in skipped.\n\n${READ_ONLY}${notes}`,
    { label: 'GitHub', phase: 'Find', schema: CANDIDATES, effort: 'low' }))
}
if (o.local === true) {
  finders.push(() => agent(
    `List the web apps running on this Mac's loopback address right now, as bookmark candidates.\n\n` +
    `- \`lsof -nP -iTCP -sTCP:LISTEN\` lists listening ports and their processes. Look only at ports bound to 127.0.0.1, ::1, localhost or *.\n` +
    `- \`curl -s -m 3 http://127.0.0.1:PORT/\` each one. Keep only ports that answer with an HTML page for people (a <title> or <html>). Leave out JSON APIs, model servers, databases, debuggers, language servers, and browsers' and editors' own ports.\n` +
    `- Each one kept is a candidate of kind "app": project is the app's name, from its page title or process; title is "NAME (local)"; url is "http://127.0.0.1:PORT/".\n\n${READ_ONLY}${notes}`,
    { label: 'Local apps', phase: 'Find', schema: CANDIDATES, effort: 'low' }))
}
if (dirs.length) {
  finders.push(() => agent(
    `Find projects in these folders: ${dirs.join(', ')}. Each git repository directly inside one is a project.\n\n` +
    `- For each, read its remote (\`git -C DIR remote get-url origin\`), the first heading of its README, and any address the README gives for the project's site, or for the app it serves on this Mac (such as http://127.0.0.1:8080/).\n` +
    `- Return its repository as kind "repo" (the https form of a GitHub remote), its site as kind "site", and its local app as kind "app", titled "NAME (local)". Name the project after the folder.\n\n${READ_ONLY}${notes}`,
    { label: 'Project folders', phase: 'Find', schema: CANDIDATES, effort: 'low' }))
}

if (!finders.length) {
  return { folder: into, applied: false, added: [], skipped: ['no sources to look in: github and local are off, and no dirs were given'], output: '' }
}

phase('Find')
// A barrier: one plan needs every source's candidates, deduplicated across them.
const found = (await parallel(finders)).filter(Boolean)
const key = url => String(url).trim().replace(/\/+$/, '').toLowerCase()
const seen = new Set()
const candidates = []
for (const c of found.flatMap(f => f.candidates || [])) {
  if (!/^https?:\/\//i.test(String(c.url)) || seen.has(key(c.url))) continue
  seen.add(key(c.url))
  candidates.push(c)
}
const skippedEarly = found.flatMap(f => f.skipped || [])
log(`${candidates.length} link(s) found by ${found.length} of ${finders.length} source(s)`)
if (!candidates.length) {
  return { folder: into, applied: false, added: [], skipped: skippedEarly, output: 'nothing found to bookmark' }
}

phase('File')
const filed = await agent(
  `Keep a DuckDuckGo for Mac bookmarks folder of projects up to date. ${TOOL}\n\n` +
  `The folder: "${into}", a path from the bookmarks bar.\n` +
  `Links found (JSON): ${JSON.stringify(candidates)}\n\n` +
  `1. Read what's bookmarked: \`${tool} tree --json\` gives the whole bar. Drop every link that's already bookmarked anywhere on it, comparing links with and without a trailing slash.\n` +
  `2. Check each remaining link: \`curl -sL -o /dev/null -m 10 -w '%{http_code}' URL\` should give 2xx or 3xx. A private repository answers 404 to curl, so check one with \`gh repo view OWNER/NAME\` and title it "NAME (private)". Leave out a link that doesn't load, and list it in skipped with its code.\n` +
  `3. Arrange them as the folder already is. If it has a subfolder for repositories (such as one titled GitHub), repositories go there. A project with two or more of the other links gets a folder of its own, titled after the project, and a project with one gets a bookmark. Keep titles short. Add to a folder that's already there rather than making a second one with a near-same name, and leave existing titles and order alone.\n` +
  `4. Write the spec, a JSON list of {"title", "url"} and {"title", "children"} items, to a file in a new temporary folder (mktemp -d), and run \`${tool} add SPEC --into "${into}" --merge --dry-run\`. If the folder doesn't exist yet, wrap the spec as [{"title": "LAST PART OF THE PATH", "children": SPEC}] and add that into the folder's parent instead.\n` +
  `5. ${apply ? 'Run the same command without --dry-run.' : 'Stop there. This run only proposes: write nothing to the bookmarks.'} Report the output of the last command you ran.\n\n` +
  `Never remove, rename or move anything that's already bookmarked, and never quit or restart DuckDuckGo.${notes}`,
  { label: 'File', phase: 'File', schema: RESULT })

return {
  folder: into,
  applied: apply,
  added: filed ? filed.added : [],
  skipped: skippedEarly.concat(filed ? filed.skipped : ['the filing agent stopped before it finished']),
  output: filed ? filed.output : '',
  shows: 'DuckDuckGo shows new bookmarks the next time it starts, or when you next add or edit a bookmark in it.',
}
