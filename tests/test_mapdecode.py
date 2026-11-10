import base64
import json
import struct
import zlib

import pytest

from chippy.core.mapdecode import MapDecodeError, MapFrame, decode_map, parse_path


def _payload(seg_inf, width=4, height=3, as_text=True):
    header = struct.pack("<hhBhhhhhhhhhhh", 1, 1, 73, 0, 0, 0, 0, 0, 0, 50, width, height, 0, 0)
    raw = header + bytes(width * height) + json.dumps({"seg_inf": seg_inf}).encode()
    packed = zlib.compress(raw)
    if not as_text:
        return packed
    return base64.b64encode(packed).decode().replace("/", "_").replace("+", "-")


def room_names(payload):
    return decode_map(payload).names


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def test_saved_maps_document_uses_the_current_map():
    # Shape of the r2209's cloud object (captured 2026-11-06), with synthetic map frames.
    doc = {
        "mapstr": [
            {"id": 0, "name": "", "angle": "0", "map": _payload({"7": {"name": _b64("Old")}})},
            {"id": 3, "name": "", "angle": "0", "map": _payload({"7": {"name": _b64("Studio")}})},
        ],
        "curr_id": 3,
    }
    assert room_names(json.dumps(doc).encode()) == {7: "Studio"}


def test_saved_maps_document_falls_back_to_the_first_map():
    doc = {"mapstr": [{"id": 0, "map": _payload({"1": {"type": 4}})}], "curr_id": 3}
    assert room_names(json.dumps(doc)) == {1: "Kitchen"}


@pytest.mark.parametrize(
    "doc", ['{"mapstr": []}', '{"mapstr": [{"id": 0}]}', '{"other": 1}', "{bad"]
)
def test_bad_saved_maps_documents(doc):
    with pytest.raises(MapDecodeError):
        room_names(doc)


def test_names_and_types():
    rooms = room_names(
        _payload({"16": {"name": _b64("Kuhinja"), "type": 4}, "17": {"type": 1}, "18": {"type": 0}})
    )
    assert rooms == {16: "Kuhinja", 17: "Living room", 18: "Room 18"}


def test_segment_ids_outside_dbus_uint32_are_dropped():
    rooms = room_names(
        _payload(
            {
                "0": {"type": 1},
                "-3": {"type": 2},
                "4294967296": {"type": 3},
                "4294967295": {"type": 4},
                "16": {"type": 6},
            }
        )
    )
    assert rooms == {4294967295: "Kitchen", 16: "Bathroom"}


def test_binary_zlib_payload():
    assert room_names(_payload({"3": {"type": 6}}, as_text=False)) == {3: "Bathroom"}


def test_bytes_text_payload():
    assert room_names(_payload({"5": {"type": 7}}).encode()) == {5: "Balcony"}


def test_garbage_raises():
    with pytest.raises(MapDecodeError):
        room_names("not a map")


def test_no_trailer_raises():
    header = struct.pack("<hhBhhhhhhhhhhh", 1, 1, 73, 0, 0, 0, 0, 0, 0, 50, 2, 2, 0, 0)
    with pytest.raises(MapDecodeError, match="no room data"):
        room_names(zlib.compress(header + bytes(4)))


def _raw(trailer: bytes, width=2, height=2) -> bytes:
    header = struct.pack("<hhBhhhhhhhhhhh", 1, 1, 73, 0, 0, 0, 0, 0, 0, 50, width, height, 0, 0)
    return header + bytes(max(width, 0) * max(height, 0)) + trailer


def test_encrypted_like_base64_raises():
    # Valid URL-safe base64, but the decoded bytes are not zlib data.
    encrypted_like = base64.urlsafe_b64encode(b"\x01\x02 not zlib at all \xff\xfe").decode()
    with pytest.raises(MapDecodeError):
        room_names(encrypted_like)


def test_list_trailer_raises():
    with pytest.raises(MapDecodeError):
        room_names(zlib.compress(_raw(b"[1, 2]")))


@pytest.mark.parametrize(
    "trailer",
    [
        b'{"seg_inf": {"abc": {"type": 1}}}',  # non-numeric segment key
        b'{"seg_inf": {"5": "Kitchen"}}',  # truthy non-dict item
        b'{"seg_inf": {"5": {"type": "abc"}}}',  # non-integer type
        b'{"seg_inf": {"5": {"type": [1]}}}',  # type of the wrong JSON kind
        b'{"seg_inf": {"5": {"type": 1e400}}}',  # overflows to float inf
        b'{"seg_inf": {"5": {"type": Infinity}}}',  # non-finite float literal
        b'{"seg_inf": ["x"]}',  # seg_inf not an object
        b'"just a string"',  # trailer not an object
    ],
)
def test_malformed_trailer_raises_map_decode_error(trailer):
    with pytest.raises(MapDecodeError):
        room_names(zlib.compress(_raw(trailer)))


def test_deeply_nested_trailer_raises():
    trailer = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(MapDecodeError):
        room_names(zlib.compress(_raw(trailer)))


def test_negative_width_raises():
    raw = _raw(b'{"seg_inf": {}}', width=-1, height=3)
    with pytest.raises(MapDecodeError, match="negative size"):
        room_names(zlib.compress(raw))


def _frame_payload(pixels: bytes, width, height, charger=(0, 0), grid=50, trailer=None):
    header = struct.pack(
        "<hhBhhhhhhhhhhh",
        1,
        1,
        73,
        0,
        0,
        0,
        charger[0],
        charger[1],
        0,
        grid,
        width,
        height,
        -100,
        -200,
    )
    body = header + pixels + json.dumps(trailer or {"seg_inf": {}}).encode()
    return base64.b64encode(zlib.compress(body)).decode()


