"""All engines read the third physical HUD row regardless of its zone name."""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ocr import OCRProcessor


def hud_images():
    binary = np.zeros((180, 600), dtype=np.uint8)
    enhanced = np.zeros_like(binary)
    raw = np.zeros((60, 200, 3), dtype=np.uint8)
    for number, (start, end) in enumerate(((21, 45), (60, 84), (99, 123), (138, 162)), 1):
        binary[start:end, 100:100 + number * 50] = 255
        enhanced[start:end, :] = number
        raw[start // 3:end // 3, :, :] = number
    return {'otsu': binary, 'adaptive': binary.copy(), 'enhanced': enhanced, 'raw': raw}


@pytest.mark.parametrize('capture_height', [60, 80, 88, 120])
def test_selection_maps_one_row_to_binary_grayscale_and_native_bgr_without_mutation(capture_height):
    images = hud_images()
    for name, source in images.items():
        scale = 1 if name == 'raw' else 3
        padding = [(0, (capture_height - 60) * scale)] + [(0, 0)] * (source.ndim - 1)
        images[name] = np.pad(source, padding)
    selected = OCRProcessor._navigation_row_images(images)
    assert selected['otsu'].shape == (32, 600)
    assert selected['raw'].shape == (12, 200, 3)
    assert set(np.unique(selected['enhanced'])) == {0, 3}
    assert set(np.unique(selected['raw'])) == {0, 3}
    assert set(np.unique(images['enhanced'])) == {0, 1, 2, 3, 4}


@pytest.mark.parametrize('engine, mode', [
    ('tesseract', 'hybrid'), ('tesseract', 'full_text'),
    ('paddle', 'hybrid'), ('paddle', 'full_text'),
])
def test_every_pipeline_receives_only_navigation_row(engine, mode):
    processor = OCRProcessor.__new__(OCRProcessor)
    processor.engine = engine
    processor.pipeline_mode = mode
    processor.reset_capture_state()
    calls = []

    def glyphs(binary, enhanced):
        assert set(np.unique(enhanced)) == {0, 3}
        assert binary.shape[0] == 32
        calls.append('glyphs')
        return None

    def text(images):
        assert images['otsu'].shape[0] == 32
        calls.append('text')
        return processor._empty_data()

    def paddle(raw, enhanced):
        assert set(np.unique(raw)) == {0, 3}
        assert set(np.unique(enhanced)) == {0, 3}
        calls.append('text')
        return processor._empty_data()

    processor._try_ncc_full_extraction = glyphs
    processor._parse_images_parallel = text
    processor._extract_full_text_paddle = paddle
    processor.extract_data(hud_images())
    assert calls == (['glyphs', 'text'] if mode == 'hybrid' else ['text'])


def test_unavailable_third_row_clears_cached_coordinates_without_reading_another_row():
    processor = OCRProcessor.__new__(OCRProcessor)
    processor.engine = 'tesseract'
    processor.pipeline_mode = 'hybrid'
    processor._last_result = {'x': 4.09056}
    processor._frame_ncc_coords = (4.09056, 12.7720, -36.9037)
    images = hud_images()
    images['otsu'][90:, :] = 0
    assert processor.extract_data(images)['x'] is None
    assert processor._last_result is None
    assert processor._frame_ncc_coords is None


@pytest.mark.parametrize('zone', ['jumppoint_nyx_castra', 'Stanton_1_Hurston', 'Any_New_Zone_42'])
@pytest.mark.parametrize('engine', ['tesseract', 'paddle'])
@pytest.mark.parametrize('values, expected', [
    ('4090.56m 12.7720km -36.9037km', (4.09056, 12.7720, -36.9037)),
    ('3.52m -4.71m 25.97m', (0.00352, -0.00471, 0.02597)),
])
def test_selected_line_accepts_variable_zone_names_and_converts_each_axis(engine, zone, values, expected):
    processor = OCRProcessor.__new__(OCRProcessor)
    processor._frame_ncc_coords = None
    line = f'Zone: {zone} Pos: {values}'
    processor._ocr_image_to_text = lambda _: line
    processor._paddle_adapter = SimpleNamespace(
        recognize_detailed=lambda _: {'texts': [line], 'scores': [1.0]},
    )
    processor.paddle_min_confidence = 0.3
    processor._paddle_stats = {'no_text': 0, 'below_conf': 0, 'regex_fail': 0, 'out_of_range': 0}
    if engine == 'tesseract':
        _, data = processor._ocr_single_pass('test', None)
    else:
        _, data = processor._paddle_single_pass(None, 'test')
    assert (data['x'], data['y'], data['z']) == pytest.approx(expected)
    assert data['ooc'] == f'Zone: {zone}'


@pytest.mark.parametrize('zone', ['Root', 'SolarSystem_802879462060'])
def test_absolute_frame_is_rejected_even_when_its_coordinates_are_small(zone):
    processor = OCRProcessor.__new__(OCRProcessor)
    processor._frame_ncc_coords = None
    processor._ocr_image_to_text = lambda _: f'Zone: {zone} Pos: 1.2345km 2.3456km 3.4567km'
    _, data = processor._ocr_single_pass('test', None)
    assert data['x'] is None
