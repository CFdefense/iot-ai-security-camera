"""Picamera2/IMX500 imaging tests that do not require Pi hardware."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.camera.picam import imaging
from src.core import config


class FakeBoxTensor:
    """Tiny tensor stand-in for the bbox operations used by the IMX500 parser."""

    def __init__(self, boxes):
        self.boxes = [tuple(float(value) for value in box) for box in boxes]

    def __truediv__(self, divisor):
        """Return normalized boxes."""
        return FakeBoxTensor(
            tuple(value / float(divisor) for value in box)
            for box in self.boxes
        )

    def __getitem__(self, key):
        """Support ``boxes[:, [1, 0, 3, 2]]`` without importing numpy."""
        rows, cols = key
        if not isinstance(rows, slice) or rows.start is not None or rows.stop is not None or rows.step is not None:
            raise AssertionError(f"unexpected row selector {rows!r}")
        return FakeBoxTensor(tuple(box[index] for index in cols) for box in self.boxes)

    def __iter__(self):
        """Iterate over boxes like a numpy array."""
        return iter(self.boxes)


class FakeCamera:
    """Camera fake that writes scripted JPEG bytes and returns metadata."""

    def __init__(self, raw: bytes = b"\xff\xd8picam\xff\xd9"):
        self.raw = raw
        self.capture_formats: list[str] = []

    def capture_file(self, buf, format: str):
        """Mirror Picamera2.capture_file enough for imaging tests."""
        self.capture_formats.append(format)
        buf.write(self.raw)
        return {"frame_id": 123}


class FakeImx500:
    """IMX500 fake that returns scripted detector outputs and scales coords."""

    def __init__(self, outputs=None):
        self.outputs = outputs
        self.output_calls: list[tuple[dict, bool]] = []
        self.converted_boxes: list[tuple[float, float, float, float]] = []

    def get_outputs(self, metadata, add_batch: bool = False):
        """Return the detector tensors provided at construction time."""
        self.output_calls.append((metadata, add_batch))
        return self.outputs

    def get_input_size(self):
        """Return the configured model input size."""
        return (320, 320)

    def convert_inference_coords(self, box, metadata, camera):
        """Scale normalized coordinates back to test-friendly pixel values."""
        self.converted_boxes.append(tuple(box))
        return tuple(value * 320 for value in box)


class FakeImage:
    """Array-like image object that records crop slices."""

    def __init__(self, *, width: int = 200, height: int = 100, crop_key=None):
        self.shape = (height, width, 3)
        self.crop_key = crop_key

    def __getitem__(self, key):
        """Return a cropped image and retain the requested y/x slices."""
        y_slice, x_slice = key
        return FakeImage(
            width=x_slice.stop - x_slice.start,
            height=y_slice.stop - y_slice.start,
            crop_key=key,
        )


def _install_fake_picam(monkeypatch, *, camera, imx500, intrinsics) -> None:
    """Route public capture calls through fake Picamera2/IMX500 objects."""
    monkeypatch.setattr(config, "CAMERA_BACKEND", "picam")
    monkeypatch.setattr(imaging, "_picam_imx500", lambda: (camera, imx500, intrinsics))


def test_picam_capture_parses_outputs_with_normalization_and_xy_order(monkeypatch):
    """Pi capture parses tensors, labels, confidences, category ids, and bboxes."""
    boxes = FakeBoxTensor(
        [
            (10, 20, 30, 40),
            (50, 60, 70, 80),
        ]
    )
    outputs = [[boxes], [[0.91234, 0.42]], [[0, 99]]]
    camera = FakeCamera()
    imx500 = FakeImx500(outputs=outputs)
    intrinsics = SimpleNamespace(labels=["person"], bbox_normalization=True, bbox_order="xy")
    _install_fake_picam(monkeypatch, camera=camera, imx500=imx500, intrinsics=intrinsics)

    frame = imaging.capture_frame_with_detections()

    assert frame.jpeg == b"\xff\xd8picam\xff\xd9"
    assert camera.capture_formats == ["jpeg"]
    assert imx500.output_calls == [({"frame_id": 123}, True)]
    assert imx500.converted_boxes == [
        (0.0625, 0.03125, 0.125, 0.09375),
        (0.1875, 0.15625, 0.25, 0.21875),
    ]
    assert [detection.to_dict() for detection in frame.detections] == [
        {
            "label": "person",
            "confidence": 0.9123,
            "bbox": {"x": 20, "y": 10, "width": 40, "height": 30},
            "category": 0,
        },
        {
            "label": "99",
            "confidence": 0.42,
            "bbox": {"x": 60, "y": 50, "width": 80, "height": 70},
            "category": 99,
        },
    ]


def test_picam_capture_without_outputs_returns_empty_detection_list(monkeypatch):
    """A valid JPEG with no IMX500 output tensors is still a usable capture."""
    camera = FakeCamera()
    imx500 = FakeImx500(outputs=None)
    intrinsics = SimpleNamespace(labels=["person"], bbox_normalization=False, bbox_order="yx")
    _install_fake_picam(monkeypatch, camera=camera, imx500=imx500, intrinsics=intrinsics)

    frame = imaging.capture_frame_with_detections()

    assert frame.jpeg == b"\xff\xd8picam\xff\xd9"
    assert frame.detections == []
    assert imx500.converted_boxes == []


def test_picam_capture_rejects_empty_camera_frame(monkeypatch):
    """An empty Picamera2 capture fails fast before later embedding code runs."""
    camera = FakeCamera(raw=b"")
    imx500 = FakeImx500(outputs=None)
    intrinsics = SimpleNamespace(labels=[], bbox_normalization=False, bbox_order="yx")
    _install_fake_picam(monkeypatch, camera=camera, imx500=imx500, intrinsics=intrinsics)

    with pytest.raises(RuntimeError, match="empty capture"):
        imaging.capture_frame_with_detections()


def test_picam_embedding_crops_to_detection_bbox_and_returns_128_floats(monkeypatch):
    """Face embedding loads JPEG bytes, crops to the object bbox, and encodes one face."""
    calls = {}

    def load_image_file(file_obj):
        calls["raw"] = file_obj.read()
        return FakeImage(width=200, height=100)

    def face_locations(image, model: str):
        calls["location_image"] = image
        calls["model"] = model
        return [(1, 2, 3, 4)]

    def face_encodings(image, known_face_locations):
        calls["encoding_image"] = image
        calls["known_face_locations"] = known_face_locations
        return [[index / 127 for index in range(128)]]

    fake_face_recognition = SimpleNamespace(
        load_image_file=load_image_file,
        face_locations=face_locations,
        face_encodings=face_encodings,
    )
    monkeypatch.setitem(sys.modules, "face_recognition", fake_face_recognition)

    embedding = imaging._embed_picam_face_bytes(b"jpeg bytes", bbox=(10, 20, 40, 50))

    crop_y, crop_x = calls["location_image"].crop_key
    assert calls["raw"] == b"jpeg bytes"
    assert calls["model"] == "hog"
    assert (crop_y.start, crop_y.stop) == (13, 77)
    assert (crop_x.start, crop_x.stop) == (4, 56)
    assert calls["encoding_image"] is calls["location_image"]
    assert calls["known_face_locations"] == [(1, 2, 3, 4)]
    assert len(embedding) == 128
    assert all(isinstance(value, float) for value in embedding)


@pytest.mark.parametrize(
    ("locations", "encodings", "message"),
    [
        ([], [], "no face detected"),
        ([(1, 2, 3, 4), (5, 6, 7, 8)], [], "multiple faces detected"),
        ([(1, 2, 3, 4)], [], "face encoding failed"),
    ],
)
def test_picam_embedding_rejects_bad_face_recognition_results(monkeypatch, locations, encodings, message):
    """Face-recognition edge cases become explicit ValueErrors for callers."""
    fake_face_recognition = SimpleNamespace(
        load_image_file=lambda _file_obj: FakeImage(),
        face_locations=lambda _image, model: locations,
        face_encodings=lambda _image, known_face_locations: encodings,
    )
    monkeypatch.setitem(sys.modules, "face_recognition", fake_face_recognition)

    with pytest.raises(ValueError, match=message):
        imaging._embed_picam_face_bytes(b"jpeg bytes")


def test_picam_embedding_wraps_invalid_image_data(monkeypatch):
    """Bad JPEG bytes are reported as invalid image data."""

    def load_image_file(_file_obj):
        raise OSError("decode failed")

    fake_face_recognition = SimpleNamespace(load_image_file=load_image_file)
    monkeypatch.setitem(sys.modules, "face_recognition", fake_face_recognition)

    with pytest.raises(ValueError, match="invalid image data: decode failed"):
        imaging._embed_picam_face_bytes(b"not a jpeg")


def test_picam_embedding_requires_face_recognition_dependency(monkeypatch):
    """The real Pi backend tells the operator when face_recognition is missing."""
    monkeypatch.setitem(sys.modules, "face_recognition", None)

    with pytest.raises(RuntimeError, match="requires face_recognition"):
        imaging._embed_picam_face_bytes(b"jpeg bytes")


def test_crop_to_bbox_rejects_empty_detection_boxes():
    """Invalid detector boxes do not silently create empty face crops."""
    with pytest.raises(ValueError, match="invalid detection bbox"):
        imaging._crop_to_bbox(FakeImage(), (10, 10, 0, 20))
