"""The room map: rooms texture, zones and walls, labels with order badges, charger, live robot
and path; click to select, scroll/pinch/drag. With an edit tool set, clicks and drags pick
rooms and draw, move, resize or delete zones and walls instead."""

import math
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from ..core.mapedit import MIN_SIZE_MM, Line, MapZones, Rect  # noqa: E402
from ..mapmodel import MapGeometry, Viewport, hit_zone, rect_corners, texture_rgba  # noqa: E402

_DARK_BG, _LIGHT_BG = (29, 29, 32), (255, 255, 255)
HEADING_LENGTH = 20  # px from the robot's centre; its dot with ring is 11.5 px
HIT_TOLERANCE = 8  # px around zone handles and walls
HANDLE_SIZE = 8  # px, white squares on the selected zone's corners / ends
DRAG_THRESHOLD = 3  # px before a press becomes a drag
TOOLS = (None, "select", "no_go", "no_mop", "wall", "order", "split", "merge")
_ZONE_FIELD = {"no_go": "no_go", "no_mop": "no_mop", "wall": "walls"}
_ZONE_COLOR = {"no_go": "#e01b24", "no_mop": "#3584e4", "wall": "#e01b24"}
_ZONE_TOOLS = ("select", "no_go", "no_mop", "wall")  # tools that show and edit zones
HATCH_SPACING = 8  # px between a zone's diagonal stripes, whatever the zoom
_DELETE_KEYS = (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace)


def _zone_list(zones: MapZones, kind: str) -> tuple:
    return getattr(zones, _ZONE_FIELD[kind])


def _with_zone(zones: MapZones, kind: str, index: int, shape) -> MapZones:
    """`zones` with item `index` of `kind` replaced by `shape` (removed when None)."""
    items = list(_zone_list(zones, kind))
    if shape is None:
        del items[index]
    else:
        items[index] = shape
    return replace(zones, **{_ZONE_FIELD[kind]: tuple(items)})


