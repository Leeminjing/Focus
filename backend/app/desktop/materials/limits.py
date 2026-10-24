"""本文件对外提供 ImageResourceLimits，集中声明上传、逐轮材料备注和图片请求资源边界。

输入为可选的配置覆盖值；输出为经过 Pydantic 校验的不可变限制对象。具体工作流为：
在应用装配时创建一次配置，上传服务使用文件/原图/像素/分块限制，运行图片解析器使用
数量、备注与聚合送模字节限制，避免路由、中间件和解析模块各自维护常量。

示例：limits = ImageResourceLimits(upload_chunk_bytes=64 * 1024)。
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImageResourceLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    general_file_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    original_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    image_pixels: int = Field(default=40_000_000, gt=0)
    upload_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    run_image_count: int = Field(default=16, gt=0)
    run_model_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    run_material_count: int = Field(default=64, gt=0)
    material_note_chars: int = Field(default=4000, gt=0)
    aggregate_material_note_chars: int = Field(default=16000, gt=0)

    @model_validator(mode="after")
    def validate_byte_boundaries(self) -> Self:
        if self.original_image_bytes > self.general_file_bytes:
            raise ValueError("图片原图上限不能大于通用文件上限")
        if self.upload_chunk_bytes > self.general_file_bytes:
            raise ValueError("上传分块不能大于通用文件上限")
        return self
