"""Find the vacuums on a Xiaomi account."""

import json
import logging
from dataclasses import dataclass

from .cloud import COUNTRIES
from .credentials import Credentials
from .profiles import is_supported, profile_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FoundVacuum:
    country: str
    did: str
    model: str
    name: str
    ip: str
    token: str
    supported: bool
    tested: bool


def _api(cloud, country: str, path: str, data: dict) -> dict:
    raw = cloud.request_country(path, country, {"data": json.dumps(data)})
    body = json.loads(raw) if raw else {}
    result = body.get("result") if isinstance(body, dict) else None
    return result if isinstance(result, dict) else {}


def _home_devices(cloud, country: str) -> list[dict]:
    """Devices in the account's own homes and in homes shared with it. micloud's plain
    device list can leave out a robot someone else shared (seen with a QR sign-in)."""
    homes: list[tuple[object, object]] = []
    owned = _api(
        cloud,
        country,
        "/v2/homeroom/gethome",
        {"fg": True, "fetch_share": True, "fetch_share_dev": True, "limit": 300, "app_ver": 7},
    )
    for home in owned.get("homelist") or []:
        homes.append((home.get("id"), home.get("uid") or cloud.user_id))
    counts = _api(
        cloud, country, "/v2/user/get_device_cnt", {"fetch_own": True, "fetch_share": True}
    )
    for home in (counts.get("share") or {}).get("share_family") or []:
        homes.append((home.get("home_id"), home.get("home_owner")))
    devices: list[dict] = []
    for home_id, owner in homes:
        try:
            home_id, owner = int(home_id), int(owner)
        except (TypeError, ValueError):
            continue
        listed = _api(
            cloud,
            country,
            "/v2/home/home_device_list",
            {
                "home_owner": owner,
                "home_id": home_id,
                "limit": 200,
                "get_split_device": True,
                "support_smart_home": True,
            },
        )
        devices.extend(d for d in listed.get("device_info") or [] if isinstance(d, dict))
    return devices


def discover_vacuums(cloud, countries=COUNTRIES) -> list[FoundVacuum]:
    """Every vacuum the account can reach, in every region. Raises the last error when no
    request at all succeeded (a refused session must not look like an empty account)."""
    found: list[FoundVacuum] = []
    seen: set[str] = set()
    answered, last_error = False, None
    for country in countries:
        devices: list[dict] = []
        for source in (
            lambda c: cloud.get_devices(country=c) or [],
            lambda c: _home_devices(cloud, c),
        ):
            try:
                devices.extend(source(country))
                answered = True
            except Exception as err:  # noqa: BLE001 - one bad region must not hide the others
                log.warning("device list for %s failed: %s", country, err)
                last_error = err
        for d in devices:
            did = str(d.get("did", ""))
            if did in seen:
                continue
            seen.add(did)
            model = str(d.get("model", ""))
            if ".vacuum." not in model:
                continue
            supported = is_supported(model)
            found.append(
                FoundVacuum(
                    country=country,
                    did=str(d.get("did", "")),
                    model=model,
                    name=str(d.get("name") or model),
                    ip=str(d.get("localip") or ""),
                    token=str(d.get("token") or ""),
                    supported=supported,
                    tested=supported and profile_for(model).tested,
                )
            )
    if not answered and last_error is not None:
        raise last_error
    return sorted(found, key=lambda v: (not v.supported, not v.tested, v.name))


def credentials_for(v: FoundVacuum, username: str, password: str, session: dict) -> Credentials:
    return Credentials(
        ip=v.ip,
        token=v.token,
        did=v.did,
        model=v.model,
        country=v.country,
        username=username,
        password=password,
        cloud_session=dict(session),
    )
