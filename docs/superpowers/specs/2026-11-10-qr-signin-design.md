# QR-code sign-in

Date: 2026-11-10. Status: approved.

## Why

Xiaomi accounts created with "Sign in with Google" (or Facebook/Apple) have no Xiaomi password, so
Chippy's email + password sign-in rejects them (`code 70016`, `securityStatus 0`). Xiaomi's QR
login lets a phone that is already signed in to Xiaomi Home approve a new session, with no
password involved.

## Xiaomi flow

Follows the maintained token extractor (PiotrMachowski/Xiaomi-cloud-tokens-extractor, MIT,
`QrCodeXiaomiCloudConnector`).

1. `GET https://account.xiaomi.com/longPolling/loginUrl` with `_qrsize=480`,
   `qs=%3Fsid%3Dxiaomiio%26_json%3Dtrue`, `callback=https://sts.api.io.mi.com/sts`,
   `_hasLogo=false`, `sid=xiaomiio`, `serviceParam=`, `_locale=en_GB`, `_dc=<ms>`.
   The reply (Xiaomi's `&&&START&&&` JSON) holds `qr` (image URL), `loginUrl` (the same login as
   a web page), `lp` (long-poll URL) and `timeout` (seconds).
2. `GET qr` → PNG.
3. Long-poll `GET lp` (10 s request timeout, repeated) until HTTP 200, the overall `timeout`
   passes, or the user cancels. The 200 reply holds `userId`, `ssecurity`, `cUserId`,
   `passToken`, `location`.
4. `GET location` → the `serviceToken` cookie.

The result is the same session a password login produces (`user_id`, `service_token`,
`ssecurity`), so device discovery and map/live downloads need no change.

## Design

- `core/cloud.py` — `XiaomiCloud.start_qr_login() -> QrLogin(image: bytes, login_url: str,
  poll_url: str, timeout: int)` and `XiaomiCloud.wait_qr_login(qr, cancelled: Callable[[],
  bool]) -> None` (returns when signed in; raises `QrLoginExpired` when the timeout passes,
  `QrLoginCancelled` when `cancelled()` turns true, `CloudError` with a safe message otherwise).
  The poll and location replies carry login tokens: they are never logged and never put in error
  texts. Uses the same timeouts session as the password login.
- `core/fake.py` — `FakeCloud` gets the same two methods: a generated QR image and an approval
  after `CHIPPY_FAKE_QR_DELAY` seconds (default 3); `CHIPPY_FAKE_QR_EXPIRE=1` makes it expire.
- `ui/login.py` — `LoginFlow.sign_in_qr()` shows page `qr` `{image, url}`, waits in the worker,
  then continues with the existing device discovery. Expiry shows the QR page's error with a
  "Show a new code" button; Back/close cancels the wait (the worker checks a flag at least every
  10 s and its late result is ignored). The account page gets a second button, "Sign in with the
  Xiaomi Home app", with the caption "For accounts that use Google, Facebook or Apple" (replacing
  the old "set a Xiaomi password" hint). The QR page: picture, three steps (open Xiaomi Home →
  scan → confirm), an "Open in browser instead" link button (`Gtk.UriLauncher` on `loginUrl`), a
  spinner with "Waiting for approval…".
- Credentials from a QR sign-in have `username=""` and `password=""`; `cloud_session` holds the
  session. `CloudMap` requires a session (`user_id` + `service_token`), not a password: a stored
  session is enough for map and live downloads; with neither a session nor a password it still
  reports "no Xiaomi login stored; sign in again". Preferences → Account shows
  "Xiaomi account <user id>" when there is no username.
- An expired session still shows the banner; its Sign In opens the same dialog (both methods).

## Testing

- Unit: `start_qr_login`/`wait_qr_login` against a fake HTTP session (success, 408/timeout
  retries until expiry, cancel, missing `serviceToken`, no tokens in error texts); `LoginFlow`
  QR path to `devices`, expiry and cancel; `CloudMap` with a session and no password;
  `credentials_for` with empty username/password.
- Nested check: new `--scenario qr` (fake cloud approves after 3 s): click the QR button, see the
  `qr` page with a picture, reach `devices`, choose, land signed in. Run on GNOME 51 locally and
  GNOME 50 on the ThinkPad (`CN_APP=flatpak`).
- Real: the user's wife signs in by QR on her ThinkPad.
