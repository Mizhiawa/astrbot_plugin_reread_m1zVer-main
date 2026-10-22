from __future__ import annotations

import asyncio
import base64
import hashlib
from collections import OrderedDict

from astrbot.api import logger
from astrbot.api.message_components import BaseMessageComponent, Face, Image, Plain


class FingerprintService:
    """生成稳定指纹，并缓存图片字节供复读时原样发回。

    NapCat 为每条收到的图片生成不同的 ``file`` / ``url``（url 带有时效 rkey），
    直接用它们做判等时，同一张图片的指纹也各不相同，阈值大于 1 永远无法触发。
    这里改为对图片内容做哈希，同时缓存 base64 数据：
    发回复读时直接使用缓存字节，避免 CDN 链接过期或只有本地文件名导致发送失败。
    """

    def __init__(
        self,
        max_entries: int = 48,
        max_chars: int = 24 * 1024 * 1024,
        download_timeout: float = 8.0,
    ) -> None:
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._download_timeout = download_timeout
        self._cache: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._cached_chars = 0
        self._semaphore = asyncio.Semaphore(3)

    async def build(
        self, seg: BaseMessageComponent
    ) -> tuple[str, str | None]:
        """返回 ``(指纹, 图片 base64)``，非图片消息的 base64 为 None。"""
        if isinstance(seg, Plain):
            return f"text:{seg.text}", None
        if isinstance(seg, Face):
            return f"face:{seg.id}", None
        if isinstance(seg, Image):
            return await self._build_image(seg)
        return f"unknown:{seg.type}", None

    async def _build_image(self, seg: Image) -> tuple[str, str | None]:
        key = "|".join(
            value
            for value in (
                getattr(seg, "file", None),
                getattr(seg, "url", None),
                getattr(seg, "path", None),
            )
            if value
        )

        if key:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        payload = await self._fetch_base64(seg)
        if not payload:
            # 下载失败时退化为原有的 file/url 指纹，至少不阻塞复读流程
            return f"image:{key or 'unknown'}", None

        raw = _decode_payload(payload)
        if not _looks_like_image(raw):
            # 避免把 CDN 返回的错误页当成同一张图片
            logger.debug("[reread] 图片内容校验失败，退化为 file/url 指纹")
            return f"image:{key or 'unknown'}", None

        digest = hashlib.md5(raw).hexdigest()
        value = (f"image:md5:{digest}", payload)
        if key:
            self._remember(key, value)
        return value

    async def _fetch_base64(self, seg: Image) -> str | None:
        convert = getattr(seg, "convert_to_base64", None)
        if convert is None:
            return None

        try:
            async with self._semaphore:
                payload = await asyncio.wait_for(
                    convert(), timeout=self._download_timeout
                )
        except Exception as exc:
            logger.debug(f"[reread] 图片指纹获取失败，退化为 file/url: {exc}")
            return None

        if isinstance(payload, bytes):
            return base64.b64encode(payload).decode("utf-8")
        return payload or None

    def _remember(self, key: str, value: tuple[str, str]) -> None:
        old = self._cache.pop(key, None)
        if old is not None:
            self._cached_chars -= len(old[1])

        self._cache[key] = value
        self._cached_chars += len(value[1])

        while self._cache and (
            len(self._cache) > self._max_entries
            or self._cached_chars > self._max_chars
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cached_chars -= len(evicted[1])


def _decode_payload(payload: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=False)
    except Exception:
        return payload.encode("utf-8")


def _looks_like_image(data: bytes) -> bool:
    """粗校验图片头，防止把 CDN 错误页当作图片参与判等。"""
    if len(data) < 12:
        return False
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):  # GIF
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP
        return True
    if data[:2] == b"BM":  # BMP
        return True
    return False
