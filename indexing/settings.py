"""
用户设置管理模块

职责:
- 配置文件读写（config.json）
- 提供统一的配置访问接口
- 支持云同步（仅同步 data_path 目录下的 files/ 子目录）

配置文件位置: 平台默认数据目录下的 config.json（不随 data_path 变化）
数据存储位置: 由 data_path 配置指定；PIECE_DATA_DIR 可覆盖默认目录
"""

import json
import os
import tempfile
import logging
import secrets
from pathlib import Path
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

from app.platform import get_default_data_dir

logger = logging.getLogger(__name__)


def generate_api_key(length: int = 32) -> str:
    """
    生成随机 API 密钥

    Args:
        length: 密钥长度（默认 32 字符）

    Returns:
        str: 随机生成的 API 密钥（十六进制字符串）
    """
    return secrets.token_hex(length // 2)


# 配置与默认数据目录不依赖当前工作目录，也不向 site-packages / .app 内写入。
DEFAULT_DATA_PATH = get_default_data_dir()

# MinerU 的单文件上限（服务端硬限制，超出返回 -60005）。
# 定义在这里而不是 mineru_client：get_parser_max_size() 要用它，
# 而 mineru_client 反过来要读 get_ocr_config()，放那边会形成循环导入。
MINERU_MAX_FILE_SIZE = 200 * 1024 * 1024

# MinerU 文档语言的合法取值（官方「language 取值参考」全表）。
# 官方注明该参数仅影响 OCR 阶段，中日英混排文档保持默认 ch 即可；
# 与语言族包（latin/arabic/…）对应的是整族共用一套 OCR 模型。
MINERU_LANGUAGES = (
    ("ch", "ch"), ("ch_server", "ch_server"), ("en", "en"),
    ("japan", "japan"), ("korean", "korean"), ("chinese_cht", "chinese_cht"),
    ("latin", "latin"), ("arabic", "arabic"), ("cyrillic", "cyrillic"),
    ("east_slavic", "east_slavic"), ("devanagari", "devanagari"),
    ("ta", "ta"), ("te", "te"), ("ka", "ka"), ("th", "th"), ("el", "el"),
)


class EmbeddingSettings(BaseModel):
    """嵌入模型配置"""
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    model: str = "BAAI/bge-m3"
    vector_dim: int = Field(default=1024, ge=1, le=65536)
    max_tokens: int = Field(default=8192, ge=1)  # 模型最大上下文长度


McpService = Literal["retrieval", "index"]


class McpSettings(BaseModel):
    """MCP 服务配置"""
    port: int = Field(default=8686, ge=1, le=65535)
    api_key: str = ""  # 检索密钥；同时兼容旧配置的共享密钥
    index_api_key: Optional[str] = None  # 未配置时沿用旧密钥；显式空字符串表示不认证
    auth_enabled: bool = True  # 是否启用密钥验证
    # get-docs 的 include_images 默认值：开启后 AI 不传参也会附带插图，
    # AI 仍可显式传 false 跳过
    include_images_default: bool = False
    max_images_per_call: int = 6  # 单次 get-docs 返回的插图数量上限

    def get_api_key(self, service: McpService = "retrieval") -> str:
        """读取指定服务的有效密钥，旧配置无需迁移即可继续使用。"""
        if service == "retrieval":
            return self.api_key
        if service == "index":
            return self.api_key if self.index_api_key is None else self.index_api_key
        raise ValueError(f"未知 MCP 服务: {service}")


class OcrOptionalPayload(BaseModel):
    """PaddleOCR 兼容服务的解析参数（job 接口的 optionalPayload 字段）。

    None 表示不发送该字段，由服务端应用默认值；显式 True/False 才覆盖。
    字段表与官方云服务（PaddleX serving schema paddleocr_vl.InferRequest）对齐，
    仅暴露对知识库解析有意义的开关，采样类进阶参数经 extra_payload 透传。
    """

    # 三态开关：None=服务端默认；True/False=显式覆盖
    use_doc_orientation_classify: Optional[bool] = None
    use_doc_unwarping: Optional[bool] = None
    use_chart_recognition: Optional[bool] = None
    use_seal_recognition: Optional[bool] = None
    use_ocr_for_image_block: Optional[bool] = None
    visualize: Optional[bool] = False
    # 版面标签列表（如 header,footer），命中区域不写入 Markdown
    markdown_ignore_labels: Optional[List[str]] = None
    # 额外参数原样透传（采样、阈值等进阶项），字段名需为官方 camelCase
    extra_payload: Dict[str, object] = Field(default_factory=dict)

    # 与项目解析管线不兼容的透传键：要么破坏逐页页对齐（processor 按页数
    # 校验），要么砍掉版面还原/插图这些项目依赖的能力。在配置写入时拒绝，
    # 避免问题暴露在索引任务中途。
    _INCOMPATIBLE_EXTRA_KEYS = {
        "restructurePages": "重构多页结果会破坏逐页对齐",
        "useLayoutDetection": "版面检测是版面还原与插图提取的根基",
        "promptLabel": "仅在关闭版面检测时生效，与本项目不兼容",
        "returnMarkdownImages": "关闭后插图提取静默失效",
        "mergeTables": "跨页表格合并仅在关闭版面检测时生效",
        "relevelTitles": "标题层级重构仅在关闭版面检测时生效",
    }

    @model_validator(mode="after")
    def _reject_pipeline_breaking_keys(self):
        conflicts = [
            f"{key}（{reason}）"
            for key, reason in self._INCOMPATIBLE_EXTRA_KEYS.items()
            if key in self.extra_payload
        ]
        if conflicts:
            raise ValueError("以下参数与解析管线不兼容: " + "、".join(conflicts))
        return self

    def to_payload(self) -> Dict[str, object]:
        """转为 camelCase 请求体，None 字段剔除、空 extra 不携带。"""
        payload: Dict[str, object] = {}
        for name, value in self.model_dump(exclude={"extra_payload"}).items():
            if value is None:
                continue
            camel = "".join(
                part if index == 0 else part.capitalize()
                for index, part in enumerate(name.split("_"))
            )
            payload[camel] = value
        payload.update(self.extra_payload)
        return payload


class OcrSettings(BaseModel):
    """PDF 解析服务配置

    provider 选择解析后端：
    - "paddle": PaddleOCR 兼容服务，版面还原并产出插图（异步作业接口）
    - "vlm": 自定义多模态模型，逐页图片转 Markdown（OpenAI chat/completions 接口），不产出插图
    - "mineru": mineru.net 精准解析 API，版面还原并产出插图（文件上传 + 批量结果接口）

    所选后端的凭据填全时才启用。MinerU 未填 Token 同样视为未配置，
    与其他后端一样回落到本地 PyMuPDF 文本层，不做静默降级。
    """
    provider: str = "paddle"  # paddle | vlm | mineru

    # PaddleOCR 兼容服务
    base_url: str = ""  # OCR 服务地址，如官方 https://paddleocr.aipaddle.com
    api_key: str = ""  # 访问令牌
    model: str = "PaddleOCR-VL"  # PaddleOCR-VL | PP-StructureV3
    payload: OcrOptionalPayload = OcrOptionalPayload()  # 解析参数（optionalPayload）

    # 自定义多模态模型（OpenAI 兼容）
    vlm_base_url: str = ""  # 如 https://api.example.com/v1
    vlm_api_key: str = ""
    vlm_model: str = ""  # 需为支持图片输入的模型
    vlm_dpi: int = 150  # 页面渲染精度，越高越清晰也越费 token
    vlm_concurrency: int = 4  # 同时在途的单页请求数

    # MinerU 精准解析（mineru.net API v4）
    mineru_token: str = ""  # 注册 mineru.net 后申请
    mineru_model_version: str = "vlm"  # pipeline | vlm；官方默认 pipeline，vlm 解析质量更好
    mineru_is_ocr: bool = False  # 强制走 OCR，扫描件解析不出内容时开启
    mineru_language: str = "ch"  # 文档语言，仅影响 OCR 阶段；取值见 MINERU_LANGUAGES


class OfficeSettings(BaseModel):
    """Office 文档转换配置

    markitdown 依赖的 python-pptx / mammoth 都会跳过 mc:AlternateContent，
    公式及其周围的正文整块丢失。先把文档转成 PDF 再走 PDF 管线可以避开这个盲区，
    转出来的 PDF 与原生 PDF 一样按 ocr.provider 选择解析后端。

    converter 选择转换后端：
    - "auto": COM 优先（保真最好），其次 LibreOffice，都没有则回退 markitdown
    - "com": 仅用 Windows COM（需已安装 Microsoft Office）
    - "libreoffice": 仅用 LibreOffice
    - "off": 关闭，始终使用 markitdown
    """

    converter: str = "auto"  # auto | com | libreoffice | off
    libreoffice_path: str = ""  # soffice 可执行文件路径，留空则自动探测


class PerformanceSettings(BaseModel):
    """索引性能配置

    嵌入限流按"每分钟 token 预算 + 每分钟请求数"双维度控制，与服务商额度对齐。
    默认值对应硅基流动实名认证额度（RPM=2000, TPM=500,000）的保守取值。
    """
    embedding_tpm: int = 400000  # 每分钟 token 预算（服务商 TPM 的 80%）
    embedding_rpm: int = 200  # 每分钟请求数上限
    embedding_concurrency: int = 4  # 同时在途的嵌入请求数
    embedding_batch_size: int = 10  # 每个嵌入请求携带的切片数
    worker_concurrency: int = 2  # Worker 同时处理的索引任务数


class AppearanceSettings(BaseModel):
    """外观设置"""
    theme: str = "light"  # "dark" | "light" | "pink"
    language: str = "zh"  # "zh" | "en"


class WebDAVSettings(BaseModel):
    """WebDAV 云同步配置"""
    enabled: bool = False  # 是否启用云同步
    hostname: str = ""  # WebDAV 服务器完整地址，如 https://dav.jianguoyun.com/dav/Piece/
    username: str = ""  # 用户名（坚果云为邮箱）
    password: str = ""  # 密码（坚果云需要应用密码）
    last_sync_time: Optional[str] = None  # 上次同步时间（ISO格式字符串）


class ApiSettings(BaseModel):
    """本地管理 API 独立凭据，永远不沿用检索密钥。"""
    admin_key: str = Field(default_factory=generate_api_key, min_length=32)


class AppSettings(BaseModel):
    """应用设置"""
    api: ApiSettings = Field(default_factory=ApiSettings)
    embedding: EmbeddingSettings = EmbeddingSettings()
    mcp: McpSettings = McpSettings()
    ocr: OcrSettings = OcrSettings()
    office: OfficeSettings = OfficeSettings()
    appearance: AppearanceSettings = AppearanceSettings()
    webdav: WebDAVSettings = WebDAVSettings()
    performance: PerformanceSettings = PerformanceSettings()
    data_path: str = Field(default_factory=lambda: str(DEFAULT_DATA_PATH))

    @model_validator(mode="after")
    def protect_management_key(self):
        if self.api.admin_key in {self.mcp.get_api_key("retrieval"), self.mcp.get_api_key("index")}:
            raise ValueError("管理 API 密钥必须与 MCP 凭据分离")
        return self

    def get_data_path(self) -> Path:
        """获取数据目录路径"""
        return Path(self.data_path)

    def get_db_path(self) -> Path:
        """获取数据库路径"""
        return self.get_data_path() / "kb.db"

    def get_files_path(self) -> Path:
        """获取文件存储路径"""
        return self.get_data_path() / "files"


def _get_config_file_path() -> Path:
    """获取配置文件路径（首次启动时使用默认路径）"""
    return DEFAULT_DATA_PATH / "config.json"


def load_settings() -> AppSettings:
    """仅服务或显式离线配置调用；坏配置直接报错，不创建另一知识库。"""
    config_path = _get_config_file_path()
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            settings = AppSettings.model_validate(data)
        except Exception:
            raise RuntimeError(f"配置无法读取或校验失败：{config_path}；请检查配置，原文件未改动") from None
        if "api" not in data and not save_settings(settings):
            raise RuntimeError("无法保存本地 API 管理凭据")
        return settings
    settings = AppSettings()
    settings.mcp.api_key = generate_api_key()
    settings.mcp.index_api_key = generate_api_key()
    if not save_settings(settings):
        raise RuntimeError(f"无法初始化配置：{config_path}")
    return settings


def save_settings(settings: AppSettings, *, update_cache: bool = True) -> bool:
    """原子保存；实例/配置锁由调用方持有，不在此处切换数据目录。"""
    path = _get_config_file_path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(settings.model_dump(), output, indent=2, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        if update_cache:
            global _settings
            _settings = settings
        return True
    except OSError:
        logger.error("[Settings] 配置保存失败，原配置未替换")
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_embedding_config() -> dict:
    """
    获取嵌入模型配置（兼容 OpenAIEmbeddings 参数格式）

    Returns:
        dict: 包含 base_url, api_key, model, check_embedding_ctx_length
    """
    settings = get_settings()  # 使用缓存
    return {
        "base_url": settings.embedding.base_url,
        "api_key": settings.embedding.api_key,
        "model": settings.embedding.model,
        "check_embedding_ctx_length": False,
    }


def get_vector_dim() -> int:
    """获取向量维度"""
    settings = get_settings()  # 使用缓存
    return settings.embedding.vector_dim


def get_mcp_api_key(service: McpService = "retrieval") -> str:
    """获取指定 MCP 服务的有效密钥，默认检索服务。"""
    return get_settings().mcp.get_api_key(service)


def get_index_mcp_port(retrieval_port: int | None = None) -> int:
    raw = os.getenv("PIECE_INDEX_MCP_PORT")
    try:
        port = int(raw) if raw else (retrieval_port if retrieval_port is not None else get_settings().mcp.port) + 1
    except ValueError:
        port = (retrieval_port if retrieval_port is not None else get_settings().mcp.port) + 1
    if not 1 <= port <= 65535:
        raise ValueError("索引 MCP 端口必须在 1–65535 之间")
    return port


def get_mcp_config() -> McpSettings:
    """获取 MCP 服务配置"""
    return get_settings().mcp


def get_ocr_config() -> OcrSettings:
    """获取 OCR 文档解析配置"""
    return get_settings().ocr


def get_performance_config() -> PerformanceSettings:
    """获取索引性能配置（嵌入限流、并发度）"""
    return get_settings().performance


def get_pdf_parser() -> str:
    """返回 PDF 解析后端: "paddle" | "vlm" | "mineru" | "local"。

    所选后端的凭据填全时才启用，否则回落到本地 PyMuPDF 文本层。
    """
    ocr = get_ocr_config()
    if ocr.provider == "vlm":
        if ocr.vlm_base_url.strip() and ocr.vlm_api_key.strip() and ocr.vlm_model.strip():
            return "vlm"
        return "local"
    if ocr.provider == "mineru":
        return "mineru" if ocr.mineru_token.strip() else "local"
    if ocr.base_url.strip() and ocr.api_key.strip():
        return "paddle"
    return "local"


def get_parser_max_size() -> Optional[int]:
    """当前生效解析后端的单文件上限（字节）；None 表示仅受全局 MAX_FILE_SIZE 约束。

    限制绑定"当前生效的后端"而不是对所有后端取交集：provider 是单选的，
    没配 MinerU 的用户不该因此丧失导入大文件的能力。
    等出现第二个受限后端再把 if 换成映射表。
    """
    return MINERU_MAX_FILE_SIZE if get_pdf_parser() == "mineru" else None


def is_mcp_auth_enabled(service: McpService = "retrieval") -> bool:
    """检查指定 MCP 服务是否启用认证。"""
    config = get_settings().mcp
    return config.auth_enabled and bool(config.get_api_key(service))


def get_db_path() -> Path:
    """获取数据库路径"""
    settings = get_settings()  # 使用缓存
    return settings.get_db_path()


# 全局设置实例（延迟加载）
_settings: Optional[AppSettings] = None


def get_settings() -> AppSettings:
    """获取全局设置实例"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings() -> AppSettings:
    """重新加载设置"""
    global _settings
    _settings = load_settings()
    return _settings


def get_webdav_config() -> dict:
    """
    获取 WebDAV 配置

    Returns:
        dict: 包含 enabled, hostname, username, password, last_sync_time
    """
    settings = get_settings()  # 使用缓存
    return {
        "enabled": settings.webdav.enabled,
        "hostname": settings.webdav.hostname,
        "username": settings.webdav.username,
        "password": settings.webdav.password,
        "last_sync_time": settings.webdav.last_sync_time,
    }


def get_chunking_config() -> dict:
    """
    获取分块配置

    根据嵌入模型的 max_tokens 自动计算最大分块大小。
    公式：max_chunk_size = max_tokens * 0.8 / 1.5
    - 0.8 是安全系数（使用 80% 容量）
    - 1.5 是字符→Token 转换系数（保守估计）

    Returns:
        dict: 包含 max_chunk_size（字符数）
    """
    settings = get_settings()  # 使用缓存
    max_chunk_size = int(settings.embedding.max_tokens * 0.8 / 1.5)
    return {
        "max_chunk_size": max_chunk_size,
    }