def _big_enough(shape) -> bool:
    if isinstance(shape, Line):
        return math.hypot(shape.x2 - shape.x1, shape.y2 - shape.y1) >= MIN_SIZE_MM
    return shape.x2 - shape.x1 >= MIN_SIZE_MM and shape.y2 - shape.y1 >= MIN_SIZE_MM


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
    __gsignals__ = {
        "room-clicked": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "zones-edited": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "zone-selected": (GObject.SignalFlags.RUN_FIRST, None, (str, int)),
        "room-picked": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "split-line": (GObject.SignalFlags.RUN_FIRST, None, (int, GObject.TYPE_PYOBJECT)),
    }

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
        # Map editing. `zones` are in map mm; `geom` converts them to display cells.
        self.zones = MapZones()
        self.geom: MapGeometry | None = None
        self.order: list[int] | None = None
        self.tool: str | None = None
        self.selected_zone: tuple[str, int] | None = None
        self._split_seg = 0  # room picked by the split tool, waiting for its line
        self._drag_mode: str | None = None  # pan | edit | draw | none, per drag
        self._drag_start = (0.0, 0.0)
        self._edit: tuple[str, int, str] | None = None  # zone part grabbed by a select drag
        self._draft: MapZones | None = None  # zones while a move/resize is in progress
        self._preview: tuple[tuple[float, float], tuple[float, float]] | None = None  # cells

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
        drag.connect("drag-end", self._on_drag_end)
        drag.connect("cancel", lambda *_a: self._reset_drag())
        self.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
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

    def set_zones(self, zones: MapZones, geom: MapGeometry | None) -> None:
        """Programmatic: emits nothing. The zone selection survives only an echo of the same
        zones (as after our own `zones-edited`); otherwise it is cleared silently, without
        `zone-selected`, so callers read `selected_zone` afterwards."""
        if zones != self.zones or geom is None:
            self.selected_zone = None
        self.zones, self.geom = zones, geom
        self._reset_drag()
        self.queue_draw()

    def set_order(self, order: list[int] | None) -> None:
        self.order = None if order is None else list(order)
        self.queue_draw()

    def set_tool(self, tool: str | None) -> None:
        """Programmatic: emits nothing. Tools that cannot show or edit a zone (None, order,
        merge, split) drop the zone selection silently."""
        if tool not in TOOLS:
            raise ValueError(f"unknown map tool {tool!r}")
        self.tool = tool
        self._split_seg = 0
        if tool not in _ZONE_TOOLS:
            self.selected_zone = None
        self.set_focusable(tool is not None)
        self._reset_drag()
        self.queue_draw()

    def order_badges(self) -> dict[int, int]:
        """Room id -> its 1-based position in the order, for rooms that show a label."""
        if self.order is None:
            return {}
        shown = {r.id for r in self.rooms if r.anchor and not r.hidden}
        return {seg: n for n, seg in enumerate(self.order, 1) if seg in shown}

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
        if self.tool is not None:
            if n_press == 2:
                self.reset_view()  # the first release already did the click's work
            elif n_press == 1 and not self._dragged:
                self._tool_click(x, y)
            return
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

    def _tool_click(self, x, y) -> None:
        self.grab_focus()  # for Delete / BackSpace
        seg = self.room_at(x, y)
        if self.tool == "select":
            hit = self._hit(x, y)
            if hit:
                self._select_zone((hit[0], hit[1]))
                return
            self._select_zone(None)
            if seg:
                self.emit("room-picked", seg)
        elif self.tool in ("order", "merge") and seg:
            self.emit("room-picked", seg)
        elif self.tool == "split" and seg:
            self._split_seg = seg
            self.emit("room-picked", seg)

    def _hit(self, x, y) -> tuple[str, int, str] | None:
        if not (self.geom and self.viewport):
            return None
        cx, cy = self.viewport.to_cell(x, y)
        return hit_zone(self.zones, self.geom, cx, cy, HIT_TOLERANCE / self.viewport.scale)

    def _select_zone(self, zone: tuple[str, int] | None) -> None:
        if zone != self.selected_zone:
            self.selected_zone = zone
            self.emit("zone-selected", *(zone or ("", -1)))
            self.queue_draw()

    def _on_key(self, _ctl, keyval, _keycode, _state) -> bool:
        if self.tool not in _ZONE_TOOLS or self.selected_zone is None or keyval not in _DELETE_KEYS:
            return False
        kind, index = self.selected_zone
        self.zones = _with_zone(self.zones, kind, index, None)
        self.emit("zones-edited", self.zones)
        self._select_zone(None)
        return True

    def _drag_mode_at(self, x, y) -> str:
        """What a drag starting at (x, y) does with the current tool."""
        editable = self.geom is not None and self.viewport is not None
        if self.tool in ("no_go", "no_mop", "wall"):
            return "draw" if editable else "none"
        if self.tool == "split" and self._split_seg:
            return "draw" if editable else "none"
        if self.tool == "select":
            self._edit = self._hit(x, y)
            if self._edit:
                return "edit"
        return "pan"

    def _reset_drag(self) -> None:
        self._drag_mode, self._edit, self._draft, self._preview = None, None, None, None
        self.queue_draw()

    def _on_drag_begin(self, _gesture, x, y) -> None:
        self._dragged = False
        if self.viewport:
            self._drag_origin = (self.viewport.ox, self.viewport.oy)
        self._reset_drag()
        self._drag_start = (x, y)
        self._drag_mode = self._drag_mode_at(x, y)
        if self.tool is not None:
            self.grab_focus()  # a claimed drag never reaches the click's release

    def _on_drag_update(self, gesture, dx, dy) -> None:
        if self.tool is not None and self._drag_mode != "pan":
            self._tool_drag_update(gesture, dx, dy)
            return
        if self.viewport and (abs(dx) > DRAG_THRESHOLD or abs(dy) > DRAG_THRESHOLD):
            self._dragged = True
            if gesture is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.viewport.ox, self.viewport.oy = self._drag_origin
            self.viewport.pan(dx, dy)
            self.queue_draw()

    def _tool_drag_update(self, gesture, dx, dy) -> None:
        if self._drag_mode not in ("edit", "draw", "none") or not self.viewport:
            return
        if not self._dragged:
            if abs(dx) <= DRAG_THRESHOLD and abs(dy) <= DRAG_THRESHOLD:
                return
            self._dragged = True
            if gesture is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if self._drag_mode == "edit":
                self._select_zone(self._edit[:2])
        if self._drag_mode == "none":
            return
        x0, y0 = self._drag_start
        start, end = self.viewport.to_cell(x0, y0), self.viewport.to_cell(x0 + dx, y0 + dy)
        if self._drag_mode == "draw":
            self._preview = (start, end)
        else:
            self._draft = self._edited(start, end)
        self.queue_draw()

    def _edited(self, start, end) -> MapZones:
        """The zones with the grabbed part dragged from cell `start` to cell `end`."""
        kind, index, part = self._edit
        (sx, sy), (ex, ey) = self.geom.to_mm(*start), self.geom.to_mm(*end)
        dx, dy = ex - sx, ey - sy
        shape = _zone_list(self.zones, kind)[index]
        if isinstance(shape, Line):
            x1, y1, x2, y2 = shape.x1, shape.y1, shape.x2, shape.y2
            if part in ("body", "end0"):
                x1, y1 = x1 + dx, y1 + dy
            if part in ("body", "end1"):
                x2, y2 = x2 + dx, y2 + dy
            moved = Line(x1, y1, x2, y2)
        elif part == "body":
            moved = Rect(shape.x1 + dx, shape.y1 + dy, shape.x2 + dx, shape.y2 + dy)
        else:
            corners = rect_corners(shape)
            n = int(part[1])
            (cx, cy), opposite = corners[n], corners[(n + 2) % 4]
            moved = Rect.of(cx + dx, cy + dy, *opposite)
        return _with_zone(self.zones, kind, index, moved)

    def _on_drag_end(self, _gesture, _dx, _dy) -> None:
        mode, edit, draft, preview = self._drag_mode, self._edit, self._draft, self._preview
        self._reset_drag()
        if not self._dragged:
            return
        if mode == "edit" and draft is not None:
            kind, index, _part = edit
            if draft != self.zones and _big_enough(_zone_list(draft, kind)[index]):
                self.zones = draft
                self.emit("zones-edited", draft)
        elif mode == "draw" and preview is not None:
            self._finish_drawing(*preview)

    def _finish_drawing(self, start, end) -> None:
        a, b = self.geom.to_mm(*start), self.geom.to_mm(*end)
        if self.tool == "split":
            line = Line(*a, *b)
            if _big_enough(line):
                seg, self._split_seg = self._split_seg, 0
                self.emit("split-line", seg, line)
            return
        kind = self.tool
        shape = Line(*a, *b) if kind == "wall" else Rect.of(*a, *b)
        if not _big_enough(shape):
            return
        items = _zone_list(self.zones, kind)
        self.zones = replace(self.zones, **{_ZONE_FIELD[kind]: (*items, shape)})
        self.emit("zones-edited", self.zones)
        self._select_zone((kind, len(items)))

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
        if self.geom is not None:
            self._zones(snapshot)
        if self.grid.charger:
            self._dot(snapshot, *vp.to_widget(*self.grid.charger), 10, "#2ec27e", "#ffffff")
        if self.live is not None:
            self._live_path(snapshot)
        self._labels(snapshot)
        if self.live is not None and self.live.robot is not None:
            self._robot(snapshot)  # on top: a label must never hide the robot

    def _widget_mm(self, x_mm, y_mm) -> tuple[float, float]:
        return self.viewport.to_widget(*self.geom.to_cell(x_mm, y_mm))

    def _widget_rect(self, a, b) -> Graphene.Rect:
        """The axis-aligned widget rectangle spanned by widget points a and b."""
        x, y = min(a[0], b[0]), min(a[1], b[1])
        return Graphene.Rect().init(x, y, abs(a[0] - b[0]), abs(a[1] - b[1]))

    def _stroke(self, snapshot, builder, width, color, dashed=False) -> None:
        path = builder.to_path()  # keep the path and stroke alive while appending
        stroke = Gsk.Stroke.new(width)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        if dashed:
            stroke.set_dash([6, 4])
        snapshot.append_stroke(path, stroke, _rgba(color))

    def _box(self, snapshot, box, kind, dashed=False) -> None:
        """A zone: light tint, diagonal hatching clipped to it, and its border."""
        tint = _rgba(_ZONE_COLOR[kind])
        tint.alpha = 0.12
        snapshot.append_color(tint, box)
        x, y, w, h = box.get_x(), box.get_y(), box.get_width(), box.get_height()
        # Lines x + y = k * spacing in widget px: fixed on screen, so zooming never
        # changes the stripes' spacing or width.
        hatch = Gsk.PathBuilder.new()
        for k in range(
            math.floor((x + y) / HATCH_SPACING), math.ceil((x + w + y + h) / HATCH_SPACING) + 1
        ):
            c = k * HATCH_SPACING
            hatch.move_to(c - y, y)
            hatch.line_to(c - y - h, y + h)
        stripe = _rgba(_ZONE_COLOR[kind])
        stripe.alpha = 0.6
        snapshot.push_clip(box)
        self._stroke(snapshot, hatch, 1.5, stripe.to_string())
        snapshot.pop()
        builder = Gsk.PathBuilder.new()
        builder.add_rect(box)
        self._stroke(snapshot, builder, 2, _ZONE_COLOR[kind], dashed)

    def _wall(self, snapshot, a, b, dashed=False, color=_ZONE_COLOR["wall"]) -> None:
        builder = Gsk.PathBuilder.new()
        builder.move_to(*a)
        builder.line_to(*b)
        self._stroke(snapshot, builder, 3, color, dashed)

    def _zones(self, snapshot) -> None:
        """No-mop, no-go, walls (bottom to top, as `hit_zone` sees them), the selected zone's
        handles and the dashed preview of a shape being drawn."""
        zones = self.zones if self._draft is None else self._draft
        to_widget = self._widget_mm
        for kind in ("no_mop", "no_go"):
            for r in _zone_list(zones, kind):
                self._box(
                    snapshot, self._widget_rect(to_widget(r.x1, r.y1), to_widget(r.x2, r.y2)), kind
                )
        for w in zones.walls:
            self._wall(snapshot, to_widget(w.x1, w.y1), to_widget(w.x2, w.y2))
        if self.selected_zone is not None:
            kind, index = self.selected_zone
            items = _zone_list(zones, kind)
            if index < len(items):
                shape = items[index]
                if isinstance(shape, Line):
                    points = [(shape.x1, shape.y1), (shape.x2, shape.y2)]
                else:
                    points = rect_corners(shape)
                for point in points:
                    self._handle(snapshot, *to_widget(*point))
        if self._preview is not None:
            a, b = (self.viewport.to_widget(*cell) for cell in self._preview)
            if self.tool in ("no_go", "no_mop"):
                self._box(snapshot, self._widget_rect(a, b), self.tool, dashed=True)
            elif self.tool == "split":
                accent = Adw.StyleManager.get_default().get_accent_color_rgba()
                self._wall(snapshot, a, b, dashed=True, color=accent.to_string())
            else:
                self._wall(snapshot, a, b, dashed=True)

    def _handle(self, snapshot, x, y) -> None:
        half = HANDLE_SIZE / 2
        outline = Graphene.Rect().init(x - half - 1, y - half - 1, HANDLE_SIZE + 2, HANDLE_SIZE + 2)
        snapshot.append_color(_rgba("rgba(0,0,0,0.6)"), outline)
        snapshot.append_color(
            _rgba("#ffffff"), Graphene.Rect().init(x - half, y - half, HANDLE_SIZE, HANDLE_SIZE)
        )

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
        badges = self.order_badges()
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
            if seg in badges:
                self._badge(snapshot, box.get_x() + box.get_width(), box.get_y(), badges[seg])

    def _badge(self, snapshot, x, y, number) -> None:
        """The room's cleaning-order position: an accent disc on the label's top-right."""
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        self._dot(snapshot, x, y, 9, accent.to_string(), "#ffffff")
        layout = self.create_pango_layout(None)
        layout.set_markup(f"<small><b>{number}</b></small>", -1)
        w, h = layout.get_pixel_size()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x - w / 2, y - h / 2))
        snapshot.append_layout(layout, _rgba("#ffffff"))
        snapshot.restore()
