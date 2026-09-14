"""OCR 解析参数（optionalPayload）链路合同。

覆盖：OcrOptionalPayload 的 camelCase 转换与 None 剔除、配置往返（GUI 表单
三态映射 + config_service 更新）、submit_job 的 optionalPayload JSON 字符串透传。
"""

import json

import pytest

from indexing.settings import OcrOptionalPayload, OcrSettings
from indexing.services import config_service
from indexing.services.ocr_client import OcrClient


def test_payload_camel_case_and_none_excluded():
    """三态开关 None 不发送；显式值转 camelCase；extra_payload 原样并入。"""
    payload = OcrOptionalPayload().to_payload()
    # 全默认时只剩 visualize=False（模型默认即不返回可视化结果）
    assert payload == {"visualize": False}

    payload = OcrOptionalPayload(
        use_doc_unwarping=True,
        use_chart_recognition=False,
        markdown_ignore_labels=["header", "footer"],
        extra_payload={"temperature": 0.1, "maxPixels": 2000000},
    ).to_payload()
    assert payload == {
        "useDocUnwarping": True,
        "useChartRecognition": False,
        "visualize": False,
        "markdownIgnoreLabels": ["header", "footer"],
        "temperature": 0.1,
        "maxPixels": 2000000,
    }


def test_settings_round_trip_through_config_service(knowledge_base):
    """CLI/config.json 路径：嵌套 ocr.payload 字段可更新且往返一致。

    patch 键名与 model_dump 的 snake_case 对齐（_merge 以已存配置为基准，
    未知字段拒绝）；camelCase 由 to_payload 在提交任务时转换。
    """
    patch = {
        "payload": {
            "use_doc_unwarping": True,
            "markdown_ignore_labels": ["header", "footer"],
            "extra_payload": {"repetitionPenalty": 1.2},
        }
    }
    result = config_service.update_config({"ocr": patch})
    assert "ocr" in result["requires_reindex"]

    saved = config_service.show_config(offline=True)["config"]["ocr"]["payload"]
    assert saved["use_doc_unwarping"] is True
    assert saved["markdown_ignore_labels"] == ["header", "footer"]
    assert saved["extra_payload"] == {"repetitionPenalty": 1.2}
    # 再次校验默认值兼容：新配置无 payload 字段时也能正常构造
    assert OcrSettings().payload.to_payload() == {"visualize": False}


def test_gui_form_three_state_mapping():
    """GUI 表单字符串与三态布尔的互转。"""
    from app.ui.handlers.settings_handlers import (
        _payload_form_value, _form_payload_bool, _build_ocr_payload,
    )

    assert _payload_form_value(None) == "default"
    assert _payload_form_value(True) == "on"
    assert _payload_form_value(False) == "off"
    assert _form_payload_bool({}, "missing") is None
    assert _form_payload_bool({"k": "on"}, "k") is True
    assert _form_payload_bool({"k": "off"}, "k") is False
    assert _form_payload_bool({"k": "default"}, "k") is None

    form = {
        "ocr_use_doc_unwarping": "on",
        "ocr_use_doc_orientation_classify": "off",
        "ocr_use_chart_recognition": "default",
        "ocr_use_seal_recognition": "default",
        "ocr_use_ocr_for_image_block": "default",
        "ocr_markdown_ignore_labels": "header, footer ,",
        "ocr_extra_payload": {},
    }
    payload = _build_ocr_payload(form).to_payload()
    assert payload == {
        "useDocUnwarping": True,
        "useDocOrientationClassify": False,
        "visualize": False,
        "markdownIgnoreLabels": ["header", "footer"],
    }


def test_extra_payload_rejects_pipeline_breaking_keys():
    """与解析管线不兼容的透传键在配置校验时即被拒绝。"""
    from pydantic import ValidationError

    for key in ("restructurePages", "useLayoutDetection", "returnMarkdownImages"):
        with pytest.raises(ValidationError, match="不兼容"):
            OcrOptionalPayload(extra_payload={key: True})
    # 兼容的采样参数不受影响
    payload = OcrOptionalPayload(
        extra_payload={"temperature": 0.1, "maxPixels": 2000000}
    ).to_payload()
    assert payload["temperature"] == 0.1


def test_submit_job_sends_optional_payload_json_string(tmp_path):
    """multipart 提交时 optionalPayload 必须是 JSON 字符串字段（与官方 SDK 一致）。"""
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"code": 0, "data": {"jobId": "job-1"}}

    class _Client:
        def post(self, url, data=None, files=None, **_k):
            sent["url"] = url
            sent["data"] = data
            return _Resp()

    client = OcrClient.__new__(OcrClient)
    object.__setattr__(client, "_jobs_url", "http://ocr.local/api/v2/ocr/jobs")
    object.__setattr__(client, "_model", "PaddleOCR-VL")
    object.__setattr__(client, "_client", _Client())

    payload = OcrOptionalPayload(use_doc_unwarping=True).to_payload()
    probe = tmp_path / "probe.pdf"
    probe.write_bytes(b"%PDF-1.4")
    client.submit_job(probe, optional_payload=payload)

    data = sent["data"]
    assert data["model"] == "PaddleOCR-VL"
    assert json.loads(data["optionalPayload"]) == {
        "useDocUnwarping": True,
        "visualize": False,
    }
    # 不传 payload 时保持旧行为：字段完全不出现
    client.submit_job(probe)
    assert "optionalPayload" not in sent["data"]
