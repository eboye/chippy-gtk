"""Xiaomi cloud: login (2FA aware), the saved session, and map file downloads.

(The device list is read through micloud's `get_devices` by `core.accounts`.)"""

import functools
import hashlib
import json
import random
import re
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import requests
from micloud import MiCloud
from micloud.micloudexception import MiCloudAccessDenied, MiCloudException

COUNTRIES = ["de", "cn", "us", "ru", "sg", "i2"]
ACCOUNT_HOST = "https://account.xiaomi.com"
# (connect, read) seconds for every request on a XiaomiCloud session: micloud passes none,
# and a request that never answers would otherwise hold its worker (and CloudMap's lock).
HTTP_TIMEOUT = (15, 15)
MAP_DOWNLOAD_TIMEOUT = 20
# Xiaomi's QR login long poll: each request waits up to 10 s, so a cancel is seen quickly.
QR_POLL_TIMEOUT = (10, 10)
QR_DEFAULT_TIMEOUT = 300


class CloudError(Exception):
    pass


class CloudSessionExpired(CloudError):
    """The saved Xiaomi session is gone (never restored, or Xiaomi refused it). Only a new
    sign-in fixes it: a password login from here would just trigger 2FA again and again."""

    def __init__(self):
        super().__init__("Xiaomi sign-in expired — sign in again")


class _TimeoutSession(requests.Session):
    """A requests session whose calls time out unless the caller says otherwise."""

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = HTTP_TIMEOUT
        return super().request(method, url, **kwargs)


class CloudUnreachable(MiCloudException):
    """A network failure talking to Xiaomi. Deliberately carries no request details: the
    failed URL can hold the password hash, the username or a 2FA identity context."""

    def __init__(self):
        super().__init__("Couldn't reach Xiaomi's login service")


# The URLs stay on `.url`; the messages are generic because they end up in logs and the UI.
class TwoFactorRequired(MiCloudAccessDenied):
    def __init__(self, url: str):
        super().__init__("Xiaomi wants to verify this sign-in")
        self.url = url


class CaptchaRequired(MiCloudAccessDenied):
    def __init__(self, url: str):
        super().__init__("Xiaomi wants a captcha")
        self.url = url


class QrLoginExpired(CloudError):
    def __init__(self):
        super().__init__("The code expired.")


class QrLoginCancelled(CloudError):
    def __init__(self):
        super().__init__("QR sign-in cancelled")


@dataclass(frozen=True)
class QrLogin:
    """A started QR sign-in. `login_url` is the same login as a web page; `poll_url` is
    secret (it collects the session) and must never be logged."""

    image: bytes = field(repr=False)
    login_url: str = ""
    poll_url: str = field(default="", repr=False)  # collects the session: never print it
    timeout: int = 0


