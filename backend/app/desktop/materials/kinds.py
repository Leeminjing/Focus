"""本文件对外提供 MaterialKindClassifier，用统一规则分类桌面材料。

输入为材料文件名或路径；输出为 image、text、pdf、document、spreadsheet、presentation、
archive 或 other 稳定类别。具体工作流为规范化扩展名后按不可重叠扩展名集合匹配，图片类别
仅表示候选类型，运行解析仍会验证真实字节。

示例：MaterialKindClassifier.classify("REPORT.PDF") 返回 "pdf"。
"""

from pathlib import Path


class MaterialKindClassifier:
    _KINDS = {
        "image": frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"}),
        "text": frozenset({
            ".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".json",
            ".yaml", ".yml", ".toml", ".ini", ".css", ".html", ".xml", ".sql",
            ".sh", ".ps1", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
        }),
        "pdf": frozenset({".pdf"}),
        "document": frozenset({".doc", ".docx", ".odt", ".rtf"}),
        "spreadsheet": frozenset({".xls", ".xlsx", ".ods", ".csv", ".tsv"}),
        "presentation": frozenset({".ppt", ".pptx", ".odp"}),
        "archive": frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}),
    }

    @classmethod
    def classify(cls, value: str | Path) -> str:
        suffix = Path(value).suffix.lower()
        for kind, suffixes in cls._KINDS.items():
            if suffix in suffixes:
                return kind
        return "other"
