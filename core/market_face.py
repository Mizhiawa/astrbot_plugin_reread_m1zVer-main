from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

# NapCat 发送 mface 时要求的字段
_SEND_FIELDS = ("emoji_package_id", "emoji_id", "key", "summary")


def _raw_segments(event: Any) -> list[Mapping[str, Any]]:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping):
        return []
    segments = raw.get("message")
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, Mapping)]


@dataclass(slots=True)
class MarketFace:
    """NapCat 商城表情（表情包）消息段。

    AstrBot 的 aiocqhttp 适配器会直接丢弃 ``mface`` 段，因此只能从原始事件里取。
    """

    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def extract(cls, event: Any) -> list[MarketFace]:
        result: list[MarketFace] = []
        for segment in _raw_segments(event):
            if segment.get("type") == "mface" and isinstance(
                segment.get("data"), Mapping
            ):
                result.append(cls(dict(segment["data"])))
        return result

    @classmethod
    def from_image_segment(cls, event: Any) -> MarketFace | None:
        """NapCat 会把商城表情/动画表情上报成带 emoji_id 的 image 段。"""
        images = [
            dict(segment["data"])
            for segment in _raw_segments(event)
            if segment.get("type") == "image"
            and isinstance(segment.get("data"), Mapping)
            and segment["data"].get("emoji_id")
        ]
        if len(images) != 1:
            return None
        return cls(images[0])

    @property
    def fingerprint(self) -> str:
        package_id = self.data.get("emoji_package_id")
        emoji_id = self.data.get("emoji_id")
        if package_id not in (None, "") and emoji_id:
            return f"mface:{package_id}:{emoji_id}"

        key = self.data.get("key")
        if key:
            return f"mface:key:{key}"

        url = self.data.get("url")
        if url:
            return f"mface:url:{url}"

        return f"mface:summary:{self.data.get('summary', '')}"

    @property
    def url(self) -> str:
        return str(self.data.get("url") or "")

    def send_payload(self) -> dict[str, Any] | None:
        """返回 NapCat 可直接发送的 mface 数据，字段不完整时返回 None。"""
        payload: dict[str, Any] = {}
        for key in _SEND_FIELDS:
            value = self.data.get(key)
            if value in (None, ""):
                return None
            payload[key] = value

        try:
            payload["emoji_package_id"] = int(payload["emoji_package_id"])
        except (TypeError, ValueError):
            return None

        payload["emoji_id"] = str(payload["emoji_id"])
        payload["key"] = str(payload["key"])
        payload["summary"] = str(payload["summary"])
        return payload


@dataclass(slots=True)
class ExpressionImage:
    """收藏/自定义表情等特殊图片段（``sub_type`` 为 1 或 2）。

    QQ 会把这类表情上报为 ``image`` 段并带 ``sub_type``，AstrBot 的 Image
    组件会丢掉该字段。回发时保留 sub_type，QQ 才会按表情展示。
    """

    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_image_segment(cls, event: Any) -> ExpressionImage | None:
        images = [
            dict(segment["data"])
            for segment in _raw_segments(event)
            if segment.get("type") == "image"
            and isinstance(segment.get("data"), Mapping)
        ]
        if len(images) != 1:
            return None

        data = images[0]
        if data.get("emoji_id"):
            # 商城表情走 MarketFace，按 emoji_id 判等
            return None

        try:
            sub_type = int(data.get("sub_type") or 0)
        except (TypeError, ValueError):
            sub_type = 0
        # PicSubType: 1=自定义表情(收藏), 2=热门表情/表情包
        if sub_type not in (1, 2):
            return None
        return cls(data)

    @property
    def summary(self) -> str:
        return str(self.data.get("summary") or "[动画表情]")

    @property
    def sub_type(self) -> int:
        return int(self.data.get("sub_type") or 1)


async def resend_expression_image(
    event: Any, sticker: ExpressionImage, payload: str
) -> bool:
    """保留 ``sub_type`` 回发表情，避免被当成普通图片。"""
    group_id = event.get_group_id()
    bot = getattr(event, "bot", None)
    if not group_id or bot is None or not hasattr(bot, "call_action"):
        return False

    try:
        await bot.call_action(
            "send_group_msg",
            group_id=int(group_id),
            message=[
                {
                    "type": "image",
                    "data": {
                        "file": f"base64://{payload}",
                        "summary": sticker.summary,
                        "sub_type": sticker.sub_type,
                    },
                }
            ],
        )
        return True
    except Exception as exc:
        logger.warning(f"[reread] 表情复读失败，回退为普通图片: {exc}")
        return False


async def resend_market_face(event: Any, face: MarketFace) -> bool:
    """原样回发表情包；NapCat 不支持时退化为发送表情图片。"""
    group_id = event.get_group_id()
    bot = getattr(event, "bot", None)
    payload = face.send_payload()

    if (
        payload is not None
        and group_id
        and bot is not None
        and hasattr(bot, "call_action")
    ):
        try:
            await bot.call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=[{"type": "mface", "data": payload}],
            )
            return True
        except Exception as exc:
            logger.warning(f"[reread] 表情包原样复读失败，尝试按图片发送: {exc}")

    if face.url:
        from astrbot.api.message_components import Image

        try:
            await event.send(event.chain_result([Image(file=face.url)]))
            return True
        except Exception as exc:
            logger.warning(f"[reread] 表情包图片复读失败: {exc}")

    return False
