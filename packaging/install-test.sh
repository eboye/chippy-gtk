#!/usr/bin/env bash
# Install a Chippy package with the distro's own package manager (which must resolve every
# dependency the package declares), then run the smoke test. Meant for a fresh container.
# Usage: packaging/install-test.sh PACKAGE_FILE
set -euo pipefail

pkg_file="$(realpath "$1")"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. /etc/os-release
echo "Installing $(basename "$pkg_file") on ${PRETTY_NAME:-$ID}"

case "$ID" in
    debian | ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -q
        apt-get install -y -q --no-install-recommends "$pkg_file"
        apt-get install -y -q --no-install-recommends xvfb xauth dbus-daemon fonts-dejavu-core
        ;;
    fedora)
        dnf install -y -q "$pkg_file"
        dnf install -y -q xorg-x11-server-Xvfb dbus-daemon dejavu-sans-fonts
        ;;
    opensuse-tumbleweed | opensuse-leap)
        zypper --non-interactive install --allow-unsigned-rpm "$pkg_file"
        zypper --non-interactive install xorg-x11-server dbus-1-daemon dejavu-fonts
        ;;
    arch)
        pacman -Syu --noconfirm --needed
        pacman -U --noconfirm "$pkg_file"
        pacman -S --noconfirm --needed xorg-server-xvfb ttf-dejavu
        ;;
    *)
        echo "don't know how to install on $ID" >&2
        exit 2
        ;;
esac

"$here/smoke-test.sh"