def _network_safe(method):
    """Turn a requests failure into a CloudUnreachable without the original's text."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except requests.RequestException:
            raise CloudUnreachable() from None

    return wrapper


class XiaomiCloud(MiCloud):
    def login(self) -> bool:
        """micloud's login without its logging (it logs the username and error texts) and
        with network failures raised as CloudUnreachable instead of a bare False."""
        if self.user_id and self.service_token:
            return True  # a saved session (also a QR sign-in, which has no password)
        if not (self.username and self.password):
            return False
        try:
            self._login_request()
        except (MiCloudAccessDenied, CloudUnreachable, CloudError):
            self._login_failed()
            raise
        except MiCloudException:
            self._login_failed()
            return False
        self.failed_logins = 0
        return True

    def _login_failed(self) -> None:
        self.failed_logins += 1
        self.service_token = None
        if self.failed_logins > 10:
            self._init_session(reset=True)

    def _login_request(self) -> bool:
        self._init_session()
        try:
            sign = self._login_step1()
            location = sign if sign.startswith("http") else self._login_step2(sign)
            response = self._login_step3(location)
        except (MiCloudAccessDenied, MiCloudException, CloudError):
            raise
        except (
            Exception
        ) as err:  # micloud's steps raise KeyError & co.; no texts, they may hold URLs
            raise MiCloudException(f"Xiaomi login failed ({type(err).__name__})") from None
        if response.status_code == 403:
            raise MiCloudAccessDenied("Xiaomi denied access")
        if response.status_code != 200:
            raise MiCloudException(f"Xiaomi login failed (HTTP {response.status_code})")
        return True

    @_network_safe
    def _login_step1(self):
        return super()._login_step1()

    @_network_safe
    def _login_step3(self, location):
        return super()._login_step3(location)

    @_network_safe
    def fetch_captcha(self, captcha_url: str) -> bytes:
        """Download the captcha image on this session (its cookies tie the answer to us)."""
        response = self.session.get(captcha_url)
        if response.status_code != 200 or not response.content:
            raise CloudError(f"could not download Xiaomi's captcha (HTTP {response.status_code})")
        return response.content

    # QR sign-in, as the maintained token extractor does it (PiotrMachowski's
    # Xiaomi-cloud-tokens-extractor, MIT): accounts without a Xiaomi password (Google,
    # Facebook, Apple) approve the login in the Xiaomi Home app.
    @_network_safe
    def start_qr_login(self) -> QrLogin:
        self._init_session(reset=True)
        response = self.session.get(
            f"{ACCOUNT_HOST}/longPolling/loginUrl",
            params={
                "_qrsize": "480",
                "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                "callback": "https://sts.api.io.mi.com/sts",
                "_hasLogo": "false",
                "sid": "xiaomiio",
                "serviceParam": "",
                "_locale": "en_GB",
                "_dc": str(int(time.time() * 1000)),
            },
        )
        data = _json_body(response.text) if response.status_code == 200 else {}
        qr, login_url, poll_url = data.get("qr"), data.get("loginUrl"), data.get("lp")
        if not (qr and login_url and poll_url):
            raise CloudError(f"Xiaomi didn't start a QR sign-in{_reason(data)}")
        image = self.session.get(qr)
        if image.status_code != 200 or not image.content:
            raise CloudError(f"couldn't download the QR code (HTTP {image.status_code})")
        try:
            timeout = int(data.get("timeout") or QR_DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = QR_DEFAULT_TIMEOUT
        return QrLogin(image.content, str(login_url), str(poll_url), timeout)

    def wait_qr_login(
        self,
        qr: QrLogin,
        cancelled: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Block until the QR login is approved (the session is then set), it expires
        (QrLoginExpired) or `cancelled()` turns true (QrLoginCancelled)."""
        deadline = clock() + qr.timeout
        while True:
            if cancelled():
                raise QrLoginCancelled()
            if clock() >= deadline:
                raise QrLoginExpired()
            try:
                response = self.session.get(qr.poll_url, timeout=QR_POLL_TIMEOUT)
            except requests.Timeout:
                sleep(0)  # the request itself already waited; just poll again
                continue
            except requests.RequestException:
                raise CloudUnreachable() from None
            if response.status_code == 200:
                break
            sleep(1)  # nothing yet: don't spin
        if cancelled():
            raise QrLoginCancelled()
        data = _json_body(response.text)
        user_id, ssecurity = data.get("userId"), data.get("ssecurity")
        location = data.get("location")
        if not (user_id and ssecurity and location):
            raise CloudError("Xiaomi's QR sign-in reply was incomplete")
        try:
            sts = self.session.get(location)
        except requests.RequestException:
            raise CloudUnreachable() from None
        token = sts.cookies.get("serviceToken") or self._cookie("serviceToken")
        if not token:
            raise CloudError("Xiaomi's QR sign-in finished without a service token")
        self.user_id = str(user_id)
        self.ssecurity = ssecurity
        self.cuser_id = data.get("cUserId")
        self.pass_token = data.get("passToken")
        self.service_token = token

    def _init_session(self, reset=False):
        """micloud's session (with timeouts), with the client identity Xiaomi's login accepts
        today. micloud's own `get_session()` only adds a User-Agent and two cookies, which are
        replaced here anyway, so a plain session in its place sends the same requests."""
        if not (reset or self.session is None):
            return  # keep the cookies of a captcha / 2FA step in progress
        if self.session is not None:
            self.session.close()
        self.session = _TimeoutSession()
        agent_id = "".join(random.choices("ABCDE", k=13))
        prefix = "".join(random.choices(string.ascii_lowercase, k=18))
        self.session.headers["User-Agent"] = (
            f"{prefix}-{agent_id} APP/com.xiaomi.mihome APPV/10.5.201"
        )
        device_id = "".join(random.choices(string.ascii_lowercase, k=6))
        self.session.cookies.clear()  # drop micloud's outdated sdkVersion/deviceId
        for domain in ("mi.com", "xiaomi.com"):
            self.session.cookies.set("sdkVersion", "accountsdk-18.8.15", domain=domain)
            self.session.cookies.set("deviceId", device_id, domain=domain)

    @_network_safe
    def _login_step2(self, sign):
        post_data = {
            "sid": "xiaomiio",
            "hash": hashlib.md5(self.password.encode()).hexdigest().upper(),
            "callback": "https://sts.api.io.mi.com/sts",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": self.username,
            "_json": "true",
        }
        if sign:
            post_data["_sign"] = sign
        captcha_code = getattr(self, "captcha_code", None)
        if captcha_code:
            post_data["captCode"] = captcha_code
            self.captcha_code = None  # one use: a wrong code gets a fresh captcha
        # Query string, as Xiaomi's current apps (and the maintained token extractor) send it.
        response = self.session.post(
            f"{ACCOUNT_HOST}/pass/serviceLoginAuth2", params=post_data, allow_redirects=False
        )
        data = _json_body(response.text)
        # Like the token extractor: the login succeeded only if it carries an ssecurity;
        # otherwise a notificationUrl means 2FA, whatever "result"/"location" say.
        if len(str(data.get("ssecurity") or "")) <= 4:
            if data.get("notificationUrl"):
                url = data["notificationUrl"]
                raise TwoFactorRequired(url if url.startswith("http") else ACCOUNT_HOST + url)
            if data.get("captchaUrl"):
                url = data["captchaUrl"]
                raise CaptchaRequired(url if url.startswith("http") else ACCOUNT_HOST + url)
            detail = _desc(data)
            raise MiCloudAccessDenied(
                f"Xiaomi rejected the login (code {data.get('code')}"
                f"{', ' + detail if detail else ''}; securityStatus "
                f"{data.get('securityStatus')})"
            )
        self.user_id = data["userId"]
        self.ssecurity = data["ssecurity"]
        self.cuser_id = data["cUserId"]
        self.pass_token = data["passToken"]
        return data["location"]

    # Email 2FA, as Xiaomi's identity pages do it (flow follows PiotrMachowski's
    # Xiaomi-cloud-tokens-extractor, MIT). The code must be requested and submitted from this
    # session: approving in a browser does not authorise our cookies.
    @_network_safe
    def send_email_code(self, notification_url: str) -> None:
        if self.session is None:
            self._init_session()
        self.session.get(notification_url)
        query = parse_qs(urlparse(notification_url).query)
        self._identity_context = (query.get("context") or [""])[0]
        if not self._identity_context:
            raise CloudError("Xiaomi's verification link has no context")
        self.session.get(
            f"{ACCOUNT_HOST}/identity/list",
            params={"sid": "xiaomiio", "context": self._identity_context, "_locale": "en_US"},
        )
        response = self.session.post(
            f"{ACCOUNT_HOST}/identity/auth/sendEmailTicket",
            params={
                "_dc": int(time.time() * 1000),
                "sid": "xiaomiio",
                "context": self._identity_context,
                "mask": "0",
                "_locale": "en_US",
            },
            data={"retry": "0", "icode": "", "_json": "true", "ick": self._ick()},
        )
        data = _json_body(response.text)
        if data.get("code", 0) != 0:
            raise CloudError(f"Xiaomi did not send the email code{_reason(data)}")

    @_network_safe
    def verify_email_code(self, code: str) -> None:
        context = getattr(self, "_identity_context", "")
        if not context:
            raise CloudError("call send_email_code first")
        response = self.session.post(
            f"{ACCOUNT_HOST}/identity/auth/verifyEmail",
            params={
                "_flag": "8",
                "_json": "true",
                "sid": "xiaomiio",
                "context": context,
                "mask": "0",
                "_locale": "en_US",
            },
            data={
                "_flag": "8",
                "ticket": code.strip(),
                "trust": "true",
                "_json": "true",
                "ick": self._ick(),
            },
        )
        data = _json_body(response.text)
        if data.get("code", 0) != 0:
            raise MiCloudAccessDenied(f"Xiaomi rejected the code{_reason(data)}")
        check_url = data.get("location")
        if not check_url:
            found = re.search(
                r"https://account\.xiaomi\.com/identity/result/check\?[^\"']+", response.text
            )
            check_url = found.group(0) if found else None
        if not check_url:
            raise CloudError("Xiaomi accepted the code but gave no next step")
        end = self.session.get(check_url, allow_redirects=False)
        end_url = end.headers.get("Location")
        if not end_url:
            raise CloudError("Xiaomi's verification check did not redirect")
        auth = self.session.get(end_url, allow_redirects=False)
        if auth.status_code == 200 and "Xiaomi Account - Tips" in auth.text:
            auth = self.session.get(end_url, allow_redirects=False)
        pragma = json.loads(auth.headers.get("extension-pragma") or "{}")
        self.ssecurity = pragma.get("ssecurity")
        sts_url = auth.headers.get("Location")
        if not self.ssecurity or not sts_url:
            raise CloudError("Xiaomi's login result was missing its security token")
        sts = self.session.get(sts_url)
        self.service_token = sts.cookies.get("serviceToken") or self._cookie("serviceToken")
        self.user_id = self.user_id or self._cookie("userId", numeric=True)
        self.cuser_id = self._cookie("cUserId")
        if not self.service_token or not self.user_id:
            raise CloudError("Xiaomi login finished without a service token")

    def _ick(self) -> str:
        return self._cookie("ick") or ""

    def _cookie(self, name: str, numeric: bool = False) -> str | None:
        for cookie in self.session.cookies:
            if cookie.name == name and cookie.value and (not numeric or cookie.value.isdigit()):
                return cookie.value
        return None

    def session_state(self) -> dict:
        """What a later process needs to reuse this login without 2FA."""
        return {
            "user_id": str(self.user_id or ""),
            "service_token": self.service_token or "",
            "ssecurity": self.ssecurity or "",
        }

    def restore_session(self, state: dict) -> None:
        if state.get("user_id") and state.get("service_token") and state.get("ssecurity"):
            self.user_id = state["user_id"]
            self.service_token = state["service_token"]
            self.ssecurity = state["ssecurity"]

    def map_file_url(self, object_name: str, country: str) -> str:
        raw = self.request_country(
            "/v2/home/get_interim_file_url",
            country,
            {"data": json.dumps({"obj_name": object_name})},
        )
        if not raw:
            raise CloudError("no response from Xiaomi cloud for the map file")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise CloudError("cloud returned an unreadable map file reply") from None
        result = body.get("result") if isinstance(body, dict) else None
        url = result.get("url") if isinstance(result, dict) else None
        if not url:
            # Never echo the reply: it can carry a signed URL or session details.
            code = body.get("code") if isinstance(body, dict) else None
            raise CloudError(f"cloud returned no map URL (code {code})")
        return url

    def property_history(
        self, did: str, key: str, start: int, end: int, limit: int, country: str
    ) -> list[tuple[int, str]]:
        raw = self.request_country(
            "/user/get_user_device_data",
            country,
            {
                "data": json.dumps(
                    {
                        "did": str(did),
                        "key": key,
                        "type": "prop",
                        "time_start": int(start),
                        "time_end": int(end),
                        "limit": int(limit),
                    }
                )
            },
        )
        if not raw:
            raise CloudError("no response from Xiaomi cloud for the robot's history")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise CloudError("cloud returned an unreadable history reply") from None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list):
            code = body.get("code") if isinstance(body, dict) else None
            raise CloudError(f"cloud returned no history (code {code})")
        rows = []
        for row in result:
            if isinstance(row, dict) and isinstance(row.get("value"), str):
                try:
                    rows.append((int(row.get("time")), row["value"]))
                except (TypeError, ValueError):
                    continue
        return sorted(rows, key=lambda r: r[0])


