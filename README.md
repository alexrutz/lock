# Lock

Single-user web vault for personal photos. Every upload is encrypted with
AES-256-GCM under a key derived from your master password (Argon2id).
Access requires the password plus a 6-digit code from a TOTP authenticator
app (Google Authenticator, Authy, 1Password, etc.).

## How it works

- Argon2id turns your password into a 32-byte master key on every login.
- Each photo gets a random data key (AES-256-GCM) that is wrapped with the
  master key. Encrypted blobs live in `data/photos/`, encrypted thumbnails
  in `data/thumbs/`. Filenames, the TOTP secret, and a sentinel are also
  encrypted with the master key.
- The master key only exists in process memory while you are logged in.
  Logout, idle timeout (30 min default), or a restart wipes it.

## Run it

Requires Python 3.10 or newer. On Debian/Ubuntu you may also need
`sudo apt install python3-venv`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m app           # or: lock
```

That launches on `0.0.0.0:8000` so the vault is reachable from other
devices on your network (phone, laptop). Override with env vars when
you want loopback-only or a different port:

```bash
LOCK_HOST=127.0.0.1 LOCK_PORT=9000 python -m app
```

The `source` step is what makes `pip` and `python` use the venv — verify
with `which pip` (should point inside `.venv/`). If `pip install` fails
with `Package 'lock' requires a different Python`, your venv is using a
too-old interpreter; recreate it with `python3.10 -m venv .venv` (or
newer).

> **Heads-up about plain HTTP on a LAN**: binding to `0.0.0.0` means
> everyone on your Wi-Fi can reach the server. Without TLS in front,
> your master password and TOTP code are visible to anyone sniffing the
> network. For trusted home networks this is usually fine; for anything
> else put Caddy / nginx / Tailscale in front (see *Security notes*
> below).

Open <http://127.0.0.1:8000/>.

1. First visit redirects to **`/setup`**. Pick a master password.
2. Scan the QR with an authenticator app. **Save the secret** shown below
   the QR — it is the only way to recover access if you lose your phone.
3. Log in with password + 6-digit code, then upload photos.

## Configuration

| Env var             | Default              | Purpose                          |
|---------------------|----------------------|----------------------------------|
| `LOCK_DATA_DIR`     | `./data`             | Where DB + encrypted blobs live  |
| `LOCK_SESSION_TTL`  | `1800` (seconds)     | Idle timeout                     |
| `LOCK_MAX_UPLOAD`   | `52428800` (50 MiB)  | Per-file upload cap              |
| `LOCK_ISSUER`       | `Lock Vault`         | Issuer label shown in TOTP app   |
| `LOCK_PAGE_SIZE`    | `60`                 | Photos per gallery page          |
| `LOCK_THUMB_CACHE`  | `512`                | In-memory decrypted thumbnails   |
| `LOCK_NAME_CACHE`   | `10000`              | In-memory decrypted filenames    |

## Organizing photos

- **Rating**: click stars on a tile (0–5). Stored as a plaintext column —
  not sensitive on its own.
- **Tags**: type a label in the `+ tag` field on any tile. Tag names are
  AES-GCM encrypted; a deterministic HMAC of the name is used as the
  UNIQUE constraint so the same tag dedups across photos without leaking
  the name. Removing the last photo from a tag deletes the tag.
- **Sort/filter bar** above the grid: sort by newest/oldest/top rated,
  filter by minimum rating, filter by tag. The filters are query-string
  driven so links are bookmarkable.
- **Hide** lets you keep mediocre photos in the vault but exclude them
  from the default view and the viewer's prev/next stepping. Toggle
  *Show hidden* in the filter bar to bring them back (visually dimmed
  with a HIDDEN badge) and unhide individual photos. *Export all* still
  includes hidden photos.

## Viewing and exporting

- Click any thumbnail to open the in-app viewer. Arrow keys / on-screen
  arrows page through the current view, `Esc` closes it.
- Select up to 4 photos with the checkboxes and use **Preview** to view
  them side-by-side in a grid (single / two-up / three / 2×2).
- **Export selected** packages the selection as a streaming ZIP of
  decrypted originals. **Export all** dumps the entire vault. The ZIP is
  built incrementally on the wire — no temp file, no memory spike, works
  for vaults of any size up to 10 000 photos per request.

## Mobile

The layout is full-width and reflows for narrow viewports: the filter bar
stacks, the grid uses 140 px columns, touch targets on stars / chips /
checkboxes are enlarged, and the viewer grid stacks vertically on
portrait phones.

## Security notes

- **No password reset.** Lose the password → lose the vault. That is the
  point of password-derived encryption.
- Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front
  before exposing it to a network. The session cookie is HttpOnly but is
  served over plain HTTP by default for local development.
- Disk access on the server reveals only ciphertext and a password verifier
  (Argon2id hash). The verifier resists offline cracking but is not magic;
  use a strong password.
- This is a personal-use project, not an audited product.
