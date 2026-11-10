"""The room map: rooms texture, labels, charger, live robot and path; click to select,
scroll/pinch/drag."""

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from ..mapmodel import Viewport, texture_rgba  # noqa: E402

_DARK_BG, _LIGHT_BG = (29, 29, 32), (255, 255, 255)
HEADING_LENGTH = 20  # px from the robot's centre; its dot with ring is 11.5 px


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def _rounded(box: Graphene.Rect, radius: float) -> Gsk.RoundedRect:
    # init_from_rect() returns a pointer into a temporary; keep our own instance instead.
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(box, radius)
    return rounded


class MapView(Gtk.Widget):
    __gtype_name__ = "ChippyMapView"
    __gsignals__ = {"room-clicked": (GObject.SignalFlags.RUN_FIRST, None, (int,))}

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.grid = None
        self.rooms = []
        self.selected: set[int] = set()
        self.viewport: Viewport | None = None
        self.live = None  # mapmodel.LiveState while the robot cleans
        self._texture = None
        self._pointer = (0.0, 0.0)
        self._drag_origin = (0.0, 0.0)
        self._zoom_origin = 1.0
        self._dragged = False
        self._click_seg = 0  # room toggled by the last single click (undone by a double)
        self._has_pointer = False

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", lambda *_a: setattr(self, "_zoom_origin", 1.0))
        zoom.connect("scale-changed", self._on_pinch)
        self.add_controller(zoom)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_a: self._invalidate())
        style.connect("notify::accent-color", lambda *_a: self.queue_draw())

    # data ------------------------------------------------------------------
    def set_map(self, grid, rooms) -> None:
        size_changed = (
            grid is None
            or self.grid is None
            or (grid.width, grid.height) != (self.grid.width, self.grid.height)
        )
        self.grid, self.rooms = grid, list(rooms)
        if grid is not None and size_changed:
            self.viewport = Viewport(grid.width, grid.height)
            self.viewport.resize(self.get_width(), self.get_height())
        if grid is None:
            self.viewport = None
        self._invalidate()

    def set_rooms(self, rooms) -> None:
        self.rooms = list(rooms)
        self._invalidate()

    def set_selection(self, selected: set[int]) -> None:
        self.selected = set(selected)
        self._invalidate()

    def set_live(self, live) -> None:
        """Robot position and path changed: redraw only, the rooms texture stays."""
        self.live = live
        self.queue_draw()

    def reset_view(self) -> None:
        if self.viewport:
            self.viewport.fit()
            self.queue_draw()

    def room_at(self, x: float, y: float) -> int:
        if not (self.grid and self.viewport):
            return 0
        cx, cy = self.viewport.to_cell(x, y)
        seg = self.grid.seg_at(int(cx // 1), int(cy // 1))
        room = next((r for r in self.rooms if r.id == seg), None)
        return seg if room and not room.hidden else 0

    def label_points(self) -> dict[int, tuple[float, float]]:
        if not self.viewport:
            return {}
        return {
            r.id: self.viewport.to_widget(*r.anchor)
            for r in self.rooms
            if r.anchor and not r.hidden
        }

    def _invalidate(self) -> None:
        self._texture = None
        self.queue_draw()

    # input -----------------------------------------------------------------
    def _on_click(self, _gesture, n_press, x, y) -> None:
        if n_press == 2:
            # The double click's first release already toggled the room under the
            # pointer; toggle it back so fitting the view leaves the selection alone.
            seg, self._click_seg = self._click_seg, 0
            if seg:
                self.emit("room-clicked", seg)
            self.reset_view()
            return
        self._click_seg = 0
        if self._dragged:
            return
        seg = self.room_at(x, y)
        if seg:
            self._click_seg = seg
            self.emit("room-clicked", seg)

    def _on_motion(self, _ctl, x, y) -> None:
        self._pointer = (x, y)
        self._has_pointer = True

    def _on_scroll(self, _ctl, _dx, dy) -> bool:
        if not self.viewport:
            return False
        factor = min(2.0, max(0.5, 1.15 ** (-dy)))
        if self._has_pointer:
            x, y = self._pointer
        else:
            x, y = self.get_width() / 2, self.get_height() / 2
        self.viewport.zoom_at(factor, x, y)
        self.queue_draw()
        return True

    def _on_drag_begin(self, _gesture, _x, _y) -> None:
        self._dragged = False
        if self.viewport:
            self._drag_origin = (self.viewport.ox, self.viewport.oy)

    def _on_drag_update(self, gesture, dx, dy) -> None:
        if self.viewport and (abs(dx) > 3 or abs(dy) > 3):
            self._dragged = True
            if gesture is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.viewport.ox, self.viewport.oy = self._drag_origin
            self.viewport.pan(dx, dy)
            self.queue_draw()

    def _on_pinch(self, gesture, scale) -> None:
        if not self.viewport:
            return
        ok, x, y = gesture.get_bounding_box_center()
        if ok:
            self.viewport.zoom_at(scale / self._zoom_origin, x, y)
            self._zoom_origin = scale
            self.queue_draw()

    # drawing ---------------------------------------------------------------
    def do_size_allocate(self, width, height, baseline) -> None:
        if self.viewport:
            self.viewport.resize(width, height)

    def _ensure_texture(self) -> None:
        if self._texture is not None or self.grid is None:
            return
        dark = Adw.StyleManager.get_default().get_dark()
        data = texture_rgba(self.grid, self.rooms, self.selected, _DARK_BG if dark else _LIGHT_BG)
        self._texture = Gdk.MemoryTexture.new(
            self.grid.width,
            self.grid.height,
            Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(data),
            self.grid.width * 4,
        )

    def do_snapshot(self, snapshot) -> None:
        if not (self.grid and self.viewport):
            return
        self._ensure_texture()
        vp = self.viewport
        rect = Graphene.Rect().init(
            vp.ox, vp.oy, self.grid.width * vp.scale, self.grid.height * vp.scale
        )
        snapshot.append_scaled_texture(self._texture, Gsk.ScalingFilter.NEAREST, rect)
        if self.grid.charger:
            self._dot(snapshot, *vp.to_widget(*self.grid.charger), 10, "#2ec27e", "#ffffff")
        if self.live is not None:
            self._live_path(snapshot)
        self._labels(snapshot)
        if self.live is not None and self.live.robot is not None:
            self._robot(snapshot)  # on top: a label must never hide the robot

    def _dot(self, snapshot, x, y, r, fill, ring) -> None:
        for radius, color in ((r + 2.5, ring), (r, fill)):
            box = Graphene.Rect().init(x - radius, y - radius, radius * 2, radius * 2)
            snapshot.push_rounded_clip(_rounded(box, radius))
            snapshot.append_color(_rgba(color), box)
            snapshot.pop()

    def _live_path(self, snapshot) -> None:
        vp, points = self.viewport, self.live.path
        if len(points) < 2:
            return
        builder = Gsk.PathBuilder.new()
        builder.move_to(*vp.to_widget(*points[0]))
        for point in points[1:]:
            builder.line_to(*vp.to_widget(*point))
        path = builder.to_path()  # keep the path and stroke alive while appending
        stroke = Gsk.Stroke.new(2.5)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        dark = Adw.StyleManager.get_default().get_dark()
        color = _rgba("rgba(255,255,255,0.75)" if dark else "rgba(0,0,0,0.6)")
        snapshot.append_stroke(path, stroke, color)

    def _robot(self, snapshot) -> None:
        live = self.live
        x, y = self.viewport.to_widget(*live.robot)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        # Heading: map angles have y up, the widget has y down, hence (cos a, -sin a). Drawn
        # under the dot, so what shows is an accent pointer with a white outline sticking out.
        a = math.radians(live.angle)
        builder = Gsk.PathBuilder.new()
        builder.move_to(x, y)
        builder.line_to(x + HEADING_LENGTH * math.cos(a), y - HEADING_LENGTH * math.sin(a))
        heading = builder.to_path()  # keep the path and strokes alive while appending
        outline = Gsk.Stroke.new(6)
        outline.set_line_cap(Gsk.LineCap.ROUND)
        snapshot.append_stroke(heading, outline, _rgba("#ffffff"))
        line = Gsk.Stroke.new(3)
        line.set_line_cap(Gsk.LineCap.ROUND)
        snapshot.append_stroke(heading, line, accent)
        self._dot(snapshot, x, y, 9, accent.to_string(), "#ffffff")

    def _labels(self, snapshot) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        for seg, (x, y) in self.label_points().items():
            room = next(r for r in self.rooms if r.id == seg)
            selected = seg in self.selected
            layout = self.create_pango_layout(None)
            area = f"\n<small>{room.area_m2:.1f} m²</small>" if room.area_m2 is not None else ""
            layout.set_markup(f"<b>{GLib.markup_escape_text(room.name)}</b>{area}", -1)
            layout.set_alignment(Pango.Alignment.CENTER)
            w, h = layout.get_pixel_size()
            box = Graphene.Rect().init(x - w / 2 - 10, y - h / 2 - 5, w + 20, h + 10)
            if selected:
                bg, fg = accent, _rgba("#ffffff")
            elif dark:
                bg, fg = _rgba("rgba(48,48,52,0.92)"), _rgba("#ffffff")
            else:
                bg, fg = _rgba("rgba(255,255,255,0.94)"), _rgba("rgba(0,0,0,0.8)")
            snapshot.push_rounded_clip(_rounded(box, 10))
            snapshot.append_color(bg, box)
            snapshot.pop()
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x - w / 2, y - h / 2))
            snapshot.append_layout(layout, fg)
            snapshot.restore()