def _json_body(text: str) -> dict:
    try:
        data = json.loads(text.replace("&&&START&&&", ""))
    except json.JSONDecodeError:
        # Never echo the reply: it can carry URLs, identity contexts or session details.
        raise CloudError("unexpected reply from Xiaomi") from None
    return data if isinstance(data, dict) else {}


def _desc(data: dict) -> str:
    """Xiaomi's short human-readable reason, if it gave one (never the rest of the reply)."""
    desc = data.get("desc") or data.get("description")
    return desc[:80] if isinstance(desc, str) else ""


def _reason(data: dict) -> str:
    code = data.get("code")
    code = code if isinstance(code, int | str) else "?"
    desc = _desc(data)
    return f" (code {code}{', ' + desc if desc else ''})"


class CloudMap:
    """The robot's cloud map file, through the Xiaomi session saved at sign-in.

    It never logs in with the password: that would hit 2FA. When the session is missing or
    Xiaomi refuses it (micloud then drops the service token), every later fetch raises
    CloudSessionExpired without a network call, until a new sign-in builds a new CloudMap.
    """

    def __init__(self, creds):
        self._creds = creds
        self._cloud: XiaomiCloud | None = None
        self._expired = False
        self._lock = threading.Lock()

    @property
    def expired(self) -> bool:
        return self._expired

    def fetch_payload(self, vacuum) -> bytes:
        """Ask the robot for its map object and download the raw file from the cloud."""

        self._need_login()
        if self._expired:
            raise CloudSessionExpired()
        return self.fetch_object_payload(vacuum.request_map_object())

    def fetch_object_payload(self, object_name: str) -> bytes:
        """Download one map object (named by the robot) from the cloud."""

        self._need_login()
        with self._lock:
            url = self._map_url(object_name)
        # The URL is presigned (access key + signature in the query): requests' error texts
        # embed it, so only a safe summary may leave this method.
        try:
            response = requests.get(url, timeout=MAP_DOWNLOAD_TIMEOUT)
            response.raise_for_status()
        except requests.HTTPError as err:
            status = err.response.status_code if err.response is not None else "error"
            raise CloudError(f"map download failed: HTTP {status}") from None
        except requests.RequestException as err:
            raise CloudError(f"map download failed: {type(err).__name__}") from None
        return response.content

    def _need_login(self) -> None:
        # A QR sign-in stores only the session; a password sign-in stores both.
        session = getattr(self._creds, "cloud_session", None) or {}
        has_session = bool(session.get("user_id") and session.get("service_token"))
        if not has_session and not (self._creds.username and self._creds.password):
            raise CloudError("no Xiaomi login stored; sign in again")

    def _map_url(self, object_name: str) -> str:
        return self._call(lambda cloud: cloud.map_file_url(object_name, self._creds.country))

    def property_history(self, did: str, key: str, start: int, end: int, limit: int = 100):
        """The robot's recent values of one MIoT property, as the cloud stored them:
        [(unix time, value string)], oldest first. The r2209 uploads its live map frames
        this way (property 6.1)."""
        self._need_login()
        with self._lock:
            return self._call(
                lambda cloud: cloud.property_history(
                    did, key, start, end, limit, self._creds.country
                )
            )

    def _call(self, request):
        """Run `request(cloud)` on the restored session; a refused session expires for good."""
        if self._expired:
            raise CloudSessionExpired()
        if self._cloud is None:
            self._cloud = XiaomiCloud(self._creds.username, self._creds.password)
            # Reuse the session saved at sign-in; a fresh password login would hit 2FA.
            self._cloud.restore_session(getattr(self._creds, "cloud_session", None) or {})
        cloud = self._cloud
        # micloud's login() would return at once here (no network); without a token it would
        # log in with the password instead, so it is not called at all.
        if not (cloud.user_id and cloud.service_token):
            raise self._expire()
        try:
            result = request(cloud)
        except (CloudError, MiCloudException):
            if not cloud.service_token:  # micloud drops it when Xiaomi answers 403
                raise self._expire() from None
            raise
        if not cloud.service_token:
            raise self._expire()
        return result

    def _expire(self) -> CloudSessionExpired:
        """Forget the session for good (sticky until a new CloudMap); returns the error."""
        self._expired = True
        self._cloud = None
        return CloudSessionExpired()
