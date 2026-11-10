import Gio from 'gi://Gio';

export const BUS_NAME = 'io.github.eboye.Chippy';
export const OBJECT_PATH = '/io/github/eboye/Chippy';
export const IFACE_NAME = 'io.github.eboye.Chippy.Vacuum';

const IFACE_XML = `
<node>
  <interface name="${IFACE_NAME}">
    <property name="State" type="s" access="read"/>
    <property name="Battery" type="y" access="read"/>
    <property name="Charging" type="b" access="read"/>
    <property name="Error" type="s" access="read"/>
    <property name="Suction" type="s" access="read"/>
    <property name="Water" type="s" access="read"/>
    <property name="HasWaterTank" type="b" access="read"/>
    <property name="Rooms" type="a(us)" access="read"/>
    <property name="CleanedArea" type="u" access="read"/>
    <property name="CleaningTime" type="u" access="read"/>
    <method name="Start"/>
    <method name="Pause"/>
    <method name="Stop"/>
    <method name="Dock"/>
    <method name="Locate"/>
    <method name="CleanRooms"><arg name="ids" type="au" direction="in"/></method>
    <method name="SetSuction"><arg name="level" type="s" direction="in"/></method>
    <method name="SetWater"><arg name="level" type="s" direction="in"/></method>
    <method name="RefreshRooms"/>
    <method name="ShowWindow"/>
  </interface>
</node>`;

const VacuumProxy = Gio.DBusProxy.makeProxyWrapper(IFACE_XML);

export function createProxy(cancellable = null) {
    return new Promise((resolve, reject) => {
        new VacuumProxy(Gio.DBus.session, BUS_NAME, OBJECT_PATH, (proxy, error) => {
            if (error)
                reject(error);
            else
                resolve(proxy);
        }, cancellable, Gio.DBusProxyFlags.DO_NOT_AUTO_START);
    });
}

// Launches Chippy if it isn't running (its desktop file is D-Bus activatable).
export function openChippy() {
    return new Promise((resolve, reject) => {
        Gio.DBus.session.call(BUS_NAME, OBJECT_PATH, IFACE_NAME, 'ShowWindow', null, null,
            Gio.DBusCallFlags.NONE, 10000, null, (conn, res) => {
                try {
                    conn.call_finish(res);
                    resolve();
                } catch (e) {
                    reject(e);
                }
            });
    });
}