def test_decode_map_returns_geometry_pixels_and_names():
    pixels = bytes([0, 1, 0x81, 2, 2, 0])
    frame = decode_map(
        _frame_payload(pixels, 3, 2, charger=(500, 35), trailer={"seg_inf": {"1": {"type": 4}}})
    )
    assert frame == MapFrame(
        width=3,
        height=2,
        grid_mm=50,
        left=-100,
        top=-200,
        charger=(500, 35),
        pixels=pixels,
        names={1: "Kitchen"},
        map_id=1,
        zones_known=False,  # no `vw` in this trailer
    )


def test_decode_map_no_charger():
    assert decode_map(_frame_payload(bytes(4), 2, 2)).charger is None


def test_decode_map_zero_grid_raises():
    with pytest.raises(MapDecodeError, match="grid"):
        decode_map(_frame_payload(bytes(4), 2, 2, grid=0))


def test_decode_map_truncated_raises():
    header = struct.pack("<hhBhhhhhhhhhhh", 1, 1, 73, 0, 0, 0, 0, 0, 0, 50, 10, 10, 0, 0)
    with pytest.raises(MapDecodeError, match="truncated"):
        decode_map(zlib.compress(header + bytes(20)))


def test_robot_and_path_fields():
    header = struct.pack("<hhBhhhhhhhhhhh", 1, 1, 73, 1200, -340, 90, 0, 0, 0, 50, 2, 2, -100, -200)
    trailer = json.dumps({"seg_inf": {}, "tr": "S1200,-340L50,0L0,50l1300,-200"}).encode()
    frame = decode_map(zlib.compress(header + bytes(4) + trailer))
    assert frame.robot == (1200, -340, 90)
    assert frame.path == [(1200, -340), (1250, -340), (1250, -290), (1300, -200)]


def test_live_frame_from_the_r2209():
    """A P frame as the r2209 uploads it (property 6.1): url-safe base64, no seg_inf, the path
    starting with an absolute `l` then relative `L` steps (shape of a real frame)."""
    import base64

    header = struct.pack("<hhBhhhhhhhhhhh", 2, 18, 80, -3536, 516, 233, 0, 0, 0, 50, 2, 1, 0, 0)
    trailer = json.dumps({"tr": "l-3487,627L-15,-49L-22,-45", "fsm": 1}).encode()
    value = base64.urlsafe_b64encode(zlib.compress(header + bytes(2) + trailer)).decode()
    frame = decode_map(value.rstrip("="))
    assert frame.robot == (-3536, 516, 233)
    assert frame.path == [(-3487, 627), (-3502, 578), (-3524, 533)]
    assert frame.names == {}


def test_no_robot_no_path():
    frame = decode_map(_frame_payload(bytes(4), 2, 2))
    assert frame.robot is None and frame.path == []


def test_non_string_path_is_ignored():
    header = struct.pack("<hhBhhhhhhhhhhh", 1, 1, 73, 0, 0, 0, 0, 0, 0, 50, 2, 2, 0, 0)
    trailer = json.dumps({"seg_inf": {}, "tr": [1, 2]}).encode()
    assert decode_map(zlib.compress(header + bytes(4) + trailer)).path == []


def test_parse_path_tolerates_junk():
    assert parse_path("") == []
    assert parse_path("M1,2 L3,4 X9,9 l1,1 Lfoo") == [(1, 2), (4, 6), (1, 1)]
    assert parse_path("L5,5S1,1") == [(1, 1)]  # a relative step needs a previous point


def test_frame_carries_map_id_and_zones():
    header = struct.pack("<hhBhhhhhhhhhhh", 4, 1, 73, 0, 0, 0, 0, 0, 0, 50, 2, 2, 0, 0)
    vw = {"line": [], "rect": [[0, 0, 500, 500]], "mop": []}
    trailer = json.dumps({"seg_inf": {}, "vw": vw}).encode()
    frame = decode_map(zlib.compress(header + bytes(4) + trailer))
    assert frame.map_id == 4
    assert len(frame.zones.no_go) == 1
    assert frame.zones_known is True


def _trailer_frame(trailer: dict):
    header = struct.pack("<hhBhhhhhhhhhhh", 3, 1, 73, 0, 0, 0, 0, 0, 0, 50, 2, 2, 0, 0)
    return decode_map(zlib.compress(header + bytes(4) + json.dumps(trailer).encode()))


def test_zones_unknown_without_vw_or_with_unreadable_items():
    assert _trailer_frame({"seg_inf": {}}).zones_known is False
    frame = _trailer_frame({"seg_inf": {}, "vw": {"line": [[1, 2]], "rect": [], "mop": []}})
    assert frame.zones_known is False and frame.zones == frame.zones.__class__()
    # the real r2209's trailer
    frame = _trailer_frame({"seg_inf": {}, "vw": {"line": [], "rect": [], "mop": []}})
    assert frame.zones_known is True


def test_neighbours_from_nei_id():
    seg_inf = {
        "1": {"nei_id": [2, 4]},
        "2": {"nei_id": [1, "x", 0, True, 2**32]},
        "4": {"nei_id": "junk"},
        "5": {},
    }
    frame = _trailer_frame({"seg_inf": seg_inf})
    assert frame.neighbours == {1: frozenset({2, 4}), 2: frozenset({1})}
    assert _trailer_frame({"seg_inf": {"1": {}}}).neighbours == {}


def test_saved_map_id_reads_curr_id():
    from chippy.core.fake import build_fake_payload
    from chippy.core.mapdecode import saved_map_id

    assert saved_map_id(build_fake_payload()) == 0
