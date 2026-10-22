from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.ban import RereadBanHandler
from .core.config import PluginConfig
from .core.engine import RereadEngine
from .core.fingerprint import FingerprintService
from .core.market_face import (
    ExpressionImage,
    MarketFace,
    resend_expression_image,
    resend_market_face,
)
from .core.state import StateManager


class RereadPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config)
        self.state_mgr = StateManager(self.cfg.window_sizes)
        self.engine = RereadEngine(self.cfg)
        self.ban_handler = RereadBanHandler(self.cfg)
        self.fingerprints = FingerprintService()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def reread_handle(self, event: AstrMessageEvent):
        if event.is_at_or_wake_command:
            return

        group_id = event.get_group_id()
        send_id = event.get_sender_id()

        if self.cfg.group_whitelist and not self.cfg.is_white_group(group_id):
            return

        chain = event.get_messages() or []
        market_faces = MarketFace.extract(event)
        image_payload: str | None = None
        expression_image: ExpressionImage | None = None

        if len(chain) == 1:
            seg = chain[0]
            seg_type = str(seg.type).split(".")[-1]

            if not self.cfg.is_supported_type(seg_type):
                return

            if isinstance(seg, Plain) and any(
                word in seg.text for word in self.cfg.blocked_words
            ):
                return

            # NapCat 把动画表情/表情包上报成带 emoji_id 的图片段：
            # 按表情 ID 判等，并以 mface 原样回发，保留动画
            sticker = (
                MarketFace.from_image_segment(event)
                if isinstance(seg, Image) and self.cfg.get_threshold("Face") > 0
                else None
            )
            if sticker is not None:
                seg = sticker
                seg_type = "Face"
                fingerprint = sticker.fingerprint
            else:
                # 图片按内容哈希判等，NapCat 的 file/url 每条消息都不同
                fingerprint, image_payload = await self.fingerprints.build(seg)
                if isinstance(seg, Image):
                    expression_image = ExpressionImage.from_image_segment(event)
                    if (
                        expression_image is not None
                        and self.cfg.get_threshold("Face") > 0
                    ):
                        # 动画表情使用“表情”阈值
                        seg_type = "Face"
        elif not chain and len(market_faces) == 1:
            # AstrBot 会丢弃 mface，表情包只能从原始事件中识别
            seg = market_faces[0]
            # 表情阈值被关闭时退回图片窗口，避免完全不复读
            seg_type = "Face" if self.cfg.get_threshold("Face") > 0 else "Image"
            if not self.cfg.is_supported_type(seg_type):
                return
            fingerprint = seg.fingerprint
        else:
            return

        state = self.state_mgr.get_state(group_id)

        async with state.lock:
            plan = self.engine.evaluate(state, seg_type, send_id, fingerprint, seg)

        if not plan:
            return

        if plan.action == "ban":
            await self.ban_handler.handle(event, plan.repeat_count)
            return

        if not plan.output_segment:
            return

        if isinstance(plan.output_segment, MarketFace):
            if await resend_market_face(event, plan.output_segment):
                event.stop_event()
            return

        output_segment = plan.output_segment
        if isinstance(output_segment, Image) and image_payload:
            # 用检测时缓存的字节回发，避免原链接过期或只有本地文件名
            if expression_image is not None and await resend_expression_image(
                event, expression_image, image_payload
            ):
                event.stop_event()
                return
            output_segment = Image(file=f"base64://{image_payload}")

        msg = event.chain_result([output_segment])
        if self.cfg.skip_pipeline:
            await event.send(msg)
            event.stop_event()
        else:
            yield msg
