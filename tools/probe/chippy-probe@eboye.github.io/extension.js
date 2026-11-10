// DEV-ONLY probe for tools/nested-check.sh. Never shipped.
// Drives the Chippy app window (fake robot) and the Chippy indicator with real
// virtual pointer/keyboard input inside the isolated headless shell.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');

const TARGET_UUID = 'chippy@eboye.github.io';
const OUT = GLib.getenv('CN_PROBE_OUT') ?? '/tmp/chippy-nest/out';

export default class ChippyProbe extends Extension {
    enable() {
        this._sources = new Set();
        this._results = [];
        this._sleep(4000)
            .then(() => this._main())
            .catch(e => this._check('probe-crashed', false, `${e}\n${e.stack}`))
            .finally(() => this._finish());
    }

    disable() {
        for (const id of this._sources)
            GLib.source_remove(id);
        this._sources.clear();
        this._pointer = null;
        this._keyboard = null;
    }

    // Real input: a virtual pointer moves to the actor's centre and clicks.
    _click(actor) {
        const [x, y] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        return this._clickAt(x + w / 2, y + h / 2);
    }

    // `clicks` = 2 sends a real double click: both presses within GTK's
    // double-click time (400 ms), so the app sees n_press == 2.
    async _clickAt(cx, cy, clicks = 1) {
        const seat = global.stage.context.get_backend().get_default_seat();
        this._pointer ??= seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        // A new pointer starts at (0, 0), the hot corner; move twice and close
        // the overview if it opened, so the click lands on the actor.
        this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), cx, cy);
        await this._sleep(250);
        this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), cx, cy);
        await this._sleep(300);
        if (Main.overview.visible) {
            Main.overview.hide();
            await this._sleep(1000);
            this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), cx, cy);
            await this._sleep(200);
        }
        for (let n = 0; n < clicks; n++) {
            if (n > 0)
                await this._sleep(80);
            try {
                this._pointer.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY,
                    Clutter.ButtonState.PRESSED);
                await this._sleep(clicks > 1 ? 50 : 80);
            } finally {
                this._pointer.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY,
                    Clutter.ButtonState.RELEASED);
            }
        }
        await this._sleep(300);
    }

    // Real keyboard input via a virtual keyboard; Mutter picks the keycode (and
    // Shift level) for each keyval.
    async _key(keyval, modifiers = []) {
        const seat = global.stage.context.get_backend().get_default_seat();
        this._keyboard ??= seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        const send = (sym, state) =>
            this._keyboard.notify_keyval(GLib.get_monotonic_time(), sym, state);
        for (const mod of modifiers)
            send(mod, Clutter.KeyState.PRESSED);
        try {
            if (modifiers.length)
                await this._sleep(80);
            send(keyval, Clutter.KeyState.PRESSED);
            await this._sleep(30);
            send(keyval, Clutter.KeyState.RELEASED);
            if (modifiers.length)
                await this._sleep(80);
        } finally {
            for (const mod of [...modifiers].reverse())
                send(mod, Clutter.KeyState.RELEASED);
        }
        await this._sleep(60);
    }

    async _type(text) {
        for (const ch of text)
            await this._key(Clutter.unicode_to_keysym(ch.codePointAt(0)));
    }

    _sleep(ms) {
        return new Promise(resolve => {
            const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
                this._sources.delete(id);
                resolve();
                return GLib.SOURCE_REMOVE;
            });
            this._sources.add(id);
        });
    }

    _check(name, ok, detail = '') {
        this._results.push(ok ? `PASS ${name}` : `FAIL ${name}: ${detail}`);
    }

    _finish() {
        GLib.mkdir_with_parents(OUT, 0o755);
        GLib.file_set_contents(`${OUT}/results.txt`, `${this._results.join('\n')}\n`);
        GLib.file_set_contents(`${OUT}/done`, 'ok');
    }

    async _shot(name) {
        const file = Gio.File.new_for_path(`${OUT}/${name}.png`);
        const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
        await new Shell.Screenshot().screenshot_area(0, 0, global.stage.width, global.stage.height, stream);
        stream.close(null);
    }

    async _waitFor(predicate, ms = 8000) {
        for (let waited = 0; waited < ms; waited += 200) {
            if (predicate())
                return true;
            await this._sleep(200);
        }
        return predicate();
    }

    // The app's test-state file (see MainWindow.test_state); null while unreadable.
    _state() {
        try {
            const [, bytes] = GLib.file_get_contents(`${OUT}/state.json`);
            return JSON.parse(new TextDecoder().decode(bytes));
        } catch {
            return null;
        }
    }

    _appWindow() {
        return global.display.list_all_windows().find(w => w.get_title() === 'Chippy') ?? null;
    }

    // Surface coordinates from the app → stage coordinates.
    _toStage(win, [x, y]) {
        const rect = win.get_buffer_rect();
        return [rect.x + x, rect.y + y];
    }

    async _clickApp(point, double = false) {
        const win = this._appWindow();
        const [sx, sy] = this._toStage(win, point);
        await this._clickAt(sx, sy, double ? 2 : 1);
    }

    async _scrollApp(point, direction) {
        const win = this._appWindow();
        const [sx, sy] = this._toStage(win, point);
        const seat = global.stage.context.get_backend().get_default_seat();
        this._pointer ??= seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), sx, sy);
        await this._sleep(200);
        this._pointer.notify_discrete_scroll(GLib.get_monotonic_time(), direction,
            Clutter.ScrollSource.WHEEL);
        await this._sleep(400);
    }

    // The app's own D-Bus State property; '' when the app is not on the bus.
    _busState() {
        try {
            const reply = Gio.DBus.session.call_sync('io.github.eboye.Chippy',
                '/io/github/eboye/Chippy', 'org.freedesktop.DBus.Properties', 'Get',
                new GLib.Variant('(ss)', ['io.github.eboye.Chippy.Vacuum', 'State']), null,
                Gio.DBusCallFlags.NO_AUTO_START, 2000, null);
            return reply.deepUnpack()[0].deepUnpack();
        } catch {
            return '';
        }
    }

    async _main() {
        const scenario = GLib.getenv('CN_SCENARIO') ?? 'main';
        const opened = await this._waitFor(() => this._appWindow() && this._state()?.page, 20000);
        // The shell starts in the overview; leave it so screenshots show the window.
        if (Main.overview.visible) {
            Main.overview.hide();
            await this._waitFor(() => !Main.overview.visible && !Main.overview.animationInProgress, 3000);
            await this._sleep(500);
        }
        if (scenario === 'signin')
            return this._signin(opened);
        if (scenario === 'qr')
            return this._qr(opened);
        return this._mainScenario(opened);
    }

    async _mainScenario(opened) {
        this._check('app-window-opens', Boolean(opened), `state=${JSON.stringify(this._state())}`);
        if (!opened)
            return;
        await this._waitFor(() => Object.keys(this._state()?.rooms ?? {}).length >= 4, 10000);
        const rooms = this._state().rooms;
        this._check('rooms-on-map', Object.keys(rooms).length === 4, JSON.stringify(rooms));
        await this._shot('main');

        await this._clickApp(rooms['1']);
        this._check('click-room-selects',
            await this._waitFor(() => JSON.stringify(this._state().selected) === '[1]', 3000),
            JSON.stringify(this._state().selected));
        this._check('clean-label', /^Clean 1 room · \d+\.\d m²$/.test(this._state().clean_label),
            this._state().clean_label);

        await this._clickApp(this._state().checks['2']);
        this._check('list-check-selects',
            await this._waitFor(() => JSON.stringify(this._state().selected) === '[1,2]', 3000),
            JSON.stringify(this._state().selected));
        await this._shot('two-selected');

        await this._clickApp(this._state().rooms['1']);
        this._check('click-again-deselects',
            await this._waitFor(() => JSON.stringify(this._state().selected) === '[2]', 3000),
            JSON.stringify(this._state().selected));

        const map = this._state().buttons.map;
        await this._scrollApp(map, Clutter.ScrollDirection.UP);
        await this._scrollApp(map, Clutter.ScrollDirection.UP);
        this._check('scroll-zooms',
            await this._waitFor(() => this._state().scale > this._state().fit_scale * 1.2, 3000),
            `scale=${this._state().scale} fit=${this._state().fit_scale}`);
        await this._shot('zoomed');
        await this._clickApp(map, true);
        // Fitting must not change the selection (the double click lands on a room).
        this._check('double-click-fits',
            await this._waitFor(() => Math.abs(this._state().scale - this._state().fit_scale) < 1e-6 &&
                JSON.stringify(this._state().selected) === '[2]', 3000),
            `scale=${this._state().scale} selected=${JSON.stringify(this._state().selected)}`);

        await this._clickApp(this._state().buttons.clean);
        this._check('clean-starts', await this._waitFor(() => this._busState() === 'cleaning', 8000),
            `State=${this._busState()}`);

        // The live robot position moves while cleaning: two snapshots ≥ 6 s apart differ.
        const robot = () => this._state()?.live_robot ?? null;
        await this._waitFor(() => robot() !== null, 15000);
        const first = robot();
        await this._sleep(6000);
        const moved = await this._waitFor(
            () => first !== null && robot() !== null && JSON.stringify(robot()) !== JSON.stringify(first),
            6000);
        this._check('live-robot-moves', moved,
            `first=${JSON.stringify(first)} now=${JSON.stringify(robot())}`);
        await this._shot('cleaning');

        const ind = Main.panel.statusArea[TARGET_UUID];
        this._check('indicator-in-panel', Boolean(ind), 'no status area entry');
        if (ind) {
            this._check('indicator-follows-app',
                await this._waitFor(() => ind._statusItem.label.text.startsWith('Cleaning'), 20000),
                ind._statusItem.label.text);
            await this._click(ind);
            await this._sleep(400);
            await this._shot('indicator-menu');
            ind.menu.close(false);
        }

        this._appWindow().delete(global.get_current_time());
        await this._sleep(1500);
        this._check('close-keeps-running', !this._appWindow() && this._busState() !== '',
            `window=${Boolean(this._appWindow())} State=${this._busState()}`);
        if (ind) {
            await this._click(ind);
            await this._click(ind._openItem);
            this._check('open-from-menu', await this._waitFor(() => this._appWindow(), 8000),
                'window did not come back');
        }

        const win = this._appWindow();
        if (win) {
            // Mutter maximizes a 1180×800 window on the 1280×800 monitor, and a
            // maximized window ignores move_resize_frame: unmaximize first.
            if (win.is_maximized()) {
                win.unmaximize();
                await this._waitFor(() => !win.is_maximized(), 2000);
                await this._sleep(500);
            }
            const r = win.get_frame_rect();
            win.move_resize_frame(false, r.x, r.y, 640, 720);
            await this._sleep(500);
            const nr = win.get_frame_rect();
            this._results.push(`INFO narrow frame=${nr.x},${nr.y} ${nr.width}x${nr.height}`);
            this._check('narrow-collapses',
                await this._waitFor(() => this._state().sidebar_collapsed === true, 5000),
                JSON.stringify(this._state()));
            await this._shot('narrow');
        }
    }

    async _qr(opened) {
        this._check('signin-page-shown',
            Boolean(opened) && await this._waitFor(() => this._state().page === 'signed-out', 10000),
            JSON.stringify(this._state()));
        // The virtual keyboard drops its very first keyval (see _signin): warm it up.
        await this._key(Clutter.KEY_Shift_L);
        await this._sleep(500);
        await this._clickApp(this._state().buttons.sign_in);
        await this._waitFor(() => this._state().dialog === 'account', 5000);
        await this._sleep(500);
        await this._shot('qr-account');
        const qrButton = this._state().dialog_buttons?.qr;
        this._check('qr-button-shown', Boolean(qrButton), JSON.stringify(this._state().dialog_buttons));
        if (!qrButton)
            return;
        await this._clickApp(qrButton);
        this._check('qr-page', await this._waitFor(() => this._state().dialog === 'qr', 8000),
            `dialog=${this._state().dialog}`);
        await this._shot('qr-code');
        this._check('qr-approved-devices',
            await this._waitFor(() => this._state().dialog === 'devices', 15000),
            `dialog=${this._state().dialog}`);
        await this._key(Clutter.KEY_Return);
        this._check('qr-signed-in', await this._waitFor(() => this._state().page === 'main', 10000),
            JSON.stringify(this._state()));
        await this._shot('qr-done');
    }

    async _signin(opened) {
        this._check('signin-page-shown',
            Boolean(opened) && await this._waitFor(() => this._state().page === 'signed-out', 10000),
            JSON.stringify(this._state()));
        await this._shot('signin-welcome');
        // Signed out the app reports State 'offline', Battery 0: no "0%" in the top bar.
        const ind = Main.panel.statusArea[TARGET_UUID];
        this._check('indicator-signed-out',
            Boolean(ind) && await this._waitFor(() => ind._statusItem.label.text === 'Offline', 5000) &&
                ind._label.text === '',
            `status=${ind?._statusItem.label.text} label=${ind?._label.text}`);
        // A new virtual keyboard drops its very first keyval (seen here: the 'c' of
        // the username was lost even 2.5 s after the dialog opened, and a harmless
        // Shift sent earlier, to the main window, fixes it). Warm it up first.
        await this._key(Clutter.KEY_Shift_L);
        await this._sleep(500);
        await this._clickApp(this._state().buttons.sign_in);
        await this._waitFor(() => this._state().dialog === 'account', 5000);
        await this._sleep(500);
        await this._type('captcha-2fa@test');
        await this._key(Clutter.KEY_Return);
        await this._type('pw');
        await this._shot('signin-account');
        await this._key(Clutter.KEY_Return);
        this._check('captcha-page', await this._waitFor(() => this._state().dialog === 'captcha', 8000),
            `dialog=${this._state().dialog}`);
        await this._shot('signin-captcha');
        await this._type('x23w2');
        await this._key(Clutter.KEY_Return);
        this._check('code-page', await this._waitFor(() => this._state().dialog === 'code', 8000),
            `dialog=${this._state().dialog}`);
        await this._type('123456');
        await this._key(Clutter.KEY_Return);
        this._check('devices-page', await this._waitFor(() => this._state().dialog === 'devices', 8000),
            `dialog=${this._state().dialog}`);
        await this._shot('signin-devices');
        await this._key(Clutter.KEY_Return);
        this._check('signed-in-main', await this._waitFor(() => this._state().page === 'main', 10000),
            JSON.stringify(this._state()));
        if (ind) {
            this._check('indicator-signed-in',
                await this._waitFor(() => /^Docked · \d+%/.test(ind._statusItem.label.text) &&
                    /^\d+%$/.test(ind._label.text), 10000),
                `status=${ind._statusItem.label.text} label=${ind._label.text}`);
        }
        await this._shot('signin-done');
    }
}
