# Desireerr

A Seerr-style request app for adult scenes: [StashDB](https://stashdb.org), the community
database of porn scenes, performers and studios, on top of
[Whisparr](https://github.com/Whisparr/Whisparr) v3, the *arr for adult content (Radarr for
porn). Browse and search StashDB, filter scenes
by tags (all of them, any of them, or leave some out), performers, studios, dates and your
StashDB favourites, then request scenes. Approved requests go to Whisparr, which searches
Prowlarr and downloads them; each scene shows whether it's pending, requested, downloading
or available.

- **One file, standard library only.** `desireerr.py` is the server (Python 3.10+); `web/` is
  a plain JavaScript single-page app with no build step.
- **Everything is behind the login.** Covers are fetched through the server and cached, so
  the browser only talks to Desireerr. Covers can be blurred, and the tab title is a setting.
- **Users and approvals.** Admins approve requests, follow performers and studios, set
  favourites and change settings. Other users' requests wait for approval unless they're auto-approved.
- **Saved searches.** Any filter on the Scenes page (tags in and out, performers, studios,
  favourites) can request its new scenes every hour, which Whisparr's follows can't.
- **Scene pages.** The cast with their ages then and now (and a † for those who died), and a
  "Similar scenes" reel at the bottom: scenes with the same women (everyone, if there are no
  women in it) or the same kind of action, the ones with the most in common first.
- **Pushes, Watch, stalled requests.** ntfy pushes like Seerr's (or discreet ones), a Watch
  button that opens downloaded scenes in Jellyfin, and a nudge for requests no release turns
  up for.

Built for [torrent-box](https://github.com/vibesclauder/torrent-box): it finds Whisparr's API key in
torrent-box's config on its own.

## Install

On a torrent-box server, it comes with Whisparr: answer yes to Whisparr in `./setup.sh` (or
set `[setup] whisparr = yes`). Setup clones this repo to `~/desireerr`, updates it on each
run and starts it. By hand:

```sh
gh repo clone vibesclauder/desireerr ~/desireerr
mkdir -p ~/.config/systemd/user
cp ~/desireerr/systemd/desireerr.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now desireerr
```

The service runs as you (user units need `loginctl enable-linger $USER` to start at boot,
which torrent-box's setup already did). Open `http://<server>:5056`. The first visit asks
for a setup code, so nobody else on the network can create the admin account first:

```sh
cat ~/.local/state/desireerr/setup-code
```

Then create the admin and paste your StashDB API key (stashdb.org → your profile → API
key). It can also be added later under Settings.

To skip that page (torrent-box's setup does this), give the admin login and the key through
the environment, never the command line:

```sh
DESIREERR_ADMIN_USER=alice DESIREERR_ADMIN_PASSWORD=... DESIREERR_STASHDB_KEY=... \
    python3 ~/desireerr/desireerr.py init
```

Run again with a new password, it updates that admin. An admin made on the first-visit page
and a key changed in Settings are left alone. `DESIREERR_JELLYFIN_KEY` sets a Jellyfin API
key the same way (a new one given later replaces it, unless one was set in Settings).

If ufw is on, open the port to the LAN (and the tailnet, if you use Tailscale):

```sh
sudo ufw allow from 192.168.1.0/24 to any port 5056 proto tcp comment 'Desireerr'
sudo ufw allow in on tailscale0 to any port 5056 proto tcp comment 'Desireerr'
```

On a phone, open it and "Add to home screen" for an app icon.

## How requests reach Whisparr

- **Request a scene:** if Whisparr already has it (unmonitored, say from a followed
  performer's back catalogue), it's monitored; otherwise it's added by its StashDB ID with the
  quality profile and folder from Settings. Then Whisparr searches for it right away, unless
  that's switched off in Settings (then only the RSS feed can grab it).
- **Favourite a performer or studio** (admins, the heart): saved on your StashDB account, and
  nothing else. Favourites are a filter: Discover's "New from your StashDB favourites", the
  Scenes page's Favourites filter, "favourites only" on Performers and Studios, and saved
  searches. Nothing is downloaded for them. (Whisparr's own StashDB import list, if you
  switch it on, does add favourites to Whisparr, with their whole back catalogue.)
- **Favourite tags** (anyone, the heart on a tag's page): StashDB has none, so Desireerr keeps
  them, per user. They're a filter too: "With any of my favourite tags" on Scenes (and saved
  searches, which use your favourites as they are at each check), a Discover row, and the
  top of the Tags page; favourite tags are marked with a heart wherever tags are listed.
  StashDB takes one tags condition per search: with tags picked as well, the favourites go
  to StashDB and the picked tags are checked by Desireerr, so pages can come back short
  (with a single favourite tag and "All tags", it's one exact search).
- **Follow a performer or studio** (admins): monitored in Whisparr with the "after" date set to
  today, so only new scenes are wanted. Without a date Whisparr would want the whole back
  catalogue. Older scenes can still be requested one by one.
- **Status** comes from Whisparr's library and queue, refreshed every two minutes:
  *available* (has a file), *downloading* (in the queue, with progress), *requested*
  (monitored, waiting for a release), *pending* (waiting for an admin).
- **Stalled:** Whisparr searches once, when a request is approved; after that it only grabs
  a release that turns up in its RSS feed. A request still waiting after 7 days (Settings,
  Whisparr) is *stalled*: it's on the Requests page's Stalled tab, where the requester or an
  admin can **search again** (one or all) or **give up** (Whisparr stops wanting it; it can
  be requested again later).

## Saved searches

On Scenes (or a tag's, performer's or studio's page), open Filters, pick tags, performers,
studios or favourites, and press **Save search**. Every hour, scenes released from the date
you pick that match are requested: new ones, and older ones tagged later so they match.

- Requests are made as you, so the usual rule applies: admins' and auto-approved users' go
  straight to Whisparr, everyone else's wait. **Ask first** leaves them pending anyway.
- Scenes Whisparr's metadata doesn't have yet (new StashDB scenes take about a day) are tried
  at each check for two weeks, and not counted as failed. One check requests 25 at most.
- Scenes already in the library, wanted, or asked for before (even declined) are skipped, as
  are requests you removed.
- The list is under **Saved searches**: check now, pause, ask first, delete (its requests
  stay).

## Notifications

Settings, Notifications: pushes through [ntfy](https://ntfy.sh), for a request waiting for
approval, one made by a saved search, a scene downloaded, a request failed or stalled
(those are on to start with), and an automatic approval, an approval or a decline.

- On a torrent-box server they go to its ntfy server and topic (its config), so the phone
  that gets torrent-box's pushes gets these too. Set a server and topic to use others.
- **Discreet** (on to start with): the browser tab title and one line, "A request is ready.",
  with no title, name or cover. Off, they're like Seerr's: what happened, the scene, its
  studio and date, who asked, its cover, and a tap opens the scene.
- Links use Desireerr's address from Settings, or else the one it was last opened at.

## Watch in Jellyfin

With a Jellyfin API key (Settings, Jellyfin; torrent-box's setup makes one), available
scenes get a **Watch** button on their page and in Requests. It finds the scene in Jellyfin
by its file (Whisparr's path, or the file name if the two see different folders) and opens
it at Jellyfin's address: the one in Settings, or the host Desireerr is opened at, port 8096.

### Scene details in Jellyfin (.nfo files and covers)

Desireerr also writes each downloaded scene's `.nfo` file beside the video (Settings,
Jellyfin, on by default): title, description, date, studio, StashDB's tags as genres, and
the performers as the cast, each with their StashDB photo, which Jellyfin shows on the cast
cards. The cover is `folder.jpg` in the scene's folder: Jellyfin takes it as the folder's and
the video's picture. (Whisparr's `screenshot.jpg` is renamed to it; without one, the scene's
picture comes from StashDB. A cover of your own stays.)

Whisparr's own metadata lists the performers without photos, and twice (Jellyfin shows them
as writers too), and in a *Home videos and photos* library its `screenshot.jpg` shows up as
a photo of its own beside the video. So Desireerr turns it off (Settings → Metadata → Kodi
(XBMC) / Emby) and back on when you untick this.

It checks every couple of minutes, writes up to 50 at a time (the scenes that lack its
`.nfo` or a cover), and with a Jellyfin API key has Jellyfin look at each one again right
away; new downloads are read on Jellyfin's next scan. A `.nfo` with Desireerr's comment at
the top is rewritten when needed, so edit the details on StashDB, not in the file.

The service's sandbox is read-only outside its own folders, so it needs Whisparr's root
folders writable. torrent-box's setup adds that; by hand, a drop-in with your root folders:

```sh
mkdir -p ~/.config/systemd/user/desireerr.service.d
printf '[Service]\nReadWritePaths=-/path/to/whisparr/root\n' \
    > ~/.config/systemd/user/desireerr.service.d/storage.conf
systemctl --user daemon-reload && systemctl --user restart desireerr
```

## Files

| Path | What |
|---|---|
| `~/.local/state/desireerr/desireerr.db` | Settings (StashDB and Jellyfin keys included), users, sessions, requests, saved searches |
| `~/.cache/desireerr/images/` | Cover cache, trimmed to 2 GB |
| `~/.config/desireerr/stashdb_api_key` | Optional: a StashDB key in a file, used when none is saved in Settings |

Logs: `journalctl --user -u desireerr`.

## Development

```sh
python3 -m unittest discover -s tests
python3 desireerr.py --port 5099 --data /tmp/d/data --cache /tmp/d/cache
python3 tools/make_icons.py     # redraw the PNG icons after changing the logo
```
