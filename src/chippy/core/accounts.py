"""Find the vacuums on a Xiaomi account."""

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


def discover_vacuums(cloud, countries=COUNTRIES) -> list[FoundVacuum]:
    found: list[FoundVacuum] = []
    for country in countries:
        try:
            devices = cloud.get_devices(country=country) or []
        except Exception as err:  # noqa: BLE001 - one bad region must not hide the others
            log.warning("device list for %s failed: %s", country, err)
            continue
        for d in devices:
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
