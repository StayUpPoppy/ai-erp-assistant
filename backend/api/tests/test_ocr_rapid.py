from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_rapidocr_detailed_result_keeps_boxes_and_confidence(monkeypatch) -> None:
    import app.ocr_engine as engine

    created = {}

    class FakeRapidOCR:
        def __init__(self, params=None):
            created["params"] = params

        def __call__(self, _raw):
            return SimpleNamespace(
                txts=("PO-100", "M001 2 10.00"),
                scores=(0.90, 0.80),
                boxes=(
                    ((10, 10), (80, 10), (80, 30), (10, 30)),
                    ((10, 40), (150, 40), (150, 60), (10, 60)),
                ),
            )

    fake_module = ModuleType("rapidocr")
    fake_module.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr", fake_module)
    monkeypatch.setattr(engine, "_rapid_ocr_instance", None)
    monkeypatch.setattr(engine, "_rapid_ocr_threads", None)
    monkeypatch.setenv("RAPIDOCR_INTRA_OP_NUM_THREADS", "2")

    result = engine.ocr_image_bytes_detailed(b"image", "scan.png", engine_override="rapid")

    expected = (0.90 * len("PO-100") + 0.80 * len("M001 2 10.00")) / (len("PO-100") + len("M001 2 10.00"))
    assert result.text == "PO-100\nM001 2 10.00"
    assert result.confidence == expected
    assert result.blocks[0].box[0] == (10.0, 10.0)
    assert created["params"]["EngineConfig.onnxruntime.intra_op_num_threads"] == 2
    assert created["params"]["EngineConfig.onnxruntime.inter_op_num_threads"] == 1


def test_rapidocr_legacy_tuple_api_is_preserved(monkeypatch) -> None:
    import app.ocr_engine as engine

    monkeypatch.setattr(
        engine,
        "_ocr_rapid",
        lambda *_args: engine.OCRResult(text="PO-2", format_label="ocr_rapid(ch_en)", confidence=0.99),
    )
    text, fmt = engine.ocr_image_bytes(b"image", "scan.png", engine_override="rapid")

    assert text == "PO-2"
    assert fmt == "ocr_rapid(ch_en)"
