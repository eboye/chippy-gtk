import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {createProxy, openChippy} from './dbus.js';

const STATE_LABELS = {
    idle: 'Idle',
    cleaning: 'Cleaning',
    paused: 'Paused',
    returning: 'Returning to dock',
    charging: 'Charging',
    docked: 'Docked',
    error: 'Error',
    offline: 'Offline',
};
const ACTIONS = [
    ['Start', 'Start'],
    ['Pause', 'Pause'],
    ['Stop', 'Stop'],
    ['Dock', 'Return to dock'],
    ['Locate', 'Locate'],
];
const SUCTION_LEVELS = ['quiet', 'standard', 'strong', 'turbo'];
const WATER_LEVELS = ['low', 'medium', 'high'];
const capitalize = s => s.charAt(0).toUpperCase() + s.slice(1);

const VacuumIndicator = GObject.registerClass(
class VacuumIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Chippy');
        this._proxy = null;
        this._proxySignals = [];
        this._selectedRooms = new Set();
        this._roomsKey = '';

        const box = new St.BoxLayout();
        this._icon = new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/chippy-symbolic.svg`),
            style_class: 'system-status-icon',
        });
        this._label = new St.Label({
            text: '',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'chippy-label',
        });
        box.add_child(this._icon);
        box.add_child(this._label);
        this.add_child(box);

        this._statusItem = new PopupMenu.PopupMenuItem('Connecting…', {reactive: false});
        this.menu.addMenuItem(this._statusItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._actionItems = new Map();
        for (const [method, label] of ACTIONS) {
            const item = this.menu.addAction(label, () => this._call(method));
            this._actionItems.set(method, item);
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._roomsMenu = new PopupMenu.PopupSubMenuMenuItem('Clean rooms');
        this._suctionMenu = this._levelMenu('Suction', SUCTION_LEVELS, 'SetSuction');
        this._waterMenu = this._levelMenu('Water', WATER_LEVELS, 'SetWater');
        this.menu.addMenuItem(this._roomsMenu);
        this.menu.addMenuItem(this._suctionMenu);
        this.menu.addMenuItem(this._waterMenu);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._openItem = this.menu.addAction('Open Chippy', () => {
            openChippy().catch(e => Main.notify('Chippy', `Couldn't open Chippy: ${e.message}`));
        });
        this._sync();
    }

    setProxy(proxy) {
        this._disconnectProxy();
        this._proxy = proxy;
        this._proxySignals = [
            proxy.connect('g-properties-changed', () => this._sync()),
            proxy.connect('notify::g-name-owner', () => this._sync()),
        ];
        this._sync();
    }

    _levelMenu(title, levels, method) {
        const sub = new PopupMenu.PopupSubMenuMenuItem(title);
        sub._levelItems = new Map();
        for (const level of levels) {
            const item = sub.menu.addAction(capitalize(level), () => this._call(method, level));
            sub._levelItems.set(level, item);
        }
        return sub;
    }

    _online() {
        return Boolean(this._proxy?.g_name_owner);
    }

    _sync() {
        const online = this._online();
        const p = this._proxy;
        const state = online ? p.State ?? 'offline' : 'offline';
        if (!online) {
            this._statusItem.label.text = this._proxy
                ? 'Chippy isn\'t running'
                : 'Connecting…';
            this._label.text = '';
        } else if (state === 'offline') {
            // Signed out or robot unreachable: there is no battery reading to show
            // (the app reports 0), so don't put "0%" in the top bar.
            this._statusItem.label.text = STATE_LABELS.offline;
            this._label.text = '';
        } else {
            let text = `${STATE_LABELS[state] ?? state} · ${p.Battery}%`;
            if (p.Error)
                text += ` · ${p.Error}`;
            if (state === 'cleaning')
                text += ` · ${p.CleanedArea} m², ${p.CleaningTime} min`;
            this._statusItem.label.text = text;
            this._label.text = `${p.Battery}%`;
        }
        if (online && state !== 'offline')
            this.remove_style_class_name('chippy-offline');
        else
            this.add_style_class_name('chippy-offline');

        const usable = online && state !== 'offline';
        for (const item of this._actionItems.values())
            item.setSensitive(usable);
        this._suctionMenu.setSensitive(usable);
        this._waterMenu.visible = usable && Boolean(p?.HasWaterTank);
        for (const [level, item] of this._suctionMenu._levelItems)
            item.setOrnament(p?.Suction === level ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        for (const [level, item] of this._waterMenu._levelItems)
            item.setOrnament(p?.Water === level ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        this._syncRooms(usable ? p.Rooms ?? [] : []);
    }

    _syncRooms(rooms) {
        const key = JSON.stringify(rooms);
        this._roomsMenu.setSensitive(rooms.length > 0);
        if (key === this._roomsKey)
            return;
        this._roomsKey = key;
        this._roomsMenu.menu.removeAll();
        const ids = new Set(rooms.map(([id]) => id));
        for (const id of [...this._selectedRooms]) {
            if (!ids.has(id))
                this._selectedRooms.delete(id);
        }
        for (const [id, name] of rooms) {
            const item = new PopupMenu.PopupSwitchMenuItem(name, this._selectedRooms.has(id));
            // Toggle without closing the whole menu.
            item.activate = () => item.toggle();
            item.connect('toggled', (_item, on) => {
                if (on)
                    this._selectedRooms.add(id);
                else
                    this._selectedRooms.delete(id);
            });
            this._roomsMenu.menu.addMenuItem(item);
        }
        this._roomsMenu.menu.addAction('Clean selected rooms', () => {
            const selected = rooms.map(([id]) => id).filter(id => this._selectedRooms.has(id));
            if (selected.length === 0) {
                Main.notify('Chippy', 'Select at least one room first.');
                return;
            }
            this._call('CleanRooms', selected);
        });
    }

    async _call(method, ...args) {
        if (!this._online())
            return;
        try {
            await this._proxy[`${method}Async`](...args);
        } catch (e) {
            if (e instanceof GLib.Error)
                Gio.DBusError.strip_remote_error(e);
            Main.notify('Chippy', `${method} failed: ${e.message}`);
        }
    }

    _disconnectProxy() {
        if (this._proxy) {
            for (const id of this._proxySignals)
                this._proxy.disconnect(id);
        }
        this._proxySignals = [];
        this._proxy = null;
    }

    destroy() {
        this._disconnectProxy();
        super.destroy();
    }
});

export default class ChippyExtension extends Extension {
    enable() {
        this._cancellable = new Gio.Cancellable();
        const indicator = new VacuumIndicator(this);
        this._indicator = indicator;
        Main.panel.addToStatusArea(this.uuid, indicator);
        createProxy(this._cancellable)
            .then(proxy => {
                // A disable/enable cycle may have replaced the indicator meanwhile.
                if (this._indicator === indicator)
                    indicator.setProxy(proxy);
            })
            .catch(e => {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.error(`[chippy] proxy failed: ${e.message}`);
            });
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
