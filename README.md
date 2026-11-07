# Desireerr

A Seerr-style request app for [StashDB](https://stashdb.org) scenes, on top of
[Whisparr](https://github.com/Whisparr/Whisparr) v3. Browse and search StashDB, filter scenes
by tags (all of them, any of them, or leave some out), performers, studios, dates and your
StashDB favourites, then request scenes. Approved requests go to Whisparr, which searches
Prowlarr and downloads them; each scene shows whether it's pending, requested, downloading
or available.

- **One file, standard library only.** `desireerr.py` is the server (Python 3.10+); `web/` is
  a plain JavaScript single-page app with no build step.
- **Everything is behind the login.** Covers are fetched through the server and cached, so
  the browser only talks to Desireerr. Covers can be blurred, and the tab title is a setting.
- **Users and approvals.** Admins approve requests, follow performers and studios, and
  change settings. Other users' requests wait for approval unless they're auto-approved.

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
and a key changed in Settings are left alone.

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
- **Follow a performer or studio** (admins): monitored in Whisparr with the "after" date set to
  today, so only new scenes are wanted. Without a date Whisparr would want the whole back
  catalogue. Older scenes can still be requested one by one.
- **Status** comes from Whisparr's library and queue, refreshed every two minutes:
  *available* (has a file), *downloading* (in the queue, with progress), *requested*
  (monitored, waiting for a release), *pending* (waiting for an admin).

## Files

| Path | What |
|---|---|
| `~/.local/state/desireerr/desireerr.db` | Settings (StashDB key included), users, sessions, requests |
| `~/.cache/desireerr/images/` | Cover cache, trimmed to 2 GB |
| `~/.config/desireerr/stashdb_api_key` | Optional: a StashDB key in a file, used when none is saved in Settings |

Logs: `journalctl --user -u desireerr`.

## Development

```sh
python3 -m unittest discover -s tests
python3 desireerr.py --port 5099 --data /tmp/d/data --cache /tmp/d/cache
python3 tools/make_icons.py     # redraw the PNG icons after changing the logo
```
