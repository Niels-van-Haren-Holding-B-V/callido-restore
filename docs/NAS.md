# Callido Restore on a NAS

Callido Restore can run on the NAS itself and be opened from any browser in your network. A restore then goes straight from
the backup (for example Google Drive) into the NAS's own shared folders: no computer has to stay on, and the data crosses the
network once instead of twice (backup → computer → NAS over SMB).

The NAS version is a Docker image for **Container Manager** (DSM 7.2 or later; Synology models with an Intel/AMD or 64-bit
ARM processor that offer Container Manager in Package Center). The same image runs on any other Docker host.

## Package or container

There are two ways to install it on a Synology; one is enough:

* **Package** (recommended; Package Center → Manual Install of `callido-restore_<processor>-7.1_<version>.spk` from the
  [Releases](https://github.com/Niels-van-Haren-Holding-B-V/callido-restore/releases)): every DSM 7 model, including
  those without Container Manager. It runs **inside DSM**: you open it from the DSM main menu, DSM's own login is the
  access check (administrators only), and it has no port, firewall rule or password of its own; the app itself listens
  on the NAS only. The installation asks nothing: the first restore asks once for your DSM user and password, and the
  app then writes to this NAS with that user's rights, into any shared folder, as the Mac and Windows apps do. The password
  is kept on the NAS in the app's settings, so use a DSM user that has Read/Write on the folders you restore into; it does
  not need to be an administrator. If DSM's *Auto Block* (Control Panel → Security → Protection) blocks `127.0.0.1` after
  mistyped passwords, add `127.0.0.1` to its allow list. It needs SynoCommunity's Python 3.14: first add their package source (Package Center → Settings → Package
  Sources → Add: name `SynoCommunity`, location `https://packages.synocommunity.com`), then install **Python 3.14** from
  the *Community* tab. See `packaging/synology/README.md`.
* **Container** (below): any Docker host, or DSM 7.2 models with Container Manager; opened at `http://<NAS>:8765` with
  its own password.

## Install on a Synology

1. In **Package Center**, install **Container Manager**.
2. In **File Station**, create a folder for the app's settings, for example `docker/callido-restore` on `volume1`.
3. In **Container Manager → Project → Create**: name `callido-restore`, path the folder from step 2, source *Create
   docker-compose.yml*, and paste:

   ```yaml
   services:
     callido-restore:
       image: ghcr.io/niels-van-haren-holding-b-v/callido-restore:latest
       container_name: callido-restore
       restart: unless-stopped
       user: "1026:100"                                  # your DSM user's id and the group "users" (see below)
       ports:
         - "8765:8765"
       environment:
         CALLIDO_PASSWORD: "choose-a-long-password"     # at least 8 characters; needed to open the app
       volumes:
         - /volume1/docker/callido-restore:/config      # settings, connections, saved restores, download cache
         - /volume1/Photos:/volume1/Photos              # every shared folder you want to restore into
   ```

   Add one `volumes` line per shared folder you want to restore into, with the same path on both sides. To allow every
   shared folder at once, use `- /volume1:/volume1`.

   `user` makes the app run as you, so restored files belong to you and every DSM user with access to the folder can
   change or delete them, as with files copied over SMB. `1026` is the first user created on a Synology; for another user,
   find the number with `id <name>` over SSH. That user needs Read/Write access to the settings folder and to every
   shared folder you mount. Without a `user` line the app runs as root and restored files belong to root.
4. Click **Next** and **Done**. The image is downloaded and started.
5. Open `http://<your NAS>:8765` in a browser (for example `http://nas:8765` or `http://192.0.2.10:8765`) and sign in with
   the password.

## Use

The app works as on a Mac or PC, with two differences:

* **Restore target:** choose a local folder such as `/volume1/Photos/Restored`. Files are written by the NAS itself; no SMB
  connection is needed.
* **Google Drive sign-in:** after *Connect*, the app shows a link to Google's sign-in page. Allow access; your browser then
  lands on a page that does not load (its address starts with `http://127.0.0.1:53682/`). That is expected: copy that whole
  address from the address bar and paste it into the app, which completes the sign-in on the NAS.

Closing the browser does not stop a restore; it keeps running on the NAS. Open the page again to follow it.

## Update

In **Container Manager → Image**, the image shows *Update available* when a new version is out: click **Update**, and the
container restarts with it. Settings, connections and saved restores stay in the settings folder; an interrupted restore
continues with *Resume*.

## Security

* Anyone in your network can reach the page; the password protects it. Choose a long one, and do not forward port 8765 on
  your router.
* For HTTPS, put the app behind DSM's reverse proxy (**Control Panel → Login Portal → Advanced → Reverse Proxy**: from
  `https://restore.example:443` to `http://localhost:8765`).
* The container can write to every folder you mount. Mount only the shared folders you restore into.
* Wrong passwords are slowed down (up to 30 s between tries); a session lasts 30 days, or until the container restarts.
  Behind DSM's reverse proxy every browser reaches the app from the proxy's address, so a mistyped password there makes
  everyone wait; on a NAS with one user that does not matter.

## Other Docker hosts

```sh
docker run -d --name callido-restore --restart unless-stopped -p 8765:8765 \
  -e CALLIDO_PASSWORD=choose-a-long-password \
  -v /srv/callido-restore:/config -v /srv/restore:/srv/restore \
  ghcr.io/niels-van-haren-holding-b-v/callido-restore:latest
```

Instead of `CALLIDO_PASSWORD`, a file can hold the password: `CALLIDO_PASSWORD_FILE=/run/secrets/callido_password`.

Without Docker (Python 3.12+, `pip install lz4`, rclone on the PATH):
`CALLIDO_PASSWORD=... python -m callido_restore --listen 0.0.0.0 --port 8765`.
